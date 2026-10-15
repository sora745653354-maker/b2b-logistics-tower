const page = "__TOWER_HTML__";
const biReport = "__TOWER_BI_REPORT__";
const maxBytes = 2 * 1024 * 1024;
const maxUncompressedBytes = 32 * 1024 * 1024;
const maxRecordCount = 30000;
const partSize = 100;
const recordFields = ["row_no", "domain_code", "soo_no", "shipping_order_id", "waybill_id", "warehouse_code", "warehouse_name", "carrier_code", "carrier_name", "shop_code", "shop_name", "channel", "production_date", "business_dates", "production_at", "tms_created_at", "load_completed_at", "park_entered_at", "park_exited_at", "actual_pickup_at", "latest_pickup_at", "tms_status", "is_loaded", "is_picked_up", "soo_qty", "gross_cbm", "gross_weight", "carton_count", "origin_province", "origin_city", "dest_province", "dest_city", "source_updated_at", "quality_flags", "timeline_quality_flags"];
const sourceFields = ["source_system", "file_name", "file_sha256", "last_source_updated_at", "last_success_sync_at", "last_success_import_at", "last_tms_check_at", "published_batch_id", "report_scope", "site_publication_mode"];
const qualityFields = ["total_body_rows", "valid_soo_count", "blank_soo_rows_excluded", "duplicate_key_rows", "invalid_datetime_values", "invalid_qty_values", "negative_qty_values", "state_conflicts"];
const transitionFields = ["interval_start", "interval_end", "interval_hours", "domain_code", "soo_no", "waybill_id", "warehouse_name", "carrier_name", "channel", "production_date", "business_dates", "previous_status", "current_status", "soo_qty", "gross_cbm", "from_batch_id", "to_batch_id", "hour_slot"];
const respond = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), {
  status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
});
const fail = (message, status) => respond({ error: message }, status);
const only = (value, fields) => Object.fromEntries(fields.filter((key) => Object.hasOwn(value, key)).map((key) => [key, value[key]]));
const dateOnly = (value) => typeof value === "string" ? value.slice(0, 10) : "";
const priorDate = (value) => { const d = new Date(value + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() - 1); return d.toISOString().slice(0, 10); };

async function readSnapshot(env) {
  if (!env.DB) throw new Error("D1 database binding unavailable");
  const batch = await env.DB.prepare(
    "SELECT batch_id, file_sha256, metadata_json FROM tower_batches WHERE status = 'published' ORDER BY batch_id DESC LIMIT 1",
  ).first();
  if (!batch) return emptySnapshot();
  const parts = await env.DB.prepare(
    "SELECT part_type, part_index, payload_json FROM tower_snapshot_parts WHERE batch_id = ? ORDER BY part_type, part_index",
  ).bind(batch.batch_id).all();
  const records = [];
  const transitions = [];
  const outboundRecords = [];
  for (const part of parts.results || []) {
    const values = JSON.parse(part.payload_json);
    if (part.part_type === "records") records.push(...values);
    if (part.part_type === "transitions") transitions.push(...values);
    if (part.part_type === "outbound") outboundRecords.push(...values);
  }
  const metadata = JSON.parse(batch.metadata_json);
  return {
    schema_version: "site-3.0",
    source: { ...metadata.source, published_batch_id: Number(batch.batch_id), file_sha256: batch.file_sha256 },
    quality: metadata.quality,
    business_dates: metadata.business_dates,
    records,
    assets: {
      warehouse_mapping: metadata.warehouse_mapping,
      outbound: { ...metadata.outbound, records: outboundRecords },
      pickup: { ...metadata.pickup, transitions },
    },
  };
}

function emptySnapshot() {
  return {
    schema_version: "site-3.0",
    source: { source_system: "TMS", published_batch_id: 0, site_publication_mode: "private_site_snapshot" },
    quality: { total_body_rows: 0, valid_soo_count: 0, blank_soo_rows_excluded: 0, duplicate_key_rows: 0,
      invalid_datetime_values: 0, invalid_qty_values: 0, negative_qty_values: 0, state_conflicts: 0 },
    business_dates: [], records: [],
    assets: { warehouse_mapping: { warehouse_carriers: {}, warehouse_regions: {} },
      outbound: { source_updated_at: null, record_count: 0, records: [] },
      pickup: { snapshot_count: 0, basis: "每个北京时间小时首份成功TMS数据源快照；相邻小时快照对比",
        time_semantics: "状态变化仅能定位在前后小时快照之间，不代表精确提货时刻", transitions: [] } },
  };
}

