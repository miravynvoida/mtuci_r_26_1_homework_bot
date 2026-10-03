import asyncio
import calendar
import logging
import os
import re
import sqlite3
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.config import BASE_DIR, DB_PATH, TZ, VERSION, HELP_USERNAME, MINI_APP_URL, WEB_HOST, WEB_PORT, TOKEN, ADMIN_IDS
from core.database import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("r26bot")

router = Router()

SLOT_TIMES = [
    ("08:30", "10:00"),
    ("10:10", "11:40"),
    ("11:50", "13:20"),
    ("13:50", "15:20"),
    ("15:30", "17:00"),
]
TEACHERS = {
    "Русский язык": "Пронина А.В.", "Литература": "Пронина А.В.", "Математика": "Иванова Т.А.",
    "Ин. язык": "Балыкина М.И.", "Информатика": "Шабаев А.А.", "Физика": "Апарин Л.А.",
    "Химия": "Зыкова Д.А.", "Биология": "Зыкова Д.А.", "История": "Тугова М.А.",
    "Обществознание": "Алексеева Л.М.", "География": "Алимова И.Н.", "Физ-ра": "Оринчук А.В.",
    "ОБЖиЗР": "Груздев И.И.", "Родной язык": "Куркина В.В.", "ОПД": "Самохвалова Е.Б.",
    "Физ. культура": "Оринчук А.В.",
}


