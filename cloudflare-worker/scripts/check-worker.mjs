import assert from "node:assert/strict";
import worker from "../dist/server/index.js";

class FakeD1 {
  batches = new Map();
  parts = [];

  prepare(sql) {
    const statement = { sql, values: [] };
    statement.bind = (...values) => { statement.values = values; return statement; };
    statement.first = async () => {
      if (sql.includes("FROM tower_batches WHERE status = 'published'")) {
        const row = [...this.batches.values()].filter((item) => item.status === "published")
          .sort((a, b) => b.batch_id - a.batch_id)[0];
        return row ? { batch_id: row.batch_id, file_sha256: row.file_sha256, metadata_json: row.metadata_json } : null;
      }
      if (sql.includes("FROM tower_batches WHERE batch_id = ?")) {
        const row = this.batches.get(Number(statement.values[0]));
        return row ? { file_sha256: row.file_sha256, status: row.status } : null;
      }
      throw new Error(`Unexpected D1 first query: ${sql}`);
    };
    statement.all = async () => {
      if (!sql.includes("FROM tower_snapshot_parts WHERE batch_id = ?")) throw new Error(`Unexpected D1 all query: ${sql}`);
      const rows = this.parts.filter((part) => part.batch_id === Number(statement.values[0]))
        .sort((a, b) => a.part_type.localeCompare(b.part_type) || a.part_index - b.part_index);
      return { results: rows };
    };
    statement.run = async () => {
      const [a, b, c, d] = statement.values;
      if (sql.startsWith("INSERT INTO tower_batches")) {
        this.batches.set(Number(a), { batch_id: Number(a), file_sha256: b, status: "staging", metadata_json: c });
      } else if (sql.startsWith("UPDATE tower_batches SET metadata_json")) {
        const row = this.batches.get(Number(b));
        if (row?.status === "staging") row.metadata_json = a;
      } else if (sql.startsWith("DELETE FROM tower_snapshot_parts")) {
        this.parts = this.parts.filter((part) => part.batch_id !== Number(a));
      } else if (sql.startsWith("INSERT INTO tower_snapshot_parts")) {
        this.parts.push({ batch_id: Number(a), part_type: b, part_index: Number(c), payload_json: d });
      } else if (sql.startsWith("UPDATE tower_batches SET status = 'published'")) {
        const row = this.batches.get(Number(b));
        if (row?.status === "staging" && row.file_sha256 === c) {
          row.status = "published";
          row.published_at = a;
        }
      } else {
        throw new Error(`Unexpected D1 run query: ${sql}`);
      }
      return { success: true, meta: { changes: 1 } };
    };
    return statement;
  }

  async batch(statements) {
    const results = [];
    for (const statement of statements) results.push(await statement.run());
    return results;
  }
}

const testSyncToken = "test-only-sync-token-should-never-be-reused";
const env = { DB: new FakeD1(), SYNC_TOKEN: testSyncToken };
const url = "https://example.test";
const call = (path, init = {}) => worker.fetch(new Request(url + path, init), env);
const snapshot = {
  source: { published_batch_id: 3, file_sha256: "example", last_success_sync_at: new Date().toISOString() },
  quality: { total_body_rows: 1, valid_soo_count: 1 }, business_dates: ["2026-10-08"],
  records: [{ soo_no: "TEST-1", production_date: "2026-10-08", soo_qty: 3,
    warehouse_name: "测试仓", is_loaded: true, latest_pickup_at: "2026-10-08T20:00:00",
    contact_phone: "must-not-publish" },
    { soo_no: "TEST-2", production_date: "2026-10-08", soo_qty: 2,
      warehouse_name: "测试仓", is_loaded: true, latest_pickup_at: "2026-10-08T17:00:00" }],
};
const initial = await (await call("/data.json")).json();
assert.equal(initial.source.published_batch_id, 0);
assert.deepEqual(initial.records, []);
assert.ok(initial.assets?.warehouse_mapping);
assert.ok(Array.isArray(initial.assets?.pickup?.transitions));
const initialSummary = await call("/mcp", { method: "POST", headers: {
  "content-type": "application/json", "oai-authenticated-user-id": "test-user",
}, body: JSON.stringify({ jsonrpc: "2.0", id: 0, method: "tools/call", params: {
  name: "tower_summary", arguments: { business_date: "2026-10-08" },
} }) });
assert.equal(typeof (await initialSummary.json()).result.structuredContent.overdue_pickup_units, "number");
const headers = { "content-type": "application/json", authorization: `Bearer ${testSyncToken}` };
const deniedUpload = await call("/api/snapshot", { method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify(snapshot) });
assert.equal(deniedUpload.status, 401);
const uploaded = await call("/api/snapshot", { method: "POST", headers,
  body: JSON.stringify({ ...snapshot, source: { ...snapshot.source,
    published_batch_id: initial.source.published_batch_id + 1 } }) });
assert.equal(uploaded.status, 200);
assert.equal((await uploaded.json()).status, "published");
const data = await (await call("/data.json")).json();
assert.equal(data.records[0].contact_phone, undefined);
assert.equal(data.source.published_batch_id, initial.source.published_batch_id + 1);
assert.equal((await call("/warehouse-carriers.json")).status, 200);
assert.equal((await call("/outbound-data.json")).status, 200);
assert.equal((await call("/bi-report.js")).status, 200);
assert.equal((await call("/api/v1/pickup/hourly-transitions?limit=5")).status, 200);
assert.equal((await call("/api/v1/imports/upload", { method: "POST" })).status, 405);
assert.equal((await call("/api/snapshot", { method: "POST", headers,
  body: JSON.stringify({ ...snapshot, source: { ...snapshot.source,
    published_batch_id: initial.source.published_batch_id + 1 } }) })).status, 200);
const discovery = await call("/mcp", { method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) });
assert.equal((await discovery.json()).result.tools.length, 3);
const query = { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "tower_summary", arguments: {} } };
assert.equal((await call("/mcp", { method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify(query) })).status, 401);
const authorized = await call("/mcp", { method: "POST", headers: {
  "content-type": "application/json", "oai-authenticated-user-id": "test-user",
}, body: JSON.stringify(query) });
const summary = (await authorized.json()).result.structuredContent;
assert.equal(summary.planned_units, 5);
assert.equal(summary.overdue_pickup_units, 2);
const missingSecret = await worker.fetch(new Request(url + "/api/snapshot", { method: "POST", headers,
  body: JSON.stringify(snapshot) }), { DB: new FakeD1() });
assert.equal(missingSecret.status, 503);
console.log("Worker checks passed: dashboard data, sanitization, idempotence, MCP authentication and summary");