function normalize(input) {
  if (!input || !Array.isArray(input.records) || !input.source || !input.quality) throw new Error("Invalid snapshot format");
  const batch = Number(input.source.published_batch_id);
  if (!Number.isSafeInteger(batch) || batch < 1 || input.records.length > maxRecordCount) throw new Error("Invalid batch or record count");
  const records = input.records.map((record) => {
    if (!record || typeof record.soo_no !== "string" || !record.soo_no) throw new Error("Invalid SOO record");
    const safeRecord = only(record, recordFields);
    if (Array.isArray(record.business_dates)) safeRecord.business_dates = record.business_dates.filter((date) => /^\d{4}-\d{2}-\d{2}$/.test(date));
    return safeRecord;
  });
  const quality = {};
  for (const key of qualityFields) quality[key] = Number(input.quality[key] || 0);
  const assets = input.assets || {};
  const mapping = assets.warehouse_mapping || {};
  const outbound = assets.outbound || {};
  const transitions = Array.isArray(assets.pickup_transitions) ? assets.pickup_transitions : [];
  const safeOutbound = (Array.isArray(outbound.records) ? outbound.records : []).slice(0, 20000)
    .filter((row) => typeof row.soo_no === "string")
    .map((row) => only(row, ["soo_no", "actual_shipment_at"]));
  return {
    schema_version: "site-3.0",
    source: { ...only(input.source, sourceFields), cloud_synced_at: new Date().toISOString() },
    quality,
    business_dates: Array.isArray(input.business_dates) ? input.business_dates.filter((date) => /^\d{4}-\d{2}-\d{2}$/.test(date)).slice(0, 1000) : [],
    records,
    assets: {
      warehouse_mapping: {
        warehouse_carriers: mapping.warehouse_carriers || {},
        warehouse_regions: mapping.warehouse_regions || {},
      },
      outbound: { source_updated_at: outbound.source_updated_at || null, record_count: safeOutbound.length, records: safeOutbound },
      pickup: {
        snapshot_count: Number(assets.pickup?.snapshot_count || 0),
        basis: String(assets.pickup?.basis || "每个北京时间小时首份成功TMS数据源快照；相邻小时快照对比"),
        time_semantics: String(assets.pickup?.time_semantics || "状态变化仅能定位在前后小时快照之间，不代表精确提货时刻"),
        transitions: transitions.slice(0, 5000).map((row) => only(row, transitionFields)),
      },
    },
  };
}

