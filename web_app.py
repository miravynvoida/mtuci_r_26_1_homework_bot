import hashlib
import hmac
import json
import os
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.google_grades import sync_google_grades

from core.config import BASE_DIR, DB_PATH, TOKEN, ADMIN_IDS, MINI_APP_URL, WEB_HOST, WEB_PORT, INIT_DATA_MAX_AGE, TIMEZONE
from core.database import db

app = FastAPI(title="R26 Electronic Diary", version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def migrate_web_tables():
    c = db()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            discipline_id INTEGER NOT NULL,
            grade_date TEXT NOT NULL,
            grade_value INTEGER NULL,
            attendance_type TEXT NULL,
            comment TEXT DEFAULT '',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE,
            CHECK (
                (grade_value IS NOT NULL AND grade_value BETWEEN 1 AND 5 AND attendance_type IS NULL)
                OR
                (grade_value IS NULL AND attendance_type IN ('Н','Б','О'))
            )
        );
        CREATE INDEX IF NOT EXISTS idx_grades_student_discipline_date
            ON grades(student_id, discipline_id, grade_date);
        CREATE INDEX IF NOT EXISTS idx_grades_discipline_date
            ON grades(discipline_id, grade_date);

        CREATE TABLE IF NOT EXISTS grade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grade_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            discipline_id INTEGER NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_by TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            reason TEXT DEFAULT '',
            comment TEXT DEFAULT '',
            FOREIGN KEY(grade_id) REFERENCES grades(id) ON DELETE CASCADE,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS event_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            discipline_id INTEGER NULL,
            grade_id INTEGER NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE SET NULL,
            FOREIGN KEY(grade_id) REFERENCES grades(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_event_feed_student_created
            ON event_feed(student_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            role TEXT NOT NULL DEFAULT 'admin',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS grade_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discipline_id INTEGER NOT NULL,
            grade_date TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS grade_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            discipline_id INTEGER NOT NULL,
            grade_date TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '+',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(value='+'),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_grade_marks_student_discipline_date
            ON grade_marks(student_id, discipline_id, grade_date);
        CREATE TABLE IF NOT EXISTS app_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS google_sync_state (id INTEGER PRIMARY KEY CHECK(id=1), last_sync_at TEXT, last_status TEXT, last_error TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS homework_upload_requests (telegram_id INTEGER PRIMARY KEY, homework_id INTEGER NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS homework_answer (homework_id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'text', text TEXT DEFAULT '', file_id TEXT DEFAULT '', caption TEXT DEFAULT '', FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS homework_media (id INTEGER PRIMARY KEY AUTOINCREMENT, homework_id INTEGER NOT NULL, kind TEXT NOT NULL, file_id TEXT NOT NULL, caption TEXT DEFAULT '', FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS notification_settings (telegram_id INTEGER PRIMARY KEY, tomorrow_schedule INTEGER NOT NULL DEFAULT 1, next_lesson INTEGER NOT NULL DEFAULT 1, updates INTEGER NOT NULL DEFAULT 1, other INTEGER NOT NULL DEFAULT 1, deadline_reminders INTEGER NOT NULL DEFAULT 1, next_lesson_minutes INTEGER NOT NULL DEFAULT 30, new_homework INTEGER NOT NULL DEFAULT 1, setup_done INTEGER NOT NULL DEFAULT 0, setup_version INTEGER NOT NULL DEFAULT 0, silent_notifications INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(telegram_id) REFERENCES students(telegram_id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS admin_file_requests (
            telegram_id INTEGER PRIMARY KEY,
            target_type TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    # Journal columns are independent occurrences, so the same calendar date
    # may appear more than once. Migrate older schemas without deleting data.
    def cols(table):
        return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    hcols=cols('homework')
    if 'client_token' not in hcols:
        c.execute('ALTER TABLE homework ADD COLUMN client_token TEXT')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_homework_client_token ON homework(client_token) WHERE client_token IS NOT NULL')
    ncols=cols('notification_settings')
    setup_version_missing = 'setup_version' not in ncols
    for col,ddl in [('updates','INTEGER NOT NULL DEFAULT 1'),('other','INTEGER NOT NULL DEFAULT 1'),('deadline_reminders','INTEGER NOT NULL DEFAULT 1'),('next_lesson_minutes','INTEGER NOT NULL DEFAULT 30'),('new_homework','INTEGER NOT NULL DEFAULT 1'),('setup_done','INTEGER NOT NULL DEFAULT 0'),('setup_version','INTEGER NOT NULL DEFAULT 0'),('silent_notifications','INTEGER NOT NULL DEFAULT 0')]:
        if col not in ncols:
            c.execute(f'ALTER TABLE notification_settings ADD COLUMN {col} {ddl}')
    if setup_version_missing:
        c.execute('UPDATE notification_settings SET setup_version=0')
    c.execute('INSERT OR IGNORE INTO notification_settings(telegram_id) SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL')

    gd_sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='grade_dates'").fetchone()
    if gd_sql and 'UNIQUE(discipline_id,grade_date)' in (gd_sql[0] or '').replace(' ', ''):
        c.execute("ALTER TABLE grade_dates RENAME TO grade_dates_old")
        c.execute("""CREATE TABLE grade_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discipline_id INTEGER NOT NULL,
            grade_date TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
        )""")
        c.execute("INSERT INTO grade_dates(id,discipline_id,grade_date,created_by,created_at) SELECT id,discipline_id,grade_date,created_by,created_at FROM grade_dates_old")
        c.execute("DROP TABLE grade_dates_old")

    if 'grade_column_id' not in cols('grades'):
        c.execute("ALTER TABLE grades ADD COLUMN grade_column_id INTEGER")

    gm_sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='grade_marks'").fetchone()
    if gm_sql and 'grade_column_id' not in cols('grade_marks'):
        c.execute("ALTER TABLE grade_marks RENAME TO grade_marks_old")
        c.execute("""CREATE TABLE grade_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            discipline_id INTEGER NOT NULL,
            grade_date TEXT NOT NULL,
            grade_column_id INTEGER,
            value TEXT NOT NULL DEFAULT '+',
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(value='+'),
            FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
        )""")
        c.execute("INSERT INTO grade_marks(id,student_id,discipline_id,grade_date,value,created_by,created_at) SELECT id,student_id,discipline_id,grade_date,value,created_by,created_at FROM grade_marks_old")
        c.execute("DROP TABLE grade_marks_old")

    existing_dates = c.execute("SELECT DISTINCT discipline_id,grade_date FROM grades UNION SELECT DISTINCT discipline_id,grade_date FROM grade_marks").fetchall()
    for row in existing_dates:
        col = c.execute("SELECT id FROM grade_dates WHERE discipline_id=? AND grade_date=? ORDER BY id LIMIT 1", (row[0], row[1])).fetchone()
        if not col:
            c.execute("INSERT INTO grade_dates(discipline_id,grade_date,created_by,created_at) VALUES(?,?,?,?)", (row[0], row[1], 'migration', now_iso()))
            col_id = c.lastrowid
        else:
            col_id = col[0]
        c.execute("UPDATE grades SET grade_column_id=? WHERE discipline_id=? AND grade_date=? AND grade_column_id IS NULL", (col_id, row[0], row[1]))
        c.execute("UPDATE grade_marks SET grade_column_id=? WHERE discipline_id=? AND grade_date=? AND grade_column_id IS NULL", (col_id, row[0], row[1]))
    c.execute("CREATE INDEX IF NOT EXISTS idx_grade_dates_discipline_date ON grade_dates(discipline_id,grade_date,id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_grades_column ON grades(grade_column_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_grade_marks_column ON grade_marks(grade_column_id)")

    ts = now_iso()
    for uid in ADMIN_IDS:
        c.execute(
            "INSERT OR IGNORE INTO admin_users(telegram_id,role,created_at) VALUES(?,?,?)",
            (uid, "admin", ts),
        )
    # v10: grades are now a read-only cache of Google Sheets. Purge the old local journal once.
    if not c.execute("SELECT 1 FROM app_migrations WHERE id='v10_google_grades_purge'").fetchone():
        c.execute("DELETE FROM grade_history")
        c.execute("DELETE FROM event_feed WHERE event_type LIKE 'grade_%'")
        c.execute("DELETE FROM grade_marks")
        c.execute("DELETE FROM grades")
        c.execute("DELETE FROM grade_dates")
        c.execute("INSERT INTO app_migrations(id,applied_at) VALUES(?,?)", ('v10_google_grades_purge', now_iso()))
    c.commit()
    c.close()


if os.getenv('SKIP_WEB_MIGRATION', '0') != '1':
    migrate_web_tables()


class HomeworkCreateIn(BaseModel):
    discipline_id: int
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=10000)
    explanation: str = Field(default="", max_length=5000)
    due_date: str
    wants_file: bool = False
    client_token: str | None = Field(default=None, max_length=80)


class HomeworkEditIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=10000)
    explanation: str = Field(default="", max_length=5000)
    due_date: str
    answer_text: str = Field(default="", max_length=10000)

class LessonIn(BaseModel):
    day: str
    lesson_no: int = Field(ge=1, le=20)
    start: str
    end: str
    discipline_id: int
    lesson_type: str = Field(default="Лекция", max_length=100)
    room: str = Field(default="", max_length=50)


class LessonPatch(BaseModel):
    lesson_no: int | None = Field(default=None, ge=1, le=20)
    start: str | None = None
    end: str | None = None
    discipline_id: int | None = None
    lesson_type: str | None = None
    room: str | None = None


def parse_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, "Неверная дата. Используйте YYYY-MM-DD.")


def parse_hw_date(value: str) -> str:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date().strftime("%d.%m.%Y")
        except ValueError:
            pass
    raise HTTPException(400, "Неверная дата сдачи.")


def grade_label(row):
    return row["attendance_type"] if row["attendance_type"] else str(row["grade_value"])


def telegram_auth(init_data: str | None):
    if not init_data or not TOKEN:
        raise HTTPException(401, "Telegram Mini App не авторизован.")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Отсутствует подпись Telegram.")
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Недействительная подпись Telegram.")
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(401, "Недействительная дата авторизации.")
    if datetime.utcnow().timestamp() - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(401, "Сессия Telegram устарела. Откройте Mini App заново.")
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(401, "Не удалось прочитать пользователя Telegram.")
    if not user.get("id"):
        raise HTTPException(401, "Не найден Telegram ID.")
    return int(user["id"]), user


def current_user(init_data: str | None, admin=False):
    uid, tg_user = telegram_auth(init_data)
    c = db()
    student = c.execute("SELECT * FROM students WHERE telegram_id=?", (uid,)).fetchone()
    is_admin = uid in ADMIN_IDS or bool(c.execute("SELECT 1 FROM admin_users WHERE telegram_id=?", (uid,)).fetchone())
    c.close()
    if admin and not is_admin:
        raise HTTPException(403, "Доступ только для администратора.")
    if not admin and not student:
        raise HTTPException(403, "Ваш Telegram аккаунт ещё не привязан к студенту.")
    return uid, student, is_admin, tg_user


def student_stats(c, student_id):
    row = c.execute(
        """
        SELECT
          COUNT(CASE WHEN grade_value IS NOT NULL THEN 1 END) AS grade_count,
          COALESCE(AVG(grade_value), 0) AS average,
          COUNT(CASE WHEN attendance_type='Н' THEN 1 END) AS n_count,
          COUNT(CASE WHEN attendance_type='Б' THEN 1 END) AS b_count,
          COUNT(CASE WHEN attendance_type='О' THEN 1 END) AS o_count
        FROM grades WHERE student_id=?
        """,
        (student_id,),
    ).fetchone()
    return {
        "average": round(float(row["average"]), 2) if row["grade_count"] else None,
        "grade_count": row["grade_count"],
        "n_count": row["n_count"],
        "b_count": row["b_count"],
        "o_count": row["o_count"],
    }


def event_row(c, student_id, event_type, discipline_id, grade_id, title, description):
    c.execute(
        """INSERT INTO event_feed(student_id,event_type,discipline_id,grade_id,title,description,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (student_id, event_type, discipline_id, grade_id, title, description, now_iso()),
    )


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me(x_telegram_init_data: str | None = Header(default=None)):
    uid, tg_user = telegram_auth(x_telegram_init_data)
    c = db()
    student = c.execute("SELECT * FROM students WHERE telegram_id=?", (uid,)).fetchone()
    is_admin = uid in ADMIN_IDS or bool(c.execute("SELECT 1 FROM admin_users WHERE telegram_id=?", (uid,)).fetchone())
    stats = student_stats(c, student["id"]) if student else {"average": None, "grade_count": 0, "n_count": 0, "b_count": 0, "o_count": 0}
    ns = c.execute("SELECT * FROM notification_settings WHERE telegram_id=?", (uid,)).fetchone() if student else None
    if student and not ns:
        c.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)", (uid,))
        c.commit()
        ns = c.execute("SELECT * FROM notification_settings WHERE telegram_id=?", (uid,)).fetchone()
    c.close()
    if not student and not is_admin:
        raise HTTPException(403, "Ваш Telegram аккаунт ещё не привязан к студенту.")
    return {
        "telegram_id": uid,
        "full_name": student["full_name"] if student else (tg_user.get("first_name") or "Администратор"),
        "student_id": student["id"] if student else None,
        "is_admin": is_admin,
        "stats": stats,
        "needs_setup": bool(ns and int(ns["setup_version"] or 0) < 1) if student else False,
        "telegram": {"first_name": tg_user.get("first_name", ""), "username": tg_user.get("username", "")},
    }

def current_and_next_lesson_rows(c):
    now = datetime.now(TIMEZONE)
    today = now.date().isoformat()
    hm = now.strftime('%H:%M')
    current = c.execute(
        """SELECT l.*,d.name discipline,d.emoji,sd.day FROM schedule_lessons l
           JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id
           WHERE sd.day=? AND l.start<=? AND l.end>?
           ORDER BY l.start,l.lesson_no LIMIT 1""",
        (today, hm, hm),
    ).fetchone()
    if current:
        next_row = c.execute(
            """SELECT l.*,d.name discipline,d.emoji,sd.day FROM schedule_lessons l
               JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id
               WHERE (sd.day=? AND l.start>=?) OR sd.day>?
               ORDER BY sd.day,l.start,l.lesson_no LIMIT 1""",
            (today, current['end'], today),
        ).fetchone()
    else:
        next_row = c.execute(
            """SELECT l.*,d.name discipline,d.emoji,sd.day FROM schedule_lessons l
               JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id
               WHERE (sd.day=? AND l.start>?) OR sd.day>?
               ORDER BY sd.day,l.start,l.lesson_no LIMIT 1""",
            (today, hm, today),
        ).fetchone()
    return (dict(current) if current else None, dict(next_row) if next_row else None)


def next_lesson_row(c):
    return current_and_next_lesson_rows(c)[1]


@app.get("/api/next-lesson")
def next_lesson(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data)
    c = db(); row = next_lesson_row(c); c.close()
    return {"lesson": row}


@app.get("/api/settings")
def get_settings(x_telegram_init_data: str | None = Header(default=None)):
    uid, student, _, _ = current_user(x_telegram_init_data)
    c=db(); c.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)", (uid,)); c.commit()
    r=c.execute("SELECT tomorrow_schedule,next_lesson,updates,other,deadline_reminders,next_lesson_minutes,new_homework,setup_done,setup_version,silent_notifications FROM notification_settings WHERE telegram_id=?", (uid,)).fetchone(); c.close()
    return dict(r)

class SettingsIn(BaseModel):
    tomorrow_schedule: bool = True
    next_lesson: bool = True
    updates: bool = True
    other: bool = True
    deadline_reminders: bool = True
    next_lesson_minutes: int = Field(default=30, ge=5, le=90)
    new_homework: bool = True
    setup_done: bool = False
    setup_version: int = 0
    silent_notifications: bool = False

@app.patch("/api/settings")
def update_settings(payload: SettingsIn, x_telegram_init_data: str | None = Header(default=None)):
    uid, student, _, _ = current_user(x_telegram_init_data)
    allowed={5,10,15,30,45,60,90}
    if payload.next_lesson_minutes not in allowed:
        raise HTTPException(400, "Недопустимое время напоминания.")
    c=db(); c.execute("""INSERT INTO notification_settings(telegram_id,tomorrow_schedule,next_lesson,updates,other,deadline_reminders,next_lesson_minutes,new_homework,setup_done,setup_version,silent_notifications)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET tomorrow_schedule=excluded.tomorrow_schedule,next_lesson=excluded.next_lesson,updates=excluded.updates,other=excluded.other,deadline_reminders=excluded.deadline_reminders,next_lesson_minutes=excluded.next_lesson_minutes,new_homework=excluded.new_homework,setup_done=excluded.setup_done,setup_version=excluded.setup_version,silent_notifications=excluded.silent_notifications""",
        (uid,int(payload.tomorrow_schedule),int(payload.next_lesson),int(payload.updates),int(payload.other),int(payload.deadline_reminders),payload.next_lesson_minutes,int(payload.new_homework),int(payload.setup_done),int(payload.setup_version),int(payload.silent_notifications)))
    c.commit(); c.close(); return {"ok":True}

@app.get("/api/home")
def home(x_telegram_init_data: str | None = Header(default=None)):
    uid, student, _, _ = current_user(x_telegram_init_data)
    c = db()
    today = datetime.now(TIMEZONE).date().isoformat()
    lessons = c.execute(
        """SELECT l.*, d.name discipline, d.emoji FROM schedule_lessons l
           JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id
           WHERE sd.day=? ORDER BY l.start""", (today,)
    ).fetchall()
    hw = c.execute(
        """SELECT h.id,h.discipline_id,h.text,h.explanation,h.published_date,h.due_date,
                  d.name discipline,d.emoji,COALESCE(e.title,substr(h.text,1,80)) title
           FROM homework h JOIN disciplines d ON d.id=h.discipline_id
           LEFT JOIN homework_extra e ON e.homework_id=h.id
           WHERE h.hidden=0 AND h.archived=0 ORDER BY d.name, h.due_date"""
    ).fetchall()
    events = c.execute(
        """SELECT e.*, d.name discipline, d.emoji FROM event_feed e
           LEFT JOIN disciplines d ON d.id=e.discipline_id
           WHERE e.student_id=? ORDER BY e.created_at DESC LIMIT 8""", (student["id"],)
    ).fetchall()
    stats = student_stats(c, student["id"])
    current_lesson, upcoming = current_and_next_lesson_rows(c)
    c.close()
    return {
        "today": today,
        "schedule": [dict(x) for x in lessons],
        "homework_count": len(hw),
        "homework": [dict(x) for x in hw[:8]],
        "events": [dict(x) for x in events],
        "stats": stats,
        "current_lesson": current_lesson,
        "next_lesson": upcoming,
    }


@app.get("/api/schedule")
def schedule(day: str = Query(...), x_telegram_init_data: str | None = Header(default=None)):
    _, student, _, _ = current_user(x_telegram_init_data)
    _ = student
    day = parse_date(day)
    c = db()
    rows = c.execute(
        """SELECT l.*,d.name discipline,d.emoji FROM schedule_lessons l
           JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id
           WHERE sd.day=? ORDER BY l.lesson_no,l.start""", (day,)
    ).fetchall()
    c.close()
    return {"date": day, "lessons": [dict(x) for x in rows]}


@app.get("/api/homework")
def homework(x_telegram_init_data: str | None = Header(default=None)):
    _, student, _, _ = current_user(x_telegram_init_data)
    c = db()
    rows = c.execute(
        """SELECT h.*,d.name discipline,d.emoji,COALESCE(e.title,substr(h.text,1,80)) title
           FROM homework h JOIN disciplines d ON d.id=h.discipline_id
           LEFT JOIN homework_extra e ON e.homework_id=h.id
           WHERE h.hidden=0 AND h.archived=0 ORDER BY d.name, h.due_date, h.id DESC"""
    ).fetchall()
    materials = c.execute("SELECT * FROM additional_materials ORDER BY id DESC").fetchall()
    hw_media = c.execute("SELECT * FROM homework_media ORDER BY id").fetchall()
    c.close()
    material_map = {}
    for m in materials:
        material_map.setdefault(m["discipline_id"], []).append(dict(m))
    media_map = {}
    for m in hw_media:
        media_map.setdefault(m["homework_id"], []).append(dict(m))
    return {
        "student_id": student["id"],
        "count": len(rows),
        "items": [
            {**dict(x), "media": media_map.get(x["id"], []), "discipline_materials": material_map.get(x["discipline_id"], [])}
            for x in rows
        ],
    }


@app.post("/api/homework/{homework_id}/media/{media_id}")
async def send_homework_media(homework_id: int, media_id: int, x_telegram_init_data: str | None = Header(default=None)):
    uid, student, _, _ = current_user(x_telegram_init_data)
    c = db()
    item = c.execute(
        """SELECT m.* FROM homework_media m JOIN homework h ON h.id=m.homework_id
           WHERE m.id=? AND h.id=? AND h.hidden=0 AND h.archived=0""", (media_id, homework_id)
    ).fetchone()
    c.close()
    if not item:
        raise HTTPException(404, "Файл задания не найден.")
    method = {"photo": "sendPhoto", "video": "sendVideo"}.get(item["kind"], "sendDocument")
    field = {"sendPhoto": "photo", "sendVideo": "video"}.get(method, "document")
    payload = {"chat_id": uid, field: item["file_id"]}
    if item["caption"]:
        payload["caption"] = item["caption"]
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=payload)
    if r.status_code >= 400 or not r.json().get("ok"):
        raise HTTPException(502, "Telegram не смог отправить файл задания.")
    return {"ok": True, "message_id": r.json().get("result", {}).get("message_id")}


@app.post("/api/homework/{homework_id}/material/{material_id}")
async def send_material(homework_id: int, material_id: int, x_telegram_init_data: str | None = Header(default=None)):
    uid, student, _, _ = current_user(x_telegram_init_data)
    c = db()
    item = c.execute(
        """SELECT m.* FROM additional_materials m
           JOIN homework h ON h.discipline_id=m.discipline_id
           WHERE m.id=? AND h.id=? AND h.hidden=0 AND h.archived=0""", (material_id, homework_id)
    ).fetchone()
    if not item:
        c.close()
        raise HTTPException(404, "Материал не найден.")
    c.close()
    method = {"photo": "sendPhoto", "video": "sendVideo"}.get(item["kind"], "sendDocument")
    field = {"sendPhoto": "photo", "sendVideo": "video"}.get(method, "document")
    payload = {"chat_id": uid, field: item["file_id"]}
    if item["caption"]:
        payload["caption"] = item["caption"]
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=payload)
    if r.status_code >= 400 or not r.json().get("ok"):
        raise HTTPException(502, "Telegram не смог отправить материал.")
    return {"ok": True, "message_id": r.json().get("result", {}).get("message_id")}


@app.get("/api/grades")
def grades(x_telegram_init_data: str | None = Header(default=None)):
    _, student, _, _ = current_user(x_telegram_init_data)
    c = db()
    rows = c.execute(
        """SELECT d.id,d.name,d.emoji,
                  COUNT(CASE WHEN g.grade_value IS NOT NULL THEN 1 END) grade_count,
                  AVG(g.grade_value) average,
                  COUNT(g.id) + (SELECT COUNT(*) FROM grade_marks gm WHERE gm.discipline_id=d.id AND gm.student_id=?) total_entries
           FROM disciplines d LEFT JOIN grades g ON g.discipline_id=d.id AND g.student_id=?
           WHERE g.id IS NOT NULL OR EXISTS(SELECT 1 FROM grade_marks gm2 WHERE gm2.discipline_id=d.id AND gm2.student_id=?)
           GROUP BY d.id ORDER BY d.name""", (student["id"], student["id"], student["id"])
    ).fetchall()
    c.close()
    return {"items": [{**dict(x), "average": round(float(x["average"]), 2) if x["average"] is not None else None} for x in rows]}


@app.get("/api/grades/{discipline_id}")
def grade_discipline(discipline_id: int, x_telegram_init_data: str | None = Header(default=None)):
    _, student, _, _ = current_user(x_telegram_init_data)
    c = db()
    discipline = c.execute("SELECT * FROM disciplines WHERE id=?", (discipline_id,)).fetchone()
    rows = c.execute(
        """SELECT id,grade_date,grade_value,attendance_type,comment,created_by,created_at,updated_at
           FROM grades WHERE student_id=? AND discipline_id=? ORDER BY grade_date DESC,id DESC""",
        (student["id"], discipline_id),
    ).fetchall()
    plus_rows = c.execute(
        "SELECT id,grade_date,value,created_by,created_at FROM grade_marks WHERE student_id=? AND discipline_id=? ORDER BY grade_date DESC,id DESC",
        (student["id"], discipline_id),
    ).fetchall()
    c.close()
    if not discipline:
        raise HTTPException(404, "Дисциплина не найдена.")
    items=[{**dict(x), "value": grade_label(x), "kind": "grade"} for x in rows]
    items += [{**dict(x), "grade_value":None, "attendance_type":None, "comment":"", "value":x["value"], "kind":"plus"} for x in plus_rows]
    items.sort(key=lambda x:(x['grade_date'],x['id']), reverse=True)
    return {"discipline": dict(discipline), "items": items}


@app.get("/api/events")
def events(x_telegram_init_data: str | None = Header(default=None)):
    _, student, _, _ = current_user(x_telegram_init_data)
    c = db()
    rows = c.execute(
        """SELECT e.*,d.name discipline,d.emoji FROM event_feed e LEFT JOIN disciplines d ON d.id=e.discipline_id
           WHERE e.student_id=? ORDER BY e.created_at DESC LIMIT 100""", (student["id"],)
    ).fetchall()
    c.close()
    return {"items": [dict(x) for x in rows]}


# -------------------- admin --------------------

@app.get("/api/admin/overview")
def admin_overview(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    c = db()
    result = {
        "students": c.execute("SELECT COUNT(*) n FROM students").fetchone()["n"],
        "authorized": c.execute("SELECT COUNT(*) n FROM students WHERE telegram_id IS NOT NULL").fetchone()["n"],
        "disciplines": c.execute("SELECT COUNT(*) n FROM disciplines WHERE active=1").fetchone()["n"],
        "active_homework": c.execute("SELECT COUNT(*) n FROM homework WHERE hidden=0 AND archived=0").fetchone()["n"],
        "archived_homework": c.execute("SELECT COUNT(*) n FROM homework WHERE archived=1").fetchone()["n"],
        "grades": c.execute("SELECT COUNT(*) n FROM grades").fetchone()["n"],
        "textbooks": c.execute("SELECT COUNT(*) n FROM textbooks").fetchone()["n"],
        "materials": c.execute("SELECT COUNT(*) n FROM additional_materials").fetchone()["n"],
        "schedule_days": c.execute("SELECT COUNT(*) n FROM schedule_days").fetchone()["n"],
        "schedule_lessons": c.execute("SELECT COUNT(*) n FROM schedule_lessons").fetchone()["n"],
        "events": c.execute("SELECT COUNT(*) n FROM event_feed").fetchone()["n"],
    }
    c.close()
    return result


@app.get("/api/admin/events")
def admin_events(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    c = db()
    rows = c.execute(
        """SELECT e.*,s.full_name,d.name discipline,d.emoji,g.grade_value,g.attendance_type,g.comment,g.created_by
           FROM event_feed e JOIN students s ON s.id=e.student_id
           LEFT JOIN disciplines d ON d.id=e.discipline_id
           LEFT JOIN grades g ON g.id=e.grade_id ORDER BY e.created_at DESC LIMIT 200"""
    ).fetchall()
    c.close()
    return {"items": [dict(x) for x in rows]}


@app.get("/api/admin/disciplines")
def admin_disciplines(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    c = db()
    rows = c.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall()
    c.close()
    return {"items": [dict(x) for x in rows]}


@app.get("/api/admin/students")
def admin_students(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    c = db()
    rows = c.execute("SELECT id,full_name,telegram_id FROM students ORDER BY full_name").fetchall()
    c.close()
    return {"items": [dict(x) for x in rows]}


@app.get("/api/admin/google-grades/status")
def google_grades_status(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    c=db(); row=c.execute("SELECT * FROM google_sync_state WHERE id=1").fetchone(); c.close()
    return {"configured": bool(os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")), "last_sync_at": row["last_sync_at"] if row else None, "status": row["last_status"] if row else "never", "error": row["last_error"] if row else None}


@app.post("/api/admin/google-grades/sync")
def google_grades_sync(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    result=sync_google_grades()
    if not result.get("ok"):
        raise HTTPException(502, result.get("error") or result.get("reason") or "Google Sheets не настроен.")
    return result


@app.post("/api/homework")
async def create_homework(payload: HomeworkCreateIn, x_telegram_init_data: str | None = Header(default=None)):
    uid, _, _, _ = current_user(x_telegram_init_data, admin=True)
    due = parse_hw_date(payload.due_date)
    c=db(); d=c.execute("SELECT * FROM disciplines WHERE id=? AND active=1",(payload.discipline_id,)).fetchone()
    if not d:
        c.close(); raise HTTPException(404,"Дисциплина не найдена.")
    now=datetime.now(TIMEZONE).date().strftime("%d.%m.%Y")
    token=(payload.client_token or "").strip() or None
    if token:
        existing=c.execute("SELECT id FROM homework WHERE client_token=?", (token,)).fetchone()
        if existing:
            hid=existing["id"]
            c.close()
            return {"ok":True,"homework_id":hid,"waiting_for_file":payload.wants_file,"duplicate":True}
    try:
        cur=c.execute("INSERT INTO homework(discipline_id,text,explanation,published_date,due_date,client_token) VALUES(?,?,?,?,?,?)",(payload.discipline_id,payload.text,payload.explanation,now,due,token))
    except sqlite3.IntegrityError:
        if token:
            existing=c.execute("SELECT id FROM homework WHERE client_token=?", (token,)).fetchone()
            if existing:
                hid=existing["id"]; c.close(); return {"ok":True,"homework_id":hid,"waiting_for_file":payload.wants_file,"duplicate":True}
        c.close(); raise
    hid=cur.lastrowid
    c.execute("INSERT INTO homework_extra(homework_id,title) VALUES(?,?)",(hid,payload.title.strip()))
    if payload.wants_file:
        c.execute("INSERT INTO homework_upload_requests(telegram_id,homework_id,created_at,status) VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET homework_id=excluded.homework_id,created_at=excluded.created_at,status='waiting'",(uid,hid,datetime.now(TIMEZONE).isoformat(),'waiting'))
    c.commit(); c.close()
    # Notify students through the bot API. The file, if requested, is attached later by the bot.
    text=f"📝 <b>Новое ДЗ</b>\n\n{d['emoji']} <b>{d['name']}</b>\n<b>{payload.title.strip()}</b>\n\n{payload.text}\n\n📅 <b>Сдать до:</b> {due}\n📤 <b>Опубликовано:</b> {now}"
    if payload.explanation.strip():
        text += f"\n\n💬 <b>Пояснение:</b>\n{payload.explanation.strip()}"
    async with httpx.AsyncClient(timeout=20) as client:
        cc=db(); users=cc.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL").fetchall(); cc.close()
        for u in users:
            try: await client.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":u["telegram_id"],"text":text,"parse_mode":"HTML"})
            except Exception: pass
        if payload.wants_file:
            try:
                await client.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":uid,"text":"📎 <b>Теперь отправьте файл этому боту.</b> Я автоматически прикреплю его к созданному ДЗ." ,"parse_mode":"HTML"})
            except Exception: pass
    return {"ok":True,"homework_id":hid,"waiting_for_file":payload.wants_file}


@app.get("/api/textbooks")
def textbooks(x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data)
    c=db(); rows=c.execute("SELECT t.id,t.title,t.kind,t.discipline_id,d.name discipline,d.emoji FROM textbooks t JOIN disciplines d ON d.id=t.discipline_id WHERE d.active=1 ORDER BY d.name,t.title").fetchall(); c.close()
    return {"items":[dict(x) for x in rows]}


@app.post("/api/textbooks/{textbook_id}/send")
async def send_textbook(textbook_id: int, x_telegram_init_data: str | None = Header(default=None)):
    uid, _, _, _ = current_user(x_telegram_init_data)
    c=db(); item=c.execute("SELECT t.*,d.name discipline,d.emoji FROM textbooks t JOIN disciplines d ON d.id=t.discipline_id WHERE t.id=? AND d.active=1",(textbook_id,)).fetchone(); c.close()
    if not item: raise HTTPException(404,"Учебник не найден.")
    method={"photo":"sendPhoto","video":"sendVideo"}.get(item["kind"],"sendDocument")
    field={"sendPhoto":"photo","sendVideo":"video"}.get(method,"document")
    payload={"chat_id":uid,field:item["file_id"],"caption":f"📘 {item['discipline']} — {item['title']}"}
    async with httpx.AsyncClient(timeout=20) as client:
        r=await client.post(f"https://api.telegram.org/bot{TOKEN}/{method}",data=payload)
    if r.status_code>=400 or not r.json().get("ok"): raise HTTPException(502,"Telegram не смог отправить учебник.")
    return {"ok":True}



def admin_auth(x_telegram_init_data: str | None):
    return current_user(x_telegram_init_data, admin=True)

def _norm_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip().lower().replace("ё", "е")
    return " ".join(value.split())

@app.get("/api/admin/homework")
def admin_homework_api(x_telegram_init_data: str | None = Header(default=None)):
    admin_auth(x_telegram_init_data); c=db()
    rows=c.execute("""SELECT h.*,d.name discipline,d.emoji,COALESCE(e.title,substr(h.text,1,80)) title FROM homework h JOIN disciplines d ON d.id=h.discipline_id LEFT JOIN homework_extra e ON e.homework_id=h.id ORDER BY h.archived,h.due_date DESC,h.id DESC""").fetchall(); c.close(); return {"items":[dict(x) for x in rows]}

@app.get("/api/admin/homework/{homework_id}")
def admin_homework_detail(homework_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db()
    h=c.execute("""SELECT h.*,d.name discipline,d.emoji,COALESCE(e.title,substr(h.text,1,80)) title FROM homework h JOIN disciplines d ON d.id=h.discipline_id LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.id=?""",(homework_id,)).fetchone()
    if not h:
        c.close(); raise HTTPException(404,"ДЗ не найдено.")
    media=c.execute("SELECT id,kind,file_id,caption FROM homework_media WHERE homework_id=? ORDER BY id",(homework_id,)).fetchall()
    answer=c.execute("SELECT homework_id,kind,text,file_id,caption FROM homework_answer WHERE homework_id=?",(homework_id,)).fetchone()
    c.close(); return {"item":dict(h),"media":[dict(x) for x in media],"answer":dict(answer) if answer else {}}

@app.patch("/api/admin/homework/{homework_id}")
def admin_homework_edit(homework_id:int, payload:HomeworkEditIn, x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data)
    due=parse_hw_date(payload.due_date)
    if not due: raise HTTPException(400,"Неверная дата сдачи.")
    c=db(); h=c.execute("SELECT id FROM homework WHERE id=?",(homework_id,)).fetchone()
    if not h: c.close(); raise HTTPException(404,"ДЗ не найдено.")
    c.execute("UPDATE homework SET text=?, explanation=?, due_date=? WHERE id=?",(payload.text,payload.explanation,due,homework_id))
    c.execute("INSERT INTO homework_extra(homework_id,title) VALUES(?,?) ON CONFLICT(homework_id) DO UPDATE SET title=excluded.title",(homework_id,payload.title))
    if payload.answer_text.strip():
        c.execute("INSERT INTO homework_answer(homework_id,kind,text,file_id,caption) VALUES(?,?,?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind='text',text=excluded.text,file_id='',caption=''",(homework_id,'text',payload.answer_text.strip(),'',''))
    elif payload.answer_text == '':
        # Preserve an existing file answer; only remove a text answer when the editor is cleared.
        a=c.execute("SELECT kind,file_id FROM homework_answer WHERE homework_id=?",(homework_id,)).fetchone()
        if a and a['kind']=='text': c.execute("DELETE FROM homework_answer WHERE homework_id=?",(homework_id,))
    c.commit(); c.close(); return {"ok":True}

@app.post("/api/admin/homework/{homework_id}/request-file")
def admin_homework_request_file(homework_id:int,x_telegram_init_data:str|None=Header(default=None)):
    uid,_,_,_=admin_auth(x_telegram_init_data); c=db(); h=c.execute("SELECT id FROM homework WHERE id=?",(homework_id,)).fetchone()
    if not h: c.close(); raise HTTPException(404,"ДЗ не найдено.")
    c.execute("INSERT OR REPLACE INTO admin_file_requests(telegram_id,target_type,target_id,created_at) VALUES(?,?,?,?)",(uid,'homework',homework_id,now_iso())); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/homework/{homework_id}/media/{media_id}")
def admin_homework_media_delete(homework_id:int,media_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("DELETE FROM homework_media WHERE id=? AND homework_id=?",(media_id,homework_id)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/admin/homework/{homework_id}/request-answer-file")
def admin_homework_request_answer_file(homework_id:int,x_telegram_init_data:str|None=Header(default=None)):
    uid,_,_,_=admin_auth(x_telegram_init_data); c=db(); h=c.execute("SELECT id FROM homework WHERE id=?",(homework_id,)).fetchone()
    if not h: c.close(); raise HTTPException(404,"ДЗ не найдено.")
    c.execute("INSERT OR REPLACE INTO admin_file_requests(telegram_id,target_type,target_id,created_at) VALUES(?,?,?,?)",(uid,'homework_answer',homework_id,now_iso())); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/homework/{homework_id}/answer")
def admin_homework_answer_delete(homework_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("DELETE FROM homework_answer WHERE homework_id=?",(homework_id,)); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/homework/{homework_id}")
def admin_homework_delete(homework_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("DELETE FROM homework WHERE id=?",(homework_id,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/admin/homework/{homework_id}/archive")
def admin_homework_archive(homework_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("UPDATE homework SET archived=1 WHERE id=?",(homework_id,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/admin/homework/{homework_id}/restore")
def admin_homework_restore(homework_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("UPDATE homework SET archived=0 WHERE id=?",(homework_id,)); c.commit(); c.close(); return {"ok":True}

@app.get("/api/admin/textbooks")
def admin_textbooks(x_telegram_init_data: str | None = Header(default=None)):
    admin_auth(x_telegram_init_data); c=db()
    rows=c.execute("""SELECT t.*,d.name discipline,d.emoji FROM textbooks t JOIN disciplines d ON d.id=t.discipline_id WHERE d.active=1 ORDER BY d.name,t.title""").fetchall(); c.close()
    return {"items":[dict(x) for x in rows]}

class AdminTextbookIn(BaseModel):
    discipline_id:int
    title:str
    file_id:str|None=None
    kind:str|None=None
    caption:str|None=None

@app.post("/api/admin/textbooks")
def admin_textbook_create(payload:AdminTextbookIn,x_telegram_init_data:str|None=Header(default=None)):
    uid,_,_,_=admin_auth(x_telegram_init_data); title=payload.title.strip()
    if not title: raise HTTPException(400,"Название учебника не может быть пустым.")
    c=db(); d=c.execute("SELECT id FROM disciplines WHERE id=? AND active=1",(payload.discipline_id,)).fetchone()
    if not d: c.close(); raise HTTPException(404,"Дисциплина не найдена.")
    c.execute("INSERT INTO textbooks(discipline_id,title,kind,file_id,caption,created_at) VALUES(?,?,?,?,?,?)",(payload.discipline_id,title,payload.kind,payload.file_id,payload.caption,now_iso())); tid=c.lastrowid; c.commit(); c.close()
    return {"ok":True,"id":tid,"needs_file":not bool(payload.file_id)}

@app.patch("/api/admin/textbooks/{textbook_id}")
def admin_textbook_update(textbook_id:int,payload:AdminTextbookIn,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); title=payload.title.strip()
    if not title: raise HTTPException(400,"Название учебника не может быть пустым.")
    c=db(); r=c.execute("SELECT id FROM textbooks WHERE id=?",(textbook_id,)).fetchone()
    if not r: c.close(); raise HTTPException(404,"Учебник не найден.")
    if payload.file_id:
        c.execute("UPDATE textbooks SET discipline_id=?,title=?,kind=?,file_id=?,caption=? WHERE id=?",(payload.discipline_id,title,payload.kind,payload.file_id,payload.caption,textbook_id))
    else:
        c.execute("UPDATE textbooks SET discipline_id=?,title=? WHERE id=?",(payload.discipline_id,title,textbook_id))
    c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/textbooks/{textbook_id}")
def admin_textbook_delete(textbook_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("DELETE FROM textbooks WHERE id=?",(textbook_id,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/admin/textbooks/{textbook_id}/request-file")
def admin_textbook_request_file(textbook_id:int,x_telegram_init_data:str|None=Header(default=None)):
    uid,_,_,_=admin_auth(x_telegram_init_data); c=db(); t=c.execute("SELECT title FROM textbooks WHERE id=?",(textbook_id,)).fetchone()
    if not t: c.close(); raise HTTPException(404,"Учебник не найден.")
    c.execute("INSERT OR REPLACE INTO admin_file_requests(telegram_id,target_type,target_id,created_at) VALUES(?,?,?,?)",(uid,'textbook',textbook_id,now_iso())); c.commit(); c.close()
    async def send():
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":uid,"text":f"📎 Отправьте следующим сообщением файл учебника «{t['title']}». Он будет прикреплён автоматически."})
    import asyncio; asyncio.create_task(send())
    return {"ok":True}

@app.get("/api/admin/materials")
def admin_materials_api(x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); rows=c.execute("""SELECT m.*,d.name discipline,d.emoji FROM additional_materials m JOIN disciplines d ON d.id=m.discipline_id WHERE d.active=1 ORDER BY d.name,m.id DESC""").fetchall(); c.close(); return {"items":[dict(x) for x in rows]}

class AdminMaterialIn(BaseModel):
    discipline_id:int
    title:str
    file_id:str|None=None
    kind:str|None=None
    caption:str|None=None

@app.post("/api/admin/materials")
def admin_material_create(payload:AdminMaterialIn,x_telegram_init_data:str|None=Header(default=None)):
    uid,_,_,_=admin_auth(x_telegram_init_data); title=payload.title.strip()
    if not title: raise HTTPException(400,"Название материала не может быть пустым.")
    c=db(); d=c.execute("SELECT id FROM disciplines WHERE id=? AND active=1",(payload.discipline_id,)).fetchone()
    if not d: c.close(); raise HTTPException(404,"Дисциплина не найдена.")
    c.execute("INSERT INTO additional_materials(discipline_id,title,kind,file_id,caption,created_at) VALUES(?,?,?,?,?,?)",(payload.discipline_id,title,payload.kind,payload.file_id,payload.caption,now_iso())); mid=c.lastrowid; c.commit(); c.close(); return {"ok":True,"id":mid,"needs_file":not bool(payload.file_id)}

@app.patch("/api/admin/materials/{material_id}")
def admin_material_update(material_id:int,payload:AdminMaterialIn,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); title=payload.title.strip()
    if not title: raise HTTPException(400,"Название материала не может быть пустым.")
    c=db(); r=c.execute("SELECT id FROM additional_materials WHERE id=?",(material_id,)).fetchone()
    if not r: c.close(); raise HTTPException(404,"Материал не найден.")
    if payload.file_id: c.execute("UPDATE additional_materials SET discipline_id=?,title=?,kind=?,file_id=?,caption=? WHERE id=?",(payload.discipline_id,title,payload.kind,payload.file_id,payload.caption,material_id))
    else: c.execute("UPDATE additional_materials SET discipline_id=?,title=? WHERE id=?",(payload.discipline_id,title,material_id))
    c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/materials/{material_id}")
def admin_material_delete(material_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("DELETE FROM additional_materials WHERE id=?",(material_id,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/admin/materials/{material_id}/request-file")
def admin_material_request_file(material_id:int,x_telegram_init_data:str|None=Header(default=None)):
    uid,_,_,_=admin_auth(x_telegram_init_data); c=db(); m=c.execute("SELECT title FROM additional_materials WHERE id=?",(material_id,)).fetchone()
    if not m: c.close(); raise HTTPException(404,"Материал не найден.")
    c.execute("INSERT OR REPLACE INTO admin_file_requests(telegram_id,target_type,target_id,created_at) VALUES(?,?,?,?)",(uid,'material',material_id,now_iso())); c.commit(); c.close()
    async def send():
        async with httpx.AsyncClient(timeout=20) as client: await client.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={"chat_id":uid,"text":f"📎 Отправьте следующим сообщением файл материала «{m['title']}». Он будет прикреплён автоматически."})
    import asyncio; asyncio.create_task(send()); return {"ok":True}

@app.get("/api/admin/students")
def admin_students_api(x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); rows=c.execute("SELECT id,full_name,telegram_id,created_at FROM students ORDER BY full_name").fetchall(); c.close(); return {"items":[dict(x) for x in rows]}

class StudentCreateIn(BaseModel):
    full_name:str

@app.post("/api/admin/students")
def admin_student_create(payload:StudentCreateIn,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); name=payload.full_name.strip()
    if not name: raise HTTPException(400,"ФИО не может быть пустым.")
    c=db()
    try: c.execute("INSERT INTO students(full_name,normalized_name,created_at) VALUES(?,?,?)",(name,_norm_name(name),now_iso())); sid=c.lastrowid; c.commit()
    except sqlite3.IntegrityError: c.close(); raise HTTPException(400,"Такое ФИО уже есть.")
    c.close(); return {"ok":True,"id":sid}

@app.delete("/api/admin/students/{student_id}")
def admin_student_delete(student_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("DELETE FROM students WHERE id=?",(student_id,)); c.commit(); c.close(); return {"ok":True}

@app.get("/api/admin/vip")
def admin_vip_api(x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); rows=c.execute("""SELECT s.id,s.full_name,s.telegram_id,CASE WHEN v.telegram_id IS NULL THEN 0 ELSE 1 END is_vip FROM students s LEFT JOIN vip_users v ON v.telegram_id=s.telegram_id WHERE s.telegram_id IS NOT NULL ORDER BY s.full_name""").fetchall(); c.close(); return {"items":[dict(x) for x in rows]}

@app.post("/api/admin/vip/{telegram_id}/toggle")
def admin_vip_toggle(telegram_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); s=c.execute("SELECT full_name FROM students WHERE telegram_id=?",(telegram_id,)).fetchone()
    if not s: c.close(); raise HTTPException(404,"Студент не найден.")
    v=c.execute("SELECT 1 FROM vip_users WHERE telegram_id=?",(telegram_id,)).fetchone()
    if v: c.execute("DELETE FROM vip_users WHERE telegram_id=?",(telegram_id,)); enabled=False
    else: c.execute("INSERT INTO vip_users(telegram_id,full_name,added_at) VALUES(?,?,?)",(telegram_id,s['full_name'],now_iso())); enabled=True
    c.commit(); c.close(); return {"ok":True,"is_vip":enabled}

@app.post("/api/admin/disciplines")
def admin_discipline_create(payload:dict,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); name=str(payload.get('name','')).strip(); emoji=str(payload.get('emoji','📚')).strip() or '📚'
    if not name: raise HTTPException(400,"Название дисциплины не может быть пустым.")
    c=db(); c.execute("INSERT INTO disciplines(name,emoji,active,textbooks_enabled) VALUES(?,?,1,1)",(name,emoji)); did=c.lastrowid; c.commit(); c.close(); return {"ok":True,"id":did}

@app.patch("/api/admin/disciplines/{discipline_id}")
def admin_discipline_update(discipline_id:int,payload:dict,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); name=str(payload.get('name','')).strip(); emoji=str(payload.get('emoji','📚')).strip() or '📚'
    if not name: raise HTTPException(400,"Название дисциплины не может быть пустым.")
    c=db(); c.execute("UPDATE disciplines SET name=?,emoji=? WHERE id=?",(name,emoji,discipline_id)); c.commit(); c.close(); return {"ok":True}

@app.delete("/api/admin/disciplines/{discipline_id}")
def admin_discipline_delete(discipline_id:int,x_telegram_init_data:str|None=Header(default=None)):
    admin_auth(x_telegram_init_data); c=db(); c.execute("UPDATE disciplines SET active=0 WHERE id=?",(discipline_id,)); c.commit(); c.close(); return {"ok":True}

class BroadcastIn(BaseModel):
    kind:str
    text:str

@app.post("/api/admin/broadcast")
async def admin_broadcast(payload:BroadcastIn,x_telegram_init_data:str|None=Header(default=None)):
    uid,_,_,_=admin_auth(x_telegram_init_data); text=payload.text.strip(); kind=payload.kind
    if not text: raise HTTPException(400,"Текст уведомления пуст.")
    labels={'important':'🚨 <b>Важная информация</b>','update':'🔄 <b>Обновление</b>','other':'📢 <b>Прочее уведомление</b>'}
    columns={'update':'updates','other':'other'}
    c=db()
    if kind=='important': users=c.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL").fetchall()
    elif kind in columns: users=c.execute(f"SELECT s.telegram_id FROM students s LEFT JOIN notification_settings n ON n.telegram_id=s.telegram_id WHERE s.telegram_id IS NOT NULL AND COALESCE(n.{columns[kind]},1)=1").fetchall()
    else: c.close(); raise HTTPException(400,"Неизвестный тип уведомления.")
    c.close(); sent=0
    async with httpx.AsyncClient(timeout=20) as client:
        for u in users:
            try:
                r=await client.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",data={'chat_id':u['telegram_id'],'text':labels[kind]+'\n\n'+text,'parse_mode':'HTML'})
                if r.status_code<400 and r.json().get('ok'): sent+=1
            except Exception: pass
    return {"ok":True,"sent":sent,"total":len(users)}

@app.get("/api/admin/schedule/{day}")
def admin_schedule(day: str, x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    day = parse_date(day)
    c = db()
    rows = c.execute(
        """SELECT l.*,d.name discipline,d.emoji FROM schedule_lessons l
           JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id
           WHERE sd.day=? ORDER BY l.lesson_no,l.start""", (day,)
    ).fetchall()
    c.close()
    return {"date": day, "lessons": [dict(x) for x in rows]}


@app.post("/api/admin/schedule/lesson")
def create_lesson(payload: LessonIn, x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    day = parse_date(payload.day)
    c = db()
    discipline = c.execute("SELECT id FROM disciplines WHERE id=?", (payload.discipline_id,)).fetchone()
    if not discipline:
        c.close(); raise HTTPException(404, "Дисциплина не найдена.")
    row = c.execute("SELECT id FROM schedule_days WHERE day=?", (day,)).fetchone()
    ts = now_iso()
    if row:
        day_id = row["id"]
        c.execute("UPDATE schedule_days SET updated_at=? WHERE id=?", (ts, day_id))
    else:
        c.execute("INSERT INTO schedule_days(day,created_at,updated_at) VALUES(?,?,?)", (day,ts,ts))
        day_id = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    c.execute(
        "INSERT INTO schedule_lessons(schedule_day_id,lesson_no,start,end,discipline_id,lesson_type,room) VALUES(?,?,?,?,?,?,?)",
        (day_id,payload.lesson_no,payload.start,payload.end,payload.discipline_id,payload.lesson_type,payload.room),
    )
    c.commit(); c.close()
    return {"ok": True}


@app.patch("/api/admin/schedule/lesson/{lesson_id}")
def patch_lesson(lesson_id: int, payload: LessonPatch, x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    fields = []
    values = []
    for name in ("lesson_no", "start", "end", "discipline_id", "lesson_type", "room"):
        value = getattr(payload, name)
        if value is not None:
            fields.append(f"{name}=?"); values.append(value)
    if not fields:
        return {"ok": True}
    c = db()
    row = c.execute("SELECT schedule_day_id FROM schedule_lessons WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404, "Пара не найдена.")
    values.append(lesson_id)
    c.execute(f"UPDATE schedule_lessons SET {','.join(fields)} WHERE id=?", values)
    c.execute("UPDATE schedule_days SET updated_at=? WHERE id=?", (now_iso(), row["schedule_day_id"]))
    c.commit(); c.close()
    return {"ok": True}


class ReorderLessonsIn(BaseModel):
    lesson_ids: list[int] = Field(min_length=1)


@app.post("/api/admin/schedule/reorder")
def reorder_lessons(payload: ReorderLessonsIn, day: str = Query(...), x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    day = parse_date(day)
    c = db()
    day_row = c.execute("SELECT id FROM schedule_days WHERE day=?", (day,)).fetchone()
    if not day_row:
        c.close(); raise HTTPException(404, "На эту дату нет расписания.")
    existing = [r["id"] for r in c.execute("SELECT id FROM schedule_lessons WHERE schedule_day_id=?", (day_row["id"],)).fetchall()]
    requested = [int(x) for x in payload.lesson_ids]
    if set(existing) != set(requested) or len(existing) != len(requested):
        c.close(); raise HTTPException(400, "Список пар устарел. Обновите расписание.")
    for no, lesson_id in enumerate(requested, 1):
        c.execute("UPDATE schedule_lessons SET lesson_no=? WHERE id=?", (no * 1000, lesson_id))
    for no, lesson_id in enumerate(requested, 1):
        c.execute("UPDATE schedule_lessons SET lesson_no=? WHERE id=?", (no, lesson_id))
    c.execute("UPDATE schedule_days SET updated_at=? WHERE id=?", (now_iso(), day_row["id"]))
    c.commit(); c.close()
    return {"ok": True}


@app.delete("/api/admin/schedule/lesson/{lesson_id}")
def delete_lesson(lesson_id: int, x_telegram_init_data: str | None = Header(default=None)):
    current_user(x_telegram_init_data, admin=True)
    c = db()
    row = c.execute("SELECT schedule_day_id FROM schedule_lessons WHERE id=?", (lesson_id,)).fetchone()
    if not row:
        c.close(); raise HTTPException(404, "Пара не найдена.")
    c.execute("DELETE FROM schedule_lessons WHERE id=?", (lesson_id,))
    c.execute("UPDATE schedule_days SET updated_at=? WHERE id=?", (now_iso(), row["schedule_day_id"]))
    c.commit(); c.close()
    return {"ok": True}


def create_app():
    return app
