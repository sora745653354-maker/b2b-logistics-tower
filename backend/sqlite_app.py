"""Loopback-only local deployment; PostgreSQL migration remains in migrations/."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import re
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager, contextmanager
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ingestion.extract_sample import (  # noqa: E402
    business_dates_for_record, extract, is_picked_up_status, parse_datetime,
)

RUNTIME = ROOT / "runtime"
DATABASE = RUNTIME / "tower.sqlite3"
ARCHIVE = RUNTIME / "archive"
STAGING = RUNTIME / "staging"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS etl_ingest_batch (
  batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_system TEXT NOT NULL, file_name TEXT NOT NULL, file_sha256 TEXT NOT NULL,
  file_storage_uri TEXT NOT NULL, report_scope TEXT, acquired_at TEXT NOT NULL,
  published_at TEXT, row_count INTEGER NOT NULL, valid_soo_count INTEGER NOT NULL,
  reject_count INTEGER NOT NULL, quality TEXT NOT NULL, status TEXT NOT NULL,
  error_message TEXT, UNIQUE(source_system, file_sha256)
);
CREATE TABLE IF NOT EXISTS ods_tms_soo_raw (
  batch_id INTEGER NOT NULL, row_no INTEGER NOT NULL, domain_code TEXT NOT NULL,
  soo_no TEXT NOT NULL, payload TEXT NOT NULL, quality_flags TEXT NOT NULL,
  PRIMARY KEY(batch_id, row_no), FOREIGN KEY(batch_id) REFERENCES etl_ingest_batch(batch_id)
);
CREATE TABLE IF NOT EXISTS dwd_soo_snapshot (
  batch_id INTEGER NOT NULL, domain_code TEXT NOT NULL, soo_no TEXT NOT NULL,
  source_updated_at TEXT, normalized_row TEXT NOT NULL,
  PRIMARY KEY(batch_id, domain_code, soo_no), FOREIGN KEY(batch_id) REFERENCES etl_ingest_batch(batch_id)
);
CREATE TABLE IF NOT EXISTS dwd_soo_current (
  domain_code TEXT NOT NULL, soo_no TEXT NOT NULL, production_date TEXT,
  source_updated_at TEXT, last_batch_id INTEGER NOT NULL,
  normalized_row TEXT NOT NULL, refreshed_at TEXT NOT NULL,
  PRIMARY KEY(domain_code, soo_no), FOREIGN KEY(last_batch_id) REFERENCES etl_ingest_batch(batch_id)
);
CREATE TABLE IF NOT EXISTS pickup_status_transition (
  transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_batch_id INTEGER NOT NULL, to_batch_id INTEGER NOT NULL,
  interval_start TEXT NOT NULL, interval_end TEXT NOT NULL,
  domain_code TEXT NOT NULL, soo_no TEXT NOT NULL, waybill_id TEXT,
  warehouse_name TEXT, carrier_name TEXT, channel TEXT,
  production_date TEXT, business_dates TEXT NOT NULL,
  previous_status TEXT, current_status TEXT NOT NULL,
  soo_qty REAL, gross_cbm REAL, sp_volume_counted INTEGER NOT NULL DEFAULT 0,
  is_self_pickup INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  UNIQUE(to_batch_id, domain_code, soo_no),
  FOREIGN KEY(from_batch_id) REFERENCES etl_ingest_batch(batch_id),
  FOREIGN KEY(to_batch_id) REFERENCES etl_ingest_batch(batch_id)
);
CREATE INDEX IF NOT EXISTS ix_pickup_transition_interval
  ON pickup_status_transition(interval_end, warehouse_name, carrier_name);
CREATE INDEX IF NOT EXISTS ix_pickup_transition_business_dates
  ON pickup_status_transition(business_dates);
CREATE TABLE IF NOT EXISTS tms_hourly_source_snapshot (
  hour_slot TEXT PRIMARY KEY, batch_id INTEGER NOT NULL,
  source_snapshot_at TEXT NOT NULL, saved_at TEXT NOT NULL,
  FOREIGN KEY(batch_id) REFERENCES etl_ingest_batch(batch_id)
);
CREATE INDEX IF NOT EXISTS ix_tms_hourly_snapshot_batch
  ON tms_hourly_source_snapshot(batch_id);
CREATE INDEX IF NOT EXISTS ix_current_production_date ON dwd_soo_current(production_date);
CREATE INDEX IF NOT EXISTS ix_current_batch ON dwd_soo_current(last_batch_id);
CREATE TABLE IF NOT EXISTS collector_job (
  job_id INTEGER PRIMARY KEY AUTOINCREMENT, job_kind TEXT NOT NULL,
  status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  batch_id INTEGER, error_message TEXT
);
CREATE TABLE IF NOT EXISTS tms_sync_run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
  finished_at TEXT, status TEXT NOT NULL, report_start TEXT NOT NULL,
  report_end TEXT NOT NULL, batch_id INTEGER, valid_soo_count INTEGER,
  unchanged INTEGER NOT NULL DEFAULT 0, error_code TEXT, error_message TEXT,
  FOREIGN KEY(batch_id) REFERENCES etl_ingest_batch(batch_id)
);
CREATE TABLE IF NOT EXISTS sync_settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tms_export_artifact (
  file_path TEXT NOT NULL, file_size INTEGER NOT NULL, file_mtime_ns INTEGER NOT NULL,
  run_id INTEGER NOT NULL, PRIMARY KEY(file_path,file_size,file_mtime_ns),
  FOREIGN KEY(run_id) REFERENCES tms_sync_run(run_id)
);
"""

