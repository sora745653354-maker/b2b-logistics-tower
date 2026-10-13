from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from backend import sqlite_app as tower
from ingestion.extract_sample import business_dates_for_record, extract, is_picked_up_status


HEADERS = ["域", "SOO订单号", "生产日期", "创建时间", "装载完成时间",
           "围栏进入时间", "围栏离开时间", "实际提货日期", "状态", "soo下发件数",
           "更新日期", "联系人姓名"]


def workbook(path: Path, updated: str, quantity: int, contact: str = "PRIVATE_CONTACT",
             status: str | None = "开放", pickup_at: str | None = None,
             production_at: str = "2026-10-07") -> Path:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "SOO明细"
    sheet.append(HEADERS)
    sheet.append(["bbk2b", "SOO-TEST-1", production_at, "2026-10-07 08:00:00",
                  "2026-10-07 10:00:00", "2026-10-07 11:00:00", "2026-10-07 12:00:00",
                  pickup_at, status, quantity, updated, contact])
    book.save(path)
    return path


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(tower, "RUNTIME", runtime)
    monkeypatch.setattr(tower, "DATABASE", runtime / "tower.sqlite3")
    monkeypatch.setattr(tower, "ARCHIVE", runtime / "archive")
    monkeypatch.setattr(tower, "STAGING", runtime / "staging")
    tower.migrate()
    return tmp_path


def test_snapshot_idempotency_and_no_pii(isolated: Path) -> None:
    source = workbook(isolated / "new.xlsx", "2026-10-08 15:00:00", 12)
    first = tower.ingest_file(source)
    second = tower.ingest_file(source)
    assert first["status"] == "success" and second["duplicate"]
    assert first["batch_id"] == second["batch_id"]
    public = tower.public_snapshot()
    assert public["records"][0]["soo_qty"] == 12
    assert public["records"][0]["tms_created_at"] == "2026-10-07T08:00:00+08:00"
    assert public["records"][0]["production_at"] == "2026-10-07T00:00:00+08:00"
    assert public["records"][0]["park_entered_at"] == "2026-10-07T11:00:00+08:00"
    assert public["records"][0]["park_exited_at"] == "2026-10-07T12:00:00+08:00"
    assert public["records"][0]["is_picked_up"] is False
    with tower.connect() as conn:
        row = conn.execute("SELECT normalized_row FROM dwd_soo_current").fetchone()
        legacy = json.loads(row[0])
        for field in ("tms_created_at", "park_entered_at", "park_exited_at"):
            legacy.pop(field)
        conn.execute("UPDATE dwd_soo_current SET normalized_row=?",
                     (json.dumps(legacy),))
    # Older published rows are hydrated from that batch's whitelisted raw fields.
    hydrated = tower.public_snapshot()["records"][0]
    assert hydrated["tms_created_at"] == "2026-10-07T08:00:00+08:00"
    assert hydrated["park_exited_at"] == "2026-10-07T12:00:00+08:00"
    assert hydrated["production_at"] == "2026-10-07T00:00:00+08:00"
    assert "PRIVATE_CONTACT" not in str(public)
    with tower.connect() as conn:
        assert conn.execute("SELECT count(*) FROM dwd_soo_current").fetchone()[0] == 1
        assert "PRIVATE_CONTACT" in conn.execute("SELECT payload FROM ods_tms_soo_raw").fetchone()[0]


