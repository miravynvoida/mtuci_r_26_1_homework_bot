"""v10 process supervisor.

Initializes the shared SQLite database once, then runs bot and Mini App as
separate child processes. A crash in one child does not stop the other.
"""
from __future__ import annotations
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent

def initialize_database():
    # Fail early with a useful message instead of letting two child processes
    # concurrently migrate a missing/corrupt SQLite database.
    from core.config import DB_PATH
    import sqlite3
    if not DB_PATH.exists():
        raise RuntimeError(
            f"SQLite database not found: {DB_PATH}. "
            "For an existing installation, copy/mount the project's bot.db to this path."
        )
    check = sqlite3.connect(DB_PATH, timeout=30)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite database integrity check failed: {integrity}")
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = [name for name in ("students", "disciplines", "homework", "textbooks") if name not in tables]
        if missing:
            raise RuntimeError(
                "The configured SQLite database is not the project database; "
                f"missing tables: {', '.join(missing)}. DB_PATH={DB_PATH}"
            )
    finally:
        check.close()

    # Importing bot_app does not start the bot; its migration is an explicit call.
    from bot_app import migrate
    migrate()
    # Web migration is deliberately run after the base bot schema exists.
    from web_app import migrate_web_tables
    migrate_web_tables()

CHILDREN = {
    "bot": [sys.executable, str(BASE / "bot_app.py")],
    "web": [sys.executable, str(BASE / "web_runner.py")],
}

def main():
    try:
        initialize_database()
    except Exception as exc:
        print(f"[startup] database initialization failed: {exc}", file=sys.stderr, flush=True)
        print("[startup] Проверьте DB_PATH и убедитесь, что это существующая база проекта с таблицей students.", file=sys.stderr, flush=True)
        raise

    processes = {}
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True
        for p in processes.values():
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    child_env = os.environ.copy()
    child_env["SKIP_DB_MIGRATION"] = "1"
    child_env["SKIP_WEB_MIGRATION"] = "1"

    while not stopped:
        for name, cmd in CHILDREN.items():
            p = processes.get(name)
            if p is None or p.poll() is not None:
                if p is not None:
                    print(f"[{name}] exited with code {p.returncode}; restarting", flush=True)
                processes[name] = subprocess.Popen(cmd, cwd=BASE, env=child_env)
                print(f"[{name}] started pid={processes[name].pid}", flush=True)
        time.sleep(2)

    deadline = time.time() + 10
    while processes and time.time() < deadline:
        if all(p.poll() is not None for p in processes.values()):
            break
        time.sleep(0.2)
    for p in processes.values():
        if p.poll() is None:
            p.kill()

if __name__ == '__main__':
    main()
