"""Google Sheets -> SQLite grade cache.

The Google spreadsheet is the source of truth. Each worksheet/tab is a discipline.
Expected layout is compatible with the existing journal screenshot:
  column A = ordinal number, column B = student name, C+ = date columns.

The service only reads Google Sheets. Administrators edit the spreadsheet itself.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("r26bot.google_grades")
BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "bot.db")))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(BASE_DIR / "secrets" / "google-service-account.json"))

VALID_VALUES = {"Н", "Б", "О", "+", "1", "2", "3", "4", "5"}


def db():
    import sqlite3
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def norm(value: str) -> str:
    return " ".join(str(value or "").strip().lower().replace("ё", "е").split())


def short_student_key(value: str) -> str:
    parts = norm(value).split()
    return " ".join(parts[:2])


def parse_date_header(value: Any) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    # gspread usually returns formatted cell text. Support common date formats.
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    # Google sometimes returns an ISO datetime string.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def parse_mark(value: Any) -> str | None:
    v = str(value or "").strip().upper().replace("Ё", "Е")
    if not v:
        return None
    if v in VALID_VALUES:
        return v
    # Keep the integration strict: unknown text in a grade cell is ignored and logged.
    return None


def _load_gspread():
    import gspread
    return gspread.service_account(filename=SERVICE_ACCOUNT_JSON)


def _match_students(c) -> dict[str, int]:
    rows = c.execute("SELECT id, full_name FROM students").fetchall()
    exact: dict[str, int] = {}
    short: dict[str, list[int]] = {}
    for r in rows:
        exact[norm(r["full_name"])] = r["id"]
        short.setdefault(short_student_key(r["full_name"]), []).append(r["id"])
    result = dict(exact)
    for key, ids in short.items():
        if len(ids) == 1:
            result[f"__short__:{key}"] = ids[0]
    return result


def _student_id(student_map: dict[str, int], sheet_name: str) -> int | None:
    n = norm(sheet_name)
    if n in student_map:
        return student_map[n]
    return student_map.get(f"__short__:{short_student_key(sheet_name)}")


def sync_google_grades() -> dict[str, Any]:
    """Synchronize all discipline tabs into the local read-only grade cache."""
    if os.getenv("GOOGLE_SHEETS_ENABLED", "1").lower() in {"0", "false", "no"}:
        return {"ok": False, "skipped": True, "reason": "disabled"}
    if not SPREADSHEET_ID:
        log.warning("GOOGLE_SHEETS_SPREADSHEET_ID is not configured")
        return {"ok": False, "skipped": True, "reason": "spreadsheet_id_missing"}
    if not Path(SERVICE_ACCOUNT_JSON).exists():
        log.warning("Google service account JSON not found: %s", SERVICE_ACCOUNT_JSON)
        return {"ok": False, "skipped": True, "reason": "credentials_missing"}

    try:
        gc = _load_gspread()
        sh = gc.open_by_key(SPREADSHEET_ID)
        c = db()
        students = _match_students(c)
        disciplines = {norm(r["name"]): r for r in c.execute("SELECT id,name,emoji FROM disciplines WHERE active=1").fetchall()}

        imported: list[tuple[int, int, str, str, int]] = []
        unmatched_students: set[str] = set()
        unknown_tabs: set[str] = set()

        for ws in sh.worksheets():
            discipline = disciplines.get(norm(ws.title))
            if not discipline:
                unknown_tabs.add(ws.title)
                continue
            values = ws.get_all_values()
            if not values or len(values) < 2:
                continue
            header = values[0]
            # Screenshot-compatible layout: student name in column B, dates from C onward.
            date_columns: list[tuple[int, str]] = []
            for idx in range(2, len(header)):
                d = parse_date_header(header[idx])
                if d:
                    date_columns.append((idx, d))
            if not date_columns:
                continue
            for row in values[1:]:
                if len(row) < 2:
                    continue
                student_name = row[1].strip()
                if not student_name:
                    continue
                sid = _student_id(students, student_name)
                if not sid:
                    unmatched_students.add(student_name)
                    continue
                for idx, grade_date in date_columns:
                    raw = row[idx] if idx < len(row) else ""
                    mark = parse_mark(raw)
                    if mark:
                        imported.append((sid, discipline["id"], grade_date, mark, idx))

        # The cache is rebuilt atomically. This guarantees that deleted/cleared cells in
        # Google Sheets disappear from the student Mini App on the next sync.
        c.execute("DELETE FROM grade_history")
        c.execute("DELETE FROM event_feed WHERE event_type LIKE 'grade_%'")
        c.execute("DELETE FROM grade_marks")
        c.execute("DELETE FROM grades")
        c.execute("DELETE FROM grade_dates")

        ts = datetime.now(TZ).isoformat()
        column_cache: dict[tuple[int, int], int] = {}
        for sid, did, grade_date, mark, source_col_idx in imported:
            key = (did, source_col_idx)
            if key not in column_cache:
                cur = c.execute("INSERT INTO grade_dates(discipline_id,grade_date,created_by,created_at) VALUES(?,?,?,?)", (did, grade_date, "google_sheets", ts))
                column_cache[key] = cur.lastrowid
            col_id = column_cache[key]
            if mark == "+":
                c.execute("INSERT INTO grade_marks(student_id,discipline_id,grade_date,grade_column_id,value,created_by,created_at) VALUES(?,?,?,?,?,?,?)", (sid,did,grade_date,col_id,"+","google_sheets",ts))
            elif mark in {"Н", "Б", "О"}:
                c.execute("INSERT INTO grades(student_id,discipline_id,grade_date,grade_value,attendance_type,comment,created_by,created_at,updated_at,grade_column_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (sid,did,grade_date,None,mark,"","google_sheets",ts,ts,col_id))
            else:
                c.execute("INSERT INTO grades(student_id,discipline_id,grade_date,grade_value,attendance_type,comment,created_by,created_at,updated_at,grade_column_id) VALUES(?,?,?,?,?,?,?,?,?,?)", (sid,did,grade_date,int(mark),None,"","google_sheets",ts,ts,col_id))

        c.execute("INSERT INTO google_sync_state(id,last_sync_at,last_status,last_error,updated_at) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_sync_at=excluded.last_sync_at,last_status=excluded.last_status,last_error=excluded.last_error,updated_at=excluded.updated_at", (ts,"ok",None,ts))
        c.commit()
        c.close()
        if unknown_tabs:
            log.warning("Google Sheets tabs without matching active discipline: %s", ", ".join(sorted(unknown_tabs)))
        if unmatched_students:
            log.warning("Google Sheets student names not matched: %s", ", ".join(sorted(unmatched_students)))
        log.info("Google grades synced: %s entries from %s worksheets", len(imported), len(sh.worksheets()))
        return {"ok": True, "entries": len(imported), "unknown_tabs": sorted(unknown_tabs), "unmatched_students": sorted(unmatched_students)}
    except Exception as exc:
        log.exception("Google grades sync failed")
        try:
            c.rollback(); c.close()
        except Exception:
            pass
        c = db()
        ts = datetime.now(TZ).isoformat()
        c.execute("INSERT INTO google_sync_state(id,last_sync_at,last_status,last_error,updated_at) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_status=excluded.last_status,last_error=excluded.last_error,updated_at=excluded.updated_at", (None,"error",str(exc)[:1000],ts))
        c.commit(); c.close()
        return {"ok": False, "error": str(exc)}