def test_pickup_transition_is_interval_bounded_idempotent_and_queryable(isolated: Path) -> None:
    pending = workbook(isolated / "订单明细报表（soo）_20261011_100000.xlsx",
                       "2026-10-11 10:00:00", 12, status="委托",
                       production_at="2026-10-10 12:00:00")
    picked = workbook(isolated / "订单明细报表（soo）_20261011_110000.xlsx",
                      "2026-10-11 11:00:00", 12, status="到货",
                      production_at="2026-10-10 12:00:00")
    first = tower.ingest_file(pending, source_system="TMS")
    assert first["status"] == "success"
    with tower.connect() as conn:
        assert conn.execute("SELECT count(*) FROM pickup_status_transition").fetchone()[0] == 0

    second = tower.ingest_file(picked, source_system="TMS")
    assert second["status"] == "success"
    assert tower.backfill_pickup_transitions() == 0
    with tower.connect() as conn:
        row = conn.execute("SELECT * FROM pickup_status_transition").fetchone()
        assert row["interval_start"] == "2026-10-11T10:00:00+08:00"
        assert row["interval_end"] == "2026-10-11T11:00:00+08:00"
        assert row["previous_status"] == "委托" and row["current_status"] == "到货"
        assert json.loads(row["business_dates"]) == ["2026-10-10", "2026-10-11"]

    with TestClient(tower.app) as client:
        response = client.get("/api/v1/pickup/transitions", params={"business_date": "2026-10-10"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["transitions"][0]["soo_no"] == "SOO-TEST-1"
    assert "非精确提货时间" in data["time_semantics"]


def test_hourly_pickup_uses_one_immutable_source_snapshot_per_hour(isolated: Path) -> None:
    baseline = workbook(isolated / "订单明细报表（soo）_20261011_100500.xlsx",
                        "2026-10-11 10:05:00", 12, status="委托",
                        production_at="2026-10-10 12:00:00")
    mid_hour = workbook(isolated / "订单明细报表（soo）_20261011_105500.xlsx",
                        "2026-10-11 10:55:00", 12, status="到货",
                        production_at="2026-10-10 12:00:00")
    next_hour = workbook(isolated / "订单明细报表（soo）_20261011_110500.xlsx",
                         "2026-10-11 11:05:00", 12, status="到货",
                         production_at="2026-10-10 12:00:00")
    tower.ingest_file(baseline, source_system="TMS")
    tower.ingest_file(mid_hour, source_system="TMS")
    with tower.connect() as conn:
        snapshot = conn.execute("SELECT hour_slot,batch_id FROM tms_hourly_source_snapshot").fetchone()
        assert snapshot["hour_slot"] == "2026-10-11T10:00:00+08:00"
        assert snapshot["batch_id"] == 1
    tower.ingest_file(next_hour, source_system="TMS")
    with TestClient(tower.app) as client:
        response = client.get("/api/v1/pickup/hourly-transitions",
                              params={"business_date": "2026-10-11"})
    assert response.status_code == 200
    data = response.json()
    assert data["baseline_ready"] is True
    assert data["snapshot_count"] == 2
    assert data["total"] == 1
    event = data["transitions"][0]
    assert event["soo_no"] == "SOO-TEST-1"
    assert event["interval_start"] == "2026-10-11T10:05:00+08:00"
    assert event["interval_end"] == "2026-10-11T11:05:00+08:00"
    assert "相邻小时快照" in data["basis"]


def test_first_seen_picked_order_is_baseline_not_transition(isolated: Path) -> None:
    already_picked = workbook(isolated / "订单明细报表（soo）_20261011_100000.xlsx",
                              "2026-10-11 10:00:00", 12, status="签收")
    tower.ingest_file(already_picked, source_system="TMS")
    assert tower.backfill_pickup_transitions() == 0
    with tower.connect() as conn:
        assert conn.execute("SELECT count(*) FROM pickup_status_transition").fetchone()[0] == 0


@pytest.mark.parametrize(("production_at", "expected"), [
    ("2026-10-10 00:00:00", ["2026-10-10", "2026-10-11"]),
    ("2026-10-10 11:59:59", ["2026-10-10", "2026-10-11"]),
    ("2026-10-10 12:00:00", ["2026-10-10", "2026-10-11"]),
    ("2026-10-10 12:00:01", ["2026-10-11"]),
    ("2026-10-10 23:59:59", ["2026-10-11"]),
])
def test_business_date_keeps_plus_one_and_adds_same_day_morning(
    isolated: Path, production_at: str, expected: list[str]
) -> None:
    payload = extract(workbook(isolated / "business-date.xlsx", "2026-10-10 14:00:00", 3,
                               production_at=production_at))
    record = payload["records"][0]
    assert record["production_at"] == production_at.replace(" ", "T") + "+08:00"
    assert business_dates_for_record(record) == expected
    assert payload["business_dates"] == expected


@pytest.mark.parametrize(("status", "expected"), [
    ("开放", False), ("委托", False), ("确认", False),
    ("提货", True), ("到货", True), ("接受", True), ("批准", True),
    (None, False), ("未知", False),
])
def test_pickup_status_business_rule(status: str | None, expected: bool) -> None:
    assert is_picked_up_status(status) is expected


def test_pickup_status_is_authoritative_and_timestamp_mismatch_is_quality_flag(isolated: Path) -> None:
    pending = extract(workbook(isolated / "pending.xlsx", "2026-10-08 15:00:00", 12,
                               status="委托", pickup_at="2026-10-07 13:00:00"))
    picked = extract(workbook(isolated / "picked.xlsx", "2026-10-08 15:00:00", 12,
                              status="到货", pickup_at=None))
    assert pending["records"][0]["is_picked_up"] is False
    assert picked["records"][0]["is_picked_up"] is True
    assert pending["records"][0]["quality_flags"] == ["状态与实际提货时间冲突"]
    assert picked["records"][0]["quality_flags"] == ["状态与实际提货时间冲突"]


def test_older_report_does_not_replace_newer_state(isolated: Path) -> None:
    tower.ingest_file(workbook(isolated / "new.xlsx", "2026-10-08 15:00:00", 12))
    tower.ingest_file(workbook(isolated / "old.xlsx", "2026-10-07 15:00:00", 1))
    assert tower.public_snapshot()["records"][0]["soo_qty"] == 12
    with tower.connect() as conn:
        assert conn.execute("SELECT count(*) FROM dwd_soo_snapshot").fetchone()[0] == 2


def test_invalid_quantity_quarantined(isolated: Path) -> None:
    result = tower.ingest_file(workbook(isolated / "bad.xlsx", "2026-10-08 15:00:00", -3))
    assert result["status"] == "quarantined"
    assert tower.public_snapshot()["records"] == []


def test_cross_origin_upload_rejected(isolated: Path) -> None:
    source = workbook(isolated / "upload.xlsx", "2026-10-08 15:00:00", 4)
    with TestClient(tower.app) as client, source.open("rb") as stream:
        response = client.post("/api/v1/imports/upload", files={"file": ("upload.xlsx", stream)},
                               headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403


def test_local_upload_preserves_original_filename(isolated: Path) -> None:
    source = workbook(isolated / "upload.xlsx", "2026-10-08 15:00:00", 4)
    with TestClient(tower.app) as client, source.open("rb") as stream:
        response = client.post("/api/v1/imports/upload", files={"file": ("真实TMS报表.xlsx", stream)},
                               headers={"Origin": "http://127.0.0.1:4174"})
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert tower.public_snapshot()["source"]["file_name"] == "真实TMS报表.xlsx"
