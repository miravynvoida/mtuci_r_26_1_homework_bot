import asyncio
import calendar
import logging
import os
import re
import sqlite3
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("r26bot")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "bot.db")))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
VERSION = os.getenv("BOT_VERSION", "2.1.1")
HELP_USERNAME = os.getenv("HELP_USERNAME", "@miravynvoida")
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is empty")

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


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = db()
    cur = c.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, discipline_id INTEGER NOT NULL UNIQUE, name TEXT NOT NULL, FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS bell_times (slot INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS schedule_days (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS schedule_lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_day_id INTEGER NOT NULL, lesson_no INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, discipline_id INTEGER NOT NULL, lesson_type TEXT NOT NULL, room TEXT NOT NULL, FOREIGN KEY(schedule_day_id) REFERENCES schedule_days(id) ON DELETE CASCADE, FOREIGN KEY(discipline_id) REFERENCES disciplines(id));
    CREATE TABLE IF NOT EXISTS notification_settings (telegram_id INTEGER PRIMARY KEY, tomorrow_schedule INTEGER NOT NULL DEFAULT 1, next_lesson INTEGER NOT NULL DEFAULT 1, FOREIGN KEY(telegram_id) REFERENCES students(telegram_id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS sent_notifications (telegram_id INTEGER NOT NULL, notification_key TEXT NOT NULL, PRIMARY KEY(telegram_id, notification_key));
    CREATE TABLE IF NOT EXISTS homework_extra (homework_id INTEGER PRIMARY KEY, title TEXT NOT NULL, FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE);
    """)
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
    try:
        await call.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        try: await call.message.delete()
        except Exception: pass
        await call.bot.send_message(call.from_user.id,text,reply_markup=markup,parse_mode=ParseMode.HTML)
    c=db(); c.execute("INSERT INTO ui_state(telegram_id,message_id) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET message_id=excluded.message_id",(call.from_user.id,call.message.message_id)); c.commit(); c.close()


def main_kb():
    return ik([
        [b("📅 Расписание","menu:schedule"), b("📝 Домашнее задание","menu:hw")],
        [b("📚 Учебники","menu:books"), b("⚙️ Настройки","menu:settings")],
        [b("👤 Личный кабинет","menu:soon")], [b("❓ Помощь","menu:help")]
    ])

async def main_menu(bot, uid, name=None):
    name = name or (is_auth(uid)["full_name"] if is_auth(uid) else "студент")
    return await show(bot,uid,f"👋 Привет, {name}!\n\nЯ — информационный бот группы <b>Р-26-1</b>.\nЗдесь можно посмотреть расписание, домашние задания и учебники.",main_kb())

class Auth(StatesGroup): name=State()
class HWCreate(StatesGroup): discipline=State(); title=State(); task=State(); due=State(); explanation=State(); media=State(); preview=State()
class HWEdit(StatesGroup): field=State(); value=State(); media=State()
class ScheduleCreate(StatesGroup): count=State(); lesson_start=State(); discipline=State(); typ=State(); room=State()
class StudentAdd(StatesGroup): name=State()
class DisciplineEdit(StatesGroup): name=State(); emoji=State()
class BookCreate(StatesGroup): title=State(); file=State()
class Broadcast(StatesGroup): text=State()

@router.message(Command("start"))
async def start(m:Message,state:FSMContext):
    await state.clear()
    if is_auth(m.from_user.id): return await main_menu(m.bot,m.from_user.id)
    await m.answer("👋 Привет! Для доступа к боту введи своё ФИО, как в списке студентов.")
    await state.set_state(Auth.name)

@router.message(Auth.name)
async def auth_name(m:Message,state:FSMContext):
    c=db(); r=c.execute("SELECT * FROM students WHERE normalized_name=?",(norm(m.text or ""),)).fetchone()
    if not r:
        c.close(); return await m.answer("❌ Такого ФИО нет в списке. Проверь написание и попробуй ещё раз.")
    if r["telegram_id"] not in (None,m.from_user.id):
        c.close(); return await m.answer("❌ Это ФИО уже привязано к другому Telegram ID.")
    c.execute("UPDATE students SET telegram_id=? WHERE id=?",(m.from_user.id,r["id"])); c.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(m.from_user.id,)); c.commit(); c.close(); await state.clear(); await main_menu(m.bot,m.from_user.id,r["full_name"])

@router.message(Command("info"))
async def info(m:Message):
    name=is_auth(m.from_user.id); n=name["full_name"] if name else "не авторизован"
    await m.answer(f"ℹ️ <b>Информация о боте</b>\n\n🤖 Инфо-бот Р-26-1\n📦 Версия: <b>{VERSION}</b>\n\n👤 <b>Пользователь</b>\n📝 ФИО: {n}\n🆔 Telegram ID: <code>{m.from_user.id}</code>\n\n💬 Помощь: {HELP_USERNAME}")

@router.callback_query(F.data=="menu:soon")
async def soon(c:CallbackQuery): await c.answer("🚧 Личный кабинет пока в разработке.",show_alert=True)
@router.callback_query(F.data=="menu:help")
async def help_(c:CallbackQuery): await c.answer(); await c.message.answer(f"❓ Помощь: {HELP_USERNAME}")
@router.callback_query(F.data=="menu:home")
async def home(c:CallbackQuery): await c.answer(); await main_menu(c.bot,c.from_user.id)

# Schedule
@router.callback_query(F.data=="menu:schedule")
async def schedule_menu(c:CallbackQuery):
    await c.answer(); await edit_or_answer(c,"📅 <b>Выберите период для показа расписания</b>",ik([[b("📌 Сегодня","sch:day:0"),b("➡️ Завтра","sch:day:1")],[b("🗓 Выбрать день","sch:cal:2026:9")],[b("🏠 Назад","menu:home")]]))

def day_str(d): return d.strftime('%Y-%m-%d')
def ru_date(d): return f"{calendar.day_name[d.weekday()] and ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'][d.weekday()]}, {d.day:02d}.{d.month:02d}.{d.year}"

def schedule_for(d):
    c=db(); day=c.execute("SELECT * FROM schedule_days WHERE day=?",(day_str(d),)).fetchone()
    lessons=[]
    if day: lessons=c.execute("SELECT l.*,d.name discipline,d.emoji FROM schedule_lessons l JOIN disciplines d ON d.id=l.discipline_id WHERE l.schedule_day_id=? ORDER BY lesson_no",(day['id'],)).fetchall()
    c.close(); return lessons

async def render_day(c,d):
    lessons=schedule_for(d)
    if not lessons:
        txt=f"📅 <b>{ru_date(d)}</b>\n\nℹ️ Расписание на этот день ещё не опубликовано."
    else:
        parts=[f"📅 <b>{ru_date(d)}</b>"]
        for x in lessons:
            teacher=TEACHERS.get(x['discipline'],"")
            # DB teacher override
            cc=db(); tr=cc.execute("SELECT name FROM teachers WHERE discipline_id=?",(x['discipline_id'],)).fetchone(); cc.close()
            teacher=tr['name'] if tr else teacher
            parts.append(f"\n<b>{x['start']}–{x['end']} — {x['lesson_no']} пара | {x['lesson_type']}</b>\n{x['emoji']} <b>{x['discipline']}</b>\n{teacher} • ауд. {x['room']}")
        txt="\n".join(parts)
    prev=(d-timedelta(days=1)).isoformat(); nxt=(d+timedelta(days=1)).isoformat()
    return txt,ik([[b("◀️ Предыдущий день",f"sch:date:{prev}"),b("▶️ Следующий день",f"sch:date:{nxt}")],[b("🗓 Выбрать день",f"sch:cal:{d.year}:{d.month}" )],[b("🏠 Главное меню","menu:home")]])

@router.callback_query(F.data.startswith("sch:day:"))
async def sch_day(c:CallbackQuery):
    offset=int(c.data.split(':')[-1]); d=datetime.now(TZ).date()+timedelta(days=offset); txt,kb=await render_day(c,d); await c.answer(); await edit_or_answer(c,txt,kb)
@router.callback_query(F.data.startswith("sch:date:"))
async def sch_date(c:CallbackQuery):
    d=date.fromisoformat(c.data.split(':',2)[2]); txt,kb=await render_day(c,d); await c.answer(); await edit_or_answer(c,txt,kb)

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
            label=f"⭐{n}" if d==today else str(n)
            if date(2026,9,1)<=d<=date(2027,7,31): row.append(b(label,f"{prefix}:date:{d.isoformat()}"))
            else: row.append(b(label,"noop"))
        rows.append(row)
    rows.append([b("🏠 Назад","menu:schedule")])
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
    cdb=db(); rows=cdb.execute("SELECT d.*,COUNT(h.id) n FROM disciplines d JOIN homework h ON h.discipline_id=d.id AND h.hidden=0 AND h.archived=0 GROUP BY d.id ORDER BY d.name",()).fetchall(); cdb.close()
    kb=two_col([b(f"{r['emoji']} {r['name']}",f"hw:disc:{r['id']}:0") for r in rows]); kb.append([b("🏠 Назад","menu:home")]); await c.answer(); await edit_or_answer(c,"📝 <b>Домашнее задание</b>\n\nВыберите дисциплину:",ik(kb))

@router.callback_query(F.data.startswith("hw:disc:"))
async def hw_disc(c:CallbackQuery):
    _,_,did,p=c.data.split(':'); did=int(did); page=int(p); off=page*7
    cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.hidden=0 AND h.archived=0 ORDER BY h.due_date,h.id",(did,)).fetchall(); cc.close()
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
    task=h['text']
    if task.startswith(title): task=task[len(title):].lstrip('\n')
    txt=f"{h['emoji']} <b>{h['discipline']}</b>\n\n<b>📝 {title}</b>\n{task}\n\n📅 <b>Дата сдачи:</b> {h['due_date']}\n📤 <b>Опубликовано:</b> {h['published_date']}"
    if h['explanation']: txt+=f"\n\n💬 <b>Пояснение:</b>\n{h['explanation']}"
    kb=[[b("💡 Посмотреть ответ",f"hw:answer:{hid}")],[b("🔙 Назад в дисциплину",f"hw:disc:{did}:{page}")]]
    await c.answer(); await edit_or_answer(c,txt,ik(kb))
    cc=db(); media=cc.execute("SELECT * FROM homework_media WHERE homework_id=? ORDER BY id",(hid,)).fetchall(); cc.close()
    for x in media:
        try:
            if x['kind']=='photo': await c.bot.send_photo(c.from_user.id,x['file_id'],caption=x['caption'])
            elif x['kind']=='video': await c.bot.send_video(c.from_user.id,x['file_id'],caption=x['caption'])
            else: await c.bot.send_document(c.from_user.id,x['file_id'],caption=x['caption'])
        except Exception: pass

@router.callback_query(F.data.startswith("hw:answer:"))
async def hw_answer(c:CallbackQuery):
    hid=int(c.data.split(':')[-1]); cc=db(); a=cc.execute("SELECT * FROM homework_answer WHERE homework_id=?",(hid,)).fetchone(); medias=cc.execute("SELECT * FROM homework_media WHERE homework_id=?",(hid,)).fetchall(); cc.close()
    if not is_vip(c.from_user.id): return await c.answer("🔒 Доступ к готовым ответам доступен только VIP-пользователям.",show_alert=True)
    if not a and not medias: return await c.answer("⏳ Готовое решение ещё не опубликовано.",show_alert=True)
    await c.answer();
    if a:
        if a['text']: await c.message.answer("💡 <b>Готовый ответ</b>\n\n"+a['text'])
        elif a['file_id']:
            if a['kind']=='photo': await c.message.answer_photo(a['file_id'],caption=a['caption'] or "💡 Готовый ответ")
            else: await c.message.answer_document(a['file_id'],caption=a['caption'] or "💡 Готовый ответ")
    for x in medias:
        if x['kind']=='photo': await c.message.answer_photo(x['file_id'],caption=x['caption'])
        elif x['kind']=='video': await c.message.answer_video(x['file_id'],caption=x['caption'])
        else: await c.message.answer_document(x['file_id'],caption=x['caption'])

# Books
@router.callback_query(F.data=="menu:books")
async def books_menu(c:CallbackQuery):
    cc=db(); rows=cc.execute("SELECT d.*,COUNT(t.id) n FROM disciplines d JOIN textbooks t ON t.discipline_id=d.id WHERE d.active=1 GROUP BY d.id ORDER BY d.name").fetchall(); cc.close(); kb=two_col([b(f"{r['emoji']} {r['name']}",f"book:disc:{r['id']}:0") for r in rows]); kb.append([b("🏠 Назад","menu:home")]); await c.answer(); await edit_or_answer(c,"📚 <b>Учебники</b>\n\nВыберите дисциплину:",ik(kb))
@router.callback_query(F.data.startswith("book:disc:"))
async def books_disc(c:CallbackQuery):
    _,_,did,p=c.data.split(':'); did=int(did);p=int(p);cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); ts=cc.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id",(did,)).fetchall();cc.close(); subset=ts[p*7:p*7+7]; rows=[[b(f"📘 {x['title'][:55]}",f"book:item:{x['id']}:{did}:{p}")] for x in subset]; nav=[]
    if p: nav.append(b("⬅️ Предыдущая",f"book:disc:{did}:{p-1}"))
    if p*7+7<len(ts): nav.append(b("➡️ Следующая",f"book:disc:{did}:{p+1}"))
    if nav: rows.append(nav)
    rows.append([b("🔙 Назад к учебникам","menu:books")]); await c.answer(); await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))
@router.callback_query(F.data.startswith("book:item:"))
async def book_item(c:CallbackQuery):
    _,_,bid,did,p=c.data.split(':');cc=db();x=cc.execute("SELECT * FROM textbooks WHERE id=?",(bid,)).fetchone();cc.close();await c.answer();
    if x['kind']=='photo': await c.message.answer_photo(x['file_id'],caption=f"📘 {x['title']}")
    else: await c.message.answer_document(x['file_id'],caption=f"📘 {x['title']}")

# Settings
@router.callback_query(F.data=="menu:settings")
async def settings(c:CallbackQuery):
    cc=db(); s=cc.execute("SELECT * FROM notification_settings WHERE telegram_id=?",(c.from_user.id,)).fetchone(); cc.close();
    if not s:
        cc=db();cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(c.from_user.id,));cc.commit();cc.close();s={'tomorrow_schedule':1,'next_lesson':1}
    await c.answer(); await edit_or_answer(c,f"⚙️ <b>Настройки уведомлений</b>\n\n📅 Расписание на завтра: {'✅' if s['tomorrow_schedule'] else '❌'}\n🔔 Следующая пара: {'✅' if s['next_lesson'] else '❌'}",ik([[b("📅 Расписание на завтра", "set:tomorrow"),b("🔔 Следующая пара","set:next")],[b("🏠 Назад","menu:home")]]))
@router.callback_query(F.data.in_({"set:tomorrow","set:next"}))
async def toggle(c:CallbackQuery):
    col='tomorrow_schedule' if c.data=='set:tomorrow' else 'next_lesson';cc=db();cc.execute("INSERT OR IGNORE INTO notification_settings(telegram_id) VALUES(?)",(c.from_user.id,));cc.execute(f"UPDATE notification_settings SET {col}=1-{col} WHERE telegram_id=?",(c.from_user.id,));cc.commit();cc.close();await settings(c)

# Admin helpers
async def admin_only(c):
    if not is_admin(c.from_user.id): await c.answer("⛔ Доступ запрещён.",show_alert=True); return False
    return True

def admin_kb():
    return ik([[b("📝 Новое ДЗ","adm:newhw")],[b("🛠 Управление ДЗ","adm:hw")],[b("📚 Управление учебниками","adm:books")],[b("📅 Управление расписанием","adm:schedule")],[b("👥 Управление студентами","adm:students")],[b("⭐ Управление VIP","adm:vip")],[b("📖 Дисциплины","adm:disc")],[b("📢 Уведомление всем","adm:broadcast")],[b("🏠 Главное меню","menu:home")]])

@router.message(Command("admin"))
async def admin(m:Message):
    if not is_admin(m.from_user.id): return await m.answer("⛔ Доступ запрещён.")
    await show(m.bot,m.from_user.id,"🛠 <b>Админ-панель</b>\n\nВыберите действие:",admin_kb())
@router.callback_query(F.data.startswith("adm:"))
async def admin_router(c:CallbackQuery,state:FSMContext):
    if not await admin_only(c): return
    act=c.data
    if act=='adm:newhw': return await newhw_start(c,state)
    if act=='adm:hw': return await admin_hw(c)
    if act=='adm:books': return await admin_books(c)
    if act=='adm:schedule': return await admin_schedule(c)
    if act=='adm:students': return await admin_students(c)
    if act=='adm:vip': return await admin_vip(c)
    if act=='adm:disc': return await admin_disc(c)
    if act=='adm:broadcast': return await broadcast_start(c,state)

async def newhw_start(c,state):
    cc=db(); ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall();cc.close(); await state.set_state(HWCreate.discipline); await c.answer(); await edit_or_answer(c,"📝 <b>Новое ДЗ</b>\n\nВыберите дисциплину:",ik(two_col([b(f"{d['emoji']} {d['name']}",f"newhw:d:{d['id']}") for d in ds])+[[b("❌ Отмена","adm:back")]]))
@router.callback_query(F.data.startswith("newhw:d:"),HWCreate.discipline)
async def newhw_d(c,state): await state.update_data(discipline=int(c.data.split(':')[-1])); await state.set_state(HWCreate.title); await c.answer(); await c.message.answer("Введите название ДЗ:")
@router.message(HWCreate.title)
async def newhw_title(m,state): await state.update_data(title=m.text); await state.set_state(HWCreate.task); await m.answer("Введите текст задания:")
@router.message(HWCreate.task)
async def newhw_task(m,state): await state.update_data(task=m.text); await state.set_state(HWCreate.due); await m.answer("Введите дату сдачи строго в формате <code>ДД.ММ.ГГГГ</code>:")
@router.message(HWCreate.due)
async def newhw_due(m,state):
    try: d=datetime.strptime(m.text.strip(),'%d.%m.%Y').date()
    except ValueError: return await m.answer("❌ Неверный формат. Пример: <code>28.09.2026</code>")
    await state.update_data(due=d.strftime('%d.%m.%Y'));await state.set_state(HWCreate.explanation);await m.answer("Введите пояснение или отправьте <code>-</code>:")
@router.message(HWCreate.explanation)
async def newhw_expl(m,state): await state.update_data(explanation='' if (m.text or '').strip()=='-' else m.text); await state.set_state(HWCreate.media); await m.answer("📎 Прикрепите файл(ы) по одному сообщению или нажмите «Без файлов».",reply_markup=ik([[b("Без файлов","newhw:no_media")],[b("Готово","newhw:done_media")]]))

def media_info(m):
    if m.document: return ('document',m.document.file_id,m.document.file_name)
    if m.photo: return ('photo',m.photo[-1].file_id,'')
    if m.video: return ('video',m.video.file_id,'')
    return None
@router.message(HWCreate.media)
async def newhw_media(m,state):
    x=media_info(m)
    if not x: return await m.answer("Пришли документ/фото/видео или нажми «Готово».")
    data=await state.get_data(); arr=data.get('media',[]);arr.append(x);await state.update_data(media=arr);await m.answer(f"✅ Файл добавлен. Всего: {len(arr)}. Можно отправить ещё или нажать «Готово»." )
@router.callback_query(F.data.in_({"newhw:no_media","newhw:done_media"}),HWCreate.media)
async def newhw_preview(c,state):
    data=await state.get_data();cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(data['discipline'],)).fetchone();cc.close(); txt=f"📝 <b>Предпросмотр ДЗ</b>\n\n{d['emoji']} <b>{d['name']}</b>\n<b>{data['title']}</b>\n\n{data['task']}\n\n📅 Сдать до: {data['due']}\n📤 Опубликовано: {date.today().strftime('%d.%m.%Y')}\n\n💬 {data['explanation'] or 'Без пояснения'}\n\n📎 Файлов: {len(data.get('media',[]))}";await state.set_state(HWCreate.preview);await c.answer();await edit_or_answer(c,txt,ik([[b("✅ Опубликовать","newhw:publish")],[b("🔄 Заполнить заново","adm:newhw")],[b("❌ Отмена","adm:back")]]))
@router.callback_query(F.data=="newhw:publish",HWCreate.preview)
async def newhw_publish(c,state):
    data=await state.get_data();now=datetime.now(TZ).date().strftime('%d.%m.%Y');cc=db();cur=cc.cursor();cur.execute("INSERT INTO homework(discipline_id,text,explanation,published_date,due_date) VALUES(?,?,?,?,?)",(data['discipline'],data['task'],data.get('explanation',''),now,data['due']));hid=cur.lastrowid
    # title is stored as the first line so old schema stays intact
    cur.execute("INSERT INTO homework_extra(homework_id,title) VALUES(?,?)",(hid,data['title']))
    for kind,fid,caption in data.get('media',[]): cur.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption) VALUES(?,?,?,?)",(hid,kind,fid,caption))
    cc.commit();cc.close();await state.clear();await c.answer("Опубликовано!");await show(c.bot,c.from_user.id,"✅ ДЗ опубликовано.\n\n📢 Авторизованные пользователи получат уведомление «📝 Новое ДЗ».",admin_kb());await broadcast_new_hw(c.bot)

async def broadcast_new_hw(bot):
    cc=db(); rows=cc.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL").fetchall();cc.close()
    for r in rows:
        try: await bot.send_message(r['telegram_id'],"📝 <b>Новое ДЗ</b>\n\nОткрой раздел «Домашнее задание», чтобы посмотреть детали.")
        except Exception: pass

@router.callback_query(F.data=="adm:back")
async def adm_back(c:CallbackQuery,state:FSMContext): await state.clear();await c.answer();await edit_or_answer(c,"🛠 <b>Админ-панель</b>",admin_kb())

# Admin HW
async def admin_hw(c):
    cc=db();ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall();cc.close();await c.answer();await edit_or_answer(c,"🛠 <b>Управление ДЗ</b>\n\nВыберите дисциплину:",ik(two_col([b(f"{d['emoji']} {d['name']}",f"ahwd:{d['id']}:0") for d in ds])+[[b("🏠 Админ-панель","adm:back")]]))
@router.callback_query(F.data.startswith("ahwd:"))
async def ahwd(c):
    _,did,p=c.data.split(':');did=int(did);p=int(p);cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone();hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.archived=0 ORDER BY h.id DESC",(did,)).fetchall();cc.close();rows=[[b(f"📝 {h['title'][:45]} • {h['due_date']}",f"ahwi:{h['id']}:{did}:{p}")] for h in hs[p*7:p*7+7]];rows.append([b("📦 Архив",f"ahwa:{did}:0")]);rows.append([b("🔙 Назад","adm:hw")]);await c.answer();await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))
@router.callback_query(F.data.startswith("ahwa:"))
async def ahwa(c:CallbackQuery):
    _,did,p=c.data.split(':'); did=int(did); p=int(p); cc=db(); d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone(); hs=cc.execute("SELECT h.*,COALESCE(e.title,substr(h.text,1,60)) title FROM homework h LEFT JOIN homework_extra e ON e.homework_id=h.id WHERE h.discipline_id=? AND h.archived=1 ORDER BY h.id DESC",(did,)).fetchall(); cc.close()
    rows=[[b(f"📦 {h['title'][:45]} • {h['due_date']}",f"ahwiarch:{h['id']}:{did}:{p}")] for h in hs[p*7:p*7+7]]
    rows.append([b("🔙 Назад",f"ahwd:{did}:0")]); await c.answer(); await edit_or_answer(c,f"📦 <b>Архив — {d['name']}</b>",ik(rows))
@router.callback_query(F.data.startswith("ahwiarch:"))
async def ahwiarch(c:CallbackQuery):
    _,hid,did,p=c.data.split(':'); cc=db(); h=cc.execute("SELECT h.*,d.name discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)).fetchone(); cc.close(); await c.answer(); await edit_or_answer(c,f"📦 {h['emoji']} <b>{h['discipline']}</b>\n\n{h['text']}\n\n📅 {h['due_date']}",ik([[b("♻️ Вернуть из архива",f"ahwunarch:{hid}:{did}:{p}")],[b("🗑 Удалить",f"ahwdel:{hid}:{did}:{p}")],[b("🔙 Назад",f"ahwa:{did}:{p}")]]))
@router.callback_query(F.data.startswith("ahwunarch:"))
async def ahwunarch(c:CallbackQuery):
    _,hid,did,p=c.data.split(':'); cc=db(); cc.execute("UPDATE homework SET archived=0 WHERE id=?",(hid,)); cc.commit(); cc.close(); await c.answer("Возвращено из архива"); await ahwa(c)

@router.callback_query(F.data.startswith("ahwi:"))
async def ahwi(c:CallbackQuery):
    _,hid,did,p=c.data.split(':')
    cc=db(); h=cc.execute("SELECT h.*,d.name discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)).fetchone(); ex=cc.execute("SELECT title FROM homework_extra WHERE homework_id=?",(hid,)).fetchone(); cc.close()
    await c.answer()
    title=ex['title'] if ex else h['text'].splitlines()[0]
    txt=f"{h['emoji']} <b>{h['discipline']}</b>\n\n<b>{title}</b>\n{h['text']}\n\n📅 {h['due_date']}\n📤 {h['published_date']}\n💬 {h['explanation'] or '—'}"
    await edit_or_answer(c,txt,ik([[b("✏️ Редактировать",f"ahwe:{hid}")],[b("🗑 Удалить",f"ahwdel:{hid}:{did}:{p}")],[b("📦 В архив",f"ahwarch:{hid}:{did}:{p}")],[b("🔙 Назад",f"ahwd:{did}:{p}")]]))
@router.callback_query(F.data.startswith("ahwdel:"))
async def ahwdel(c):
    _,hid,did,p=c.data.split(':');await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить ДЗ безвозвратно?</b>\nЭто удалит и прикреплённые файлы, и готовый ответ.",ik([[b("🗑 Да, удалить",f"ahwdelok:{hid}:{did}:{p}")],[b("❌ Отмена",f"ahwi:{hid}:{did}:{p}")]]))
@router.callback_query(F.data.startswith("ahwdelok:"))
async def ahwdelok(c):
    _,hid,did,p=c.data.split(':');cc=db();cc.execute("DELETE FROM homework WHERE id=?",(hid,));cc.commit();cc.close();await c.answer("Удалено");await ahwd(c)
@router.callback_query(F.data.startswith("ahwarch:"))
async def ahwarch(c):
    _,hid,did,p=c.data.split(':');cc=db();cc.execute("UPDATE homework SET archived=1 WHERE id=?",(hid,));cc.commit();cc.close();await c.answer("Перемещено в архив");await ahwd(c)

# simple HW editor
@router.callback_query(F.data.startswith("ahwe:"))
async def ahwe(c:CallbackQuery,state:FSMContext):
    hid=int(c.data.split(':')[-1]);await state.update_data(hid=hid);await c.answer();await edit_or_answer(c,"✏️ <b>Что изменить?</b>",ik([[b("Название","edit:title")],[b("Задание","edit:task")],[b("Дата сдачи","edit:due")],[b("Дата публикации","edit:pub")],[b("Пояснение","edit:expl")],[b("📎 Файлы ДЗ","edit:media")],[b("💡 Готовый ответ","edit:answer")],[b("🔙 Назад","adm:hw")]]))
@router.callback_query(F.data.startswith("edit:"))
async def edit_field(c:CallbackQuery,state:FSMContext):
    field=c.data.split(':')[1];await state.update_data(field=field);await state.set_state(HWEdit.value);await c.answer();await c.message.answer("Введите новое значение. Для готового ответа можно отправить текст или документ.")
@router.message(HWEdit.value)
async def edit_value(m:Message,state:FSMContext):
    data=await state.get_data();hid=data['hid'];field=data['field'];cc=db()
    if field=='answer':
        if m.document: cc.execute("INSERT INTO homework_answer(homework_id,kind,file_id) VALUES(?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind=excluded.kind,file_id=excluded.file_id,text=NULL",(hid,'document',m.document.file_id))
        elif m.photo: cc.execute("INSERT INTO homework_answer(homework_id,kind,file_id) VALUES(?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind=excluded.kind,file_id=excluded.file_id,text=NULL",(hid,'photo',m.photo[-1].file_id))
        else: cc.execute("INSERT INTO homework_answer(homework_id,kind,text) VALUES(?,?,?) ON CONFLICT(homework_id) DO UPDATE SET kind=excluded.kind,text=excluded.text,file_id=NULL",(hid,'text',m.text or ''))
    elif field=='title':
        cc.execute("INSERT INTO homework_extra(homework_id,title) VALUES(?,?) ON CONFLICT(homework_id) DO UPDATE SET title=excluded.title",(hid,m.text.strip()))
    elif field=='media':
        x=media_info(m)
        if not x: cc.close(); return await m.answer("Отправь файл/фото/видео.")
        cc.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption) VALUES(?,?,?,?)",(hid,x[0],x[1],x[2]))
    else:
        col={'task':'text','due':'due_date','pub':'published_date','expl':'explanation'}[field];cc.execute(f"UPDATE homework SET {col}=? WHERE id=?",(m.text,hid))
    cc.commit();cc.close();await state.clear();await m.answer("✅ Сохранено.");

# Admin books
async def admin_books(c):
    cc=db();ds=cc.execute("SELECT d.*,COUNT(t.id) n FROM disciplines d LEFT JOIN textbooks t ON t.discipline_id=d.id WHERE d.active=1 GROUP BY d.id ORDER BY d.name").fetchall();cc.close();rows=[[b(f"{d['emoji']} {d['name']} {'🟢' if d['n'] else '⚪'}",f"abooksd:{d['id']}")] for d in ds];rows.append([b("🏠 Админ-панель","adm:back")]);await c.answer();await edit_or_answer(c,"📚 <b>Управление учебниками</b>",ik(rows))
@router.callback_query(F.data.startswith("abooksd:"))
async def abooksd(c:CallbackQuery):
    did=int(c.data.split(':')[-1]);cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone();ts=cc.execute("SELECT * FROM textbooks WHERE discipline_id=?",(did,)).fetchall();cc.close();rows=[[b(f"📘 {t['title'][:55]}",f"abook:{t['id']}:{did}")] for t in ts];rows.append([b("➕ Добавить учебник",f"abooknew:{did}")]);rows.append([b("🔙 Назад", "adm:books")]);await c.answer();await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik(rows))
@router.callback_query(F.data.startswith("abooknew:"))
async def abooknew(c,state): await state.set_state(BookCreate.title);await state.update_data(did=int(c.data.split(':')[-1]));await c.answer();await c.message.answer("Введите название учебника:")
@router.message(BookCreate.title)
async def abooktitle(m,state):
    data=await state.get_data()
    if 'bid' in data:
        cc=db(); cc.execute("UPDATE textbooks SET title=? WHERE id=?",(m.text.strip(),data['bid'])); cc.commit(); cc.close(); await state.clear(); return await m.answer("✅ Название изменено.")
    await state.update_data(title=m.text);await state.set_state(BookCreate.file);await m.answer("Отправьте файл учебника:")
@router.message(BookCreate.file)
async def abookfile(m,state):
    x=media_info(m)
    if not x: return await m.answer("Нужен документ, фото или видео.")
    data=await state.get_data();cc=db();cc.execute("INSERT INTO textbooks(discipline_id,title,kind,file_id,created_at) VALUES(?,?,?,?,?)",(data['did'],data['title'],x[0],x[1],datetime.now(TZ).isoformat()));cc.commit();cc.close();await state.clear();await m.answer("✅ Учебник добавлен.")
@router.callback_query(F.data.startswith("abook:"))
async def abook(c):
    _,bid,did=c.data.split(':');cc=db();t=cc.execute("SELECT * FROM textbooks WHERE id=?",(bid,)).fetchone();cc.close();await c.answer();await edit_or_answer(c,f"📘 <b>{t['title']}</b>",ik([[b("✏️ Изменить название",f"abookrename:{bid}:{did}")],[b("🗑 Удалить",f"abookdel:{bid}:{did}")],[b("🔙 Назад",f"abooksd:{did}")]]))
@router.callback_query(F.data.startswith("abookrename:"))
async def abookrename(c:CallbackQuery,state:FSMContext):
    _,bid,did=c.data.split(':'); await state.update_data(bid=int(bid),did=int(did)); await state.set_state(BookCreate.title); await c.answer(); await c.message.answer("Введите новое название учебника:")
@router.callback_query(F.data.startswith("abookdel:"))
async def abookdel(c):
    _,bid,did=c.data.split(':');await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить учебник безвозвратно?</b>",ik([[b("🗑 Да, удалить",f"abookdelok:{bid}:{did}")],[b("❌ Отмена",f"abook:{bid}:{did}")]]))
@router.callback_query(F.data.startswith("abookdelok:"))
async def abookdelok(c):
    _,bid,did=c.data.split(':');cc=db();cc.execute("DELETE FROM textbooks WHERE id=?",(bid,));cc.commit();cc.close();await c.answer("Удалено");await abooksd(c)

# Schedule admin
async def admin_schedule(c): await c.answer();await edit_or_answer(c,"📅 <b>Управление расписанием</b>\nВыберите месяц:",calendar_kb(2026,9,'asch'))
@router.callback_query(F.data.startswith("asch:month:"))
async def asch_month(c):
    _,_,y,m=c.data.split(':');y=int(y);m=int(m);y,m=max((2026,9),(y,m));y,m=min((2027,7),(y,m));await c.answer();await edit_or_answer(c,f"📅 <b>{m:02d}.{y}</b>\nВыберите дату:",calendar_kb(y,m,'asch'))
@router.callback_query(F.data.startswith("asch:date:"))
async def asch_date(c,state):
    d=date.fromisoformat(c.data.split(':',2)[2]);cc=db();day=cc.execute("SELECT id FROM schedule_days WHERE day=?",(d.isoformat(),)).fetchone();cc.close();await c.answer();
    if day: return await edit_or_answer(c,f"📅 <b>{ru_date(d)}</b>\nРасписание уже опубликовано.",ik([[b("✏️ Редактировать","asch:edit:"+d.isoformat())],[b("🗑 Удалить",f"asch:del:{d.isoformat()}")],[b("🔙 Назад",f"adm:schedule")]]))
    await edit_or_answer(c,f"📅 <b>{ru_date(d)}</b>\nРасписания ещё нет.",ik([[b("➕ Добавить расписание",f"asch:add:{d.isoformat()}")],[b("🔙 Назад","adm:schedule")]]))
@router.callback_query(F.data.startswith("asch:add:"))
async def asch_add(c,state):
    d=c.data.split(':',2)[2];await state.update_data(day=d);await state.set_state(ScheduleCreate.count);await c.answer();await edit_or_answer(c,"Сколько всего пар?",ik([[b(str(i),f"schc:count:{i}") for i in range(1,6)],[b("❌ Отмена","adm:schedule")]]))
@router.callback_query(F.data.startswith("schc:count:"),ScheduleCreate.count)
async def schc_count(c,state): await state.update_data(count=int(c.data.split(':')[-1]),idx=1);await state.set_state(ScheduleCreate.lesson_start);await c.answer();await edit_or_answer(c,"Выберите время 1-й пары:",ik([[b(f"{s}–{e}",f"schc:time:{s}:{e}")] for s,e in SLOT_TIMES]))
@router.callback_query(F.data.startswith("schc:time:"),ScheduleCreate.lesson_start)
async def schc_time(c,state):
    _,_,s,e=c.data.split(':');await state.update_data(start=s,end=e);await state.set_state(ScheduleCreate.discipline);cc=db();ds=cc.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name").fetchall();cc.close();await c.answer();await edit_or_answer(c,"Выберите дисциплину:",ik(two_col([b(f"{d['emoji']} {d['name']}",f"schc:d:{d['id']}") for d in ds])))
@router.callback_query(F.data.startswith("schc:d:"),ScheduleCreate.discipline)
async def schc_d(c,state): await state.update_data(discipline=int(c.data.split(':')[-1]));await state.set_state(ScheduleCreate.typ);await c.answer();await edit_or_answer(c,"Выберите тип пары:",ik([[b("📖 ЛЕК","schc:t:ЛЕК"),b("📝 ПР","schc:t:ПР"),b("🔬 ЛАБ","schc:t:ЛАБ")]]))
@router.callback_query(F.data.startswith("schc:t:"),ScheduleCreate.typ)
async def schc_t(c,state): await state.update_data(typ=c.data.split(':',2)[2]);await state.set_state(ScheduleCreate.room);await c.answer();await c.message.answer("Введите номер аудитории:")
@router.message(ScheduleCreate.room)
async def schc_room(m,state):
    data=await state.get_data();idx=data['idx'];less=data.get('lessons',[]);less.append((idx,data['start'],data['end'],data['discipline'],data['typ'],m.text.strip()));
    if idx<data['count']:
        await state.update_data(lessons=less,idx=idx+1);await state.set_state(ScheduleCreate.lesson_start);await m.answer(f"Выберите время {idx+1}-й пары:",reply_markup=ik([[b(f"{s}–{e}",f"schc:time:{s}:{e}")] for s,e in SLOT_TIMES]));return
    cc=db();now=datetime.now(TZ).isoformat();cc.execute("INSERT INTO schedule_days(day,created_at,updated_at) VALUES(?,?,?)",(data['day'],now,now));sid=cc.execute("SELECT last_insert_rowid()").fetchone()[0]
    for x in less: cc.execute("INSERT INTO schedule_lessons(schedule_day_id,lesson_no,start,end,discipline_id,lesson_type,room) VALUES(?,?,?,?,?,?,?)",(sid,*x))
    cc.commit();cc.close();await state.clear();await m.answer("✅ Расписание опубликовано.")
@router.callback_query(F.data.startswith("asch:del:"))
async def asch_del(c):
    d=c.data.split(':',2)[2];await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить расписание на этот день безвозвратно?</b>",ik([[b("🗑 Да, удалить",f"asch:delok:{d}")],[b("❌ Отмена",f"asch:date:{d}")]]))
@router.callback_query(F.data.startswith("asch:delok:"))
async def asch_delok(c):
    d=c.data.split(':',2)[2];cc=db();cc.execute("DELETE FROM schedule_days WHERE day=?",(d,));cc.commit();cc.close();await c.answer("Удалено");await asch_date(CallbackQuery(id=c.id,from_user=c.from_user,chat_instance=c.chat_instance,message=c.message,data='asch:date:'+d),FSMContext) if False else c.message.edit_text("🗑 Расписание удалено.")
@router.callback_query(F.data.startswith("asch:edit:"))
async def asch_edit(c:CallbackQuery,state:FSMContext):
    d=c.data.split(':',2)[2]; await c.answer(); await edit_or_answer(c,"✏️ <b>Редактировать расписание?</b>\nТекущее расписание будет заменено новой версией.",ik([[b("✅ Да, редактировать",f"asch:editok:{d}")],[b("❌ Отмена",f"asch:date:{d}")]]))
@router.callback_query(F.data.startswith("asch:editok:"))
async def asch_editok(c:CallbackQuery,state:FSMContext):
    d=c.data.split(':',2)[2]; cc=db(); cc.execute("DELETE FROM schedule_days WHERE day=?",(d,)); cc.commit(); cc.close(); await state.update_data(day=d); await state.set_state(ScheduleCreate.count); await c.answer(); await edit_or_answer(c,"Сколько всего пар?",ik([[b(str(i),f"schc:count:{i}") for i in range(1,6)],[b("❌ Отмена","adm:schedule")]]))

# Students / VIP / disciplines
async def admin_students(c):
    cc=db();ss=cc.execute("SELECT * FROM students ORDER BY full_name").fetchall();cc.close(); rows=[[b(f"👤 {s['full_name']} {'🟢' if s['telegram_id'] else '⚪'}",f"astud:{s['id']}")] for s in ss];rows.append([b("➕ Добавить студента","astudadd")]);rows.append([b("🏠 Админ-панель","adm:back")]);await c.answer();await edit_or_answer(c,"👥 <b>Управление студентами</b>",ik(rows))
@router.callback_query(F.data.startswith("astud:"))
async def astud(c):
    sid=int(c.data.split(':')[-1]);cc=db();s=cc.execute("SELECT * FROM students WHERE id=?",(sid,)).fetchone();cc.close();await c.answer();await edit_or_answer(c,f"👤 <b>{s['full_name']}</b>\n🆔 {s['telegram_id'] or 'не привязан'}",ik([[b("🗑 Удалить",f"astuddel:{sid}")],[b("🔙 Назад","adm:students")]]))
@router.callback_query(F.data=="astudadd")
async def astudadd(c,state):await state.set_state(StudentAdd.name);await c.answer();await c.message.answer("Введите ФИО студента:")
@router.message(StudentAdd.name)
async def astudadd_save(m,state):
    cc=db();
    try: cc.execute("INSERT INTO students(full_name,normalized_name,created_at) VALUES(?,?,?)",(m.text.strip(),norm(m.text),datetime.now(TZ).isoformat()));cc.commit();await m.answer("✅ Студент добавлен.")
    except sqlite3.IntegrityError: await m.answer("❌ Такое ФИО уже есть.")
    cc.close();await state.clear()
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
    did=int(c.data.split(':')[-1]); await state.update_data(did=did); await state.set_state(DisciplineEdit.name); await c.answer(); await c.message.answer("Введите новое название дисциплины:")
@router.message(DisciplineEdit.name)
async def adisce_name(m:Message,state:FSMContext): await state.update_data(name=m.text.strip()); await state.set_state(DisciplineEdit.emoji); await m.answer("Введите эмодзи дисциплины (например 📐):")
@router.message(DisciplineEdit.emoji)
async def adisce_emoji(m:Message,state:FSMContext):
    data=await state.get_data(); cc=db(); 
    if data.get('did') is None:
        cc.execute("INSERT INTO disciplines(name,emoji,active,textbooks_enabled) VALUES(?,?,1,1)",(data['name'],m.text.strip() or '📚'))
    else:
        cc.execute("UPDATE disciplines SET name=?,emoji=? WHERE id=?",(data['name'],m.text.strip() or '📚',data['did']))
    cc.commit(); cc.close(); await state.clear(); await m.answer("✅ Дисциплина сохранена.")
@router.callback_query(F.data=="adiscadd")
async def adiscadd(c:CallbackQuery,state:FSMContext): await state.set_state(DisciplineEdit.name); await state.update_data(did=None); await c.answer(); await c.message.answer("Введите название новой дисциплины:")

async def admin_disc(c):
    cc=db();ds=cc.execute("SELECT * FROM disciplines ORDER BY name").fetchall();cc.close();rows=[[b(f"{d['emoji']} {d['name']}",f"adisc:{d['id']}")] for d in ds];rows.append([b("➕ Добавить","adiscadd")]);rows.append([b("🏠 Админ-панель","adm:back")]);await c.answer();await edit_or_answer(c,"📖 <b>Дисциплины</b>",ik(rows))
@router.callback_query(F.data.startswith("adisc:"))
async def adisc(c):did=int(c.data.split(':')[-1]);cc=db();d=cc.execute("SELECT * FROM disciplines WHERE id=?",(did,)).fetchone();cc.close();await c.answer();await edit_or_answer(c,f"{d['emoji']} <b>{d['name']}</b>",ik([[b("✏️ Изменить название/эмодзи",f"adisce:{did}")],[b("🗑 Удалить",f"adiscdel:{did}")],[b("🔙 Назад","adm:disc")]]))
@router.callback_query(F.data.startswith("adiscdel:"))
async def adiscdel(c):did=c.data.split(':')[-1];await c.answer();await edit_or_answer(c,"⚠️ <b>Удалить дисциплину?</b>\nСвязанные ДЗ, учебники и расписание также могут быть удалены.",ik([[b("🗑 Да, удалить",f"adiscdelok:{did}")],[b("❌ Отмена",f"adisc:{did}")]]))
@router.callback_query(F.data.startswith("adiscdelok:"))
async def adiscdelok(c):did=c.data.split(':')[-1];cc=db();cc.execute("DELETE FROM disciplines WHERE id=?",(did,));cc.commit();cc.close();await c.answer("Удалено");await admin_disc(c)

# Broadcast
async def broadcast_start(c,state): await state.set_state(Broadcast.text);await c.answer();await c.message.answer("📢 Введите текст уведомления всем:")
@router.message(Broadcast.text)
async def broadcast_send(m,state):
    await state.clear();cc=db();ss=cc.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL").fetchall();cc.close();sent=0
    for s in ss:
        try: await m.bot.send_message(s['telegram_id'],"📢 <b>Уведомление</b>\n\n"+m.text);sent+=1
        except Exception: pass
    await m.answer(f"✅ Отправлено: {sent}")

# Notifications
async def notification_job(bot):
    now=datetime.now(TZ)
    # 19:00 tomorrow schedule
    if now.hour==19 and now.minute==0:
        tomorrow=now.date()+timedelta(days=1)
        if tomorrow.weekday()!=6 and schedule_for(tomorrow):
            cc=db();users=cc.execute("SELECT s.telegram_id FROM students s JOIN notification_settings n ON n.telegram_id=s.telegram_id WHERE n.tomorrow_schedule=1").fetchall();cc.close()
            txt,kb=await render_day(None,tomorrow)
            for u in users:
                key=f"tomorrow:{tomorrow}"
                cc=db();seen=cc.execute("SELECT 1 FROM sent_notifications WHERE telegram_id=? AND notification_key=?",(u['telegram_id'],key)).fetchone()
                if not seen:
                    try: await bot.send_message(u['telegram_id'],"📅 <b>Расписание на завтра</b>\n\n"+txt)
                    except Exception: pass
                    cc.execute("INSERT OR IGNORE INTO sent_notifications VALUES(?,?)",(u['telegram_id'],key));cc.commit()
                cc.close()
    # next lesson exactly 5 minutes before current lesson ends
    if now.weekday()==6: return
    for slot,(s,e) in enumerate(SLOT_TIMES,1):
        end=datetime.combine(now.date(),time.fromisoformat(e),tzinfo=TZ)
        if 0 <= (end-now).total_seconds() <= 300 and now.second < 60:
            nxtslot=slot+1
            if nxtslot>5: return
            lessons=schedule_for(now.date()); nxt=[x for x in lessons if x['lesson_no']==nxtslot]
            if not nxt: return
            x=nxt[0]; start=x['start'];end2=x['end']; cc=db();users=cc.execute("SELECT s.telegram_id FROM students s JOIN notification_settings n ON n.telegram_id=s.telegram_id WHERE n.next_lesson=1").fetchall();cc.close();tr=TEACHERS.get(x['discipline'],'');cc=db();r=cc.execute("SELECT name FROM teachers WHERE discipline_id=?",(x['discipline_id'],)).fetchone();cc.close();tr=r['name'] if r else tr
            txt=f"🔔 <b>Следующая пара через 5 минут</b>\n\n{start}–{end2} — {x['lesson_no']} пара | {x['lesson_type']}\n{x['emoji']} <b>{x['discipline']}</b>\n{tr} • ауд. {x['room']}"
            for u in users:
                key=f"next:{now.date()}:{nxtslot}"
                cc=db();seen=cc.execute("SELECT 1 FROM sent_notifications WHERE telegram_id=? AND notification_key=?",(u['telegram_id'],key)).fetchone()
                if not seen:
                    try: await bot.send_message(u['telegram_id'],txt)
                    except Exception: pass
                    cc.execute("INSERT OR IGNORE INTO sent_notifications VALUES(?,?)",(u['telegram_id'],key));cc.commit()
                cc.close()

@router.callback_query(F.data=="noop")
async def noop(c:CallbackQuery): await c.answer()

async def main():
    migrate()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    scheduler=AsyncIOScheduler(timezone=TZ);scheduler.add_job(notification_job,'interval',args=[bot],minutes=1,coalesce=True,max_instances=1);scheduler.start()
    log.info("R26 bot %s started",VERSION)
    try: await dp.start_polling(bot)
    finally: scheduler.shutdown();await bot.session.close()

if __name__=='__main__': asyncio.run(main())
