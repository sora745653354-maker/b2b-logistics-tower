"""Local PostgreSQL-backed API for the B2B logistics control tower."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ingestion.extract_sample import business_dates_for_record, extract  # noqa: E402

RUNTIME = ROOT / "runtime"
CONFIG = RUNTIME / "db-connection.json"
ARCHIVE = RUNTIME / "archive"
STAGING = RUNTIME / "staging"
MIGRATION = ROOT / "migrations" / "001_init.sql"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

app = FastAPI(title="B2B 物流控制塔", docs_url="/api/docs", redoc_url=None)


def conninfo() -> dict[str, Any]:
    if not CONFIG.exists():
        raise RuntimeError("数据库尚未初始化，请先运行 backend/setup_local.py")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def connect() -> psycopg.Connection:
    return psycopg.connect(**conninfo())


def migrate() -> None:
    with connect() as conn:
        conn.execute(MIGRATION.read_text(encoding="utf-8"))


def newest_success(conn: psycopg.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT batch_id, source_system, file_name, file_sha256, acquired_at, published_at, "
        "row_count, valid_soo_count, quality, report_scope FROM etl_ingest_batch "
        "WHERE status = 'success' ORDER BY published_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    fields = ["batch_id", "source_system", "file_name", "file_sha256", "acquired_at",
              "published_at", "row_count", "valid_soo_count", "quality", "report_scope"]
    return dict(zip(fields, row, strict=True))


def public_snapshot() -> dict[str, Any]:
    with connect() as conn:
        latest = newest_success(conn)
        if not latest:
            return {"schema_version": "mvp-2.0", "source": {}, "quality": {},
                    "business_dates": [], "records": []}
        records = [row[0] for row in conn.execute(
            "SELECT normalized_row FROM dwd_soo_current ORDER BY production_date DESC NULLS LAST, soo_no"
        ).fetchall()]
        dates = sorted({business_date for row in records
                        for business_date in business_dates_for_record(row)})
        updates = sorted({row["source_updated_at"] for row in records if row.get("source_updated_at")})
        quality = dict(latest["quality"] or {})
        return {
            "schema_version": "mvp-2.0",
            "source": {
                "source_system": latest["source_system"],
                "file_name": latest["file_name"],
                "file_sha256": latest["file_sha256"],
                "last_source_updated_at": updates[-1] if updates else None,
                "last_success_sync_at": latest["published_at"].isoformat() if latest["published_at"] else None,
                "published_batch_id": latest["batch_id"],
                "report_scope": latest["report_scope"],
            },
            "quality": quality,
            "business_dates": dates,
            "records": records,
        }


def ingest_file(path: Path, source_system: str = "TMS", report_scope: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("导入文件不存在")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("报表超过 200 MB 限制")
    if path.suffix.lower() not in (".xlsx", ".csv"):
        raise ValueError("只接受 XLSX 或 CSV")
    payload = extract(path)
    digest = payload["source"]["file_sha256"]
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    os.chmod(ARCHIVE, 0o700)
    archived = ARCHIVE / f"{digest}{path.suffix.lower()}"
    if not archived.exists():
        shutil.copy2(path, archived)
        os.chmod(archived, 0o600)

    quality = payload["quality"]
    raw_rows = payload.pop("raw_rows")
    records = payload["records"]
    valid = len(records)
    hard_failure = (valid == 0 or quality["invalid_qty_values"] > 0 or
                    quality["negative_qty_values"] > 0 or
                    quality["invalid_datetime_values"] > max(5, valid // 10))
    by_row_no = {record["row_no"]: record for record in records}
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = (record["domain_code"], record["soo_no"])
        previous = latest_by_key.get(key)
        if previous is None or (record["source_updated_at"] or "", record["row_no"]) >= (
            previous["source_updated_at"] or "", previous["row_no"]
        ):
            latest_by_key[key] = record

    with connect() as conn:
        existing = conn.execute(
            "SELECT batch_id, status FROM etl_ingest_batch WHERE source_system = %s AND file_sha256 = %s",
            (source_system, digest),
        ).fetchone()
        if existing:
            return {"batch_id": existing[0], "status": existing[1], "duplicate": True,
                    "valid_soo_count": valid}
        batch_id = conn.execute(
            "INSERT INTO etl_ingest_batch (source_system, file_name, file_sha256, file_storage_uri, "
            "report_scope, row_count, valid_soo_count, reject_count, quality, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'running') RETURNING batch_id",
            (source_system, path.name, digest, str(archived), report_scope,
             quality["total_body_rows"], valid, quality["invalid_qty_values"] +
             quality["negative_qty_values"], Jsonb(quality)),
        ).fetchone()[0]
        with conn.cursor() as cur:
            for raw in raw_rows:
                record = by_row_no[raw["row_no"]]
                cur.execute(
                    "INSERT INTO ods_tms_soo_raw (batch_id, row_no, domain_code, soo_no, payload, quality_flags) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (batch_id, raw["row_no"], raw["domain_code"], raw["soo_no"],
                     Jsonb(raw["payload"]), Jsonb(record["quality_flags"])),
                )
            if hard_failure:
                cur.execute(
                    "UPDATE etl_ingest_batch SET status = 'quarantined', error_message = %s WHERE batch_id = %s",
                    ("有效 SOO 为空或关键字段质量未通过", batch_id),
                )
                return {"batch_id": batch_id, "status": "quarantined", "duplicate": False,
                        "valid_soo_count": valid, "quality": quality}
            for record in latest_by_key.values():
                cur.execute(
                    "INSERT INTO dwd_soo_snapshot "
                    "(batch_id, domain_code, soo_no, source_updated_at, normalized_row) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (batch_id, record["domain_code"], record["soo_no"],
                     record["source_updated_at"], Jsonb(record)),
                )
                cur.execute(
                    "INSERT INTO dwd_soo_current "
                    "(domain_code, soo_no, production_date, source_updated_at, last_batch_id, normalized_row) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (domain_code, soo_no) DO UPDATE SET "
                    "production_date = EXCLUDED.production_date, "
                    "source_updated_at = EXCLUDED.source_updated_at, "
                    "last_batch_id = EXCLUDED.last_batch_id, "
                    "normalized_row = EXCLUDED.normalized_row, refreshed_at = now() "
                    "WHERE COALESCE(EXCLUDED.source_updated_at, '-infinity'::timestamptz) >= "
                    "COALESCE(dwd_soo_current.source_updated_at, '-infinity'::timestamptz)",
                    (record["domain_code"], record["soo_no"], record["production_date"],
                     record["source_updated_at"], batch_id, Jsonb(record)),
                )
            cur.execute("UPDATE etl_ingest_batch SET status = 'success', published_at = now() WHERE batch_id = %s", (batch_id,))
            cur.execute("UPDATE app_data_version SET published_batch_id = %s, updated_at = now() WHERE singleton", (batch_id,))
    return {"batch_id": batch_id, "status": "success", "duplicate": False,
            "valid_soo_count": valid, "quality": quality}


@app.get("/")
def homepage() -> FileResponse:
    return FileResponse(ROOT / "dist" / "index.html")


@app.get("/data.json")
def data_json() -> JSONResponse:
    try:
        return JSONResponse(public_snapshot())
    except (psycopg.Error, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="数据库暂不可用") from exc


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    with connect() as conn:
        latest = newest_success(conn)
        current_count = conn.execute("SELECT count(*) FROM dwd_soo_current").fetchone()[0]
    return {"status": "ok", "database": "postgresql", "current_soos": current_count,
            "latest_batch_id": latest["batch_id"] if latest else None,
            "last_success_sync_at": latest["published_at"].isoformat() if latest else None}


@app.get("/api/v1/imports")
def imports() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT batch_id, source_system, file_name, acquired_at, published_at, row_count, "
            "valid_soo_count, status, error_message FROM etl_ingest_batch ORDER BY batch_id DESC LIMIT 30"
        ).fetchall()
    names = ["batch_id", "source_system", "file_name", "acquired_at", "published_at",
             "row_count", "valid_soo_count", "status", "error_message"]
    return [{key: value.isoformat() if isinstance(value, datetime) else value
             for key, value in zip(names, row, strict=True)} for row in rows]


@app.post("/api/v1/imports/upload")
def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".xlsx", ".csv"):
        raise HTTPException(status_code=400, detail="只接受 XLSX 或 CSV")
    STAGING.mkdir(parents=True, exist_ok=True)
    os.chmod(STAGING, 0o700)
    fd, name = tempfile.mkstemp(suffix=suffix, dir=STAGING)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := file.file.read(1024 * 1024):
                if out.tell() + len(chunk) > MAX_UPLOAD_BYTES:
                    raise ValueError("报表超过 200 MB 限制")
                out.write(chunk)
        return ingest_file(Path(name), source_system="TMS_UPLOAD")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        Path(name).unlink(missing_ok=True)


@app.get("/api/v1/snapshots/{batch_id}")
def snapshot(batch_id: int) -> dict[str, Any]:
    with connect() as conn:
        batch = conn.execute(
            "SELECT batch_id, status, published_at FROM etl_ingest_batch WHERE batch_id = %s", (batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="批次不存在")
        rows = [row[0] for row in conn.execute(
            "SELECT normalized_row FROM dwd_soo_snapshot WHERE batch_id = %s ORDER BY soo_no", (batch_id,)
        ).fetchall()]
    return {"batch_id": batch_id, "status": batch[1],
            "published_at": batch[2].isoformat() if batch[2] else None, "records": rows}