async function updateSnapshot(request, env) {
  if (!env.DB) return fail("D1 database binding unavailable", 503);
  if (typeof env.SYNC_TOKEN !== "string" || env.SYNC_TOKEN.length < 32) return fail("Snapshot sync is not configured", 503);
  const authorization = request.headers.get("authorization") || "";
  const suppliedToken = authorization.match(/^Bearer\s+(.+)$/i)?.[1] || "";
  const [expectedDigest, suppliedDigest] = await Promise.all([
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(env.SYNC_TOKEN)),
    crypto.subtle.digest("SHA-256", new TextEncoder().encode(suppliedToken)),
  ]);
  const expectedBytes = new Uint8Array(expectedDigest);
  const suppliedBytes = new Uint8Array(suppliedDigest);
  let tokenDifference = 0;
  for (let i = 0; i < expectedBytes.length; i++) tokenDifference |= expectedBytes[i] ^ suppliedBytes[i];
  if (tokenDifference !== 0 || !suppliedToken) return fail("Unauthorized", 401);
  if (!request.headers.get("content-type")?.startsWith("application/json")) return fail("Unsupported update request", 415);
  const origin = request.headers.get("origin");
  if (origin && origin !== new URL(request.url).origin) return fail("Forbidden origin", 403);
  if (Number(request.headers.get("content-length") || 0) > maxBytes) return fail("Snapshot too large", 413);
  let body;
  try {
    const stream = request.headers.get("content-encoding") === "gzip"
      ? request.body.pipeThrough(new DecompressionStream("gzip")) : request.body;
    body = await new Response(stream).text();
  } catch { return fail("Invalid compressed snapshot", 400); }
  if (new TextEncoder().encode(body).length > maxUncompressedBytes) return fail("Snapshot too large", 413);
  let next;
  try { next = normalize(JSON.parse(body)); } catch (e) { return fail(e.message || "Invalid snapshot", 400); }
  const current = await readSnapshot(env);
  const batch = next.source.published_batch_id;
  const currentBatch = current?.source?.published_batch_id || 0;
  if (batch < currentBatch) return fail("Older batch cannot replace latest batch", 409);
  if (batch === currentBatch) {
    if (next.source.file_sha256 !== current.source.file_sha256) return fail("Batch ID conflicts with published file", 409);
    return respond({ status: "unchanged", batch_id: batch, cloud_synced_at: current.source.cloud_synced_at });
  }

  const existing = await env.DB.prepare(
    "SELECT file_sha256, status FROM tower_batches WHERE batch_id = ?",
  ).bind(batch).first();
  if (existing && existing.file_sha256 !== next.source.file_sha256) return fail("Batch ID conflicts with published file", 409);
  if (existing?.status === "published") {
    return respond({ status: "unchanged", batch_id: batch, cloud_synced_at: current.source.cloud_synced_at });
  }

  const metadata = {
    source: { ...only(next.source, sourceFields), cloud_synced_at: next.source.cloud_synced_at },
    quality: next.quality,
    business_dates: next.business_dates,
    warehouse_mapping: next.assets.warehouse_mapping,
    outbound: { source_updated_at: next.assets.outbound.source_updated_at,
      record_count: next.assets.outbound.record_count },
    pickup: { snapshot_count: next.assets.pickup.snapshot_count,
      basis: next.assets.pickup.basis, time_semantics: next.assets.pickup.time_semantics },
  };
  if (existing) {
    await env.DB.prepare(
      "UPDATE tower_batches SET metadata_json = ?, status = 'staging', published_at = NULL WHERE batch_id = ? AND status = 'staging'",
    ).bind(JSON.stringify(metadata), batch).run();
    await env.DB.prepare("DELETE FROM tower_snapshot_parts WHERE batch_id = ?").bind(batch).run();
  } else {
    await env.DB.prepare(
      "INSERT INTO tower_batches (batch_id, file_sha256, status, metadata_json) VALUES (?, ?, 'staging', ?)",
    ).bind(batch, next.source.file_sha256, JSON.stringify(metadata)).run();
  }

  const parts = [];
  for (let i = 0; i < next.records.length; i += partSize) {
    parts.push({ type: "records", index: Math.floor(i / partSize), rows: next.records.slice(i, i + partSize) });
  }
  for (let i = 0; i < next.assets.pickup.transitions.length; i += partSize) {
    parts.push({ type: "transitions", index: Math.floor(i / partSize), rows: next.assets.pickup.transitions.slice(i, i + partSize) });
  }
  for (let i = 0; i < next.assets.outbound.records.length; i += partSize) {
    parts.push({ type: "outbound", index: Math.floor(i / partSize), rows: next.assets.outbound.records.slice(i, i + partSize) });
  }
  for (let i = 0; i < parts.length; i += 50) {
    const statements = parts.slice(i, i + 50).map((part) => env.DB.prepare(
      "INSERT INTO tower_snapshot_parts (batch_id, part_type, part_index, payload_json) VALUES (?, ?, ?, ?)",
    ).bind(batch, part.type, part.index, JSON.stringify(part.rows)));
    await env.DB.batch(statements);
  }
  const publishedAt = new Date().toISOString();
  await env.DB.prepare(
    "UPDATE tower_batches SET status = 'published', published_at = ? WHERE batch_id = ? AND status = 'staging' AND file_sha256 = ?",
  ).bind(publishedAt, batch, next.source.file_sha256).run();
  return respond({ status: "published", batch_id: batch, record_count: next.records.length, cloud_synced_at: publishedAt });
}

