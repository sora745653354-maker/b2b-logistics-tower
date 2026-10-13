from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backend import sqlite_app as tower
from ingestion import watch_tms_exports as watcher
from test_sqlite_ingestion import isolated, workbook  # noqa: F401


def today_folder(root: Path, *, padded: bool = False) -> Path:
    today = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = f"{today.day:02d}" if padded else str(today.day)
    folder = root / f"{today.month}月{day}日工作流"
    folder.mkdir(parents=True)
    return folder


def test_watcher_accepts_unpadded_today_folder_and_imports_only_newest(isolated: Path,
                                                                         monkeypatch) -> None:
    root = isolated / "workflow-root"
    folder = today_folder(root)
    monkeypatch.setattr(watcher, "DOWNLOAD_DIR", root)
    older = workbook(folder / "订单明细报表（soo）.xlsx", "2026-10-08 15:00:00", 11)
    newest = workbook(folder / "订单明细报表（soo） (1).xlsx", "2026-10-09 15:00:00", 22)
    old_stamp = datetime.now().timestamp() - 60
    os.utime(older, (old_stamp - 5, old_stamp - 5))
    os.utime(newest, (old_stamp, old_stamp))

    assert watcher.candidates() == [newest]
    results = watcher.scan_once()

    assert len(results) == 1 and results[0]["status"] == "success"
    snapshot = tower.public_snapshot()
    assert snapshot["source"]["file_name"] == newest.name
    assert snapshot["records"][0]["soo_qty"] == 22
    with tower.connect() as conn:
        assert conn.execute("SELECT count(*) FROM etl_ingest_batch").fetchone()[0] == 1


def test_watcher_does_not_fall_back_to_previous_day(isolated: Path, monkeypatch) -> None:
    root = isolated / "workflow-root"
    today = datetime.now(ZoneInfo("Asia/Shanghai"))
    yesterday = today - timedelta(days=1)
    folder = root / f"{yesterday.month}月{yesterday.day}日工作流"
    folder.mkdir(parents=True)
    stale = workbook(folder / "订单明细报表（soo）.xlsx", "2026-10-08 15:00:00", 11)
    os.utime(stale, (datetime.now().timestamp() - 60,) * 2)
    monkeypatch.setattr(watcher, "DOWNLOAD_DIR", root)

    assert watcher.candidates() == []
    assert watcher.scan_once() == []
    assert tower.public_snapshot()["records"] == []


def test_watcher_uses_padded_folder_when_that_is_the_only_daily_folder(isolated: Path,
                                                                         monkeypatch) -> None:
    root = isolated / "workflow-root"
    folder = today_folder(root, padded=True)
    monkeypatch.setattr(watcher, "DOWNLOAD_DIR", root)
    report = workbook(folder / "订单明细报表（soo）.xlsx", "2026-10-09 15:00:00", 33)
    os.utime(report, (datetime.now().timestamp() - 60,) * 2)

    assert watcher.current_download_dir() == folder
    assert watcher.candidates() == [report]


def test_watcher_prefers_populated_folder_if_both_daily_spellings_exist(isolated: Path,
                                                                          monkeypatch) -> None:
    root = isolated / "workflow-root"
    populated = today_folder(root)
    empty_duplicate = today_folder(root, padded=True)
    report = workbook(populated / "订单明细报表（soo）.xlsx", "2026-10-09 15:00:00", 44)
    old_stamp = datetime.now().timestamp() - 60
    os.utime(report, (old_stamp - 5, old_stamp - 5))
    os.utime(empty_duplicate, (datetime.now().timestamp(),) * 2)
    monkeypatch.setattr(watcher, "DOWNLOAD_DIR", root)

    assert watcher.current_download_dir() == populated
    assert watcher.candidates() == [report]