@asynccontextmanager
async def lifespan(_: FastAPI):
    migrate()
    yield


app = FastAPI(title="B2B 物流控制塔", docs_url="/api/docs", redoc_url=None,
              lifespan=lifespan)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    conn = sqlite3.connect(DATABASE, timeout=30)
    os.chmod(DATABASE, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def migrate() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        has_hourly_baseline = conn.execute(
            "SELECT 1 FROM tms_hourly_source_snapshot LIMIT 1").fetchone()
        latest_tms = conn.execute(
            "SELECT * FROM etl_ingest_batch WHERE source_system='TMS' AND status='success' "
            "ORDER BY batch_id DESC LIMIT 1").fetchone()
        if not has_hourly_baseline and latest_tms:
            _save_hourly_source_snapshot(conn, latest_tms["batch_id"])
    backfill_pickup_transitions()


def _snapshot_time(batch: sqlite3.Row) -> datetime:
    """Prefer the export timestamp in the TMS filename; otherwise use acquisition time."""
    match = re.search(r"_(\d{8})_(\d{6})", batch["file_name"] or "")
    if match:
        parsed = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
        return parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    value = batch["acquired_at"] or batch["published_at"]
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _save_hourly_source_snapshot(conn: sqlite3.Connection, batch_id: int) -> bool:
    """Keep the first valid full SOO source snapshot observed for each Beijing hour."""
    from zoneinfo import ZoneInfo

    batch = conn.execute("SELECT * FROM etl_ingest_batch WHERE batch_id=? AND status='success'",
                         (batch_id,)).fetchone()
    if not batch or batch["source_system"] != "TMS":
        return False
    source_time = _snapshot_time(batch).astimezone(ZoneInfo("Asia/Shanghai"))
    slot = source_time.replace(minute=0, second=0, microsecond=0).isoformat()
    cursor = conn.execute(
        "INSERT OR IGNORE INTO tms_hourly_source_snapshot(hour_slot,batch_id,source_snapshot_at,saved_at) "
        "VALUES (?,?,?,?)", (slot, batch_id, source_time.isoformat(), now()))
    return cursor.rowcount > 0


def _hourly_pickup_events(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Diff adjacent immutable hourly source snapshots; never infer an exact pickup time."""
    snapshots = conn.execute(
        "SELECT hour_slot,batch_id,source_snapshot_at FROM tms_hourly_source_snapshot "
        "ORDER BY hour_slot").fetchall()
    events: list[dict[str, Any]] = []
    for previous, current in zip(snapshots, snapshots[1:]):
        before_rows = conn.execute(
            "SELECT domain_code,soo_no,normalized_row FROM dwd_soo_snapshot WHERE batch_id=?",
            (previous["batch_id"],)).fetchall()
        current_rows = conn.execute(
            "SELECT domain_code,soo_no,normalized_row FROM dwd_soo_snapshot WHERE batch_id=?",
            (current["batch_id"],)).fetchall()
        before = {(row["domain_code"], row["soo_no"]): json.loads(row["normalized_row"])
                  for row in before_rows}
        after = {(row["domain_code"], row["soo_no"]): json.loads(row["normalized_row"])
                 for row in current_rows}
        sp_volumes: dict[str, list[float]] = {}
        for record in after.values():
            waybill, volume = record.get("waybill_id"), record.get("gross_cbm")
            if waybill and isinstance(volume, (int, float)):
                sp_volumes.setdefault(str(waybill).strip().upper(), []).append(float(volume))
        modal_volume = {sp: Counter(values).most_common(1)[0][0]
                        for sp, values in sp_volumes.items()}
        interval_start, interval_end = previous["source_snapshot_at"], current["source_snapshot_at"]
        for key in sorted(after):
            record, old = after[key], before.get(key)
            if old is None or is_picked_up_status(old.get("tms_status")) \
                    or not is_picked_up_status(record.get("tms_status")):
                continue
            if _contains_self_pickup(record):
                continue
            waybill = record.get("waybill_id")
            sp_key = str(waybill).strip().upper() if waybill else f"SOO:{key[0]}:{key[1]}"
            events.append({
                "interval_start": interval_start, "interval_end": interval_end,
                "interval_hours": max(0, (datetime.fromisoformat(interval_end) -
                                           datetime.fromisoformat(interval_start)).total_seconds() / 3600),
                "domain_code": key[0], "soo_no": key[1], "waybill_id": waybill,
                "warehouse_name": record.get("warehouse_name"),
                "carrier_name": record.get("carrier_name"), "channel": record.get("channel"),
                "production_date": record.get("production_date"),
                "business_dates": json.dumps(business_dates_for_record(record)),
                "previous_status": old.get("tms_status"),
                "current_status": record.get("tms_status") or "未知",
                "soo_qty": record.get("soo_qty"),
                "gross_cbm": modal_volume.get(sp_key, record.get("gross_cbm")),
                "from_batch_id": previous["batch_id"], "to_batch_id": current["batch_id"],
                "hour_slot": current["hour_slot"],
            })
    return events, [dict(snapshot) for snapshot in snapshots]


def _contains_self_pickup(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_self_pickup(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_self_pickup(item) for item in value)
    return isinstance(value, str) and "自提" in value


def _insert_pickup_transitions(conn: sqlite3.Connection, previous: sqlite3.Row,
                               current: sqlite3.Row) -> int:
    start, end = _snapshot_time(previous), _snapshot_time(current)
    if end < start:
        return 0
    previous_rows = conn.execute(
        "SELECT domain_code,soo_no,normalized_row FROM dwd_soo_snapshot WHERE batch_id=?",
        (previous["batch_id"],)).fetchall()
    current_rows = conn.execute(
        "SELECT domain_code,soo_no,normalized_row FROM dwd_soo_snapshot WHERE batch_id=?",
        (current["batch_id"],)).fetchall()
    before = {(row["domain_code"], row["soo_no"]): json.loads(row["normalized_row"])
              for row in previous_rows}
    now_rows = {(row["domain_code"], row["soo_no"]): json.loads(row["normalized_row"])
                for row in current_rows}

    # The SP volume is a consolidated shipment value, not a per-SOO sum.
    volumes: dict[str, list[float]] = {}
    for record in now_rows.values():
        waybill = record.get("waybill_id")
        volume = record.get("gross_cbm")
        if waybill and isinstance(volume, (int, float)):
            volumes.setdefault(str(waybill).strip().upper(), []).append(float(volume))
    modal_volume = {sp: Counter(values).most_common(1)[0][0] for sp, values in volumes.items()}

    transitions = []
    for key in sorted(now_rows):
        record = now_rows[key]
        old = before.get(key)
        if old is None or old.get("is_picked_up") or not record.get("is_picked_up"):
            continue
        waybill = record.get("waybill_id")
        sp_key = str(waybill).strip().upper() if waybill else f"SOO:{key[0]}:{key[1]}"
        cbm = modal_volume.get(sp_key, record.get("gross_cbm"))
        self_pickup = _contains_self_pickup(record)
        transitions.append((previous["batch_id"], current["batch_id"], start.isoformat(),
            end.isoformat(), key[0], key[1], waybill, record.get("warehouse_name"),
            record.get("carrier_name"), record.get("channel"), record.get("production_date"),
            json.dumps(business_dates_for_record(record)), old.get("tms_status"),
            record.get("tms_status") or "未知", record.get("soo_qty"), cbm, sp_key,
            int(self_pickup)))

    inserted = 0
    counted_sp_ids: set[str] = set()
    for item in transitions:
        from_batch, to_batch, interval_start, interval_end, domain, soo, waybill, warehouse, carrier, channel, prod_day, dates, old_status, new_status, qty, cbm, sp_key, self_pickup = item
        already_counted = conn.execute(
            "SELECT 1 FROM pickup_status_transition WHERE waybill_id=? AND sp_volume_counted=1 LIMIT 1",
            (waybill,)).fetchone() if waybill else None
        volume_counted = int(not self_pickup and already_counted is None and sp_key not in counted_sp_ids)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO pickup_status_transition (from_batch_id,to_batch_id,interval_start,interval_end,"
            "domain_code,soo_no,waybill_id,warehouse_name,carrier_name,channel,production_date,business_dates,"
            "previous_status,current_status,soo_qty,gross_cbm,sp_volume_counted,is_self_pickup,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (from_batch, to_batch, interval_start, interval_end, domain, soo, waybill, warehouse,
             carrier, channel, prod_day, dates, old_status, new_status, qty, cbm,
             volume_counted, self_pickup, now()))
        inserted += cursor.rowcount
        if cursor.rowcount and volume_counted:
            counted_sp_ids.add(sp_key)
    return inserted


def backfill_pickup_transitions() -> int:
    """Idempotently derive status-change events from already archived TMS snapshots."""
    inserted = 0
    with connect() as conn:
        batches = conn.execute(
            "SELECT * FROM etl_ingest_batch WHERE source_system='TMS' AND status='success' ORDER BY batch_id"
        ).fetchall()
        previous = None
        for batch in batches:
            if previous is not None:
                inserted += _insert_pickup_transitions(conn, previous, batch)
            previous = batch
    return inserted


def latest_success(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM etl_ingest_batch WHERE status='success' "
                        "ORDER BY batch_id DESC LIMIT 1").fetchone()


def collection_status(conn: sqlite3.Connection) -> dict[str, Any]:
    latest = conn.execute("SELECT * FROM tms_sync_run ORDER BY run_id DESC LIMIT 1").fetchone()
    success = conn.execute("SELECT * FROM tms_sync_run WHERE status='success' "
                           "ORDER BY run_id DESC LIMIT 1").fetchone()
    watcher_job = conn.execute("SELECT * FROM collector_job WHERE job_kind='download-folder' "
                               "ORDER BY job_id DESC LIMIT 1").fetchone()
    batch = latest_success(conn)
    from ingestion.watch_tms_exports import candidates, current_download_dir

    workflow_dir = current_download_dir()
    latest_file = next(iter(candidates()), None)
    current = datetime.now(timezone.utc)
    latest_data = dict(latest) if latest else None
    if latest_data and latest_data["status"] == "running" and (
        current - datetime.fromisoformat(latest_data["started_at"])
    ).total_seconds() > 15 * 60:
        latest_data.update(status="stalled", error_code="collection_timeout",
                           error_message="采集超过 15 分钟未完成，保留上一成功数据")
    return {"interval_seconds": 30, "monitor_mode": "daily_workflow_latest",
            "workflow_dir": str(workflow_dir), "workflow_dir_name": workflow_dir.name,
            "workflow_file_present": latest_file is not None,
            "latest_workflow_file": latest_file.name if latest_file else None,
            "latest_workflow_file_mtime": datetime.fromtimestamp(
                latest_file.stat().st_mtime, timezone.utc).isoformat() if latest_file else None,
            "latest_workflow_job": dict(watcher_job) if watcher_job else None,
            "schedule_enabled": False, "automation_id": None,
            "next_scheduled_at": None,
            "latest_run": latest_data,
            "last_success_at": batch["published_at"] if batch else (
                success["finished_at"] if success else None),
            "last_success_batch_id": batch["batch_id"] if batch else (
                success["batch_id"] if success else None)}


def public_snapshot() -> dict[str, Any]:
    with connect() as conn:
        collection = collection_status(conn)
        latest = latest_success(conn)
        if not latest:
            return {"schema_version": "mvp-2.0", "source": {}, "quality": {},
                    "business_dates": [], "records": [], "collection": collection}
        records = []
        current_rows = conn.execute(
            "SELECT current.normalized_row, raw.payload FROM dwd_soo_current AS current "
            "LEFT JOIN ods_tms_soo_raw AS raw ON raw.batch_id=current.last_batch_id "
            "AND raw.domain_code=current.domain_code AND raw.soo_no=current.soo_no "
            "AND raw.row_no=json_extract(current.normalized_row, '$.row_no') "
            "ORDER BY current.production_date DESC, current.soo_no").fetchall()
        timeline_fields = {
            "创建时间": "tms_created_at",
            "围栏进入时间": "park_entered_at",
            "围栏离开时间": "park_exited_at",
            "生产日期": "production_at",
        }
        for row in current_rows:
            record = json.loads(row[0])
            if row[1] and any(not record.get(field) for field in timeline_fields.values()):
                raw_payload = json.loads(row[1])
                for source_name, field in timeline_fields.items():
                    if not record.get(field) and raw_payload.get(source_name):
                        timestamp = parse_datetime(raw_payload[source_name])[0]
                        record[field] = timestamp
                        if field == "production_at" and timestamp:
                            record["production_date"] = timestamp[:10]
            record["is_picked_up"] = is_picked_up_status(record.get("tms_status"))
            flags = [flag for flag in record.get("quality_flags", [])
                     if flag != "状态与实际提货时间冲突"]
            if bool(record.get("actual_pickup_at")) != record["is_picked_up"]:
                flags.append("状态与实际提货时间冲突")
            record["quality_flags"] = flags
            records.append(record)
        dates = sorted({business_date for row in records
                        for business_date in business_dates_for_record(row)})
        latest_rows = [row for row in records if row.get("source_updated_at")]
        latest_source_time = max((row["source_updated_at"] for row in latest_rows), default=None)
        checked_at = max(filter(None, (latest["published_at"], collection["last_success_at"])))
        return {"schema_version": "mvp-2.0", "source": {
            "source_system": latest["source_system"], "file_name": latest["file_name"],
            "file_sha256": latest["file_sha256"],
            "last_source_updated_at": latest_source_time,
            "last_success_sync_at": checked_at,
            "last_success_import_at": latest["published_at"],
            "last_tms_check_at": collection["last_success_at"],
            "published_batch_id": latest["batch_id"], "report_scope": latest["report_scope"],
        }, "quality": {**json.loads(latest["quality"]),
                       "state_conflicts": sum(
                           bool(row.get("actual_pickup_at")) != row["is_picked_up"]
                           for row in records)}, "business_dates": dates,
            "records": records, "collection": collection}


def ingest_file(path: Path, source_system: str = "TMS", report_scope: str | None = None,
                file_name: str | None = None) -> dict[str, Any]:
    if not path.is_file() or path.suffix.lower() not in (".xlsx", ".csv"):
        raise ValueError("仅接受已存在的 XLSX/CSV 报表")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("报表超过 200 MB 限制")
    payload = extract(path)
    digest = payload["source"]["file_sha256"]
    raw_rows = payload.pop("raw_rows")
    quality = payload["quality"]
    records = payload["records"]
    valid = len(records)
    hard_failure = (valid == 0 or quality["invalid_qty_values"] > 0 or
                    quality["negative_qty_values"] > 0 or
                    quality["invalid_datetime_values"] > max(5, valid // 10))
    ARCHIVE.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ARCHIVE, 0o700)
    archived = ARCHIVE / f"{digest}{path.suffix.lower()}"
    if not archived.exists():
        shutil.copy2(path, archived)
        os.chmod(archived, 0o600)
    record_by_row = {record["row_no"]: record for record in records}
    newest_by_soo: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = (record["domain_code"], record["soo_no"])
        previous = newest_by_soo.get(key)
        if previous is None or (record["source_updated_at"] or "", record["row_no"]) >= (
            previous["source_updated_at"] or "", previous["row_no"]
        ):
            newest_by_soo[key] = record
    with connect() as conn:
        existing = conn.execute(
            "SELECT batch_id,status FROM etl_ingest_batch WHERE source_system=? AND file_sha256=?",
            (source_system, digest)).fetchone()
        if existing:
            if source_system == "TMS" and existing["status"] == "success":
                _save_hourly_source_snapshot(conn, existing["batch_id"])
            return {"batch_id": existing["batch_id"], "status": existing["status"],
                    "duplicate": True, "valid_soo_count": valid}
        batch_id = conn.execute(
            "INSERT INTO etl_ingest_batch (source_system,file_name,file_sha256,file_storage_uri,"
            "report_scope,acquired_at,row_count,valid_soo_count,reject_count,quality,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING batch_id",
            (source_system, file_name or path.name, digest, str(archived), report_scope, now(),
             quality["total_body_rows"], valid,
             quality["invalid_qty_values"] + quality["negative_qty_values"],
             json.dumps(quality, ensure_ascii=False), "running")).fetchone()[0]
        for raw in raw_rows:
            record = record_by_row[raw["row_no"]]
            conn.execute("INSERT INTO ods_tms_soo_raw VALUES (?,?,?,?,?,?)",
                         (batch_id, raw["row_no"], raw["domain_code"], raw["soo_no"],
                          json.dumps(raw["payload"], ensure_ascii=False),
                          json.dumps(record["quality_flags"], ensure_ascii=False)))
        if hard_failure:
            conn.execute("UPDATE etl_ingest_batch SET status='quarantined',error_message=? "
                         "WHERE batch_id=?", ("有效 SOO 为空或关键字段质量未通过", batch_id))
            return {"batch_id": batch_id, "status": "quarantined", "duplicate": False,
                    "valid_soo_count": valid, "quality": quality}
        for record in newest_by_soo.values():
            normalized = json.dumps(record, ensure_ascii=False)
            values = (record["domain_code"], record["soo_no"], record["source_updated_at"])
            conn.execute("INSERT INTO dwd_soo_snapshot VALUES (?,?,?,?,?)",
                         (batch_id, *values, normalized))
            current = conn.execute(
                "SELECT source_updated_at FROM dwd_soo_current WHERE domain_code=? AND soo_no=?",
                values[:2]).fetchone()
            if current and current["source_updated_at"] and (
                not record["source_updated_at"] or
                record["source_updated_at"] < current["source_updated_at"]
            ):
                continue
            conn.execute(
                "INSERT INTO dwd_soo_current VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(domain_code,soo_no) DO UPDATE SET "
                "production_date=excluded.production_date,source_updated_at=excluded.source_updated_at,"
                "last_batch_id=excluded.last_batch_id,normalized_row=excluded.normalized_row,"
                "refreshed_at=excluded.refreshed_at",
                (record["domain_code"], record["soo_no"], record["production_date"],
                 record["source_updated_at"], batch_id, normalized, now()))
        conn.execute("UPDATE etl_ingest_batch SET status='success',published_at=? WHERE batch_id=?",
                     (now(), batch_id))
        if source_system == "TMS":
            _save_hourly_source_snapshot(conn, batch_id)
            previous_batch = conn.execute(
                "SELECT * FROM etl_ingest_batch WHERE source_system='TMS' AND status='success' "
                "AND batch_id<? ORDER BY batch_id DESC LIMIT 1", (batch_id,)).fetchone()
            if previous_batch:
                _insert_pickup_transitions(conn, previous_batch,
                    conn.execute("SELECT * FROM etl_ingest_batch WHERE batch_id=?", (batch_id,)).fetchone())
    return {"batch_id": batch_id, "status": "success", "duplicate": False,
            "valid_soo_count": valid, "quality": quality}


@app.get("/")
def homepage() -> FileResponse:
    return FileResponse(ROOT / "dist" / "index.html")


@app.get("/data.json")
def data_json() -> JSONResponse:
    return JSONResponse(public_snapshot(), headers={"Cache-Control": "no-store"})


@app.get("/bi-report.js")
def bi_report_script() -> FileResponse:
    return FileResponse(ROOT / "dist" / "bi-report.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})


@app.get("/warehouse-carriers.json")
def warehouse_carriers_json() -> FileResponse:
    path = ROOT / "dist" / "warehouse-carriers.json"
    if not path.is_file():
        raise HTTPException(status_code=503, detail="仓库承运商关系尚未导入")
    return FileResponse(path, media_type="application/json",
                        headers={"Cache-Control": "no-store"})


@app.get("/outbound-data.json")
def outbound_data_json() -> FileResponse:
    path = ROOT / "dist" / "outbound-data.json"
    if not path.is_file():
        raise HTTPException(status_code=503, detail="出库查询表尚未导入")
    return FileResponse(path, media_type="application/json",
                        headers={"Cache-Control": "no-store"})


@app.get("/hourly-outbound-data.json")
def hourly_outbound_data_json() -> FileResponse:
    path = ROOT / "dist" / "hourly-outbound-data.json"
    if not path.is_file():
        raise HTTPException(status_code=503, detail="每小时出库数据尚未导入")
    return FileResponse(path, media_type="application/json",
                        headers={"Cache-Control": "no-store"})


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    with connect() as conn:
        latest = latest_success(conn)
        count = conn.execute("SELECT count(*) FROM dwd_soo_current").fetchone()[0]
        collection = collection_status(conn)
    return {"status": "ok", "database": "sqlite", "current_soos": count,
            "latest_batch_id": latest["batch_id"] if latest else None,
            "last_success_sync_at": max(filter(None, (
                latest["published_at"] if latest else None, collection["last_success_at"])), default=None),
            "collection": collection}


@app.get("/api/v1/sync/status")
def sync_status() -> dict[str, Any]:
    with connect() as conn:
        return collection_status(conn)


@app.get("/api/v1/imports")
def imports() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT batch_id,source_system,file_name,acquired_at,published_at,"
                            "row_count,valid_soo_count,status,error_message FROM etl_ingest_batch "
                            "ORDER BY batch_id DESC LIMIT 30").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/v1/pickup/transitions")
def pickup_transitions(business_date: str | None = None, limit: int = 500,
                       offset: int = 0) -> dict[str, Any]:
    """Return status transitions as snapshot-bounded time intervals, not fabricated exact times."""
    if business_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", business_date):
        raise HTTPException(status_code=400, detail="business_date 请使用 YYYY-MM-DD")
    limit = min(max(limit, 1), 5000)
    offset = max(offset, 0)
    with connect() as conn:
        if business_date:
            where = "WHERE is_self_pickup=0 AND business_dates LIKE ?"
            params: tuple[Any, ...] = (f'%"{business_date}"%',)
            total = conn.execute(
                f"SELECT count(*) FROM pickup_status_transition {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM pickup_status_transition {where} "
                "ORDER BY interval_end,warehouse_name,carrier_name,soo_no LIMIT ? OFFSET ?",
                (*params, limit, offset)).fetchall()
        else:
            total = conn.execute(
                "SELECT count(*) FROM pickup_status_transition WHERE is_self_pickup=0").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM pickup_status_transition WHERE is_self_pickup=0 "
                "ORDER BY interval_end,warehouse_name,carrier_name,soo_no LIMIT ? OFFSET ?",
                (limit, offset)).fetchall()
    return {"time_semantics": "提货发生于 interval_start（上次快照）至 interval_end（本次快照）之间，非精确提货时间",
            "business_date": business_date, "count": len(rows), "total": total,
            "limit": limit, "offset": offset,
            "transitions": [{key: row[key] for key in row.keys() if key not in ("created_at",)}
                            for row in rows]}


@app.get("/api/v1/pickup/hourly-transitions")
def hourly_pickup_transitions(business_date: str | None = None, limit: int = 5000,
                              offset: int = 0) -> dict[str, Any]:
    """Return pickup changes derived only from the saved once-per-Beijing-hour snapshots."""
    if business_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", business_date):
        raise HTTPException(status_code=400, detail="business_date 请使用 YYYY-MM-DD")
    limit = min(max(limit, 1), 5000)
    offset = max(offset, 0)
    with connect() as conn:
        transitions, snapshots = _hourly_pickup_events(conn)
    if business_date:
        transitions = [event for event in transitions
                       if business_date in json.loads(event["business_dates"])]
    total = len(transitions)
    selected = transitions[offset:offset + limit]
    return {
        "basis": "每个北京时间小时首份成功TMS数据源快照；相邻小时快照对比",
        "time_semantics": "状态变化仅能定位在前后小时快照之间，不代表精确提货时刻",
        "business_date": business_date, "snapshot_count": len(snapshots),
        "baseline_ready": len(snapshots) >= 2,
        "snapshot_hours": [snapshot["hour_slot"] for snapshot in snapshots],
        "count": len(selected), "total": total, "limit": limit, "offset": offset,
        "transitions": selected,
    }


@app.post("/api/v1/imports/upload")
def upload(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    origin = request.headers.get("origin")
    if origin and origin not in ("http://127.0.0.1:4174", "http://localhost:4174"):
        raise HTTPException(status_code=403, detail="只允许本机驾驶舱页面上传")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".xlsx", ".csv"):
        raise HTTPException(status_code=400, detail="只接受 XLSX 或 CSV")
    STAGING.mkdir(mode=0o700, exist_ok=True)
    os.chmod(STAGING, 0o700)
    fd, name = tempfile.mkstemp(suffix=suffix, dir=STAGING)
    try:
        with os.fdopen(fd, "wb") as target:
            while chunk := file.file.read(1024 * 1024):
                if target.tell() + len(chunk) > MAX_UPLOAD_BYTES:
                    raise ValueError("报表超过 200 MB 限制")
                target.write(chunk)
        original_name = (file.filename or "uploaded.xlsx").replace("\\", "/").split("/")[-1]
        return ingest_file(Path(name), source_system="TMS_UPLOAD", file_name=original_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        Path(name).unlink(missing_ok=True)


@app.get("/api/v1/snapshots/{batch_id}")
def snapshot(batch_id: int) -> dict[str, Any]:
    with connect() as conn:
        batch = conn.execute("SELECT status,published_at FROM etl_ingest_batch WHERE batch_id=?",
                             (batch_id,)).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="批次不存在")
        rows = [json.loads(row[0]) for row in conn.execute(
            "SELECT normalized_row FROM dwd_soo_snapshot WHERE batch_id=? ORDER BY soo_no",
            (batch_id,)).fetchall()]
    return {"batch_id": batch_id, "status": batch["status"],
            "published_at": batch["published_at"], "records": rows}
