"""Publish the latest successful local TMS batch to the private logistics Site.

The TMS export itself is a separate prerequisite. This script never reads browser
cookies or raw report archives and never uploads contact fields.
"""

from __future__ import annotations

import argparse
import gzip
import getpass
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.sqlite_app import public_snapshot  # noqa: E402

KEYCHAIN_SERVICE = "b2b-logistics-tower-cloudflare-token"
KEYCHAIN_ACCOUNT = "tower-uploader"


def credential(from_stdin: bool) -> str:
    if from_stdin:
        value = (getpass.getpass("Site token: ") if sys.stdin.isatty()
                 else sys.stdin.readline().strip())
    else:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise RuntimeError("Site access credential is unavailable in macOS Keychain")
        value = result.stdout.strip()
    if not value:
        raise RuntimeError("Site access credential is empty")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--token-stdin", action="store_true",
                        help="Read token from stdin for one-off setup; never echo it")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--export-path", type=Path,
                        help="Write a local sanitized snapshot for the first private release")
    args = parser.parse_args()

    site_url = args.site_url.rstrip("/")
    if not site_url.startswith("https://"):
        raise RuntimeError("Site URL must use HTTPS")
    snapshot = public_snapshot()
    batch = snapshot.get("source", {}).get("published_batch_id")
    if not batch or not snapshot.get("records"):
        raise RuntimeError("No successful local TMS batch is available")
    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 32 * 1024 * 1024:
        raise RuntimeError("Sanitized snapshot exceeds the 32 MiB uncompressed Site limit")
    compressed_body = gzip.compress(body, mtime=0)
    if len(compressed_body) > 2 * 1024 * 1024:
        raise RuntimeError("Compressed snapshot exceeds the 2 MiB Site upload limit")
    if args.dry_run:
        print(json.dumps({"batch_id": batch, "records": len(snapshot["records"]),
                          "bytes": len(body), "compressed_bytes": len(compressed_body)}, ensure_ascii=False))
        return 0
    if args.export_path:
        args.export_path.parent.mkdir(parents=True, exist_ok=True)
        args.export_path.write_bytes(body + b"\n")
        print(json.dumps({"batch_id": batch, "records": len(snapshot["records"]),
                          "bytes": len(body), "compressed_bytes": len(compressed_body),
                          "exported": str(args.export_path)},
                         ensure_ascii=False))
        return 0

    token = credential(args.token_stdin)
    request = Request(
        site_url + "/api/snapshot", data=compressed_body, method="POST",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip",
                 "Authorization": "Bearer " + token},
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        message = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"Site update returned HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise RuntimeError(f"Site update network error: {exc.reason}") from exc
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