def migrate():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = db()
    cur = c.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, discipline_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS bell_times (slot INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS schedule_days (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS schedule_lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_day_id INTEGER NOT NULL, lesson_no INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, discipline_id INTEGER NOT NULL, lesson_type TEXT NOT NULL, room TEXT NOT NULL, FOREIGN KEY(schedule_day_id) REFERENCES schedule_days(id) ON DELETE CASCADE, FOREIGN KEY(discipline_id) REFERENCES disciplines(id));
    CREATE TABLE IF NOT EXISTS notification_settings (telegram_id INTEGER PRIMARY KEY, tomorrow_schedule INTEGER NOT NULL DEFAULT 1, next_lesson INTEGER NOT NULL DEFAULT 1, updates INTEGER NOT NULL DEFAULT 1, other INTEGER NOT NULL DEFAULT 1, deadline_reminders INTEGER NOT NULL DEFAULT 1, new_homework INTEGER NOT NULL DEFAULT 1, next_lesson_minutes INTEGER NOT NULL DEFAULT 30, setup_done INTEGER NOT NULL DEFAULT 0, setup_version INTEGER NOT NULL DEFAULT 0, silent_notifications INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(telegram_id) REFERENCES students(telegram_id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS sent_notifications (telegram_id INTEGER NOT NULL, notification_key TEXT NOT NULL, PRIMARY KEY(telegram_id, notification_key));
    CREATE TABLE IF NOT EXISTS homework_extra (homework_id INTEGER PRIMARY KEY, title TEXT NOT NULL, FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS homework_views (telegram_id INTEGER NOT NULL, homework_id INTEGER NOT NULL, viewed_at TEXT NOT NULL, PRIMARY KEY(telegram_id, homework_id));
    CREATE TABLE IF NOT EXISTS additional_materials (id INTEGER PRIMARY KEY AUTOINCREMENT, discipline_id INTEGER NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL, file_id TEXT NOT NULL, caption TEXT DEFAULT '', created_at TEXT NOT NULL, FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS app_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS google_sync_state (id INTEGER PRIMARY KEY CHECK(id=1), last_sync_at TEXT, last_status TEXT, last_error TEXT, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS homework_upload_requests (telegram_id INTEGER PRIMARY KEY, homework_id INTEGER NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS homework_answer (homework_id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'text', text TEXT DEFAULT '', file_id TEXT DEFAULT '', caption TEXT DEFAULT '', FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS homework_media (id INTEGER PRIMARY KEY AUTOINCREMENT, homework_id INTEGER NOT NULL, kind TEXT NOT NULL, file_id TEXT NOT NULL, caption TEXT DEFAULT '', FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
    
    """)
    notif_cols = {r[1] for r in cur.execute("PRAGMA table_info(notification_settings)").fetchall()}
    for col in ("updates", "other", "deadline_reminders", "new_homework"):
        if col not in notif_cols:
            cur.execute(f"ALTER TABLE notification_settings ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
    if "setup_done" not in notif_cols:
        cur.execute("ALTER TABLE notification_settings ADD COLUMN setup_done INTEGER NOT NULL DEFAULT 0")
    if "next_lesson_minutes" not in notif_cols:
        cur.execute("ALTER TABLE notification_settings ADD COLUMN next_lesson_minutes INTEGER NOT NULL DEFAULT 30")
    if "setup_version" not in notif_cols:
        cur.execute("ALTER TABLE notification_settings ADD COLUMN setup_version INTEGER NOT NULL DEFAULT 0")
    if "silent_notifications" not in notif_cols:
        cur.execute("ALTER TABLE notification_settings ADD COLUMN silent_notifications INTEGER NOT NULL DEFAULT 0")
    # Users from older builds did not have a durable setup marker. Give them the new one-time setup now.
    if "setup_version" not in notif_cols:
        cur.execute("UPDATE notification_settings SET setup_version=0")
    else:
        cur.execute("UPDATE notification_settings SET setup_version=0 WHERE setup_version IS NULL")
    hcols = {r[1] for r in cur.execute("PRAGMA table_info(homework)").fetchall()}
    if "client_token" not in hcols:
        cur.execute("ALTER TABLE homework ADD COLUMN client_token TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_homework_client_token ON homework(client_token) WHERE client_token IS NOT NULL")
    cur.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL")
    cur.execute("INSERT OR IGNORE INTO homework_views(telegram_id,homework_id,viewed_at) SELECT s.telegram_id,h.id,? FROM students s CROSS JOIN homework h WHERE s.telegram_id IS NOT NULL", (datetime.now(TZ).isoformat(),))

    for i, (s,e) in enumerate(SLOT_TIMES, 1):
        cur.execute("INSERT OR IGNORE INTO bell_times(slot,start,end) VALUES(?,?,?)", (i,s,e))
    for name, teacher in TEACHERS.items():
        r = cur.execute("SELECT id FROM disciplines WHERE name=?", (name,)).fetchone()
        if r:
            cur.execute("INSERT INTO teachers(discipline_id,name) VALUES(?,?) ON CONFLICT(discipline_id) DO UPDATE SET name=excluded.name", (r[0], teacher))
    cur.execute("INSERT OR IGNORE INTO bot_info(id,version,updated_date) VALUES(1,?,?)", (VERSION, date.today().strftime('%d.%m.%Y')))
    for h in cur.execute("SELECT id,text FROM homework").fetchall():
        title=(h[1].splitlines()[0] if h[1] else 'Задание')[:200]
        cur.execute("INSERT OR IGNORE INTO homework_extra(homework_id,title) VALUES(?,?)", (h[0], title))
    cur.execute("UPDATE bot_info SET version=?, updated_date=? WHERE id=1", (VERSION, date.today().strftime('%d.%m.%Y')))
    # v10: Google Sheets becomes the only source of truth for grades.
    # Purge the old local journal exactly once; subsequent starts keep the Google cache.
    if not cur.execute("SELECT 1 FROM app_migrations WHERE id='v10_google_grades_purge'").fetchone():
        cur.execute("DELETE FROM grade_history")
        cur.execute("DELETE FROM event_feed WHERE event_type LIKE 'grade_%'")
        cur.execute("DELETE FROM grade_marks")
        cur.execute("DELETE FROM grades")
        cur.execute("DELETE FROM grade_dates")
        cur.execute("INSERT INTO app_migrations(id,applied_at) VALUES(?,?)", ('v10_google_grades_purge', datetime.now(TZ).isoformat()))
    c.commit(); c.close()


def norm(s):
    return " ".join(s.lower().replace("ё", "е").split())


def is_admin(uid): return uid in ADMIN_IDS

def is_auth(uid):
    c=db(); r=c.execute("SELECT * FROM students WHERE telegram_id=?",(uid,)).fetchone(); c.close(); return r

def is_vip(uid):
    c=db(); r=c.execute("SELECT 1 FROM vip_users WHERE telegram_id=?",(uid,)).fetchone(); c.close(); return bool(r)


def ik(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def b(text, data): return InlineKeyboardButton(text=text, callback_data=data)

def two_col(items):
    rows=[]
    for i in range(0,len(items),2): rows.append(items[i:i+2])
    return rows

async def safe_delete(bot, uid):
    c=db(); r=c.execute("SELECT message_id FROM ui_state WHERE telegram_id=?",(uid,)).fetchone(); c.close()
    if r:
        try: await bot.delete_message(uid, r[0])
        except Exception: pass

async def show(bot, uid, text, markup=None, parse_mode=ParseMode.HTML):
    await safe_delete(bot, uid)
    m=await bot.send_message(uid,text,reply_markup=markup,parse_mode=parse_mode)
    c=db(); c.execute("INSERT INTO ui_state(telegram_id,message_id) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET message_id=excluded.message_id",(uid,m.message_id)); c.commit(); c.close()
    return m

async def edit_or_answer(call, text, markup=None):
    # Normal navigation edits the current control message in place.
    # This keeps the chat clean and avoids creating a new message on every click.
    # Screens that have sent files/media separately can explicitly use show()
    # on their return action so the control message appears below those files.
    if isinstance(call, CallbackQuery) and call.message:
        try:
            await call.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            c=db(); c.execute("INSERT INTO ui_state(telegram_id,message_id) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET message_id=excluded.message_id",(call.from_user.id,call.message.message_id)); c.commit(); c.close()
            return call.message
        except TelegramBadRequest as e:
            # Some Telegram errors (e.g. identical content) are harmless.
            if "message is not modified" in str(e).lower():
                return call.message
    return await show(call.bot, call.from_user.id, text, markup)


def main_kb(uid=None):
    rows = [
        [b("📅 Расписание","menu:schedule"), b("📝 Домашнее задание","menu:hw")],
        [b("📚 Учебники","menu:books"), b("⚙️ Настройки","menu:settings")],
    ]
    if MINI_APP_URL:
        rows.append([InlineKeyboardButton(text="📖 Электронный дневник", web_app=WebAppInfo(url=MINI_APP_URL + (("&" if "?" in MINI_APP_URL else "?") + "admin=1") if uid is not None and is_admin(uid) else MINI_APP_URL))])
    else:
        rows.append([b("👤 Личный кабинет","menu:soon")])
    if uid is not None and is_admin(uid):
        rows.append([b("🛠 Админ панель","menu:admin")])
    rows.append([InlineKeyboardButton(text="❓ Помощь", url=f"https://t.me/{HELP_USERNAME.lstrip('@')}")])
    return ik(rows)

async def main_menu(bot, uid, name=None):
    student = is_auth(uid)
    name = name or (student["full_name"] if student else "студент")
    now = datetime.now(TZ)
    d = now.date()
    lessons = schedule_for(d)
    current = None
    upcoming = None
    for x in lessons:
        st = datetime.combine(d, time.fromisoformat(x['start']), tzinfo=TZ)
        en = datetime.combine(d, time.fromisoformat(x['end']), tzinfo=TZ)
        if st <= now < en:
            current = x; break
        if st > now and upcoming is None:
            upcoming = x
    x = current or upcoming
    text = f"👋 <b>Добро пожаловать в главное меню!</b>\n\nЯ — информационный бот группы <b>Р-26-1</b>.\nЗдесь можно посмотреть расписание, домашние задания и учебники.\n\n"
    if x:
        cc=db(); tr=cc.execute("SELECT name FROM teachers WHERE discipline_id=?",(x['discipline_id'],)).fetchone(); cc.close()
        teacher=tr['name'] if tr else TEACHERS.get(x['discipline'],'')
        if current:
            text += f"🔴 <b>Сейчас идёт пара</b>\n\n<b>{x['start']}–{x['end']} — {x['lesson_no']} пара | {x['lesson_type']}</b>\n{x['emoji']} <b>{x['discipline']}</b>\n{teacher} • ауд. {x['room']}"
        else:
            mins=max(0,int((datetime.combine(d,time.fromisoformat(x['start']),tzinfo=TZ)-now).total_seconds()//60))
            text += f"⏳ <b>Следующая пара</b>\n\n<b>{x['start']}–{x['end']} — {x['lesson_no']} пара | {x['lesson_type']}</b>\n{x['emoji']} <b>{x['discipline']}</b>\n{teacher} • ауд. {x['room']}\n\n⏱ Через <b>{mins} мин.</b>"
    else:
        text += "🌙 <b>На сегодня пар больше нет.</b>" if lessons else "💤 <b>Сегодня занятий нет.</b>"
    return await show(bot,uid,text,main_kb(uid))

class Auth(StatesGroup): name=State()
class HWCreate(StatesGroup): discipline=State(); title=State(); task=State(); due=State(); explanation=State(); media=State(); preview=State()
class HWEdit(StatesGroup): field=State(); value=State(); media=State()
class ScheduleCreate(StatesGroup): count=State(); lesson_start=State(); discipline=State(); typ=State(); room=State()
class ScheduleLessonEdit(StatesGroup): time=State(); discipline=State(); typ=State(); room=State()
class StudentAdd(StatesGroup): name=State()
class DisciplineEdit(StatesGroup): name=State(); emoji=State()
class BookCreate(StatesGroup): title=State(); file=State()
class Broadcast(StatesGroup): text=State()
class MaterialCreate(StatesGroup): discipline=State(); title=State(); file=State()
class ScheduleCreateCustomTime(StatesGroup): value=State()
class ScheduleEditCustomTime(StatesGroup): value=State()
class ScheduleAddCustomTime(StatesGroup): value=State()
class ScheduleDateInput(StatesGroup): value=State()

@router.message(Command("cancel"))
async def cancel_command(m:Message,state:FSMContext):
    await state.clear()
    if is_admin(m.from_user.id):
        return await show(m.bot,m.from_user.id,"❌ <b>Действие отменено.</b>\n\n🛠 Выберите действие:",admin_kb())
    if is_auth(m.from_user.id):
        return await main_menu(m.bot,m.from_user.id)
    await m.answer("❌ Действие отменено.")

@router.message(Command("start"))
async def start(m:Message,state:FSMContext):
    await state.clear()
    if is_auth(m.from_user.id):
        if await initial_settings_prompt(m.bot,m.from_user.id): return
        return await main_menu(m.bot,m.from_user.id)
    await m.answer("👋 Привет! Для доступа к боту введи своё ФИО, как в списке студентов.")
    await state.set_state(Auth.name)

@router.message(Auth.name)
async def auth_name(m:Message,state:FSMContext):
    c=db(); r=c.execute("SELECT * FROM students WHERE normalized_name=?",(norm(m.text or ""),)).fetchone()
    if not r:
        c.close(); return await m.answer("❌ Такого ФИО нет в списке. Проверь написание и попробуй ещё раз.")
    if r["telegram_id"] not in (None,m.from_user.id):
        c.close(); return await m.answer("❌ Это ФИО уже привязано к другому Telegram ID.")
    c.execute("UPDATE students SET telegram_id=? WHERE id=?",(m.from_user.id,r["id"])); c.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(m.from_user.id,)); c.execute("INSERT OR IGNORE INTO homework_views(telegram_id,homework_id,viewed_at) SELECT ?,id,? FROM homework",(m.from_user.id,datetime.now(TZ).isoformat())); c.commit(); c.close(); await state.clear();
    if await initial_settings_prompt(m.bot,m.from_user.id): return
    await main_menu(m.bot,m.from_user.id,r["full_name"])

@router.message(Command("info"))
async def info(m:Message):
    name=is_auth(m.from_user.id); n=name["full_name"] if name else "не авторизован"
    await m.answer(f"ℹ️ <b>Информация о боте</b>\n\n🤖 Инфо-бот Р-26-1\n📦 Версия: <b>{VERSION}</b>\n\n👤 <b>Пользователь</b>\n📝 ФИО: {n}\n🆔 Telegram ID: <code>{m.from_user.id}</code>\n\n💬 Помощь: {HELP_USERNAME}")

@router.callback_query(F.data=="menu:admin")
async def menu_admin(c:CallbackQuery,state:FSMContext):
    if not await admin_only(c): return
    await state.clear(); await c.answer(); await edit_or_answer(c,"🛠 <b>Админ-панель</b>\\n\\nВыберите действие:",admin_kb())

@router.callback_query(F.data=="menu:soon")
async def soon(c:CallbackQuery):
    student=is_auth(c.from_user.id)
    if not student:
        return await c.answer("Сначала авторизуйтесь.",show_alert=True)
    cc=db()
    try:
        st=cc.execute("""SELECT COUNT(CASE WHEN grade_value IS NOT NULL THEN 1 END) grade_count,
                              AVG(grade_value) average,
                              COUNT(CASE WHEN attendance_type='Н' THEN 1 END) n_count,
                              COUNT(CASE WHEN attendance_type='Б' THEN 1 END) b_count,
                              COUNT(CASE WHEN attendance_type='О' THEN 1 END) o_count
                       FROM grades WHERE student_id=?""",(student['id'],)).fetchone()
        events=cc.execute("""SELECT e.title,e.description,e.created_at,d.emoji FROM event_feed e
                            LEFT JOIN disciplines d ON d.id=e.discipline_id
                            WHERE e.student_id=? ORDER BY e.created_at DESC LIMIT 5""",(student['id'],)).fetchall()
    except sqlite3.OperationalError:
        st=None; events=[]
    cc.close()
    if not st or not st['grade_count']:
        text=f"👤 <b>{student['full_name']}</b>\n\nПрофиль готов. Оценок пока нет.\n\n📖 Полный дневник — в Mini App."
    else:
        ev='\n'.join(f"• {x['emoji'] or ''} {x['title']}" for x in events) or 'Пока нет событий.'
        text=(f"👤 <b>{student['full_name']}</b>\n\n"
              f"📊 Средний балл: <b>{st['average']:.2f}</b>\n"
              f"📝 Оценок: <b>{st['grade_count']}</b>\n"
              f"Н: <b>{st['n_count']}</b> • Б: <b>{st['b_count']}</b> • О: <b>{st['o_count']}</b>\n\n"
              f"🕘 <b>Последние события</b>\n{ev}\n\n📖 Полный дневник — в Mini App.")
    await c.answer(); await edit_or_answer(c,text,ik([[InlineKeyboardButton(text="📖 Открыть дневник", web_app=WebAppInfo(url=MINI_APP_URL))] if MINI_APP_URL else [b("🏠 Назад","menu:home")]]))
@router.callback_query(F.data=="menu:home")
async def home(c:CallbackQuery): await c.answer(); await main_menu(c.bot,c.from_user.id)

# Schedule
@router.callback_query(F.data=="menu:schedule")
async def schedule_menu(c:CallbackQuery):
    await c.answer(); await edit_or_answer(c,"📅 <b>Расписание</b>\n\nВыберите период для показа расписания.\n\n✅ — пара уже прошла\n🔴 — пара идёт сейчас\n⏳ — пара скоро начнётся",ik([[b("📌 Сегодня","sch:day:0"),b("➡️ Завтра","sch:day:1")], [b("🗓 Выбрать день",f"sch:cal:{datetime.now(TZ).year}:{datetime.now(TZ).month}")],[b("🏠 Назад","menu:home")]]))

def day_str(d):
    return d.strftime('%Y-%m-%d')


def ru_date(d):
    names = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье']
    return f"{names[d.weekday()]}, {d.day:02d}.{d.month:02d}.{d.year}"


def is_study_day(d):
    return d.weekday() != 6


def shift_study_day(d, step):
    current = d
    while True:
        current += timedelta(days=step)
        if is_study_day(current):
            return current


def schedule_for(d):
    c=db(); day=c.execute("SELECT * FROM schedule_days WHERE day=?",(day_str(d),)).fetchone()
    lessons=[]
    if day: lessons=c.execute("SELECT l.*,d.name discipline,d.emoji FROM schedule_lessons l JOIN disciplines d ON d.id=l.discipline_id WHERE l.schedule_day_id=? ORDER BY start",(day['id'],)).fetchall()
    c.close(); return lessons


async def render_day(c,d, show_status=True, show_legend=True, show_navigation=True):
    lessons=schedule_for(d)
    if not lessons:
        txt=f"📅 <b>{ru_date(d)}</b>\n\nℹ️ Расписание на этот день ещё не опубликовано."
    else:
        now=datetime.now(TZ) if d==datetime.now(TZ).date() else None
        parts=[f"📅 <b>{ru_date(d)}</b>"]
        for x in lessons:
            teacher=TEACHERS.get(x['discipline'],"")
            cc=db(); tr=cc.execute("SELECT name FROM teachers WHERE discipline_id=?",(x['discipline_id'],)).fetchone(); cc.close()
            teacher=tr['name'] if tr else teacher
            status=""
            if show_status and now:
                st=datetime.combine(d,time.fromisoformat(x['start']),tzinfo=TZ)
                en=datetime.combine(d,time.fromisoformat(x['end']),tzinfo=TZ)
                status="🔴 " if st<=now<en else ("⏳ " if st>now else "✅ ")
            parts.append(f"\n{status}<b>{x['start']}–{x['end']} — {x['lesson_no']} пара | {x['lesson_type']}</b>\n{x['emoji']} <b>{x['discipline']}</b>\n{teacher} • ауд. {x['room']}")
        txt="\n".join(parts)
        if show_legend:
            txt += "\n\n<b>Обозначения:</b> ✅ прошла • 🔴 идёт сейчас • ⏳ скоро начнётся"
    if not show_navigation:
        return txt, None
    prev=shift_study_day(d,-1); nxt=shift_study_day(d,1)
    return txt,ik([[b("◀️ Предыдущий день",f"sch:date:{prev.isoformat()}"),b("▶️ Следующий день",f"sch:date:{nxt.isoformat()}")],[b("🗓 Выбрать день",f"sch:cal:{d.year}:{d.month}")],[b("🏠 Главное меню","menu:home")]])


@router.callback_query(F.data.startswith("sch:day:"))
async def sch_day(c:CallbackQuery):
    offset=int(c.data.split(':')[-1])
    d=datetime.now(TZ).date()
    if offset > 0:
        for _ in range(offset): d=shift_study_day(d,1)
    elif offset < 0:
        for _ in range(abs(offset)): d=shift_study_day(d,-1)
    if not schedule_for(d):
        return await c.answer("ℹ️ На этот день пока нет расписания.",show_alert=True)
    txt,kb=await render_day(c,d); await c.answer(); await edit_or_answer(c,txt,kb)


@router.callback_query(F.data.startswith("sch:date:"))
async def sch_date(c:CallbackQuery):
    d=date.fromisoformat(c.data.split(':',2)[2])
    if not is_study_day(d):
        return await c.answer("ℹ️ Воскресенье в расписании не используется.",show_alert=True)
    if not schedule_for(d):
        return await c.answer("ℹ️ На этот день пока нет расписания.",show_alert=True)
    txt,kb=await render_day(c,d); await c.answer(); await edit_or_answer(c,txt,kb)


def calendar_kb(year,month,prefix='sch'):
    first=date(year,month,1); weeks=calendar.monthcalendar(year,month); rows=[[b("⬅️",f"{prefix}:month:{(year if month>1 else year-1)}:{(month-1 if month>1 else 12)}"),b(f"📅 {['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'][month-1]} {year}","noop"),b("➡️",f"{prefix}:month:{(year if month<12 else year+1)}:{(month+1 if month<12 else 1)}")]]
    rows.append([b(x,x) for x in []])
    rows.append([b(x,f"noop") for x in ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]])
    today=datetime.now(TZ).date()
    for w in weeks:
        row=[]
        for n in w:
            if not n: row.append(b(" ","noop")); continue
            d=date(year,month,n)
            label=f"*{n}" if d==today else str(n)
            if date(2026,9,1)<=d<=date(2027,7,31) and not (prefix == "sch" and d.weekday() == 6):
                row.append(b(label,f"{prefix}:date:{d.isoformat()}"))
            else:
                row.append(b("—" if prefix == "sch" and d.weekday() == 6 else label,"noop"))
        rows.append(row)
    back_callback = "adm:back" if prefix == "asch" else "menu:schedule"
    rows.append([b("🏠 Назад", back_callback)])
    return ik(rows)

@router.callback_query(F.data.startswith("sch:cal:"))
async def sch_cal(c:CallbackQuery):
    _,_,y,m=c.data.split(':'); await c.answer(); await edit_or_answer(c,f"🗓 <b>{['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'][int(m)-1]} {y}</b>\nВыберите нужный день:",calendar_kb(int(y),int(m)))
@router.callback_query(F.data.startswith("sch:month:"))
async def sch_month(c:CallbackQuery):
    _,_,y,m=c.data.split(':'); y=int(y);m=int(m)
    if (y,m)<(2026,9): y,m=2026,9
    if (y,m)>(2027,7): y,m=2027,7
    await c.answer(); await edit_or_answer(c,f"🗓 <b>{['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'][m-1]} {y}</b>\nВыберите нужный день:",calendar_kb(y,m))

# Homework
@router.callback_query(F.data=="menu:hw")
async def hw_menu(c:CallbackQuery):
    cdb=db(); rows=cdb.execute("SELECT d.*,COUNT(h.id) n FROM disciplines d JOIN homework h ON h.discipline_id=d.id AND h.hidden=0 AND h.archived=0 GROUP BY d.id ORDER BY d.name",()).fetchall()
    viewed={r['homework_id'] for r in cdb.execute("SELECT homework_id FROM homework_views WHERE telegram_id=?",(c.from_user.id,)).fetchall()}
    fresh={}
    for r in rows:
        ids=cdb.execute("SELECT id FROM homework WHERE discipline_id=? AND hidden=0 AND archived=0",(r['id'],)).fetchall()
        fresh[r['id']]=any(x['id'] not in viewed for x in ids)
    cdb.close(); kb=two_col([b(f"{r['emoji']} {r['name']} {'🔴' if fresh.get(r['id']) else '⚪'}",f"hw:disc:{r['id']}:0") for r in rows]); kb.append([b("🏠 Назад","menu:home")]); await c.answer(); await edit_or_answer(c,"📝 <b>Домашнее задание</b>\n\n🔴 — есть непросмотренное новое ДЗ\n⚪ — все задания уже просмотрены\n\nВыберите дисциплину:",ik(kb))

@router.callback_query(F.data.startswith("hw:disc:"))
async def hw_disc(c:CallbackQuery):
    _,_,did,p=c.data.split(':'); did=int(did); page=int(p); off=page*7
    cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.hidden=0 AND h.archived=0 ORDER BY substr(h.published_date,7,4)||substr(h.published_date,4,2)||substr(h.published_date,1,2), h.id",(did,)).fetchall(); cc.close()
    subset=hs[off:off+7]; rows=[]
    for h in subset:
        title=(h['title'] or 'Задание').strip(); title=title[:48]
        rows.append([b(f"📝 {title} • до {h['due_date']}",f"hw:item:{h['id']}:{did}:{page}")])
    nav=[]
    if page>0: nav.append(b("⬅️ Предыдущая",f"hw:disc:{did}:{page-1}"))
    if off+7<len(hs): nav.append(b("➡️ Следующая",f"hw:disc:{did}:{page+1}"))
    if nav: rows.append(nav)
    rows.append([b("🔙 Назад к дисциплинам","menu:hw")]); await c.answer(); await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>\n\nВыберите задание:",ik(rows))

@router.callback_query(F.data.startswith("hw:item:"))
async def hw_item(c:CallbackQuery):
    _,_,hid,did,page=c.data.split(':'); cc=db(); h=cc.execute("SELECT h.*,d.name discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)).fetchone(); cc.close()
    cc=db(); ex=cc.execute("SELECT title FROM homework_extra WHERE homework_id=?",(hid,)).fetchone(); cc.close(); title=ex['title'] if ex else h['text'].splitlines()[0]
    cc=db(); cc.execute("INSERT OR IGNORE INTO homework_views(telegram_id,homework_id,viewed_at) VALUES(?,?,?)",(c.from_user.id,int(hid),datetime.now(TZ).isoformat())); cc.commit(); cc.close()
    task=h['text']
    if task.startswith(title): task=task[len(title):].lstrip('\n')
    txt=f"{h['emoji']} <b>{h['discipline']}</b>\n\n<b>📝 {title}</b>\n{task}\n\n📅 <b>Дата сдачи:</b> {h['due_date']}\n📤 <b>Опубликовано:</b> {h['published_date']}"
    if h['explanation']: txt+=f"\n\n💬 <b>Пояснение:</b>\n{h['explanation']}"
    kb=[[b("💡 Посмотреть ответ",f"hw:answer:{hid}")],[b("🔙 Назад в дисциплину",f"hw:back:{did}:{page}")]]
    cc=db(); media=cc.execute("SELECT * FROM homework_media WHERE homework_id=? ORDER BY id",(hid,)).fetchall(); cc.close()
    # Telegram messages are ordered by send time: files first, then the action message.
    try:
        if c.message: await c.message.delete()
    except Exception: pass
    await c.answer()
    for x in media:
        try:
            if x['kind']=='photo': await c.bot.send_photo(c.from_user.id,x['file_id'],caption=x['caption'])
            elif x['kind']=='video': await c.bot.send_video(c.from_user.id,x['file_id'],caption=x['caption'])
            else: await c.bot.send_document(c.from_user.id,x['file_id'],caption=x['caption'])
        except Exception: pass
    await show(c.bot,c.from_user.id,txt,ik(kb))

@router.callback_query(F.data.startswith("hw:back:"))
async def hw_back(c:CallbackQuery):
    _,_,did,page=c.data.split(':')
    await c.answer()
    # Files sent after the homework screen are separate Telegram messages.
    # Re-send the list screen so it appears below those files.
    did=int(did); page=int(page); off=page*7
    cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.hidden=0 AND h.archived=0 ORDER BY substr(h.published_date,7,4)||substr(h.published_date,4,2)||substr(h.published_date,1,2), h.id",(did,)).fetchall(); cc.close()
    rows=[]
    for h in hs[off:off+7]:
        rows.append([b(f"📝 {(h['title'] or 'Задание').strip()[:48]} • до {h['due_date']}",f"hw:item:{h['id']}:{did}:{page}")])
    nav=[]
    if page>0: nav.append(b("⬅️ Предыдущая",f"hw:disc:{did}:{page-1}"))
    if off+7<len(hs): nav.append(b("➡️ Следующая",f"hw:disc:{did}:{page+1}"))
    if nav: rows.append(nav)
    rows.append([b("🔙 Назад к дисциплинам","menu:hw")])
    await show(c.bot,c.from_user.id,f"{d['emoji']} <b>{d['name']}</b>\n\nВыберите задание:",ik(rows))

@router.callback_query(F.data.startswith("hw:answer:"))
async def hw_answer(c:CallbackQuery):
    hid=int(c.data.split(':')[-1]); cc=db(); a=cc.execute("SELECT * FROM homework_answer WHERE homework_id=?",(hid,)).fetchone(); cc.close()
    if not is_vip(c.from_user.id): return await c.answer("🔒 Доступ к готовым ответам доступен только VIP-пользователям.",show_alert=True)
    if not a: return await c.answer("⏳ Готовое решение ещё не опубликовано.",show_alert=True)
    await c.answer()
    if a['text']: await c.message.answer("💡 <b>Готовый ответ</b>\n\n"+a['text'])
    elif a['file_id']:
        if a['kind']=='photo': await c.message.answer_photo(a['file_id'],caption=a['caption'] or "💡 Готовый ответ")
        else: await c.message.answer_document(a['file_id'],caption=a['caption'] or "💡 Готовый ответ")

# Books
@router.callback_query(F.data=="menu:books")
async def books_menu(c:CallbackQuery):
    cc=db(); rows=cc.execute("SELECT d.*,COUNT(DISTINCT t.id) n,COUNT(DISTINCT m.id) mn FROM disciplines d LEFT JOIN textbooks t ON t.discipline_id=d.id LEFT JOIN additional_materials m ON m.discipline_id=d.id WHERE d.active=1 GROUP BY d.id HAVING n>0 OR mn>0 ORDER BY d.name").fetchall(); cc.close(); kb=two_col([b(f"{r['emoji']} {r['name']}",f"book:disc:{r['id']}:0") for r in rows]); kb.append([b("🏠 Назад","menu:home")]); await c.answer(); await edit_or_answer(c,"📚 <b>Учебники и материалы</b>\n\nВыберите дисциплину:",ik(kb))
@router.callback_query(F.data.startswith("book:disc:"))
async def books_disc(c:CallbackQuery):
    _,_,did,p=c.data.split(':'); did=int(did);p=int(p);cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ts=cc.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id",(did,)).fetchall();cc.close(); subset=ts[p*7:p*7+7]; rows=[[b(f"📘 {x['title'][:55]}",f"book:item:{x['id']}:{did}:{p}")] for x in subset]; rows.append([b("📎 Дополнительные материалы",f"mat:disc:{did}:0")]); nav=[]
    if p: nav.append(b("⬅️ Предыдущая",f"book:disc:{did}:{p-1}"))
    if p*7+7<len(ts): nav.append(b("➡️ Следующая",f"book:disc:{did}:{p+1}"))
    if nav: rows.append(nav)
    rows.append([b("🔙 Назад к учебникам","menu:books")]); await c.answer(); await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))
@router.callback_query(F.data.startswith("book:item:"))
async def book_item(c:CallbackQuery):
    _,_,bid,did,p=c.data.split(':');cc=db();x=cc.execute("SELECT * FROM textbooks WHERE id=?",(bid,)).fetchone();cc.close();await c.answer();
    # Keep a tiny control screen so the user can return after receiving the file.
    try:
        await c.message.edit_text(f"📘 <b>{x['title']}</b>\n\nФайл учебника отправлен ниже.", reply_markup=ik([[b("🔙 Назад к учебникам",f"book:back:{did}:{p}")]]), parse_mode=ParseMode.HTML)
    except Exception:
        pass
    if x['kind']=='photo': await c.bot.send_photo(c.from_user.id,x['file_id'],caption=f"📘 {x['title']}")
    else: await c.bot.send_document(c.from_user.id,x['file_id'],caption=f"📘 {x['title']}")

@router.callback_query(F.data.startswith("book:back:"))
async def book_back(c:CallbackQuery):
    _,_,did,p=c.data.split(':'); did=int(did); p=int(p); cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ts=cc.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id",(did,)).fetchall(); cc.close()
    subset=ts[p*7:p*7+7]; rows=[[b(f"📘 {x['title'][:55]}",f"book:item:{x['id']}:{did}:{p}")] for x in subset]; rows.append([b("📎 Дополнительные материалы",f"mat:disc:{did}:0")]); nav=[]
    if p: nav.append(b("⬅️ Предыдущая",f"book:disc:{did}:{p-1}"))
    if p*7+7<len(ts): nav.append(b("➡️ Следующая",f"book:disc:{did}:{p+1}"))
    if nav: rows.append(nav)
    rows.append([b("🔙 Назад к учебникам","menu:books")])
    await c.answer(); await show(c.bot,c.from_user.id,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))

# Settings
async def initial_settings_prompt(bot, uid):
    cc=db()
    cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)", (uid,))
    cc.commit()
    r=cc.execute("SELECT setup_done,COALESCE(setup_version,0) setup_version FROM notification_settings WHERE telegram_id=?", (uid,)).fetchone()
    cc.close()
    if not r:
        return False
    if int(r["setup_version"] or 0) < 1:
        await show(bot,uid,"⚙️ <b>Первоначальная настройка</b>\n\nДавайте один раз настроим бота под вас. Вы сможете выбрать уведомления, время напоминаний и другие параметры. Настройки сохраняются и больше не потеряются при следующем /start.",ik([[b("⚙️ Пройти полную настройку","settings:notif:setup")],[b("⏭ Пропустить сейчас","settings:setup_skip")]]))
        return True
    # After the one-time setup, /start always goes straight to the main menu.
    return False

@router.callback_query(F.data=="settings:setup_skip")
async def settings_setup_skip(c:CallbackQuery):
    cc=db(); cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(c.from_user.id,)); cc.execute("UPDATE notification_settings SET setup_done=1,setup_version=1 WHERE telegram_id=?",(c.from_user.id,)); cc.commit(); cc.close(); await c.answer(); await main_menu(c.bot,c.from_user.id)

@router.callback_query(F.data=="start:menu")
async def start_menu_callback(c:CallbackQuery):
    await c.answer(); await main_menu(c.bot,c.from_user.id)

@router.callback_query(F.data=="menu:settings")
async def settings(c:CallbackQuery):
    cc=db(); s=cc.execute("SELECT * FROM notification_settings WHERE telegram_id=?",(c.from_user.id,)).fetchone(); cc.close()
    if not s:
        cc=db(); cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(c.from_user.id,)); cc.commit(); cc.close()
        cc=db(); s=cc.execute("SELECT * FROM notification_settings WHERE telegram_id=?",(c.from_user.id,)).fetchone(); cc.close()
    await c.answer(); await edit_or_answer(c,"⚙️ <b>Настройки</b>\n\nВыберите раздел:",ik([[b("🔔 Уведомления","settings:notif")],[b("🏠 Назад","menu:home")]]))

@router.callback_query(F.data.in_({"settings:notif","settings:notif:setup"}))
async def notification_settings_menu(c:CallbackQuery):
    cc=db(); s=cc.execute("SELECT * FROM notification_settings WHERE telegram_id=?",(c.from_user.id,)).fetchone(); cc.close()
    setup_mode = c.data == "settings:notif:setup" or not s["setup_done"]
    await c.answer(); await edit_or_answer(c,
        ("⚙️ <b>Первоначальная настройка уведомлений</b>\n\n" if setup_mode else "🔔 <b>Уведомления</b>\n\n") +
        f"🔔 Следующая пара: {'✅' if s['next_lesson'] else '❌'}\n"
        f"⏱ Напоминать за: <b>{s['next_lesson_minutes']} мин.</b>\n"
        f"📅 Расписание на завтра: {'✅' if s['tomorrow_schedule'] else '❌'}\n"
        f"🔄 Обновления: {'✅' if s['updates'] else '❌'}\n"
        f"📢 Прочие уведомления: {'✅' if s['other'] else '❌'}\n"
        f"⏰ Напоминания о дедлайнах: {'✅' if s['deadline_reminders'] else '❌'}\n"
        f"📝 Новое ДЗ: {'✅' if s['new_homework'] else '❌'}\n"
        f"🔕 Тихие уведомления: {'✅' if s['silent_notifications'] else '❌'}\n\n"
        "🚨 Важные системные сообщения нельзя отключить.",
        ik([
            [b("🔔 Вкл./выкл. следующую пару","set:next_toggle")],
            [b("⏱ За сколько напоминать","set:next_time")],
            [b("📅 Расписание на завтра","set:tomorrow")],
            [b("🔄 Обновления","set:updates")],
            [b("📢 Прочие уведомления","set:other")],
            [b("⏰ Напоминания о дедлайнах","set:deadline")],
            [b("📝 Новое ДЗ","set:new_homework")],
            [b("🔕 Тихие уведомления","set:silent")],
            ([b("✅ Готово","settings:setup_done")] if setup_mode else [b("🔙 Назад","menu:settings")])
        ]))


@router.callback_query(F.data=="settings:setup_done")
async def settings_setup_done(c:CallbackQuery):
    cc=db(); cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(c.from_user.id,)); cc.execute("UPDATE notification_settings SET setup_done=1,setup_version=1 WHERE telegram_id=?",(c.from_user.id,)); cc.commit(); cc.close(); await c.answer("Настройки сохранены"); await main_menu(c.bot,c.from_user.id)

@router.callback_query(F.data=="set:next_time")
async def next_time_menu(c:CallbackQuery):
    cc=db(); s=cc.execute("SELECT next_lesson_minutes FROM notification_settings WHERE telegram_id=?",(c.from_user.id,)).fetchone(); cc.close()
    current=s['next_lesson_minutes'] if s else 30
    options=[5,10,15,30,45,60,90]
    rows=[]
    for i in range(0,len(options),3):
        rows.append([b(f"{'✅ ' if x==current else ''}{x} мин.",f"set:next_time:{x}") for x in options[i:i+3]])
    rows.append([b("🔙 Назад","settings:notif")])
    await c.answer()
    await edit_or_answer(c,"⏱ <b>За сколько до следующей пары присылать уведомление?</b>\n\nВыберите нужное время:",ik(rows))


@router.callback_query(F.data.startswith("set:next_time:"))
async def set_next_time(c:CallbackQuery):
    minutes=int(c.data.split(':')[-1])
    if minutes not in {5,10,15,30,45,60,90}:
        return await c.answer("Недопустимое время.",show_alert=True)
    cc=db()
    cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(c.from_user.id,))
    cc.execute("UPDATE notification_settings SET next_lesson_minutes=? WHERE telegram_id=?",(minutes,c.from_user.id))
    cc.commit(); cc.close()
    await notification_settings_menu(c)


@router.callback_query(F.data.in_({"set:tomorrow","set:next_toggle","set:updates","set:other","set:deadline","set:new_homework","set:silent"}))
async def toggle(c:CallbackQuery):
    col={"set:tomorrow":"tomorrow_schedule","set:next_toggle":"next_lesson","set:updates":"updates","set:other":"other","set:deadline":"deadline_reminders","set:new_homework":"new_homework","set:silent":"silent_notifications"}[c.data]
    cc=db(); cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(c.from_user.id,)); cc.execute(f"UPDATE notification_settings SET {col}=1-{col} WHERE telegram_id=?",(c.from_user.id,)); cc.commit(); cc.close(); await notification_settings_menu(c)


@router.callback_query(F.data.startswith("mat:disc:"))
async def mat_disc(c:CallbackQuery):
    _,_,did,p=c.data.split(':'); did=int(did); p=int(p); cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ms=cc.execute("SELECT * FROM additional_materials WHERE discipline_id=? ORDER BY id DESC",(did,)).fetchall(); cc.close(); rows=[[b(f"📎 {m['title'][:55]}",f"mat:item:{m['id']}")] for m in ms[p*7:p*7+7]]; rows.append([b("🔙 Назад к дисциплине",f"book:disc:{did}:0")]); await c.answer(); await edit_or_answer(c,f"📎 <b>Дополнительные материалы — {d['name']}</b>",ik(rows))

@router.callback_query(F.data.startswith("mat:item:"))
async def mat_item(c:CallbackQuery):
    mid=int(c.data.split(':')[-1]); cc=db(); m=cc.execute("SELECT * FROM additional_materials WHERE id=?",(mid,)).fetchone(); cc.close();
    if not m: return await c.answer("Материал не найден",show_alert=True)
    await c.answer();
    if m['kind']=='photo': await c.bot.send_photo(c.from_user.id,m['file_id'],caption=m['caption'] or m['title'])
    elif m['kind']=='video': await c.bot.send_video(c.from_user.id,m['file_id'],caption=m['caption'] or m['title'])
    else: await c.bot.send_document(c.from_user.id,m['file_id'],caption=m['caption'] or m['title'])

# Admin helpers
async def admin_only(c):
    if not is_admin(c.from_user.id): await c.answer("⛔ Доступ запрещён.",show_alert=True); return False
    return True

def admin_kb():
    rows = [[b("📊 Обзор и статистика","adm:dashboard")],[b("📝 Новое ДЗ","adm:newhw")],[b("🛠 Управление ДЗ","adm:hw")],[b("📚 Управление учебниками","adm:books")],[b("📎 Доп. материалы","adm:materials")],[b("📅 Управление расписанием","adm:schedule")],[b("👥 Управление студентами","adm:students")],[b("⭐ Управление VIP","adm:vip")],[b("📖 Дисциплины","adm:disc")],[b("📢 Уведомление всем","adm:broadcast")]]
    if MINI_APP_URL:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель Mini App", web_app=WebAppInfo(url=MINI_APP_URL + ("&" if "?" in MINI_APP_URL else "?") + "admin=1") )])
    rows.append([b("🏠 Главное меню","menu:home")])
    return ik(rows)

@router.callback_query(F.data.startswith("adm:"))
async def admin_router(c:CallbackQuery,state:FSMContext):
    if not await admin_only(c): return
    act=c.data
    if act=='adm:back':
        await state.clear()
        await c.answer()
        return await edit_or_answer(c, "🛠 <b>Админ-панель</b>\n\nВыберите действие:", admin_kb())
    if act=='adm:dashboard': return await admin_dashboard(c)
    if act=='adm:newhw': return await newhw_start(c,state)
    if act=='adm:hw': return await admin_hw(c)
    if act=='adm:books': return await admin_books(c)
    if act=='adm:materials': return await admin_materials(c)
    if act=='adm:schedule': return await admin_schedule(c)
    if act=='adm:students': return await admin_students(c)
    if act=='adm:vip': return await admin_vip(c)
    if act=='adm:disc': return await admin_disc(c)
    if act=='adm:broadcast': return await broadcast_start(c,state)

async def newhw_start(c,state):
    cc=db(); ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall();cc.close(); await state.set_state(HWCreate.discipline); await c.answer(); await edit_or_answer(c,"📝 <b>Новое ДЗ</b>\n\nВыберите дисциплину:",ik(two_col([b(f"{d['emoji']} {d['name']}",f"newhw:d:{d['id']}") for d in ds])+[[b("❌ Отмена","adm:back")]]))
@router.callback_query(F.data.startswith("newhw:d:"),HWCreate.discipline)
async def newhw_d(c,state): await state.update_data(discipline=int(c.data.split(':')[-1])); await state.set_state(HWCreate.title); await c.answer(); await show(c.bot,c.from_user.id,"Введите название ДЗ:")
@router.message(HWCreate.title)
async def newhw_title(m,state): await state.update_data(title=m.text); await state.set_state(HWCreate.task); await show(m.bot,m.from_user.id,"Введите текст задания:")
@router.message(HWCreate.task)
async def newhw_task(m,state):
    await state.update_data(task=m.text)
    data=await state.get_data()
    cc=db(); rows=cc.execute("""SELECT sd.day,l.lesson_type,d.name discipline FROM schedule_lessons l JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id WHERE l.discipline_id=? AND sd.day>=? ORDER BY sd.day,l.start,l.lesson_no LIMIT 20""",(data['discipline'],date.today().isoformat())).fetchall(); cc.close()
    grouped=[]
    for r in rows:
        if not grouped or grouped[-1][0]!=r['day']:
            grouped.append([r['day'],[]])
        if r['lesson_type'] not in grouped[-1][1]: grouped[-1][1].append(r['lesson_type'])
    buttons=[]
    for day,types in grouped[:12]:
        buttons.append([b(f"📅 {datetime.fromisoformat(day).strftime('%d.%m')} · {' / '.join(types)}",f"newhw:due:{day}")])
    buttons.append([b("📅 Своя дата","newhw:custom_due")])
    await state.set_state(HWCreate.due); await show(m.bot,m.from_user.id,"📅 <b>Выберите дату сдачи</b> по расписанию этой дисциплины:",ik(buttons))

@router.callback_query(F.data.startswith("newhw:due:"),HWCreate.due)
async def newhw_due_pick(c,state):
    day=c.data.split(':',2)[2]
    try: d=date.fromisoformat(day)
    except ValueError: return await c.answer("Неверная дата",show_alert=True)
    await state.update_data(due=d.strftime('%d.%m.%Y')); await state.set_state(HWCreate.explanation); await c.answer(); await show(c.bot,c.from_user.id,"Введите пояснение или отправьте <code>-</code>:")

@router.callback_query(F.data=="newhw:custom_due",HWCreate.due)
async def newhw_custom_due(c,state):
    await c.answer(); await show(c.bot,c.from_user.id,"📅 Введите свою дату сдачи в формате <code>ДД.ММ.ГГГГ</code>:")

@router.message(HWCreate.due)
async def newhw_due(m,state):
    try: d=datetime.strptime(m.text.strip(),'%d.%m.%Y').date()
    except ValueError: return await m.answer("❌ Неверный формат. Пример: <code>28.09.2026</code>")
    await state.update_data(due=d.strftime('%d.%m.%Y'));await state.set_state(HWCreate.explanation);await show(m.bot,m.from_user.id,"Введите пояснение или отправьте <code>-</code>:")
@router.message(HWCreate.explanation)
async def newhw_expl(m,state): await state.update_data(explanation='' if (m.text or '').strip()=='-' else m.text); await state.set_state(HWCreate.media); await show(m.bot,m.from_user.id,"📎 Прикрепите файл(ы) по одному сообщению или нажмите «Без файлов».",ik([[b("Без файлов","newhw:no_media")],[b("Готово","newhw:done_media")]]))

def media_info(m):
    if m.document: return ('document',m.document.file_id,m.document.file_name)
    if m.photo: return ('photo',m.photo[-1].file_id,'')
    if m.video: return ('video',m.video.file_id,'')
    return None


class PendingHomeworkFileFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not media_info(message):
            return False
        cc = db()
        row = cc.execute("SELECT 1 FROM homework_upload_requests WHERE telegram_id=? AND status='waiting'", (message.from_user.id,)).fetchone()
        cc.close()
        return bool(row)

@router.message(PendingHomeworkFileFilter())
async def receive_miniapp_homework_file(m: Message):
    x = media_info(m)
    cc = db()
    req = cc.execute("SELECT homework_id FROM homework_upload_requests WHERE telegram_id=? AND status='waiting'", (m.from_user.id,)).fetchone()
    if not req:
        cc.close(); return
    cc.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption) VALUES(?,?,?,?)", (req['homework_id'], x[0], x[1], x[2]))
    cc.execute("UPDATE homework_upload_requests SET status='done' WHERE telegram_id=?", (m.from_user.id,))
    cc.commit(); cc.close()
    await m.answer("✅ Файл прикреплён к ДЗ. Теперь он отображается у студентов в Mini App.")
@router.message(HWCreate.media)
async def newhw_media(m,state):
    x=media_info(m)
    if not x: return await m.answer("Пришли документ/фото/видео или нажми «Готово».")
    data=await state.get_data(); arr=data.get('media',[]);arr.append(x);await state.update_data(media=arr)
    # Keep the control message below the newly attached file.
    await show(m.bot,m.from_user.id,f"✅ Файл добавлен. Всего: {len(arr)}. Можно отправить ещё или нажать «Готово».",ik([[b("Без файлов","newhw:no_media")],[b("Готово","newhw:done_media")]]))
@router.callback_query(F.data.in_({"newhw:no_media","newhw:done_media"}),HWCreate.media)
async def newhw_preview(c,state):
    data=await state.get_data();cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(data['discipline'],)).fetchone();cc.close(); txt=f"📝 <b>Предпросмотр ДЗ</b>\n\n{d['emoji']} <b>{d['name']}</b>\n<b>{data['title']}</b>\n\n{data['task']}\n\n📅 Сдать до: {data['due']}\n📤 Опубликовано: {date.today().strftime('%d.%m.%Y')}\n\n💬 {data['explanation'] or 'Без пояснения'}\n\n📎 Файлов: {len(data.get('media',[]))}";await state.set_state(HWCreate.preview);await c.answer();await edit_or_answer(c,txt,ik([[b("✅ Опубликовать","newhw:publish")],[b("🔄 Заполнить заново","adm:newhw")],[b("❌ Отмена","adm:back")]]))
@router.callback_query(F.data=="newhw:publish",HWCreate.preview)
async def newhw_publish(c,state):
    data=await state.get_data();now=datetime.now(TZ).date().strftime('%d.%m.%Y');cc=db();cur=cc.cursor();cur.execute("INSERT INTO homework(discipline_id,text,explanation,published_date,due_date) VALUES(?,?,?,?,?)",(data['discipline'],data['task'],data.get('explanation',''),now,data['due']));hid=cur.lastrowid
    # title is stored as the first line so old schema stays intact
    cur.execute("INSERT INTO homework_extra(homework_id,title) VALUES(?,?)",(hid,data['title']))
    for kind,fid,caption in data.get('media',[]): cur.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption) VALUES(?,?,?,?)",(hid,kind,fid,caption))
    cc.commit();cc.close();await state.clear();await c.answer("Опубликовано!");await show(c.bot,c.from_user.id,"✅ ДЗ опубликовано.\n\n📢 Авторизованные пользователи получат уведомление «📝 Новое ДЗ».",admin_kb());await broadcast_new_hw(c.bot,hid)

async def broadcast_new_hw(bot,hid):
    cc=db(); h=cc.execute("SELECT h.*,d.name discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)).fetchone(); ex=cc.execute("SELECT title FROM homework_extra WHERE homework_id=?",(hid,)).fetchone(); users=cc.execute("SELECT s.telegram_id FROM students s JOIN notification_settings n ON n.telegram_id=s.telegram_id WHERE n.new_homework=1").fetchall(); cc.close()
    if not h: return
    title=ex['title'] if ex else 'Задание'
    text=f"📝 <b>Новое ДЗ</b>\n\n{h['emoji']} <b>{h['discipline']}</b>\n<b>{title}</b>\n\n{h['text']}\n\n📅 <b>Сдать до:</b> {h['due_date']}\n📤 <b>Опубликовано:</b> {h['published_date']}"
    if h['explanation']: text+=f"\n\n💬 <b>Пояснение:</b>\n{h['explanation']}"
    kb=ik([[b("📖 Открыть ДЗ",f"hw:item:{hid}:{h['discipline_id']}:0")]])
    for u in users:
        try: await bot.send_message(u['telegram_id'],text,reply_markup=kb)
        except Exception: pass

@router.callback_query(F.data=="adm:back")
async def adm_back(c:CallbackQuery,state:FSMContext): await state.clear();await c.answer();await edit_or_answer(c,"🛠 <b>Админ-панель</b>",admin_kb())

# Admin HW
async def admin_hw(c):
    cc=db();ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall();cc.close();await c.answer();await edit_or_answer(c,"🛠 <b>Управление ДЗ</b>\n\nВыберите дисциплину:",ik(two_col([b(f"{d['emoji']} {d['name']}",f"ahwd:{d['id']}:0") for d in ds])+[[b("🏠 Админ-панель","adm:back")]]))
@router.callback_query(F.data.startswith("ahwd:"))
async def ahwd(c):
    _,did,p=c.data.split(':');did=int(did);p=int(p);cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone();hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.archived=0 ORDER BY substr(h.published_date,7,4)||substr(h.published_date,4,2)||substr(h.published_date,1,2), h.id",(did,)).fetchall();cc.close();rows=[[b(f"📝 {h['title'][:45]} • {h['due_date']}",f"ahwi:{h['id']}:{did}:{p}")] for h in hs[p*7:p*7+7]];rows.append([b("📦 Архив",f"ahwa:{did}:0")]);rows.append([b("🔙 Назад","adm:hw")]);await c.answer();await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))
async def ahwd_for(c:CallbackQuery,did:int,p:int):
    cc=db()
    d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone()
    hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.archived=0 ORDER BY substr(h.published_date,7,4)||substr(h.published_date,4,2)||substr(h.published_date,1,2), h.id",(did,)).fetchall()
    cc.close()
    if not d:
        return await edit_or_answer(c,"❌ Дисциплина не найдена.",ik([[b("🔙 Админ-панель","adm:back")]]))
    rows=[[b(f"📝 {h['title'][:45]} • {h['due_date']}",f"ahwi:{h['id']}:{did}:{p}")] for h in hs[p*7:p*7+7]]
    rows.append([b("📦 Архив",f"ahwa:{did}:0")]); rows.append([b("🔙 Назад","adm:hw")])
    await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))

async def ahwa_for(c:CallbackQuery,did:int,p:int):
    cc=db()
    d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone()
    hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.archived=1 ORDER BY substr(h.published_date,7,4)||substr(h.published_date,4,2)||substr(h.published_date,1,2), h.id",(did,)).fetchall()
    cc.close()
    if not d:
        return await edit_or_answer(c,"❌ Дисциплина не найдена.",ik([[b("🔙 Админ-панель","adm:back")]]))
    rows=[[b(f"📦 {h['title'][:45]} • {h['due_date']}",f"ahwiarch:{h['id']}:{did}:{p}")] for h in hs[p*7:p*7+7]]
    rows.append([b("🔙 Назад",f"ahwd:{did}:0")])
    await edit_or_answer(c,f"📦 <b>Архив — {d['name']}</b>",ik(rows))

@router.callback_query(F.data.startswith("ahwa:"))
async def ahwa(c:CallbackQuery):
    _,did,p=c.data.split(':'); did=int(did); p=int(p)
    cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone()
    hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.archived=1 ORDER BY substr(h.published_date,7,4)||substr(h.published_date,4,2)||substr(h.published_date,1,2), h.id",(did,)).fetchall()
    cc.close()
    if not d:
        await c.answer("Дисциплина не найдена",show_alert=True); return
    rows=[[b(f"📦 {h['title'][:45]} • {h['due_date']}",f"ahwiarch:{h['id']}:{did}:{p}")] for h in hs[p*7:p*7+7]]
    rows.append([b("🔙 Назад",f"ahwd:{did}:0")])
    await c.answer()
    await edit_or_answer(c,f"📦 <b>Архив — {d['name']}</b>",ik(rows))

@router.callback_query(F.data.startswith("ahwiarch:"))
async def ahwiarch(c:CallbackQuery):
    _,hid,did,p=c.data.split(':')
    cc=db()
    h=cc.execute("SELECT h.*,d.name discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)).fetchone()
    ex=cc.execute("SELECT title FROM homework_extra WHERE homework_id=?",(hid,)).fetchone()
    media=cc.execute("SELECT id,kind,caption,file_id FROM homework_media WHERE homework_id=? ORDER BY id",(hid,)).fetchall()
    cc.close()
    if not h:
        await c.answer("ДЗ уже удалено.",show_alert=True)
        return await ahwa(c)
    title=ex['title'] if ex and ex['title'] else ((h['text'] or 'Задание').splitlines()[0] if h['text'] else 'Задание')
    text=f"📦 {h['emoji']} <b>{h['discipline']}</b>\n\n<b>{title}</b>\n{h['text'] or '—'}\n\n📅 {h['due_date']}"
    buttons=[[b("♻️ Вернуть из архива",f"ahwunarch:{hid}:{did}:{p}")],
             [b("🗑 Удалить",f"ahwdelarch:{hid}:{did}:{p}")],
             [b("🔙 Назад",f"ahwa:{did}:{p}")]]
    if media:
        try:
            if c.message: await c.message.delete()
        except Exception: pass
        for m in media:
            try:
                cap=m['caption'] or title
                if m['kind']=='photo': await c.bot.send_photo(c.from_user.id,m['file_id'],caption=cap)
                elif m['kind']=='video': await c.bot.send_video(c.from_user.id,m['file_id'],caption=cap)
                else: await c.bot.send_document(c.from_user.id,m['file_id'],caption=cap)
            except Exception: pass
        await show(c.bot,c.from_user.id,text,ik(buttons))
    else:
        await c.answer()
        await edit_or_answer(c,text,ik(buttons))

@router.callback_query(F.data.startswith("ahwunarch:"))
async def ahwunarch(c:CallbackQuery):
    _,hid,did,p=c.data.split(':')
    cc=db(); cc.execute("UPDATE homework SET archived=0 WHERE id=?",(hid,)); cc.commit(); cc.close()
    await c.answer("Возвращено из архива")
    await ahwa(c)

async def _render_homework_admin(c:CallbackQuery,hid:int,did:int,p:int):
    await c.answer()
    cc=db()
    h=cc.execute("SELECT h.*,d.name discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)).fetchone()
    ex=cc.execute("SELECT title FROM homework_extra WHERE homework_id=?",(hid,)).fetchone()
    media=cc.execute("SELECT id,kind,caption,file_id FROM homework_media WHERE homework_id=? ORDER BY id",(hid,)).fetchall()
    cc.close()
    if not h:
        await c.answer("ДЗ уже удалено.",show_alert=True)
        return await ahwd(c)
    title=ex['title'] if ex and ex['title'] else ((h['text'] or 'Задание').splitlines()[0] if h['text'] else 'Задание')
    text=f"{h['emoji']} <b>{h['discipline']}</b>\n\n<b>{title}</b>\n{h['text'] or '—'}\n\n📅 {h['due_date']}\n📤 {h['published_date']}\n💬 {h['explanation'] or '—'}"
    buttons=[[b("✏️ Редактировать",f"ahwe:{hid}")],
             [b("📎 Управление файлами",f"ahwmedia:{hid}:{did}:{p}:0")],
             [b("🗑 Удалить",f"ahwdel:{hid}:{did}:{p}")],
             [b("📦 В архив",f"ahwarch:{hid}:{did}:{p}")],
             [b("🔙 Назад",f"ahwd:{did}:{p}")]]
    if media:
        try:
            if c.message: await c.message.delete()
        except Exception: pass
        for m in media:
            try:
                cap=m['caption'] or title
                if m['kind']=='photo': await c.bot.send_photo(c.from_user.id,m['file_id'],caption=cap)
                elif m['kind']=='video': await c.bot.send_video(c.from_user.id,m['file_id'],caption=cap)
                else: await c.bot.send_document(c.from_user.id,m['file_id'],caption=cap)
            except Exception: pass
        await show(c.bot,c.from_user.id,text,ik(buttons))
    else:
        await edit_or_answer(c,text,ik(buttons))

@router.callback_query(F.data.startswith("ahwi:"))
async def ahwi(c:CallbackQuery):
    _,hid,did,p=c.data.split(':')
    await _render_homework_admin(c,int(hid),int(did),int(p))

@router.callback_query(F.data.startswith("ahwmedia:"))
async def ahwmedia(c):
    _,hid,did,p,mid=c.data.split(':'); hid=int(hid); did=int(did); p=int(p); mid=int(mid)
    cc=db(); rows=cc.execute("SELECT id,kind,caption FROM homework_media WHERE homework_id=? ORDER BY id",(hid,)).fetchall(); cc.close()
    if mid:
        await c.answer(); await edit_or_answer(c,"⚠️ <b>Удалить этот файл?</b>",ik([[b("🗑 Да, удалить",f"ahwmedia_del:{hid}:{did}:{p}:{mid}")],[b("❌ Отмена",f"ahwmedia:{hid}:{did}:{p}:0")]])); return
    buttons=[[b(f"🗑 {i+1}. {m['caption'] or m['kind']}",f"ahwmedia:{hid}:{did}:{p}:{m['id']}")] for i,m in enumerate(rows)]
    buttons.append([b("➕ Добавить файл",f"ahwe:{hid}")]); buttons.append([b("🔙 Назад",f"ahwi:{hid}:{did}:{p}")])
    await c.answer(); await edit_or_answer(c,"📎 <b>Файлы ДЗ</b>\n\nНажмите на файл, чтобы удалить его.",ik(buttons))

@router.callback_query(F.data.startswith("ahwmedia_del:"))
async def ahwmedia_del(c):
    _,hid,did,p,mid=c.data.split(':')
    cc=db(); cc.execute("DELETE FROM homework_media WHERE id=? AND homework_id=?",(mid,hid)); cc.commit(); cc.close()
    await c.answer("Файл удалён")
    cc=db(); rows=cc.execute("SELECT id,kind,caption FROM homework_media WHERE homework_id=? ORDER BY id",(hid,)).fetchall(); cc.close()
    buttons=[[b(f"🗑 {i+1}. {m['caption'] or m['kind']}",f"ahwmedia:{hid}:{did}:{p}:{m['id']}")] for m in rows]
    buttons.append([b("➕ Добавить файл",f"ahwe:{hid}")]); buttons.append([b("🔙 Назад",f"ahwi:{hid}:{did}:{p}")])
    await edit_or_answer(c,"📎 <b>Файлы ДЗ</b>\n\nНажмите на файл, чтобы удалить его.",ik(buttons))

@router.callback_query(F.data.startswith("ahwdel:"))
async def ahwdel(c):
    _,hid,did,p=c.data.split(':')
    await c.answer()
    await edit_or_answer(c,"⚠️ <b>Удалить ДЗ безвозвратно?</b>\nЭто удалит и прикреплённые файлы, и готовый ответ.",ik([[b("🗑 Да, удалить",f"ahwdelok:{hid}:{did}:{p}")],[b("❌ Отмена",f"ahwi:{hid}:{did}:{p}")]]))

@router.callback_query(F.data.startswith("ahwdelok:"))
async def ahwdelok(c):
    _,hid,did,p=c.data.split(':')
    cc=db(); cc.execute("DELETE FROM homework WHERE id=?",(hid,)); cc.commit(); cc.close()
    await c.answer("Удалено")
    # Do not mutate frozen aiogram CallbackQuery; render the target list directly.
    await ahwd_for(c,int(did),int(p))

@router.callback_query(F.data.startswith("ahwdelarch:"))
async def ahwdelarch(c):
    _,hid,did,p=c.data.split(':')
    cc=db(); cc.execute("DELETE FROM homework WHERE id=?",(hid,)); cc.commit(); cc.close()
    await c.answer("Удалено")
    await ahwa_for(c,int(did),int(p))

@router.callback_query(F.data.startswith("ahwarch:"))
async def ahwarch(c):
    _,hid,did,p=c.data.split(':')
    cc=db(); cc.execute("UPDATE homework SET archived=1 WHERE id=?",(hid,)); cc.commit(); cc.close()
    await c.answer("Перемещено в архив")
    await ahwd_for(c,int(did),int(p))

# Homework editor
@router.callback_query(F.data.startswith("ahwe:"))
async def ahwe(c:CallbackQuery,state:FSMContext):
    hid=int(c.data.split(':')[-1]); await state.update_data(hid=hid); await c.answer()
    await edit_or_answer(c,"✏️ <b>Что изменить?</b>",ik([[b("Название","edit:title")],[b("Задание","edit:task")],[b("Дата сдачи","edit:due")],[b("Дата публикации","edit:pub")],[b("Пояснение","edit:expl")],[b("📎 Файлы ДЗ","edit:media")],[b("💡 Готовый ответ","edit:answer")],[b("🔙 Назад",f"ahwi:{hid}:0:0")]]))

@router.callback_query(F.data.startswith("edit:"))
async def edit_field(c:CallbackQuery,state:FSMContext):
    field=c.data.split(':',1)[1]
    if field=='cancel': return await edit_cancel(c,state)
    prompts={"title":"✏️ Введите новое <b>название ДЗ</b>:","task":"📝 Введите новый <b>текст задания</b>:","due":"📅 Введите новую <b>дату сдачи</b> строго в формате <code>ДД.ММ.ГГГГ</code>:","pub":"📤 Введите новую <b>дату публикации</b> строго в формате <code>ДД.ММ.ГГГГ</code>:","expl":"💬 Введите новое <b>пояснение</b> или отправьте <code>-</code> для его удаления:","media":"📎 Отправьте <b>новый файл ДЗ</b> (документ, фото или видео). Он будет добавлен к существующим файлам:","answer":"💡 Отправьте <b>готовый ответ</b> — текст или документ/фото."}
    if field not in prompts: return await c.answer("Неизвестное поле",show_alert=True)
    await state.update_data(field=field); await state.set_state(HWEdit.value); await c.answer(); await show(c.bot,c.from_user.id,prompts[field],ik([[b("❌ Отмена","edit:cancel")]]))

async def edit_cancel(c:CallbackQuery,state:FSMContext):
    data=await state.get_data(); hid=data.get('hid'); await state.clear(); await c.answer("Отменено")
    if not hid: return await edit_or_answer(c,"🛠 <b>Управление ДЗ</b>",ik([[b("🔙 Назад","adm:hw")]]))
    cc=db(); h=cc.execute("SELECT h.*,d.name discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)).fetchone(); ex=cc.execute("SELECT title FROM homework_extra WHERE homework_id=?",(hid,)).fetchone(); cc.close()
    if not h: return await edit_or_answer(c,"❌ ДЗ не найдено.",ik([[b("🔙 К управлению ДЗ","adm:hw")]]))
    title=ex['title'] if ex else 'Задание'; txt=f"{h['emoji']} <b>{h['discipline']}</b>\n\n<b>{title}</b>\n{h['text']}\n\n📅 {h['due_date']}\n📤 {h['published_date']}\n💬 {h['explanation'] or '—'}"
    await edit_or_answer(c,txt,ik([[b("✏️ Редактировать",f"ahwe:{hid}")],[b("🔙 Назад","adm:hw")]]))

@router.callback_query(F.data=="edit:cancel")
async def edit_cancel_callback(c:CallbackQuery,state:FSMContext): await edit_cancel(c,state)

def parse_hw_date(value):
    try: return datetime.strptime((value or '').strip(),'%d.%m.%Y').date()
    except (TypeError,ValueError): return None

@router.message(HWEdit.value)
async def edit_value(m:Message,state:FSMContext):
    data=await state.get_data(); hid=data.get('hid'); field=data.get('field')
    if not hid or not field: await state.clear(); return await m.answer("❌ Редактирование сброшено.")
    cc=db()
    if field=='answer':
        if m.document: cc.execute("INSERT INTO homework_answer(homework_id,kind,file_id) VALUES(?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind=excluded.kind,file_id=excluded.file_id,text=NULL",(hid,'document',m.document.file_id))
        elif m.photo: cc.execute("INSERT INTO homework_answer(homework_id,kind,file_id) VALUES(?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind=excluded.kind,file_id=excluded.file_id,text=NULL",(hid,'photo',m.photo[-1].file_id))
        elif m.text and m.text.strip(): cc.execute("INSERT INTO homework_answer(homework_id,kind,text) VALUES(?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind=excluded.kind,text=excluded.text,file_id=NULL",(hid,'text',m.text.strip()))
        else: cc.close(); return await m.answer("❌ Для готового ответа отправь текст, документ или фото.")
    elif field=='title':
        if not m.text or not m.text.strip(): cc.close(); return await m.answer("❌ Название не может быть пустым.")
        cc.execute("INSERT INTO homework_extra(homework_id,title) VALUES(?,?) ON CONFLICT(homework_id) DO UPDATE SET title=excluded.title",(hid,m.text.strip()))
    elif field=='task':
        if not m.text or not m.text.strip(): cc.close(); return await m.answer("❌ Текст задания не может быть пустым.")
        cc.execute("UPDATE homework SET text=? WHERE id=?",(m.text.strip(),hid))
    elif field in ('due','pub'):
        d=parse_hw_date(m.text)
        if not d: cc.close(); return await m.answer("❌ Неверный формат. Используй строго <code>ДД.ММ.ГГГГ</code>, например <code>28.09.2026</code>.")
        col='due_date' if field=='due' else 'published_date'; cc.execute(f"UPDATE homework SET {col}=? WHERE id=?",(d.strftime('%d.%m.%Y'),hid))
    elif field=='expl':
        if not m.text: cc.close(); return await m.answer("❌ Отправь текст пояснения или <code>-</code>.")
        cc.execute("UPDATE homework SET explanation=? WHERE id=?",('' if m.text.strip()=='-' else m.text.strip(),hid))
    elif field=='media':
        x=media_info(m)
        if not x: cc.close(); return await m.answer("❌ Отправь документ, фото или видео.")
        cc.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption) VALUES(?,?,?,?)",(hid,x[0],x[1],x[2]))
    cc.commit(); cc.close(); await state.clear(); await m.answer("✅ Изменение сохранено.")

# Admin books
async def admin_dashboard(c):
    cc=db()
    students=cc.execute("SELECT COUNT(*) n FROM students").fetchone()['n']
    auth=cc.execute("SELECT COUNT(*) n FROM students WHERE telegram_id IS NOT NULL").fetchone()['n']
    disciplines=cc.execute("SELECT COUNT(*) n FROM disciplines WHERE active=1").fetchone()['n']
    hw=cc.execute("SELECT COUNT(*) n FROM homework WHERE hidden=0 AND archived=0").fetchone()['n']
    archived=cc.execute("SELECT COUNT(*) n FROM homework WHERE archived=1").fetchone()['n']
    books=cc.execute("SELECT COUNT(*) n FROM textbooks").fetchone()['n']
    mats=cc.execute("SELECT COUNT(*) n FROM additional_materials").fetchone()['n']
    days=cc.execute("SELECT COUNT(*) n FROM schedule_days").fetchone()['n']
    lessons=cc.execute("SELECT COUNT(*) n FROM schedule_lessons").fetchone()['n']
    grades=cc.execute("SELECT COUNT(*) n FROM grades").fetchone()['n']
    events=cc.execute("SELECT COUNT(*) n FROM event_feed").fetchone()['n']
    cc.close()
    await c.answer()
    await edit_or_answer(c,f"📊 <b>Обзор и статистика</b>\n\n"
        f"👥 Студентов: <b>{students}</b>\n🟢 Авторизовано: <b>{auth}</b>\n"
        f"📖 Дисциплин: <b>{disciplines}</b>\n📝 Активных ДЗ: <b>{hw}</b>\n📦 ДЗ в архиве: <b>{archived}</b>\n"
        f"📚 Учебников: <b>{books}</b>\n📎 Доп. материалов: <b>{mats}</b>\n"
        f"📅 Дней с расписанием: <b>{days}</b>\n🎓 Пар в расписании: <b>{lessons}</b>\n"
        f"📊 Оценок в кеше: <b>{grades}</b>\n🔔 Событий: <b>{events}</b>",
        ik([[b("🔙 Админ-панель","adm:back")]]))

async def admin_materials(c):
    cc=db(); ds=cc.execute("SELECT d.*,COUNT(m.id) n FROM disciplines d LEFT JOIN additional_materials m ON m.discipline_id=d.id WHERE d.active=1 GROUP BY d.id ORDER BY d.name").fetchall(); cc.close(); await c.answer(); await edit_or_answer(c,"📎 <b>Дополнительные материалы</b>\n\nВыберите дисциплину:",ik(two_col([b(f"{d['emoji']} {d['name']} ({d['n']})",f"amat:d:{d['id']}") for d in ds])+[[b("🔙 Админ-панель","adm:back")]]))

@router.callback_query(F.data.startswith("amat:d:"))
async def amat_d(c:CallbackQuery,state:FSMContext):
    did=int(c.data.split(':')[-1]); cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ms=cc.execute("SELECT * FROM additional_materials WHERE discipline_id=? ORDER BY id DESC",(did,)).fetchall(); cc.close(); rows=[[b(f"📎 {m['title'][:50]}",f"amat:item:{m['id']}:{did}")] for m in ms]; rows.append([b("➕ Добавить материал",f"amat:add:{did}")]); rows.append([b("🔙 Назад","adm:materials")]); await c.answer(); await edit_or_answer(c,f"📎 <b>{d['name']}</b>",ik(rows))

@router.callback_query(F.data.startswith("amat:add:"))
async def amat_add(c:CallbackQuery,state:FSMContext):
    did=int(c.data.split(':')[-1]); await state.update_data(did=did); await state.set_state(MaterialCreate.title); await c.answer(); await show(c.bot,c.from_user.id,"Введите название дополнительного материала:")

@router.message(MaterialCreate.title)
async def amat_title(m:Message,state:FSMContext):
    if not (m.text or '').strip(): return await m.answer("❌ Название не может быть пустым.")
    data=await state.get_data()
    if data.get('material_rename') and data.get('mid'):
        cc=db(); cc.execute("UPDATE additional_materials SET title=? WHERE id=?",(m.text.strip(),data['mid'])); cc.commit(); cc.close(); mid=data['mid']; did=data['did']; await state.clear()
        return await show(m.bot,m.from_user.id,"✅ Название изменено.",ik([[b("🔙 К материалу",f"amat:item:{mid}:{did}")],[b("📎 Все материалы",f"amat:d:{did}")],[b("🏠 Админ-панель","adm:back")]]))
    await state.update_data(title=m.text.strip()); await state.set_state(MaterialCreate.file); await m.answer("📎 Отправьте документ, фото или видео:")

@router.message(MaterialCreate.file)
async def amat_file(m:Message,state:FSMContext):
    x=media_info(m)
    if not x: return await m.answer("❌ Отправьте документ, фото или видео.")
    data=await state.get_data(); cc=db()
    if data.get('material_change_file') and data.get('mid'):
        cc.execute("UPDATE additional_materials SET kind=?,file_id=?,caption=? WHERE id=?",(x[0],x[1],x[2],data['mid'])); msg="✅ Файл материала заменён."
    else:
        cc.execute("INSERT INTO additional_materials(discipline_id,title,kind,file_id,caption,created_at) VALUES(?,?,?,?,?,?)",(data['did'],data['title'],x[0],x[1],x[2],datetime.now(TZ).isoformat())); msg="✅ Материал добавлен."
    cc.commit(); cc.close(); did=data['did']; await state.clear()
    await show(m.bot,m.from_user.id,msg,ik([[b("📎 Все материалы",f"amat:d:{did}")],[b("🏠 Админ-панель","adm:back")]]))

@router.callback_query(F.data.startswith("amat:item:"))
async def amat_item(c:CallbackQuery):
    _,_,mid,did=c.data.split(':'); cc=db(); m=cc.execute("SELECT * FROM additional_materials WHERE id=?",(mid,)).fetchone(); cc.close()
    if not m: return await c.answer("Материал не найден.",show_alert=True)
    await c.answer(); await edit_or_answer(c,f"📎 <b>{m['title']}</b>",ik([[b("✏️ Изменить название",f"amat:rename:{mid}:{did}")],[b("📎 Поменять файл",f"amat:file:{mid}:{did}")],[b("🗑 Удалить",f"amat:del:{mid}:{did}")],[b("🔙 Назад",f"amat:d:{did}")]]))

@router.callback_query(F.data.startswith("amat:rename:"))
async def amat_rename(c:CallbackQuery,state:FSMContext):
    _,_,mid,did=c.data.split(':'); cc=db(); m=cc.execute("SELECT title FROM additional_materials WHERE id=?",(mid,)).fetchone(); cc.close()
    await state.update_data(mid=int(mid),did=int(did),material_rename=True); await state.set_state(MaterialCreate.title); await c.answer(); await show(c.bot,c.from_user.id,f"✏️ Введите новое название материала:\n\n<b>{m['title'] if m else ''}</b>",ik([[b("❌ Отмена",f"amat:item:{mid}:{did}")]]))

@router.callback_query(F.data.startswith("amat:file:"))
async def amat_file_start(c:CallbackQuery,state:FSMContext):
    _,_,mid,did=c.data.split(':'); await state.update_data(mid=int(mid),did=int(did),material_change_file=True); await state.set_state(MaterialCreate.file); await c.answer(); await show(c.bot,c.from_user.id,"📎 Отправьте новый файл дополнительного материала:",ik([[b("❌ Отмена",f"amat:item:{mid}:{did}")]]))

@router.callback_query(F.data.startswith("amat:del:"))
async def amat_del(c:CallbackQuery):
    _,_,mid,did=c.data.split(':'); await c.answer(); await edit_or_answer(c,"⚠️ <b>Удалить материал?</b>",ik([[b("🗑 Да, удалить",f"amat:delok:{mid}:{did}")],[b("❌ Отмена",f"amat:item:{mid}:{did}")]]))

@router.callback_query(F.data.startswith("amat:delok:"))
async def amat_delok(c:CallbackQuery):
    _,_,mid,did=c.data.split(':'); cc=db(); cc.execute("DELETE FROM additional_materials WHERE id=?",(mid,)); cc.commit(); cc.close(); await c.answer("Удалено")
    cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ms=cc.execute("SELECT * FROM additional_materials WHERE discipline_id=? ORDER BY id DESC",(did,)).fetchall(); cc.close(); rows=[[b(f"📎 {m['title'][:50]}",f"amat:item:{m['id']}:{did}")] for m in ms]; rows.append([b("➕ Добавить материал",f"amat:add:{did}")]); rows.append([b("🔙 Назад","adm:materials")]); await edit_or_answer(c,f"📎 <b>{d['name']}</b>",ik(rows))

async def admin_books(c):
    cc=db();ds=cc.execute("SELECT d.*,COUNT(t.id) n FROM disciplines d LEFT JOIN textbooks t ON t.discipline_id=d.id WHERE d.active=1 GROUP BY d.id ORDER BY d.name").fetchall();cc.close();rows=[[b(f"{d['emoji']} {d['name']} {'🟢' if d['n'] else '⚪'}",f"abooksd:{d['id']}")] for d in ds];rows.append([b("🏠 Админ-панель","adm:back")]);await c.answer();await edit_or_answer(c,"📚 <b>Управление учебниками</b>",ik(rows))
@router.callback_query(F.data.startswith("abooksd:"))
async def abooksd(c:CallbackQuery):
    did=int(c.data.split(':')[-1]);cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone();ts=cc.execute("SELECT * FROM textbooks WHERE discipline_id=?",(did,)).fetchall();cc.close();rows=[[b(f"📘 {t['title'][:55]}",f"abook:{t['id']}:{did}")] for t in ts];rows.append([b("➕ Добавить учебник",f"abooknew:{did}")]);rows.append([b("🔙 Назад", "adm:books")]);await c.answer();await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))
@router.callback_query(F.data.startswith("abooknew:"))
async def abooknew(c,state): await state.set_state(BookCreate.title);await state.update_data(did=int(c.data.split(':')[-1]));await c.answer();await show(c.bot,c.from_user.id,"Введите название учебника:")
@router.message(BookCreate.title)
async def abooktitle(m,state):
    data=await state.get_data()
    if 'bid' in data and not data.get('change_file'):
        cc=db(); cc.execute("UPDATE textbooks SET title=? WHERE id=?",(m.text.strip(),data['bid'])); cc.commit(); cc.close()
        did=data.get('did'); await state.clear()
        cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ts=cc.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id",(did,)).fetchall(); cc.close()
        rows=[[b(f"📘 {t['title'][:55]}",f"abook:{t['id']}:{did}")] for t in ts]; rows.append([b("➕ Добавить учебник",f"abooknew:{did}")]); rows.append([b("🔙 Назад","adm:books")])
        return await show(m.bot,m.from_user.id,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))
    await state.update_data(title=m.text);await state.set_state(BookCreate.file);await m.answer("Отправьте файл учебника:")
@router.message(BookCreate.file)
async def abookfile(m,state):
    x=media_info(m)
    if not x: return await m.answer("Нужен документ, фото или видео.")
    data=await state.get_data(); cc=db()
    if data.get('change_file') and data.get('bid'):
        cc.execute("UPDATE textbooks SET kind=?,file_id=? WHERE id=?",(x[0],x[1],data['bid']))
        msg="✅ Файл учебника заменён."
    else:
        cc.execute("INSERT INTO textbooks(discipline_id,title,kind,file_id,created_at) VALUES(?,?,?,?,?)",(data['did'],data['title'],x[0],x[1],datetime.now(TZ).isoformat()))
        msg="✅ Учебник добавлен."
    cc.commit();cc.close();did=data['did'];await state.clear()
    cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ts=cc.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id",(did,)).fetchall(); cc.close()
    rows=[[b(f"📘 {t['title'][:55]}",f"abook:{t['id']}:{did}")] for t in ts]; rows.append([b("➕ Добавить учебник",f"abooknew:{did}")]); rows.append([b("🔙 Назад","adm:books")])
    await show(m.bot,m.from_user.id,f"{msg}\n\n{d['emoji']} <b>{d['name']}</b>",ik(rows))
@router.callback_query(F.data.startswith("abook:"))
async def abook(c):
    _,bid,did=c.data.split(':');cc=db();t=cc.execute("SELECT * FROM textbooks WHERE id=?",(bid,)).fetchone();cc.close();await c.answer();await edit_or_answer(c,f"📘 <b>{t['title']}</b>",ik([[b("✏️ Изменить название",f"abookrename:{bid}:{did}")],[b("📎 Поменять файл",f"abookfile:{bid}:{did}")],[b("🗑 Удалить",f"abookdel:{bid}:{did}")],[b("🔙 Назад",f"abooksd:{did}")]]))
@router.callback_query(F.data.startswith("abookfile:"))
async def abookfile_start(c:CallbackQuery,state:FSMContext):
    _,bid,did=c.data.split(':'); await state.update_data(bid=int(bid),did=int(did),change_file=True); await state.set_state(BookCreate.file); await c.answer(); await show(c.bot,c.from_user.id,"📎 Отправьте новый файл учебника:",ik([[b("❌ Отмена",f"abook:{bid}:{did}")]]))

@router.callback_query(F.data.startswith("abookrename:"))
async def abookrename(c:CallbackQuery,state:FSMContext):
    _,bid,did=c.data.split(':'); await state.update_data(bid=int(bid),did=int(did)); await state.set_state(BookCreate.title); await c.answer(); await show(c.bot,c.from_user.id,"Введите новое название учебника:")
@router.callback_query(F.data.startswith("abookdel:"))
async def abookdel(c):
    _,bid,did=c.data.split(':');await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить учебник безвозвратно?</b>",ik([[b("🗑 Да, удалить",f"abookdelok:{bid}:{did}")],[b("❌ Отмена",f"abook:{bid}:{did}")]]))
@router.callback_query(F.data.startswith("abookdelok:"))
async def abookdelok(c):
    _,bid,did=c.data.split(':');cc=db();cc.execute("DELETE FROM textbooks WHERE id=?",(bid,));cc.commit();cc.close();await c.answer("Удалено");await abooksd(c)

# Schedule admin
async def admin_schedule(c):
    now=datetime.now(TZ); await c.answer(); await edit_or_answer(c,"📅 <b>Управление расписанием</b>\nВыберите месяц:",calendar_kb(now.year,now.month,'asch'))
@router.callback_query(F.data.startswith("asch:month:"))
async def asch_month(c):
    _,_,y,m=c.data.split(':');y=int(y);m=int(m);y,m=max((2026,9),(y,m));y,m=min((2027,7),(y,m));await c.answer();await edit_or_answer(c,f"📅 <b>{m:02d}.{y}</b>\nВыберите дату:",calendar_kb(y,m,'asch'))
@router.callback_query(F.data.startswith("asch:date:"))
async def asch_date(c,state):
    d=date.fromisoformat(c.data.split(':',2)[2]);cc=db();day=cc.execute("SELECT id FROM schedule_days WHERE day=?",(d.isoformat(),)).fetchone();cc.close();await c.answer();
    prev=(d-timedelta(days=1)).isoformat(); nxt=(d+timedelta(days=1)).isoformat()
    nav=[[b("◀️ Предыдущий день",f"asch:date:{prev}"),b("▶️ Следующий день",f"asch:date:{nxt}")]]
    if day:
        return await edit_or_answer(c,f"📅 <b>{ru_date(d)}</b>\nРасписание уже опубликовано.",ik(nav+[[b("⚡ Быстрое изменение",f"asch:edit:{d.isoformat()}")],[b("🔁 Перенести/скопировать",f"asch:move:{d.isoformat()}")],[b("🗑 Удалить",f"asch:del:{d.isoformat()}")], [b("📋 Массовые действия",f"asch:bulk:{d.isoformat()}")],[b("🗓 Календарь месяца",f"asch:month:{d.year}:{d.month}")],[b("🏠 Админ-панель","adm:back")]]))
    await edit_or_answer(c,f"📅 <b>{ru_date(d)}</b>\nРасписания ещё нет.",ik(nav+[[b("➕ Добавить расписание",f"asch:add:{d.isoformat()}")],[b("🗓 Календарь месяца",f"asch:month:{d.year}:{d.month}")],[b("🏠 Админ-панель","adm:back")]]))
@router.callback_query(F.data.startswith("asch:move:"))
async def asch_move(c:CallbackQuery):
    d=c.data.split(':',2)[2]; cc=db(); rows=cc.execute("SELECT l.id,l.lesson_no,l.start,l.end,d.name,d.emoji FROM schedule_lessons l JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id WHERE sd.day=? ORDER BY l.start",(d,)).fetchall(); cc.close(); await c.answer(); await edit_or_answer(c,"🔁 <b>Выберите пару для переноса/копирования:</b>",ik([[b(f"{r['lesson_no']} • {r['start']}–{r['end']} • {r['emoji']} {r['name']}",f"asch:moveone:{d}:{r['id']}")] for r in rows]+[[b("🔙 Назад",f"asch:date:{d}")]]))

@router.callback_query(F.data.startswith("asch:moveone:"))
async def asch_moveone(c:CallbackQuery):
    _,_,d,lid=c.data.split(':'); await c.answer(); await edit_or_answer(c,"🔁 <b>Что сделать?</b>",ik([[b("🔁 Перенести",f"asch:transfer:{d}:{lid}"),b("📋 Скопировать",f"asch:copy:{d}:{lid}")],[b("🔙 Назад",f"asch:move:{d}")]]))

@router.callback_query(F.data.startswith("asch:transfer:"))
async def asch_transfer(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':'); await state.update_data(transfer_lid=int(lid),transfer_day=d); await state.set_state(ScheduleDateInput.value); await c.answer(); await edit_or_answer(c,"📅 Введите дату назначения в формате <code>ДД.ММ.ГГГГ</code>:",ik([[b("❌ Отмена",f"asch:date:{d}")]]))

@router.message(ScheduleDateInput.value)
async def schedule_date_action(m:Message,state:FSMContext):
    data=await state.get_data(); dt=parse_hw_date(m.text)
    if not dt: return await m.answer("❌ Неверный формат. Используй <code>ДД.ММ.ГГГГ</code>.")
    cc=db(); now=datetime.now(TZ).isoformat()
    if data.get('transfer_lid'):
        lid=data['transfer_lid']; srcday=data['transfer_day']; src=cc.execute("SELECT * FROM schedule_lessons WHERE id=?",(lid,)).fetchone(); dest=cc.execute("SELECT id FROM schedule_days WHERE day=?",(dt.isoformat(),)).fetchone()
        if not src: cc.close(); await state.clear(); return await m.answer("❌ Пара не найдена.")
        if not dest:
            cc.execute("INSERT INTO schedule_days(day,created_at,updated_at) VALUES(?,?,?)",(dt.isoformat(),now,now)); destid=cc.execute("SELECT last_insert_rowid()").fetchone()[0]
        else: destid=dest['id']
        maxno=cc.execute("SELECT COALESCE(MAX(lesson_no),0) FROM schedule_lessons WHERE schedule_day_id=?",(destid,)).fetchone()[0]
        cc.execute("INSERT INTO schedule_lessons(schedule_day_id,lesson_no,start,end,discipline_id,lesson_type,room) VALUES(?,?,?,?,?,?,?)",(destid,maxno+1,src['start'],src['end'],src['discipline_id'],src['lesson_type'],src['room']))
        cc.execute("DELETE FROM schedule_lessons WHERE id=?",(lid,)); cc.execute("UPDATE schedule_days SET updated_at=? WHERE day=?",(now,srcday)); cc.commit(); cc.close(); await state.clear(); return await m.answer("✅ Пара перенесена.")
    if data.get('copy_lid'):
        src=cc.execute("SELECT * FROM schedule_lessons WHERE id=?",(data['copy_lid'],)).fetchone(); dest=cc.execute("SELECT id FROM schedule_days WHERE day=?",(dt.isoformat(),)).fetchone()
        if not src: cc.close(); await state.clear(); return await m.answer("❌ Пара не найдена.")
        if not dest:
            cc.execute("INSERT INTO schedule_days(day,created_at,updated_at) VALUES(?,?,?)",(dt.isoformat(),now,now)); destid=cc.execute("SELECT last_insert_rowid()").fetchone()[0]
        else: destid=dest['id']
        maxno=cc.execute("SELECT COALESCE(MAX(lesson_no),0) FROM schedule_lessons WHERE schedule_day_id=?",(destid,)).fetchone()[0]
        cc.execute("INSERT INTO schedule_lessons(schedule_day_id,lesson_no,start,end,discipline_id,lesson_type,room) VALUES(?,?,?,?,?,?,?)",(destid,maxno+1,src['start'],src['end'],src['discipline_id'],src['lesson_type'],src['room'])); cc.commit(); cc.close(); await state.clear(); return await m.answer("✅ Пара скопирована.")
    if data.get('bulkcopy_day'):
        srcs=cc.execute("SELECT * FROM schedule_lessons l JOIN schedule_days sd ON sd.id=l.schedule_day_id WHERE sd.day=? ORDER BY l.start",(data['bulkcopy_day'],)).fetchall(); dest=cc.execute("SELECT id FROM schedule_days WHERE day=?",(dt.isoformat(),)).fetchone()
        if not srcs: cc.close(); await state.clear(); return await m.answer("❌ В исходном дне нет расписания.")
        if not dest:
            cc.execute("INSERT INTO schedule_days(day,created_at,updated_at) VALUES(?,?,?)",(dt.isoformat(),now,now)); destid=cc.execute("SELECT last_insert_rowid()").fetchone()[0]
        else: destid=dest['id']; cc.execute("DELETE FROM schedule_lessons WHERE schedule_day_id=?",(destid,))
        for n,x in enumerate(srcs,1): cc.execute("INSERT INTO schedule_lessons(schedule_day_id,lesson_no,start,end,discipline_id,lesson_type,room) VALUES(?,?,?,?,?,?,?)",(destid,n,x['start'],x['end'],x['discipline_id'],x['lesson_type'],x['room']))
        cc.execute("UPDATE schedule_days SET updated_at=? WHERE id=?",(now,destid)); cc.commit(); cc.close(); await state.clear(); return await m.answer("✅ Расписание всего дня скопировано.")
    cc.close(); await state.clear(); await m.answer("❌ Неизвестное действие.")

@router.callback_query(F.data.startswith("asch:copy:"))
async def asch_copy(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':'); await state.update_data(copy_lid=int(lid),copy_day=d); await state.set_state(ScheduleDateInput.value); await c.answer(); await show(c.bot,c.from_user.id,"📅 Введите дату, куда скопировать, в формате <code>ДД.ММ.ГГГГ</code>:")

@router.callback_query(F.data.startswith("asch:bulk:"))
async def asch_bulk(c:CallbackQuery):
    d=c.data.split(':',2)[2]; await c.answer(); await edit_or_answer(c,"📋 <b>Массовые действия</b>",ik([[b("📋 Скопировать весь день",f"asch:bulkcopy:{d}")],[b("🗑 Очистить день",f"asch:del:{d}")],[b("🔙 Назад",f"asch:date:{d}")]]))

@router.callback_query(F.data.startswith("asch:bulkcopy:"))
async def asch_bulkcopy(c:CallbackQuery,state:FSMContext):
    d=c.data.split(':',2)[2]; await state.update_data(bulkcopy_day=d); await state.set_state(ScheduleDateInput.value); await c.answer(); await show(c.bot,c.from_user.id,"📅 Введите дату назначения в формате <code>ДД.ММ.ГГГГ</code>:")

@router.callback_query(F.data.startswith("asch:add:"))
async def asch_add(c,state):
    d=c.data.split(':',2)[2];await state.update_data(day=d);await state.set_state(ScheduleCreate.count);await c.answer();await edit_or_answer(c,"Сколько всего пар?",ik([[b(str(i),f"schc:count:{i}") for i in range(1,6)],[b("❌ Отмена","adm:schedule")]]))
@router.callback_query(F.data.startswith("schc:count:"),ScheduleCreate.count)
async def schc_count(c,state):
    await state.update_data(count=int(c.data.split(':')[-1]),idx=1); await state.set_state(ScheduleCreate.lesson_start); await c.answer();
    await edit_or_answer(c,"Выберите время 1-й пары или задайте своё:",ik([[b(f"{s}–{e}",f"schc:time:{i}")] for i,(s,e) in enumerate(SLOT_TIMES,1)]+[[b("🕐 Своё время","schc:custom")],[b("❌ Отмена","adm:schedule")]]))
@router.callback_query(F.data.startswith("schc:time:"),ScheduleCreate.lesson_start)
async def schc_time(c,state):
    slot=int(c.data.split(':')[-1])
    if not 1 <= slot <= len(SLOT_TIMES):
        return await c.answer("❌ Неверный интервал времени.", show_alert=True)
    s,e=SLOT_TIMES[slot-1]
    await state.update_data(start=s,end=e)
    await state.set_state(ScheduleCreate.discipline)
    cc=db();ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall();cc.close()
    await c.answer()
    await edit_or_answer(c,"Выберите дисциплину:",ik(two_col([b(f"{d['emoji']} {d['name']}",f"schc:d:{d['id']}") for d in ds])))
@router.callback_query(F.data=="schc:custom",ScheduleCreate.lesson_start)
async def schc_custom(c:CallbackQuery,state:FSMContext):
    await state.set_state(ScheduleCreateCustomTime.value); await c.answer(); await show(c.bot,c.from_user.id,"🕐 Введите время в формате <code>ЧЧ:ММ–ЧЧ:ММ</code> или <code>ЧЧ:ММ-ЧЧ:ММ</code>:",ik([[b("❌ Отмена","adm:schedule")]]))

@router.message(ScheduleCreateCustomTime.value)
async def schc_custom_value(m:Message,state:FSMContext):
    raw=(m.text or '').strip().replace('–','-'); parts=[x.strip() for x in raw.split('-',1)]
    try:
        st=time.fromisoformat(parts[0]); en=time.fromisoformat(parts[1]);
        if st>=en: raise ValueError
    except Exception: return await m.answer("❌ Неверный формат. Пример: <code>09:15-10:45</code>")
    await state.update_data(start=st.strftime('%H:%M'),end=en.strftime('%H:%M')); await state.set_state(ScheduleCreate.discipline); await m.answer("📚 Выберите дисциплину:",reply_markup=ik(two_col([b(f"{d['emoji']} {d['name']}",f"schc:d:{d['id']}") for d in db().execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall()])))

@router.callback_query(F.data.startswith("schc:d:"),ScheduleCreate.discipline)
async def schc_d(c,state): await state.update_data(discipline=int(c.data.split(':')[-1]));await state.set_state(ScheduleCreate.typ);await c.answer();await edit_or_answer(c,"Выберите тип пары:",ik([[b("📖 ЛЕК","schc:t:ЛЕК"),b("📝 ПР","schc:t:ПР"),b("🔬 ЛАБ","schc:t:ЛАБ")]]))
@router.callback_query(F.data.startswith("schc:t:"),ScheduleCreate.typ)
async def schc_t(c,state): await state.update_data(typ=c.data.split(':',2)[2]);await state.set_state(ScheduleCreate.room);await c.answer();await show(c.bot,c.from_user.id,"Введите номер аудитории:")
@router.message(ScheduleCreate.room)
async def schc_room(m,state):
    data=await state.get_data();idx=data['idx'];less=data.get('lessons',[]);less.append((idx,data['start'],data['end'],data['discipline'],data['typ'],m.text.strip()));
    if idx<data['count']:
        await state.update_data(lessons=less,idx=idx+1);await state.set_state(ScheduleCreate.lesson_start)
        await show(m.bot,m.from_user.id,f"Выберите время {idx+1}-й пары или задайте своё:",ik([[b(f"{s}–{e}",f"schc:time:{i}")] for i,(s,e) in enumerate(SLOT_TIMES,1)]+[[b("🕐 Своё время","schc:custom")],[b("❌ Отмена","adm:schedule")]]));return
    cc=db();now=datetime.now(TZ).isoformat();cc.execute("INSERT INTO schedule_days(day,created_at,updated_at) VALUES(?,?,?)",(data['day'],now,now));sid=cc.execute("SELECT last_insert_rowid()").fetchone()[0]
    for x in less: cc.execute("INSERT INTO schedule_lessons(schedule_day_id,lesson_no,start,end,discipline_id,lesson_type,room) VALUES(?,?,?,?,?,?,?)",(sid,*x))
    cc.commit();cc.close();await state.clear()
    dt=date.fromisoformat(data['day'])
    prev=(dt-timedelta(days=1)).isoformat(); nxt=(dt+timedelta(days=1)).isoformat()
    # The final room message is a normal user message, so explicitly replace
    # the old prompt with the completed result and keep the result at the bottom.
    await show(m.bot, m.from_user.id, f"📅 <b>{ru_date(dt)}</b>\n\n✅ Расписание опубликовано.", ik([[b("◀️ Предыдущий день",f"asch:date:{prev}"),b("▶️ Следующий день",f"asch:date:{nxt}")],[b("🗓 Календарь месяца",f"asch:month:{dt.year}:{dt.month}" )],[b("🏠 Админ-панель","adm:back")]]))
@router.callback_query(F.data.startswith("asch:del:"))
async def asch_del(c):
    d=c.data.split(':',2)[2];await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить расписание на этот день безвозвратно?</b>",ik([[b("🗑 Да, удалить",f"asch:delok:{d}")],[b("❌ Отмена",f"asch:date:{d}")]]))
@router.callback_query(F.data.startswith("asch:delok:"))
async def asch_delok(c):
    d=c.data.split(':',2)[2]
    cc=db();cc.execute("DELETE FROM schedule_days WHERE day=?",(d,));cc.commit();cc.close()
    await c.answer("Удалено")
    dt=date.fromisoformat(d)
    await edit_or_answer(c, f"📅 <b>{ru_date(dt)}</b>\n\n🗑 Расписание удалено.", ik([[b("🗓 Вернуться к календарю", f"asch:month:{dt.year}:{dt.month}")],[b("🏠 Админ-панель", "adm:back")]]))
@router.callback_query(F.data.startswith("asch:edit:"))
async def asch_edit(c:CallbackQuery, state:FSMContext):
    d=c.data.split(':',2)[2]
    cc=db()
    rows=cc.execute("""SELECT l.id,l.lesson_no,l.start,l.end,l.lesson_type,d.name,d.emoji
                       FROM schedule_lessons l JOIN disciplines d ON d.id=l.discipline_id
                       JOIN schedule_days sd ON sd.id=l.schedule_day_id
                       WHERE sd.day=? ORDER BY l.lesson_no""",(d,)).fetchall()
    cc.close()
    await state.clear(); await c.answer()
    buttons=[]
    for r in rows:
        buttons.append([b(f"✏️ {r['lesson_no']} пара • {r['start']}–{r['end']} • {r['emoji']} {r['name']}",f"asche:lesson:{d}:{r['id']}")])
    buttons.append([b("➕ Добавить пару",f"asche:add:{d}")])
    buttons.append([b("🗑 Удалить пару",f"asche:delete:{d}")])
    buttons.append([b("🔙 Назад",f"asch:date:{d}")])
    await edit_or_answer(c,f"✏️ <b>Редактирование расписания</b>\n📅 {ru_date(date.fromisoformat(d))}\n\nВыберите, что изменить:",ik(buttons))

@router.callback_query(F.data.startswith("asche:lesson:"))
async def asche_lesson(c:CallbackQuery, state:FSMContext):
    _,_,d,lid=c.data.split(':',3)
    cc=db(); r=cc.execute("""SELECT l.*,d.name discipline,d.emoji FROM schedule_lessons l
                           JOIN disciplines d ON d.id=l.discipline_id WHERE l.id=?""",(lid,)).fetchone(); cc.close()
    if not r: return await c.answer("Пара не найдена",show_alert=True)
    await state.clear(); await c.answer()
    kb=ik([[b("🕐 Изменить время",f"asche:time:{d}:{lid}"), b("📚 Изменить дисциплину",f"asche:discipline:{d}:{lid}")],
           [b("📖 Изменить тип",f"asche:type:{d}:{lid}"), b("🚪 Изменить аудиторию",f"asche:room:{d}:{lid}")],
           [b("🔙 Назад",f"asch:edit:{d}")]])
    await edit_or_answer(c,f"✏️ <b>{r['lesson_no']} пара</b>\n\n🕐 {r['start']}–{r['end']}\n{r['emoji']} {r['discipline']}\n📖 {r['lesson_type']}\n🚪 ауд. {r['room']}",kb)

@router.callback_query(F.data.startswith("asche:time:"))
async def asche_time(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':',3); await state.update_data(day=d,lid=int(lid)); await state.set_state(ScheduleLessonEdit.time); await c.answer()
    await edit_or_answer(c,"🕐 <b>Выберите новое время пары:</b>",ik([[b(f"{s}–{e}",f"asche:settime:{i}")] for i,(s,e) in enumerate(SLOT_TIMES,1)]+[[b("🕐 Своё время","asche:customtime")],[b("🔙 Назад",f"asche:lesson:{d}:{lid}")]]))

@router.callback_query(F.data.startswith("asche:settime:"),ScheduleLessonEdit.time)
async def asche_settime(c:CallbackQuery,state:FSMContext):
    slot=int(c.data.split(':')[-1]); data=await state.get_data(); s,e=SLOT_TIMES[slot-1]
    cc=db(); cc.execute("UPDATE schedule_lessons SET start=?,end=? WHERE id=?",(s,e,data['lid'])); cc.commit(); cc.close(); await state.clear(); await c.answer("Время изменено")
    await asche_lesson(c,state)

@router.callback_query(F.data=="asche:customtime",ScheduleLessonEdit.time)
async def asche_customtime(c:CallbackQuery,state:FSMContext):
    await state.set_state(ScheduleEditCustomTime.value); await c.answer(); await edit_or_answer(c,"🕐 Введите новое время в формате <code>ЧЧ:ММ–ЧЧ:ММ</code>:",ik([[b("🔙 Назад",f"asche:lesson:{(await state.get_data())['day']}:{(await state.get_data())['lid']}")]]))

@router.message(ScheduleEditCustomTime.value)
async def asche_customtime_value(m:Message,state:FSMContext):
    data=await state.get_data(); raw=(m.text or '').strip().replace('–','-'); parts=[x.strip() for x in raw.split('-',1)]
    try:
        st=time.fromisoformat(parts[0]); en=time.fromisoformat(parts[1]);
        if st>=en: raise ValueError
    except Exception: return await m.answer("❌ Неверный формат. Пример: <code>09:15-10:45</code>")
    cc=db(); cc.execute("UPDATE schedule_lessons SET start=?,end=? WHERE id=?",(st.strftime('%H:%M'),en.strftime('%H:%M'),data['lid'])); cc.execute("UPDATE schedule_days SET updated_at=? WHERE day=?",(datetime.now(TZ).isoformat(),data['day'])); cc.commit(); cc.close(); await state.clear(); await m.answer("✅ Время изменено."); await show(m.bot,m.from_user.id,"✏️ <b>Редактирование расписания</b>",ik([[b("🔙 К расписанию дня",f"asch:date:{data['day']}")],[b("✏️ Продолжить редактирование",f"asch:edit:{data['day']}")]]))

@router.callback_query(F.data.startswith("asche:discipline:"))
async def asche_discipline(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':',3); await state.update_data(day=d,lid=int(lid)); await state.set_state(ScheduleLessonEdit.discipline); await c.answer()
    cc=db(); ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall(); cc.close()
    await edit_or_answer(c,"📚 <b>Выберите новую дисциплину:</b>",ik(two_col([b(f"{x['emoji']} {x['name']}",f"asche:setdisc:{x['id']}") for x in ds])+[[b("🔙 Назад",f"asche:lesson:{d}:{lid}")]]))

@router.callback_query(F.data.startswith("asche:setdisc:"),ScheduleLessonEdit.discipline)
async def asche_setdisc(c:CallbackQuery,state:FSMContext):
    did=int(c.data.split(':')[-1]); data=await state.get_data(); cc=db(); cc.execute("UPDATE schedule_lessons SET discipline_id=? WHERE id=?",(did,data['lid'])); cc.commit(); cc.close(); await state.clear(); await c.answer("Дисциплина изменена"); await asche_lesson(c,state)

@router.callback_query(F.data.startswith("asche:type:"))
async def asche_type(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':',3); await state.update_data(day=d,lid=int(lid)); await state.set_state(ScheduleLessonEdit.typ); await c.answer()
    await edit_or_answer(c,"📖 <b>Выберите новый тип пары:</b>",ik([[b("📖 ЛЕК","asche:settype:ЛЕК"),b("📝 ПР","asche:settype:ПР"),b("🔬 ЛАБ","asche:settype:ЛАБ")],[b("🔙 Назад",f"asche:lesson:{d}:{lid}")]]))

@router.callback_query(F.data.startswith("asche:settype:"),ScheduleLessonEdit.typ)
async def asche_settype(c:CallbackQuery,state:FSMContext):
    typ=c.data.split(':',2)[2]; data=await state.get_data(); cc=db(); cc.execute("UPDATE schedule_lessons SET lesson_type=? WHERE id=?",(typ,data['lid'])); cc.commit(); cc.close(); await state.clear(); await c.answer("Тип изменён"); await asche_lesson(c,state)

@router.callback_query(F.data.startswith("asche:room:"))
async def asche_room(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':',3); await state.update_data(day=d,lid=int(lid)); await state.set_state(ScheduleLessonEdit.room); await c.answer(); await edit_or_answer(c,"🚪 Введите новый номер аудитории:",ik([[b("🔙 Назад",f"asche:lesson:{d}:{lid}")]]))

@router.callback_query(F.data.startswith("asche:add:"))
async def asche_add(c:CallbackQuery,state:FSMContext):
    d=c.data.split(':',2)[2]; await state.update_data(day=d); await state.set_state(ScheduleLessonEdit.time); await c.answer()
    await edit_or_answer(c,"➕ <b>Выберите время новой пары:</b>",ik([[b(f"{s}–{e}",f"asche:addtime:{i}")] for i,(s,e) in enumerate(SLOT_TIMES,1)]+[[b("🕐 Своё время","asche:addcustomtime")],[b("🔙 Назад",f"asch:edit:{d}")]]))

@router.callback_query(F.data.startswith("asche:addtime:"),ScheduleLessonEdit.time)
async def asche_addtime(c:CallbackQuery,state:FSMContext):
    slot=int(c.data.split(':')[-1]); data=await state.get_data(); s,e=SLOT_TIMES[slot-1]; await state.update_data(start=s,end=e); await state.set_state(ScheduleLessonEdit.discipline); await c.answer()
    cc=db(); ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall(); cc.close(); await edit_or_answer(c,"📚 <b>Выберите дисциплину:</b>",ik(two_col([b(f"{x['emoji']} {x['name']}",f"asche:adddisc:{x['id']}") for x in ds])+[[b("🔙 Назад",f"asch:edit:{data['day']}")]]))

@router.callback_query(F.data=="asche:addcustomtime",ScheduleLessonEdit.time)
async def asche_addcustomtime(c:CallbackQuery,state:FSMContext):
    await state.set_state(ScheduleAddCustomTime.value); await c.answer(); await show(c.bot,c.from_user.id,"🕐 Введите время новой пары в формате <code>ЧЧ:ММ–ЧЧ:ММ</code>:",ik([[b("❌ Отмена","adm:schedule")]]))

@router.message(ScheduleAddCustomTime.value)
async def asche_addcustomtime_value(m:Message,state:FSMContext):
    data=await state.get_data(); raw=(m.text or '').strip().replace('–','-'); parts=[x.strip() for x in raw.split('-',1)]
    try:
        st=time.fromisoformat(parts[0]); en=time.fromisoformat(parts[1]);
        if st>=en: raise ValueError
    except Exception: return await m.answer("❌ Неверный формат. Пример: <code>09:15-10:45</code>")
    await state.update_data(start=st.strftime('%H:%M'),end=en.strftime('%H:%M')); await state.set_state(ScheduleLessonEdit.discipline); cc=db(); ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall(); cc.close(); await m.answer("📚 Выберите дисциплину:",reply_markup=ik(two_col([b(f"{x['emoji']} {x['name']}",f"asche:adddisc:{x['id']}") for x in ds])))

@router.callback_query(F.data.startswith("asche:adddisc:"),ScheduleLessonEdit.discipline)
async def asche_adddisc(c:CallbackQuery,state:FSMContext):
    did=int(c.data.split(':')[-1]); await state.update_data(discipline=did); await state.set_state(ScheduleLessonEdit.typ); await c.answer(); await edit_or_answer(c,"📖 <b>Выберите тип новой пары:</b>",ik([[b("📖 ЛЕК","asche:addtype:ЛЕК"),b("📝 ПР","asche:addtype:ПР"),b("🔬 ЛАБ","asche:addtype:ЛАБ")],[b("🔙 Назад",f"asch:edit:{(await state.get_data())['day']}")]]))

@router.callback_query(F.data.startswith("asche:addtype:"),ScheduleLessonEdit.typ)
async def asche_addtype(c:CallbackQuery,state:FSMContext):
    typ=c.data.split(':',2)[2]; data=await state.get_data(); await state.update_data(typ=typ); await state.set_state(ScheduleLessonEdit.room); await c.answer(); await edit_or_answer(c,"🚪 Введите номер аудитории для новой пары:",ik([[b("🔙 Назад",f"asch:edit:{data['day']}")]]))

@router.message(ScheduleLessonEdit.room)
async def asche_add_or_edit_room(m:Message,state:FSMContext):
    data=await state.get_data(); room=(m.text or '').strip()
    if not room:
        return await m.answer("❌ Введите номер аудитории.")
    cc=db()
    if data.get('lid'):
        cc.execute("UPDATE schedule_lessons SET room=? WHERE id=?",(room,data['lid']))
        cc.execute("UPDATE schedule_days SET updated_at=? WHERE day=?",(datetime.now(TZ).isoformat(),data['day']))
        cc.commit(); cc.close(); await state.clear()
        return await show(m.bot,m.from_user.id,"✅ Аудитория изменена.",ik([[b("✏️ Продолжить редактирование",f"asch:edit:{data['day']}")],[b("📅 К расписанию дня",f"asch:date:{data['day']}")]]))
    day=cc.execute("SELECT id FROM schedule_days WHERE day=?",(data['day'],)).fetchone()
    if not day:
        cc.close(); await state.clear(); return await m.answer("❌ Расписание этого дня не найдено.")
    maxno=cc.execute("SELECT COALESCE(MAX(lesson_no),0) n FROM schedule_lessons WHERE schedule_day_id=?",(day['id'],)).fetchone()['n']
    if maxno >= 5:
        cc.close(); await state.clear(); return await show(m.bot,m.from_user.id,"❌ Нельзя добавить больше 5 пар.",ik([[b("✏️ К редактированию",f"asch:edit:{data['day']}")],[b("📅 К расписанию дня",f"asch:date:{data['day']}")]]))
    cc.execute("INSERT INTO schedule_lessons(schedule_day_id,lesson_no,start,end,discipline_id,lesson_type,room) VALUES(?,?,?,?,?,?,?)",(day['id'],maxno+1,data['start'],data['end'],data['discipline'],data['typ'],room))
    cc.execute("UPDATE schedule_days SET updated_at=? WHERE id=?",(datetime.now(TZ).isoformat(),day['id']))
    cc.commit(); cc.close(); await state.clear()
    await show(m.bot,m.from_user.id,"✅ Пара добавлена.",ik([[b("✏️ Продолжить редактирование",f"asch:edit:{data['day']}")],[b("📅 К расписанию дня",f"asch:date:{data['day']}")]]))

@router.callback_query(F.data.startswith("asche:delete:"))
async def asche_delete(c:CallbackQuery,state:FSMContext):
    d=c.data.split(':',2)[2]; await state.clear(); await c.answer(); cc=db(); rows=cc.execute("SELECT l.id,l.lesson_no,d.name FROM schedule_lessons l JOIN schedule_days sd ON sd.id=l.schedule_day_id JOIN disciplines d ON d.id=l.discipline_id WHERE sd.day=? ORDER BY l.lesson_no",(d,)).fetchall(); cc.close()
    await edit_or_answer(c,"🗑 <b>Выберите пару для удаления:</b>",ik([[b(f"🗑 {r['lesson_no']} пара — {r['name']}",f"asche:delone:{d}:{r['id']}")] for r in rows]+[[b("🔙 Назад",f"asch:edit:{d}")]]))

@router.callback_query(F.data.startswith("asche:delone:"))
async def asche_delone(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':',3); await c.answer(); await edit_or_answer(c,"⚠️ <b>Удалить эту пару?</b>",ik([[b("🗑 Да, удалить",f"asche:delok:{d}:{lid}")],[b("❌ Отмена",f"asch:edit:{d}")]]))

@router.callback_query(F.data.startswith("asche:delok:"))
async def asche_delok(c:CallbackQuery,state:FSMContext):
    _,_,d,lid=c.data.split(':',3); cc=db(); cc.execute("DELETE FROM schedule_lessons WHERE id=?",(lid,))
    dayrow=cc.execute("SELECT id FROM schedule_days WHERE day=?",(d,)).fetchone()
    if dayrow:
        remaining=cc.execute("SELECT id FROM schedule_lessons WHERE schedule_day_id=? ORDER BY start",(dayrow['id'],)).fetchall()
        for n,row in enumerate(remaining,1): cc.execute("UPDATE schedule_lessons SET lesson_no=? WHERE id=?",(n,row['id']))
        if not remaining: cc.execute("DELETE FROM schedule_days WHERE id=?",(dayrow['id'],))
        else: cc.execute("UPDATE schedule_days SET updated_at=? WHERE id=?",(datetime.now(TZ).isoformat(),dayrow['id']))
    cc.commit(); cc.close(); await state.clear(); await c.answer("Пара удалена"); await edit_or_answer(c,"✅ Пара удалена.",ik([[b("✏️ Редактировать день",f"asch:edit:{d}")],[b("📅 К расписанию дня",f"asch:date:{d}")]]))


# Students / VIP / disciplines
async def admin_students(c):
    cc=db();ss=cc.execute("SELECT * FROM students ORDER BY full_name").fetchall();cc.close(); rows=[[b(f"👤 {s['full_name']} {'🟢' if s['telegram_id'] else '⚪'}",f"astud:{s['id']}")] for s in ss];rows.append([b("➕ Добавить студента","astudadd")]);rows.append([b("🏠 Админ-панель","adm:back")]);await c.answer();await edit_or_answer(c,"👥 <b>Управление студентами</b>",ik(rows))
@router.callback_query(F.data.startswith("astud:"))
async def astud(c):
    sid=int(c.data.split(':')[-1]);cc=db();s=cc.execute("SELECT * FROM students WHERE id=?",(sid,)).fetchone();cc.close();await c.answer();await edit_or_answer(c,f"👤 <b>{s['full_name']}</b>\n🆔 {s['telegram_id'] or 'не привязан'}",ik([[b("🗑 Удалить",f"astuddel:{sid}")],[b("🔙 Назад","adm:students")]]))
@router.callback_query(F.data=="astudadd")
async def astudadd(c,state):await state.set_state(StudentAdd.name);await c.answer();await show(c.bot,c.from_user.id,"Введите ФИО студента:")
@router.message(StudentAdd.name)
async def astudadd_save(m,state):
    cc=db()
    try:
        cc.execute("INSERT INTO students(full_name,normalized_name,created_at) VALUES(?,?,?)",(m.text.strip(),norm(m.text),datetime.now(TZ).isoformat()));cc.commit(); msg="✅ Студент добавлен."
    except sqlite3.IntegrityError: msg="❌ Такое ФИО уже есть."
    cc.close();await state.clear()
    cc=db(); ss=cc.execute("SELECT * FROM students ORDER BY full_name").fetchall(); cc.close()
    rows=[[b(f"{s['full_name']} {'🟢' if s['telegram_id'] else '⚪'}",f"astud:{s['id']}")] for s in ss]; rows.append([b("➕ Добавить студента","astudadd")]); rows.append([b("🏠 Админ-панель","adm:back")])
    await show(m.bot,m.from_user.id,msg+"\n\n👥 <b>Студенты</b>",ik(rows))
@router.callback_query(F.data.startswith("astuddel:"))
async def astuddel(c):sid=c.data.split(':')[-1];await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить студента безвозвратно?</b>",ik([[b("🗑 Да, удалить",f"astuddelok:{sid}")],[b("❌ Отмена",f"astud:{sid}")]]))
@router.callback_query(F.data.startswith("astuddelok:"))
async def astuddelok(c):sid=c.data.split(':')[-1];cc=db();cc.execute("DELETE FROM students WHERE id=?",(sid,));cc.commit();cc.close();await c.answer("Удалено");await admin_students(c)

async def admin_vip(c):
    cc=db();ss=cc.execute("SELECT * FROM students WHERE telegram_id IS NOT NULL ORDER BY full_name").fetchall();v={r['telegram_id'] for r in cc.execute("SELECT telegram_id FROM vip_users")};cc.close();rows=[[b(f"{'⭐' if s['telegram_id'] in v else '⚪'} {s['full_name']}",f"vip:toggle:{s['telegram_id']}")] for s in ss];rows.append([b("🏠 Админ-панель","adm:back")]);await c.answer();await edit_or_answer(c,"⭐ <b>VIP-список</b>\nНажмите на студента, чтобы изменить доступ к готовым ответам.",ik(rows))
@router.callback_query(F.data.startswith("vip:toggle:"))
async def vip_toggle(c):
    uid=int(c.data.split(':')[-1]);cc=db();x=cc.execute("SELECT full_name FROM students WHERE telegram_id=?",(uid,)).fetchone();v=cc.execute("SELECT 1 FROM vip_users WHERE telegram_id=?",(uid,)).fetchone();
    if v: cc.execute("DELETE FROM vip_users WHERE telegram_id=?",(uid,))
    else: cc.execute("INSERT INTO vip_users(telegram_id,full_name,added_at) VALUES(?,?,?)",(uid,x['full_name'],datetime.now(TZ).isoformat()))
    cc.commit();cc.close();await admin_vip(c)

@router.callback_query(F.data.startswith("adisce:"))
async def adisce(c:CallbackQuery,state:FSMContext):
    did=int(c.data.split(':')[-1]); await state.update_data(did=did); await state.set_state(DisciplineEdit.name); await c.answer(); await show(c.bot,c.from_user.id,"Введите новое название дисциплины:")
@router.message(DisciplineEdit.name)
async def adisce_name(m:Message,state:FSMContext): await state.update_data(name=m.text.strip()); await state.set_state(DisciplineEdit.emoji); await m.answer("Введите эмодзи дисциплины (например 📐):")
@router.message(DisciplineEdit.emoji)
async def adisce_emoji(m:Message,state:FSMContext):
    data=await state.get_data(); cc=db()
    if data.get('did') is None:
        cc.execute("INSERT INTO disciplines(name,emoji,active,textbooks_enabled) VALUES(?,?,1,1)",(data['name'],m.text.strip() or '📚'))
    else:
        cc.execute("UPDATE disciplines SET name=?,emoji=? WHERE id=?",(data['name'],m.text.strip() or '📚',data['did']))
    cc.commit(); cc.close(); await state.clear()
    cc=db(); ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall(); cc.close()
    rows=[[b(f"{d['emoji']} {d['name']}",f"adisc:{d['id']}")] for d in ds]; rows.append([b("➕ Добавить","adiscadd")]); rows.append([b("🏠 Админ-панель","adm:back")])
    await show(m.bot,m.from_user.id,"✅ Дисциплина сохранена.\n\n📖 <b>Дисциплины</b>",ik(rows))
@router.callback_query(F.data=="adiscadd")
async def adiscadd(c:CallbackQuery,state:FSMContext): await state.set_state(DisciplineEdit.name); await state.update_data(did=None); await c.answer(); await show(c.bot,c.from_user.id,"Введите название новой дисциплины:")

async def admin_disc(c):
    cc=db();ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall();cc.close();rows=[[b(f"{d['emoji']} {d['name']}",f"adisc:{d['id']}")] for d in ds];rows.append([b("➕ Добавить","adiscadd")]);rows.append([b("🏠 Админ-панель","adm:back")]);await c.answer();await edit_or_answer(c,"📖 <b>Дисциплины</b>",ik(rows))
@router.callback_query(F.data.startswith("adisc:"))
async def adisc(c):did=int(c.data.split(':')[-1]);cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone();cc.close();await c.answer();await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik([[b("✏️ Изменить название/эмодзи",f"adisce:{did}")],[b("🗑 Удалить",f"adiscdel:{did}")],[b("🔙 Назад","adm:disc")]]))
@router.callback_query(F.data.startswith("adiscdel:"))
async def adiscdel(c):did=c.data.split(':')[-1];await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить дисциплину?</b>\nСвязанные ДЗ, учебники и расписание также могут быть удалены.",ik([[b("🗑 Да, удалить",f"adiscdelok:{did}")],[b("❌ Отмена",f"adisc:{did}")]]))
@router.callback_query(F.data.startswith("adiscdelok:"))
async def adiscdelok(c):
    did=c.data.split(':')[-1]
    cc=db(); cc.execute("UPDATE disciplines SET active=0 WHERE id=?",(did,)); cc.commit(); cc.close()
    await c.answer("Дисциплина скрыта из активного списка")
    await admin_disc(c)

# Broadcast
async def broadcast_start(c,state):
    await state.set_state(Broadcast.text); await state.update_data(kind=None); await c.answer(); await edit_or_answer(c,"📢 <b>Тип уведомления</b>",ik([[b("🚨 Важная информация","broadcast:type:important")],[b("🔄 Обновление","broadcast:type:update")],[b("📢 Прочее уведомление","broadcast:type:other")],[b("❌ Отмена","adm:back")]]))

@router.callback_query(F.data.startswith("broadcast:type:"), StateFilter(Broadcast.text))
async def broadcast_type(c:CallbackQuery,state:FSMContext):
    kind=c.data.split(':')[-1]; await state.update_data(kind=kind); await c.answer(); labels={'important':'🚨 Важная информация','update':'🔄 Обновление','other':'📢 Прочее уведомление'}; templates={
        'important':[('🚨 Важная информация','Внимание!\n\n'),('📅 Изменение расписания','Расписание группы изменено.\n\n')],
        'update':[('🔄 Обновление бота','В боте доступно обновление.\n\n'),('🆕 Новая функция','В боте появилась новая функция.\n\n')],
        'other':[('📢 Объявление','Объявление для группы.\n\n'),('📌 Напоминание','Напоминание для группы.\n\n')]
    }
    await edit_or_answer(c,f"{labels[kind]}\n\nВыберите шаблон или введите свой текст:",ik([[b(t, f"broadcast:template:{kind}:{i}")] for i,(t,_) in enumerate(templates[kind])]+[[b("❌ Отмена","adm:back")]]))

@router.callback_query(F.data.startswith("broadcast:template:"), StateFilter(Broadcast.text))
async def broadcast_template(c:CallbackQuery,state:FSMContext):
    _,_,kind,idx=c.data.split(':'); templates={
        'important':['Внимание!\n\n','Расписание группы изменено.\n\n'],
        'update':['В боте доступно обновление.\n\n','В боте появилась новая функция.\n\n'],
        'other':['Объявление для группы.\n\n','Напоминание для группы.\n\n']}
    await state.update_data(template=templates[kind][int(idx)]); await c.answer(); await edit_or_answer(c,"✍️ <b>Введите продолжение текста уведомления:</b>\n\n"+templates[kind][int(idx)],ik([[b("❌ Отмена","adm:back")]]))

@router.message(Broadcast.text)
async def broadcast_send(m,state):
    data=await state.get_data(); kind=data.get('kind'); prefix=data.get('template','')
    if not kind: return await m.answer("Сначала выбери тип уведомления в админ-панели.")
    await state.clear(); column={'important':None,'update':'updates','other':'other'}[kind]; cc=db()
    if column is None: ss=cc.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL").fetchall()
    else: ss=cc.execute(f"SELECT s.telegram_id FROM students s JOIN notification_settings n ON n.telegram_id=s.telegram_id WHERE n.{column}=1").fetchall()
    cc.close(); sent=0; labels={'important':'🚨 <b>Важная информация</b>','update':'🔄 <b>Обновление</b>','other':'📢 <b>Прочее уведомление</b>'}
    for x in ss:
        try: await m.bot.send_message(x['telegram_id'],labels[kind]+"\n\n"+prefix+(m.text or '')); sent+=1
        except Exception: pass
    await m.answer(f"✅ Уведомление отправлено: {sent}")
    await show(m.bot,m.from_user.id,"🛠 <b>Админ-панель</b>\n\nВыберите действие:",admin_kb())

# Mini App -> Telegram file bridge for admin textbooks/materials.
@router.message()
async def admin_file_request_bridge(m: Message):
    if not is_admin(m.from_user.id):
        return
    x = media_info(m)
    if not x:
        return
    cc = db()
    req = cc.execute("SELECT target_type,target_id FROM admin_file_requests WHERE telegram_id=?", (m.from_user.id,)).fetchone()
    if not req:
        cc.close()
        return
    if req['target_type'] == 'textbook':
        cc.execute("UPDATE textbooks SET kind=?,file_id=?,caption=? WHERE id=?", (x[0],x[1],x[2],req['target_id']))
        msg = '✅ Файл учебника прикреплён.'
    elif req['target_type'] == 'material':
        cc.execute("UPDATE additional_materials SET kind=?,file_id=?,caption=? WHERE id=?", (x[0],x[1],x[2],req['target_id']))
        msg = '✅ Файл дополнительного материала прикреплён.'
    elif req['target_type'] == 'homework':
        cc.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption) VALUES(?,?,?,?)",(req['target_id'],x[0],x[1],x[2]))
        msg = '✅ Файл ДЗ прикреплён.'
    elif req['target_type'] == 'homework_answer':
        cc.execute("INSERT INTO homework_answer(homework_id,kind,file_id,text,caption) VALUES(?,?,?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind=excluded.kind,file_id=excluded.file_id,text='',caption=excluded.caption",(req['target_id'],x[0],x[1],'',x[2]))
        msg = '✅ Файл готового ответа прикреплён.'
    else:
        cc.close()
        return
    cc.execute("DELETE FROM admin_file_requests WHERE telegram_id=?", (m.from_user.id,))
    cc.commit(); cc.close()
    await m.answer(msg)

# Notifications
async def cleanup_old_data(now):
    """Удаляет прошедшее расписание и архивирует просроченные ДЗ.
    Не мешает обычным обработчикам при кратковременной блокировке SQLite.
    """
    today=now.date().isoformat()
    for attempt in range(3):
        cc=None
        try:
            cc=db()
            cc.execute("DELETE FROM schedule_days WHERE day < ?",(today,))
            cc.execute("""
                UPDATE homework
                SET archived=1
                WHERE hidden=0
                  AND archived=0
                  AND substr(due_date,7,4)||'-'||substr(due_date,4,2)||substr(due_date,1,2) <= ?
            """,(today,))
            cc.commit(); cc.close()
            return
        except sqlite3.OperationalError as e:
            if cc:
                try: cc.rollback(); cc.close()
                except Exception: pass
            if "locked" not in str(e).lower() or attempt == 2:
                log.warning("cleanup_old_data skipped: %s", e)
                return
            await asyncio.sleep(0.5 * (attempt + 1))


async def notification_job(bot):
    now=datetime.now(TZ)
    await cleanup_old_data(now)

    # 19:00 — расписание на завтра, без статусов и легенды.
    if now.hour==19 and now.minute==0:
        tomorrow=now.date()+timedelta(days=1)
        if is_study_day(tomorrow) and schedule_for(tomorrow):
            cc=db()
            users=cc.execute("SELECT s.telegram_id FROM students s JOIN notification_settings n ON n.telegram_id=s.telegram_id WHERE n.tomorrow_schedule=1").fetchall()
            cc.close()
            txt,_=await render_day(None,tomorrow,show_status=False,show_legend=False,show_navigation=False)
            for u in users:
                key=f"tomorrow:{tomorrow}"
                cc=db(); seen=cc.execute("SELECT 1 FROM sent_notifications WHERE telegram_id=? AND notification_key=?",(u['telegram_id'],key)).fetchone()
                if not seen:
                    try: await bot.send_message(u['telegram_id'],"📅 <b>Расписание на завтра</b>\n\n"+txt)
                    except Exception: pass
                    cc.execute("INSERT OR IGNORE INTO sent_notifications VALUES(?,?)",(u['telegram_id'],key)); cc.commit()
                cc.close()

    # Deadline reminders: once per homework, 24 hours before due date.
    if now.hour==18 and now.minute==0:
        cc=db()
        hs=cc.execute("SELECT h.id,h.discipline_id,h.due_date,d.name discipline,d.emoji,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h JOIN disciplines d ON d.id=h.discipline_id LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.hidden=0 AND h.archived=0").fetchall()
        users=cc.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL").fetchall()
        enabled={r['telegram_id'] for r in cc.execute("SELECT telegram_id FROM notification_settings WHERE deadline_reminders=1").fetchall()}
        cc.close()
        target=now.date()+timedelta(days=1)
        for h in hs:
            if parse_hw_date(h['due_date'])==target:
                for u in users:
                    if u['telegram_id'] not in enabled: continue
                    key=f"deadline:{h['id']}:{target}"
                    cc=db(); seen=cc.execute("SELECT 1 FROM sent_notifications WHERE telegram_id=? AND notification_key=?",(u['telegram_id'],key)).fetchone()
                    if not seen:
                        try:
                            await bot.send_message(u['telegram_id'],f"⏰ <b>Напоминание о дедлайне</b>\n\n{h['emoji']} <b>{h['discipline']}</b>\n{h['title']}\n\n📅 Сдать до: {h['due_date']}",reply_markup=ik([[b("📖 Открыть ДЗ",f"hw:item:{h['id']}:{h['discipline_id']}:0")]]))
                        except Exception: pass
                        cc.execute("INSERT OR IGNORE INTO sent_notifications VALUES(?,?)",(u['telegram_id'],key)); cc.commit()
                    cc.close()

    # Следующая пара: время выбирается пользователем в настройках.
    if now.weekday()==6:
        return
    lessons=schedule_for(now.date())
    if not lessons:
        return
    cc=db()
    users=cc.execute("""
        SELECT s.telegram_id, n.next_lesson_minutes
        FROM students s
        JOIN notification_settings n ON n.telegram_id=s.telegram_id
        WHERE n.next_lesson=1
    """).fetchall()
    cc.close()

    for x in lessons:
        start_dt=datetime.combine(now.date(),time.fromisoformat(x['start']),tzinfo=TZ)
        seconds=(start_dt-now).total_seconds()
        if seconds < 0:
            continue
        tr=TEACHERS.get(x['discipline'],'')
        cc=db(); r=cc.execute("SELECT name FROM teachers WHERE discipline_id=?",(x['discipline_id'],)).fetchone(); cc.close()
        tr=r['name'] if r else tr
        for u in users:
            minutes=max(1,int(u['next_lesson_minutes'] or 30))
            if seconds <= minutes*60:
                txt=f"🔔 <b>Следующая пара через {minutes} минут</b>\n\n{x['start']}–{x['end']} — {x['lesson_no']} пара | {x['lesson_type']}\n{x['emoji']} <b>{x['discipline']}</b>\n{tr} • ауд. {x['room']}"
                key=f"next:{now.date()}:{x['lesson_no']}:{x['start']}:{minutes}"
                cc=db(); seen=cc.execute("SELECT 1 FROM sent_notifications WHERE telegram_id=? AND notification_key=?",(u['telegram_id'],key)).fetchone()
                if not seen:
                    try: await bot.send_message(u['telegram_id'],txt)
                    except Exception: pass
                    cc.execute("INSERT OR IGNORE INTO sent_notifications VALUES(?,?)",(u['telegram_id'],key)); cc.commit()
                cc.close()


@router.callback_query(F.data=="noop")
async def noop(c:CallbackQuery): await c.answer()

async def main():
    migrate()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    scheduler=AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(notification_job,'interval',args=[bot],minutes=1,coalesce=True,max_instances=1)
    try:
        from services.google_grades import sync_google_grades
        scheduler.add_job(sync_google_grades,'interval',minutes=int(os.getenv('GOOGLE_GRADES_SYNC_MINUTES','60')),coalesce=True,max_instances=1,misfire_grace_time=300)
        log.info('Google grades sync scheduled every %s minutes', os.getenv('GOOGLE_GRADES_SYNC_MINUTES','60'))
    except Exception:
        log.exception('Google grades service could not be initialized; bot continues without sync')
    scheduler.start()
    log.info("R26 bot %s started",VERSION)
    try: await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()

if __name__=='__main__': asyncio.run(main())