function status(snapshot) {
  const imported = Date.parse(snapshot.source.last_success_sync_at || "");
  const minutes = Number.isFinite(imported) ? Math.max(0, Math.floor((Date.now() - imported) / 60000)) : null;
  return { batch_id: snapshot.source.published_batch_id, record_count: snapshot.records.length,
    business_dates: snapshot.business_dates, last_tms_import_at: snapshot.source.last_success_sync_at || null,
    last_cloud_upload_at: snapshot.source.cloud_synced_at || null, minutes_since_tms_import: minutes,
    data_delayed: minutes === null || minutes > 60, note: "Last imported TMS report; not live TMS data." };
}

function summary(snapshot, args) {
  const date = args.business_date || snapshot.business_dates.at(-1) || "";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error("business_date must be YYYY-MM-DD");
  const rows = snapshot.records.filter((row) => !args.warehouse_name || row.warehouse_name === args.warehouse_name);
  const forBusinessDate = (row, target) => Array.isArray(row.business_dates)
    ? row.business_dates.includes(target) : row.production_date === target;
  const planned = rows.filter((row) => forBusinessDate(row, date));
  const prior = rows.filter((row) => forBusinessDate(row, priorDate(date)));
  const sum = (items) => items.reduce((n, row) => n + Number(row.soo_qty || 0), 0);
  const waiting = rows.filter((row) => row.is_loaded && !row.actual_pickup_at && row.production_date && row.production_date <= date);
  const overdue = waiting.filter((row) => {
    const deadlineDay = dateOnly(row.latest_pickup_at);
    return deadlineDay && deadlineDay <= date && (deadlineDay < date || String(row.latest_pickup_at).slice(11, 16) < "18:00");
  });
  return { business_date: date, warehouse_name: args.warehouse_name || "全部仓库", batch_id: snapshot.source.published_batch_id,
    planned_units: sum(planned), loaded_units: sum(planned.filter((row) => row.is_loaded)), prior_day_planned_units: sum(prior),
    prior_day_picked_up_today_units: sum(prior.filter((row) => dateOnly(row.actual_pickup_at) === date)),
    waiting_pickup_units: sum(waiting), overdue_pickup_units: sum(overdue),
    source_imported_at: snapshot.source.last_success_sync_at || null, data_delayed: status(snapshot).data_delayed };
}

function findSoo(snapshot, args) {
  const number = String(args.soo_no || "").trim();
  if (!number || number.length > 100) throw new Error("soo_no required (max 100 characters)");
  const records = snapshot.records.filter((row) => row.soo_no === number).slice(0, 10).map((row) => only(row, ["soo_no", "shipping_order_id", "waybill_id", "warehouse_name", "carrier_name", "production_date", "soo_qty", "tms_status", "actual_pickup_at", "quality_flags"]));
  return { batch_id: snapshot.source.published_batch_id, records, data_delayed: status(snapshot).data_delayed };
}

