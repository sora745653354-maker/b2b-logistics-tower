"""Automatically import SOO files downloaded by the user's authorized TMS browser.

This does not log in to TMS or trigger exports. It watches a local download folder
and imports each new version through the same validation and snapshot pipeline.
"""

from __future__ import annotations

import os
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.sqlite_app import connect, ingest_file, migrate, now  # noqa: E402

DEFAULT_DIR = Path("/Users/harumi/backup/b2b订单明细-files")
DOWNLOAD_ROOT = Path(os.environ.get("TMS_DOWNLOAD_DIR", str(DEFAULT_DIR)))
# Keep the historical name available to local tests and existing operator tooling.
DOWNLOAD_DIR = DOWNLOAD_ROOT
POLL_SECONDS = 30


def current_download_dir(root: Path | None = None) -> Path:
    """Resolve today's workflow folder, accepting both padded and natural day names."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Shanghai"))
    folder_names = dict.fromkeys((f"{today.month}月{today.day}日工作流",
                                  f"{today.month}月{today.day:02d}日工作流"))
    base = root or DOWNLOAD_DIR
    folders = [base / name for name in folder_names if (base / name).is_dir()]
    if not folders:
        return base / next(iter(folder_names))
    # If both spellings exist, prefer a folder containing a report over an empty duplicate.
    with_reports = [(folder, report_files(folder)) for folder in folders]
    populated = [(folder, files) for folder, files in with_reports if files]
    if populated:
        return max(populated, key=lambda item: max(path.stat().st_mtime_ns for path in item[1]))[0]
    return max(folders, key=lambda folder: folder.stat().st_mtime_ns)


def report_files(folder: Path) -> list[Path]:
    """List only spreadsheet exports for the SOO order-detail report."""
    if not folder.is_dir():
        return []
    return [path for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in {".xlsx", ".csv"}
            and "订单明细报表" in path.name and "soo" in path.name.lower()
            and not path.name.startswith("~$")]


def candidates() -> list[Path]:
    download_dir = current_download_dir()
    files = report_files(download_dir)
    return [max(files, key=lambda path: (path.stat().st_mtime_ns, path.name))] if files else []


def log_job(status: str, batch_id: int | None, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO collector_job(job_kind,status,started_at,finished_at,batch_id,error_message) "
                     "VALUES (?,?,?,?,?,?)", ("download-folder", status, now(), now(), batch_id, error))


def scan_once(seen: dict[Path, tuple[int, int]] | None = None) -> list[dict]:
    seen = seen if seen is not None else {}
    results = []
    for path in candidates():
        signature = None
        try:
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if signature == seen.get(path) or time.time() - stat.st_mtime < 3:
                continue
            result = ingest_file(path, source_system="TMS",
                                 report_scope=f"本机当日工作流目录 {path.parent.name} 最新 SOO 报表")
            seen[path] = signature
            results.append(result)
            if not result["duplicate"]:
                log_job(result["status"], result["batch_id"],
                        None if result["status"] == "success" else "最新文件未通过数据质量闸门")
                print(f"已处理 TMS 报表：批次 #{result['batch_id']}，状态 {result['status']}，"
                      f"有效 SOO {result['valid_soo_count']}", flush=True)
        except Exception as exc:
            if signature is not None:
                seen[path] = signature
            log_job("failed", None, f"{path.name}: {type(exc).__name__}: {exc}")
            print(f"TMS 报表处理失败：{path.name}: {type(exc).__name__}: {exc}", flush=True)
    return results


def run() -> None:
    migrate()
    seen: dict[Path, tuple[int, int]] = {}
    print(f"SOO 工作流目录监听根目录：{DOWNLOAD_DIR}（按北京时间只导入当天最新报表）", flush=True)
    while True:
        scan_once(seen)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        migrate()
        print(json.dumps(scan_once(), ensure_ascii=False))
    else:
        run()
