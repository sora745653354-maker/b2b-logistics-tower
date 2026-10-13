"""Track authorized browser exports and import only the newly acquired SOO file.

The Codex hourly task operates the TMS browser UI. This module handles local
planning, locking, validation, import, and durable success/failure evidence.
It never reads browser credentials, cookies, or hidden TMS endpoints.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import sqlite_app as tower  # noqa: E402


def start_run() -> dict:
    stamp = tower.now()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    start = f"{today - timedelta(days=1)} 00:00:00"
    end = f"{today + timedelta(days=1)} 00:00:00"
    with tower.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute("SELECT * FROM tms_sync_run WHERE status='running' "
                               "ORDER BY run_id DESC LIMIT 1").fetchone()
        if running and (datetime.fromisoformat(stamp) -
                        datetime.fromisoformat(running["started_at"])).total_seconds() < 900:
            raise ValueError(f"采集 #{running['run_id']} 尚在执行；禁止重叠导出")
        conn.execute("UPDATE tms_sync_run SET status='failed',finished_at=?,"
                     "error_code='collection_timeout',error_message='前次采集超时未完成' "
                     "WHERE status='running'", (stamp,))
        run_id = conn.execute("INSERT INTO tms_sync_run "
                              "(started_at,status,report_start,report_end) "
                              "VALUES (?,'running',?,?) RETURNING run_id",
                              (stamp, start, end)).fetchone()[0]
    return {"run_id": run_id, "started_at": stamp, "report_start": start,
            "report_end": end, "report": "订单明细报表（soo）",
            "date_field": "计划生产日期", "timezone": "Asia/Shanghai"}


def fail_run(run_id: int, code: str, message: str) -> dict:
    with tower.connect() as conn:
        changed = conn.execute("UPDATE tms_sync_run SET status='failed',finished_at=?,"
                               "error_code=?,error_message=? WHERE run_id=? AND status='running'",
                               (tower.now(), code, message[:800], run_id)).rowcount
        if not changed:
            raise ValueError("采集不存在或已经结束，不能改写结果")
    return {"run_id": run_id, "status": "failed", "error_code": code, "error_message": message[:800]}


def complete_run(run_id: int, path: Path, expected_soos: int | None = None,
                 expected_rows: int | None = None) -> dict:
    with tower.connect() as conn:
        run = conn.execute("SELECT * FROM tms_sync_run WHERE run_id=?", (run_id,)).fetchone()
    if not run or run["status"] != "running":
        raise ValueError("采集不存在或已经结束")
    try:
        if not path.is_file() or path.suffix.lower() != ".xlsx":
            raise ValueError("必须提供本次下载完成的 XLSX 文件")
        stat = path.stat()
        with tower.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO tms_export_artifact VALUES (?,?,?,?)",
                         (str(path.resolve()), stat.st_size, stat.st_mtime_ns, run_id))
        if path.stat().st_mtime < datetime.fromisoformat(run["started_at"]).timestamp() - 10:
            raise ValueError("文件早于本次采集，不能将旧文件当成新导出")
        if path.stat().st_size > tower.MAX_UPLOAD_BYTES:
            raise ValueError("报表超过 200 MB 限制")
        extracted = tower.extract(path)
        valid = extracted["quality"]["valid_soo_count"]
        total = extracted["quality"]["total_body_rows"]
        if expected_rows is not None and total != expected_rows:
            raise ValueError(f"导出表体行数 {total} 与 TMS 查询总数 {expected_rows} 不符")
        if expected_soos is not None and valid != expected_soos:
            raise ValueError(f"导出有效 SOO 数 {valid} 与 TMS 查询结果 {expected_soos} 不符")
        days = [r["production_date"] for r in extracted["records"] if r.get("production_date")]
        if any(day < run["report_start"][:10] or day >= run["report_end"][:10] for day in days):
            raise ValueError("导出文件含筛选范围以外的生产日期，请核对日期控件")
        scope = f"计划生产日期 {run['report_start']} 至 {run['report_end']}（北京时间）"
        result = tower.ingest_file(path, source_system="TMS", report_scope=scope)
        if result["status"] != "success":
            raise ValueError(f"批次 #{result['batch_id']} 未通过质量闸门：{result['status']}")
    except Exception as exc:
        fail_run(run_id, "export_or_import_failed", f"{type(exc).__name__}: {exc}")
        raise
    with tower.connect() as conn:
        changed = conn.execute("UPDATE tms_sync_run SET status='success',finished_at=?,batch_id=?,"
                               "valid_soo_count=?,unchanged=? WHERE run_id=? AND status='running'",
                               (tower.now(), result["batch_id"], valid, int(result["duplicate"]), run_id)).rowcount
        if not changed:
            raise ValueError("采集状态已改变，请检查数据库中的最终结果")
    return {"run_id": run_id, "status": "success", **result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start")
    sub.add_parser("status")
    complete = sub.add_parser("complete")
    complete.add_argument("--run-id", type=int, required=True)
    complete.add_argument("--file", type=Path, required=True)
    complete.add_argument("--expected-soos", type=int)
    complete.add_argument("--expected-rows", type=int, help="TMS 查询页面的总条数（含空 SOO 行）")
    fail = sub.add_parser("fail")
    fail.add_argument("--run-id", type=int, required=True)
    fail.add_argument("--code", required=True)
    fail.add_argument("--message", required=True, help="只记录原因，不包含凭据或联系人")
    schedule = sub.add_parser("schedule")
    schedule.add_argument("--automation-id", required=True)
    schedule.add_argument("--enabled", choices=("yes", "no"), required=True)
    args = parser.parse_args()
    tower.migrate()
    try:
        if args.command == "start":
            result = start_run()
        elif args.command == "complete":
            result = complete_run(args.run_id, args.file, args.expected_soos, args.expected_rows)
        elif args.command == "fail":
            result = fail_run(args.run_id, args.code, args.message)
        else:
            with tower.connect() as conn:
                if args.command == "schedule":
                    conn.execute("INSERT INTO sync_settings VALUES ('hourly_schedule',?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 (json.dumps({"enabled": args.enabled == "yes",
                                              "automation_id": args.automation_id}),))
                result = tower.collection_status(conn)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
