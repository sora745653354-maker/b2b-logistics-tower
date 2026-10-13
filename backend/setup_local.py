"""Initialize the workspace-local PostgreSQL instance and control-tower database."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT.parent / "work"
BIN = WORK / "pgsql" / "bin"
RUNTIME = ROOT / "runtime"
PGDATA = RUNTIME / "pgdata"
SOCKET = WORK / "pgsocket"
ADMIN_PASSWORD = RUNTIME / "admin-password"
CONFIG = RUNTIME / "db-connection.json"
PORT = 55432


def private_write(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(value)
    os.chmod(path, 0o600)


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def main() -> None:
    for name in ("initdb", "pg_ctl"):
        if not (BIN / name).is_file():
            raise SystemExit(f"缺少 PostgreSQL 二进制文件：{BIN / name}")
    RUNTIME.mkdir(exist_ok=True)
    SOCKET.mkdir(exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    os.chmod(SOCKET, 0o700)

    if not PGDATA.exists():
        if not ADMIN_PASSWORD.exists():
            private_write(ADMIN_PASSWORD, secrets.token_urlsafe(36) + "\n")
        run(str(BIN / "initdb"), "-D", str(PGDATA), "--username=tower_admin",
            f"--pwfile={ADMIN_PASSWORD}", "--auth-local=scram-sha-256", "--auth-host=reject",
            "-c", "dynamic_shared_memory_type=mmap", "-c", "shared_memory_type=mmap",
            "--no-instructions")
    if not ADMIN_PASSWORD.exists():
        raise SystemExit("管理员密码文件丢失；请先恢复，不要重新初始化数据目录")

    result = subprocess.run([str(BIN / "pg_ctl"), "-D", str(PGDATA), "status"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        options = (f"-c listen_addresses='' -c unix_socket_directories='{SOCKET}' -c port={PORT} "
                   "-c dynamic_shared_memory_type=mmap -c shared_memory_type=mmap")
        run(str(BIN / "pg_ctl"), "-D", str(PGDATA), "-l", str(RUNTIME / "postgres.log"),
            "-o", options, "-w", "start")

    admin_conn = dict(host=str(SOCKET), port=PORT, dbname="postgres", user="tower_admin",
                      password=ADMIN_PASSWORD.read_text(encoding="utf-8").strip())
    with psycopg.connect(**admin_conn, autocommit=True) as conn:
        existing_role = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'tower_app'").fetchone()
        if CONFIG.exists():
            config = json.loads(CONFIG.read_text(encoding="utf-8"))
            app_password = config["password"]
        else:
            app_password = secrets.token_urlsafe(36)
        if not existing_role:
            conn.execute(sql.SQL("CREATE ROLE tower_app LOGIN PASSWORD {}").format(sql.Literal(app_password)))
        elif not CONFIG.exists():
            raise SystemExit("tower_app 已存在，但连接配置丢失；请人工恢复配置")
        database_exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = 'logistics_tower'").fetchone()
        if not database_exists:
            conn.execute("CREATE DATABASE logistics_tower OWNER tower_app")
    if not CONFIG.exists():
        config = {"host": str(SOCKET), "port": PORT, "dbname": "logistics_tower",
                  "user": "tower_app", "password": app_password}
        private_write(CONFIG, json.dumps(config, ensure_ascii=False))
    os.chmod(CONFIG, 0o600)

    sys.path.insert(0, str(ROOT))
    from backend.app import migrate  # imported after config exists
    migrate()
    print("本机 PostgreSQL 已启动，logistics_tower 数据库及表结构已就绪。")


if __name__ == "__main__":
    main()