const mcpTools = [
  { name: "tower_status", description: "Read the published TMS batch and source freshness, not live TMS data.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "tower_summary", description: "Read SOO totals for one business date and optional warehouse from the latest published report.", inputSchema: { type: "object", properties: { business_date: { type: "string", description: "YYYY-MM-DD; defaults to latest date" }, warehouse_name: { type: "string" } }, additionalProperties: false } },
  { name: "find_soo", description: "Find one exact SOO number; returns at most ten non-contact records.", inputSchema: { type: "object", properties: { soo_no: { type: "string" } }, required: ["soo_no"], additionalProperties: false } },
];

async function mcp(request, env) {
  if (request.headers.get("content-type")?.split(";")[0] !== "application/json") return fail("Expected application/json", 415);
  const raw = await request.text();
  if (raw.length > 100000) return fail("MCP request too large", 413);
  let rpc;
  try { rpc = JSON.parse(raw); } catch { return respond({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } }, 400); }
  const id = rpc.id ?? null;
  const result = (value) => respond({ jsonrpc: "2.0", id, result: value });
  const rpcError = (code, message) => respond({ jsonrpc: "2.0", id, error: { code, message } });
  if (rpc.method === "initialize") return result({ protocolVersion: "2025-06-18", capabilities: { tools: { listChanged: false } }, serverInfo: { name: "b2b-logistics-tower", version: "1.0.0" } });
  if (rpc.method === "notifications/initialized") return new Response(null, { status: 202 });
  if (rpc.method === "ping") return result({});
  if (rpc.method === "tools/list") return result({ tools: mcpTools });
  if (rpc.method !== "tools/call") return rpcError(-32601, "Method not found");
  if (!request.headers.get("oai-authenticated-user-id")) return fail("Signed-in user required", 401);
  const name = rpc.params?.name;
  if (!mcpTools.some((tool) => tool.name === name)) return rpcError(-32602, "Unknown tool");
  const snapshot = await readSnapshot(env);
  if (!snapshot) return result({ isError: true, content: [{ type: "text", text: "No TMS batch published yet." }] });
  try {
    const args = rpc.params?.arguments || {};
    const value = name === "tower_status" ? status(snapshot) : name === "tower_summary" ? summary(snapshot, args) : findSoo(snapshot, args);
    return result({ content: [{ type: "text", text: JSON.stringify(value) }], structuredContent: value });
  } catch (e) { return result({ isError: true, content: [{ type: "text", text: e.message || "Query failed" }] }); }
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    try {
      if (request.method === "GET" && path === "/") return new Response(page, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff", "referrer-policy": "no-referrer" } });
      if (request.method === "GET" && path === "/data.json") {
        const snapshot = await readSnapshot(env);
        return snapshot ? respond(snapshot) : fail("No batch published yet", 404);
      }
      if (request.method === "GET" && path === "/warehouse-carriers.json") {
        const snapshot = await readSnapshot(env);
        return snapshot ? respond(snapshot.assets.warehouse_mapping) : fail("No batch published yet", 404);
      }
      if (request.method === "GET" && path === "/outbound-data.json") {
        const snapshot = await readSnapshot(env);
        return snapshot ? respond(snapshot.assets.outbound) : fail("No batch published yet", 404);
      }
      if (request.method === "GET" && path === "/bi-report.js") {
        return new Response(biReport, { headers: { "content-type": "application/javascript; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" } });
      }
      if (request.method === "GET" && path === "/api/v1/pickup/hourly-transitions") {
        const snapshot = await readSnapshot(env);
        if (!snapshot) return fail("No batch published yet", 404);
        const url = new URL(request.url);
        const businessDate = url.searchParams.get("business_date");
        if (businessDate && !/^\d{4}-\d{2}-\d{2}$/.test(businessDate)) return fail("business_date must be YYYY-MM-DD", 400);
        const limit = Math.min(Math.max(Number(url.searchParams.get("limit") || 5000), 1), 5000);
        const offset = Math.max(Number(url.searchParams.get("offset") || 0), 0);
        const all = snapshot.assets.pickup.transitions.filter((event) => !businessDate || JSON.parse(event.business_dates || "[]").includes(businessDate));
        const transitions = all.slice(offset, offset + limit);
        return respond({ basis: snapshot.assets.pickup.basis, time_semantics: snapshot.assets.pickup.time_semantics,
          business_date: businessDate, snapshot_count: snapshot.assets.pickup.snapshot_count,
          baseline_ready: snapshot.assets.pickup.snapshot_count >= 2, count: transitions.length,
          total: all.length, limit, offset, transitions });
      }
      if (request.method === "POST" && path === "/api/v1/imports/upload") return fail("私有站点为只读快照，不能上传本机数据源。", 405);
      if (request.method === "POST" && path === "/api/snapshot") return updateSnapshot(request, env);
      if (request.method === "POST" && path === "/mcp") return mcp(request, env);
      return fail("Not found", 404);
    } catch (e) { return fail(e.message || "Service unavailable", 503); }
  },
};
