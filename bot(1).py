import asyncio
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from openpyxl import load_workbook

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db")
XLSX_PATH = os.getenv("STUDENTS_XLSX", "students.xlsx")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в .env")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

router = Router()

EMOJIS = {
    "Биология": "🧬",
    "География": "🌍",
    "Иностранный язык": "🌐",
    "Информатика": "💻",
    "История": "🏛️",
    "Литература": "📚",
    "Математика": "📐",
    "ОБЖиЗР": "🛡️",
    "Обществознание": "⚖️",
    "ОПД": "📖",
    "Родной язык": "🗣️",
    "Русский язык": "✍️",
    "Физика": "⚛️",
    "Физическая культура": "🏃",
    "Химия": "🧪",
}

def norm(s: str) -> str:
    s = (s or "").replace("ё", "е").replace("Ё", "Е")
    return " ".join(s.strip().lower().split())

def esc(s: str) -> str:
    # ParseMode HTML
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    return conn

async def init_db():
    conn = await db()
    await conn.executescript("""
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL UNIQUE,
        telegram_id INTEGER UNIQUE,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS disciplines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        emoji TEXT NOT NULL DEFAULT '📚',
        active INTEGER NOT NULL DEFAULT 1,
        textbooks_enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS homework (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discipline_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        explanation TEXT,
        published_date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        hidden INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS homework_media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        homework_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        file_id TEXT NOT NULL,
        caption TEXT,
        FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS textbooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discipline_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        kind TEXT NOT NULL,
        file_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS ui_state (
        telegram_id INTEGER PRIMARY KEY,
        message_id INTEGER
    );
    """)
    # Миграция для уже существующей базы: добавляем настройку показа учебников.
    cur = await conn.execute("PRAGMA table_info(disciplines)")
    columns = {row[1] for row in await cur.fetchall()}
    if "textbooks_enabled" not in columns:
        await conn.execute("ALTER TABLE disciplines ADD COLUMN textbooks_enabled INTEGER NOT NULL DEFAULT 1")
    cur = await conn.execute("PRAGMA table_info(homework)")
    hw_columns = {row[1] for row in await cur.fetchall()}
    if "hidden" not in hw_columns:
        await conn.execute("ALTER TABLE homework ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    await conn.commit()
    await conn.close()

async def import_xlsx():
    path = Path(XLSX_PATH)
    if not path.exists():
        return
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    students, disciplines = [], []
    for row in ws.iter_rows(min_col=1, max_col=3, values_only=True):
        a, _, c = row
        if isinstance(a, str) and a.strip():
            students.append(a.strip())
        if isinstance(c, str) and c.strip():
            disciplines.append(c.strip())
    conn = await db()
    for name in students:
        try:
            await conn.execute(
                "INSERT INTO students(full_name, normalized_name, created_at) VALUES(?,?,?)",
                (name, norm(name), datetime.now().isoformat())
            )
        except aiosqlite.IntegrityError:
            pass
    for name in disciplines:
        emoji = EMOJIS.get(name, "📚")
        try:
            await conn.execute(
                "INSERT INTO disciplines(name, emoji) VALUES(?,?)",
                (name, emoji)
            )
        except aiosqlite.IntegrityError:
            await conn.execute(
                "UPDATE disciplines SET emoji=? WHERE name=?",
                (emoji, name)
            )
    await conn.commit()
    await conn.close()
    log.info("Excel import: %s students, %s disciplines", len(students), len(disciplines))

async def set_ui(chat_id: int, message_id: int):
    conn = await db()
    await conn.execute(
        "INSERT INTO ui_state(telegram_id,message_id) VALUES(?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET message_id=excluded.message_id",
        (chat_id, message_id)
    )
    await conn.commit()
    await conn.close()

async def delete_previous(bot: Bot, chat_id: int):
    conn = await db()
    cur = await conn.execute("SELECT message_id FROM ui_state WHERE telegram_id=?", (chat_id,))
    row = await cur.fetchone()
    await conn.close()
    if row:
        try:
            await bot.delete_message(chat_id, row["message_id"])
        except TelegramBadRequest:
            pass

async def replace_message(message: Message, text: str, keyboard):
    bot = message.bot
    await delete_previous(bot, message.chat.id)
    sent = await message.answer(text, reply_markup=keyboard)
    await set_ui(message.chat.id, sent.message_id)
    return sent

async def replace_callback(c: CallbackQuery, text: str, keyboard):
    bot = c.bot
    chat_id = c.message.chat.id
    await delete_previous(bot, chat_id)
    sent = await bot.send_message(chat_id, text, reply_markup=keyboard)
    await set_ui(chat_id, sent.message_id)
    await c.answer()
    return sent

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def student_for(tg_id: int):
    conn = await db()
    cur = await conn.execute("SELECT * FROM students WHERE telegram_id=?", (tg_id,))
    row = await cur.fetchone()
    await conn.close()
    return row

async def disciplines_keyboard():
    conn = await db()
    cur = await conn.execute("""
        SELECT d.id, d.name, d.emoji,
               EXISTS(SELECT 1 FROM homework h WHERE h.discipline_id=d.id AND h.hidden=0 AND date(substr(h.due_date,7,4)||"-"||substr(h.due_date,4,2)||"-"||substr(h.due_date,1,2)) >= date("now")) AS has_hw
        FROM disciplines d
        WHERE d.active=1
        ORDER BY has_hw DESC, d.name COLLATE NOCASE
    """)
    rows = await cur.fetchall()
    await conn.close()
    buttons = []
    for r in rows:
        suffix = " | 📝 Есть ДЗ" if r["has_hw"] else ""
        buttons.append(InlineKeyboardButton(
            text=f'{r["emoji"]} {r["name"]}{suffix}',
            callback_data=f'disc:{r["id"]}'
        ))
    return kb([buttons[i:i+2] for i in range(0, len(buttons), 2)])

async def main_text_for(user_id: int):
    student = await student_for(user_id)
    name = student["full_name"] if student else ""
    greeting = f'👋 <b>Привет, {esc(name)}!</b>\n\n' if name else '👋 <b>Добро пожаловать!</b>\n\n'
    return (
        greeting
        + '📚 Здесь ты можешь быстро посмотреть домашние задания и учебники.\n\n'
        + 'Выбери нужный раздел или дисциплину:'
    )

async def top_menu_keyboard():
    return kb([
        [
            InlineKeyboardButton(text="📖 Учебники", callback_data="books_main"),
            InlineKeyboardButton(text="📝 Домашнее задание", callback_data="main_disciplines"),
        ],
        [
            InlineKeyboardButton(text="🗓 Расписание", url="https://t.me/vvfsched_bot"),
            InlineKeyboardButton(text="🆘 Помощь", url="https://t.me/miravynvoida"),
        ],
    ])

async def main_menu_keyboard():
    # В главном меню только основные разделы. Дисциплины открываются
    # отдельно внутри «Домашнее задание» и «Учебники».
    return await top_menu_keyboard()

async def show_main_message(message: Message):
    return await replace_message(message, await main_text_for(message.from_user.id), await main_menu_keyboard())

async def show_main_callback(c: CallbackQuery):
    return await replace_callback(c, await main_text_for(c.from_user.id), await main_menu_keyboard())

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    student = await student_for(message.from_user.id)
    if student:
        await show_main_message(message)
        return
    await replace_message(
        message,
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Для доступа к боту введи свою <b>фамилию и имя</b> ровно как в списке группы.\n\n"
        "Например: <i>Иванов Фёдор</i>",
        kb([[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])
    )
    await state.set_state(Register.waiting_name)

class Register(StatesGroup):
    waiting_name = State()

@router.message(Register.waiting_name)
async def register_name(message: Message, state: FSMContext):
    entered = norm(message.text or "")
    conn = await db()
    cur = await conn.execute(
        "SELECT * FROM students WHERE normalized_name=?",
        (entered,)
    )
    student = await cur.fetchone()
    if not student:
        await conn.close()
        await message.answer("❌ Такого ФИО нет в списке группы.\nПроверь написание и попробуй ещё раз.")
        return
    if student["telegram_id"] and student["telegram_id"] != message.from_user.id:
        await conn.close()
        await message.answer("❌ Это ФИО уже привязано к другому Telegram-аккаунту. Обратись к администратору.")
        return
    await conn.execute(
        "UPDATE students SET telegram_id=? WHERE id=?",
        (message.from_user.id, student["id"])
    )
    await conn.commit()
    await conn.close()
    await state.clear()
    await message.answer("✅ <b>Готово!</b> Ты добавлен в список группы.")
    await show_main_message(message)

@router.callback_query(F.data == "cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_callback(c)

@router.callback_query(F.data.startswith("disc:"))
async def discipline(c: CallbackQuery):
    if not await student_for(c.from_user.id) and not is_admin(c.from_user.id):
        await c.answer("Сначала пройди проверку по ФИО.", show_alert=True)
        return
    did = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM disciplines WHERE id=? AND active=1", (did,))
    d = await cur.fetchone()
    cur = await conn.execute(
        "SELECT * FROM homework WHERE discipline_id=? AND hidden=0 AND due_date >= ? ORDER BY due_date ASC, id DESC",
        (did, date.today().strftime("%d.%m.%Y"))
    )
    hws = await cur.fetchall()
    cur = await conn.execute(
        "SELECT textbooks_enabled, (SELECT COUNT(*) FROM textbooks WHERE discipline_id=?) AS book_count FROM disciplines WHERE id=?",
        (did, did)
    )
    book_info = await cur.fetchone()
    await conn.close()
    if not d:
        await c.answer("Дисциплина не найдена.", show_alert=True)
        return
    rows = []
    for h in hws:
        rows.append([InlineKeyboardButton(
            text=f'📝 ДЗ от {h["published_date"]} — сдать до {h["due_date"]}',
            callback_data=f'hw:{h["id"]}'
        )])
    if book_info and book_info["textbooks_enabled"] and book_info["book_count"]:
        rows.append([InlineKeyboardButton(text="📖 Учебники", callback_data=f'books:{did}')])
    rows.append([InlineKeyboardButton(text="⬅️ К дисциплинам", callback_data="main_disciplines")])
    text = f'{d["emoji"]} <b>{esc(d["name"])}</b>\n\n'
    text += "Выберите домашнее задание:" if hws else "Пока нет активных домашних заданий."
    await replace_callback(c, text, kb(rows))

@router.callback_query(F.data == "main")
async def main_cb(c: CallbackQuery):
    await show_main_callback(c)

@router.callback_query(F.data == "main_disciplines")
async def main_disciplines_cb(c: CallbackQuery):
    if not await student_for(c.from_user.id) and not is_admin(c.from_user.id):
        await c.answer("Сначала пройди проверку по ФИО.", show_alert=True)
        return
    await replace_callback(
        c,
        "📝 <b>Домашнее задание</b>\n\nВыбери дисциплину:",
        await disciplines_keyboard_with_back(),
    )

async def disciplines_keyboard_with_back():
    base = await disciplines_keyboard()
    rows = list(base.inline_keyboard)
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")])
    return kb(rows)

@router.callback_query(F.data == "books_main")
async def books_main(c: CallbackQuery):
    if not await student_for(c.from_user.id) and not is_admin(c.from_user.id):
        await c.answer("Сначала пройди проверку по ФИО.", show_alert=True)
        return
    conn = await db()
    cur = await conn.execute("""
        SELECT d.id, d.name, d.emoji, COUNT(t.id) AS book_count
        FROM disciplines d
        JOIN textbooks t ON t.discipline_id=d.id
        WHERE d.active=1 AND d.textbooks_enabled=1
        GROUP BY d.id
        HAVING COUNT(t.id) > 0
        ORDER BY d.name COLLATE NOCASE
    """)
    ds = await cur.fetchall()
    await conn.close()
    buttons = [InlineKeyboardButton(text=f'{d["emoji"]} {d["name"]}', callback_data=f'books:{d["id"]}') for d in ds]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")])
    text = "📖 <b>Учебники</b>\n\nВыбери дисциплину:" if ds else "📖 <b>Учебники</b>\n\nПока нет доступных учебников."
    await replace_callback(c, text, kb(rows))

async def send_media(bot: Bot, chat_id: int, kind: str, file_id: str, caption=None):
    cap = esc(caption) if caption else None
    if kind == "document":
        return await bot.send_document(chat_id, file_id, caption=cap)
    if kind == "photo":
        return await bot.send_photo(chat_id, file_id, caption=cap)
    if kind == "video":
        return await bot.send_video(chat_id, file_id, caption=cap)
    return None

@router.callback_query(F.data.startswith("hw:"))
async def homework_view(c: CallbackQuery):
    if not await student_for(c.from_user.id) and not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    hid = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("""
        SELECT h.*, d.name AS discipline, d.emoji
        FROM homework h JOIN disciplines d ON d.id=h.discipline_id
        WHERE h.id=?
    """, (hid,))
    h = await cur.fetchone()
    cur = await conn.execute("SELECT * FROM homework_media WHERE homework_id=? ORDER BY id", (hid,))
    media = await cur.fetchall()
    await conn.close()
    if not h:
        await c.answer("ДЗ не найдено.", show_alert=True)
        return
    text = (
        f'{h["emoji"]} <b>{esc(h["discipline"])}</b>\n\n'
        f'📝 <b>Домашнее задание</b>\n{esc(h["text"])}\n\n'
        f'📅 Опубликовано: <b>{h["published_date"]}</b>\n'
        f'⏳ Сдать до: <b>{h["due_date"]}</b>'
    )
    if h["explanation"]:
        text += f'\n\n💬 <b>Пояснение:</b>\n{esc(h["explanation"])}'
    rows = [[InlineKeyboardButton(text="⬅️ Назад", callback_data=f'disc:{h["discipline_id"]}')]]
    await replace_callback(c, text, kb(rows))
    # Материалы отправляем отдельными сообщениями; последнее меню остаётся последним.
    for m in media:
        try:
            await send_media(c.bot, c.message.chat.id, m["kind"], m["file_id"], m["caption"])
        except TelegramBadRequest:
            pass

@router.callback_query(F.data.startswith("books:"))
async def books(c: CallbackQuery):
    if not await student_for(c.from_user.id) and not is_admin(c.from_user.id):
        await c.answer("Сначала пройди проверку по ФИО.", show_alert=True)
        return
    did = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM disciplines WHERE id=? AND active=1 AND textbooks_enabled=1", (did,))
    d = await cur.fetchone()
    cur = await conn.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id DESC", (did,))
    books_ = await cur.fetchall()
    await conn.close()
    if not d:
        await c.answer("Дисциплина не найдена.", show_alert=True)
        return
    rows = []
    for b in books_:
        rows.append([InlineKeyboardButton(
            text=f'📕 {b["title"]}',
            callback_data=f'book:{b["id"]}'
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f'disc:{did}')])
    text = f'{d["emoji"]} <b>Учебники — {esc(d["name"])}</b>\n\n'
    text += "Выберите учебник:" if books_ else "Учебников пока нет."
    await replace_callback(c, text, kb(rows))

@router.callback_query(F.data.startswith("book:"))
async def book_view(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM textbooks WHERE id=?", (bid,))
    b = await cur.fetchone()
    await conn.close()
    if not b:
        await c.answer("Учебник не найден.", show_alert=True)
        return
    try:
        await send_media(c.bot, c.message.chat.id, b["kind"], b["file_id"], b["title"])
    except TelegramBadRequest:
        await c.message.answer("Не удалось отправить учебник.")
    await replace_callback(
        c,
        f'📕 <b>{esc(b["title"])}</b>',
        kb([[InlineKeyboardButton(text="⬅️ К учебникам", callback_data=f'books:{b["discipline_id"]}')]])
    )

# ---------------- ADMIN ----------------

def admin_menu():
    return kb([
        [InlineKeyboardButton(text="➕ Добавить ДЗ", callback_data="adm:addhw"), InlineKeyboardButton(text="📋 Управление ДЗ", callback_data="adm:hws")],
        [InlineKeyboardButton(text="📚 Дисциплины", callback_data="adm:disc"), InlineKeyboardButton(text="📖 Учебники", callback_data="adm:books")],
        [InlineKeyboardButton(text="👥 Студенты", callback_data="adm:students"), InlineKeyboardButton(text="📣 Уведомление всем", callback_data="adm:broadcast")],
    ])

@router.message(Command("admin"))
async def admin_cmd(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await replace_message(message, "🔐 <b>Админ-панель</b>\n\nВыберите действие:", admin_menu())

@router.callback_query(F.data == "adm")
async def admin_cb(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await replace_callback(c, "🔐 <b>Админ-панель</b>\n\nВыберите действие:", admin_menu())

class AddHW(StatesGroup):
    discipline = State()
    text = State()
    due = State()
    explanation = State()
    media = State()

@router.callback_query(F.data == "adm:addhw")
async def addhw_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return
    conn = await db()
    cur = await conn.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name")
    ds = await cur.fetchall()
    await conn.close()
    rows = [[InlineKeyboardButton(text=f'{d["emoji"]} {d["name"]}', callback_data=f'adddid:{d["id"]}')] for d in ds]
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")])
    await state.clear()
    await state.set_state(AddHW.discipline)
    await replace_callback(c, "➕ <b>Новое ДЗ</b>\n\nВыбери дисциплину:", kb(rows))

@router.callback_query(AddHW.discipline, F.data.startswith("adddid:"))
async def addhw_discipline(c: CallbackQuery, state: FSMContext):
    await state.update_data(discipline_id=int(c.data.split(":")[1]))
    await state.set_state(AddHW.text)
    await replace_callback(c, "✏️ Введи текст домашнего задания:", kb([[InlineKeyboardButton(text="❌ Отмена", callback_data="adm")]]))

@router.message(AddHW.text)
async def addhw_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text or "")
    await state.set_state(AddHW.due)
    await message.answer("📅 Введи дату сдачи в формате <b>ДД.ММ.ГГГГ</b>:", parse_mode=ParseMode.HTML)

def valid_date(s):
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None

@router.message(AddHW.due)
async def addhw_due(message: Message, state: FSMContext):
    d = valid_date(message.text or "")
    if not d:
        await message.answer("❌ Неверная дата. Пример: <b>25.09.2026</b>", parse_mode=ParseMode.HTML)
        return
    await state.update_data(due=d.strftime("%d.%m.%Y"))
    await state.set_state(AddHW.explanation)
    await message.answer("💬 Введи пояснение или отправь <b>—</b>, если оно не нужно:", parse_mode=ParseMode.HTML)

@router.message(AddHW.explanation)
async def addhw_explanation(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    await state.update_data(explanation="" if value in {"—", "-", ""} else value)
    await state.update_data(media=[])
    await state.set_state(AddHW.media)
    await message.answer(
        "📎 Теперь можешь прислать любое количество файлов, фото или видео.\n\n"
        "Когда закончишь, нажми кнопку ниже.",
        reply_markup=kb([[InlineKeyboardButton(text="✅ Готово", callback_data="addhw:finish")]])
    )

@router.message(AddHW.media)
async def addhw_media(message: Message, state: FSMContext):
    data = await state.get_data()
    media = data.get("media", [])
    item = None
    if message.document:
        item = ("document", message.document.file_id, None)
    elif message.photo:
        item = ("photo", message.photo[-1].file_id, message.caption)
    elif message.video:
        item = ("video", message.video.file_id, message.caption)
    if not item:
        await message.answer("Пришли файл, фото или видео. Для завершения нажми «✅ Готово».")
        return
    media.append(item)
    await state.update_data(media=media)
    await message.answer(f"✅ Материал добавлен. Сейчас материалов: {len(media)}")

@router.callback_query(AddHW.media, F.data == "addhw:finish")
async def addhw_finish(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    published = date.today().strftime("%d.%m.%Y")
    conn = await db()
    cur = await conn.execute(
        "INSERT INTO homework(discipline_id,text,explanation,published_date,due_date) VALUES(?,?,?,?,?)",
        (data["discipline_id"], data["text"], data.get("explanation",""), published, data["due"])
    )
    hid = cur.lastrowid
    for kind, fid, caption in data.get("media", []):
        await conn.execute(
            "INSERT INTO homework_media(homework_id,kind,file_id,caption) VALUES(?,?,?,?)",
            (hid, kind, fid, caption)
        )
    await conn.commit()
    cur = await conn.execute("SELECT name, emoji FROM disciplines WHERE id=?", (data["discipline_id"],))
    disc = await cur.fetchone()
    await conn.close()
    await state.clear()

    # Уведомляем зарегистрированных студентов.
    conn = await db()
    cur = await conn.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL")
    recipients = [r["telegram_id"] for r in await cur.fetchall()]
    await conn.close()
    notify = (
        f'🔔 <b>Новое домашнее задание!</b>\n\n'
        f'{disc["emoji"]} <b>{esc(disc["name"])}</b>\n'
        f'📅 Опубликовано: <b>{published}</b>\n'
        f'⏳ Сдать до: <b>{data["due"]}</b>\n\n'
        f'{esc(data["text"]) }'
    )
    sent = 0
    for uid in recipients:
        try:
            await c.bot.send_message(uid, notify, reply_markup=kb([
                [InlineKeyboardButton(text="📖 Открыть ДЗ", callback_data=f"hw:{hid}")]
            ]))
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
    await replace_callback(
        c,
        f"✅ <b>ДЗ опубликовано!</b>\n\n{disc['emoji']} {esc(disc['name'])}\n"
        f"📅 {published} → ⏳ {data['due']}\n\n"
        f"Уведомлено студентов: {sent}",
        kb([[InlineKeyboardButton(text="🔐 Админ-панель", callback_data="adm")]])
    )

class Broadcast(StatesGroup):
    text = State()

@router.callback_query(F.data == "adm:broadcast")
async def broadcast_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return
    await state.clear()
    await state.set_state(Broadcast.text)
    await replace_callback(
        c,
        "📣 <b>Рассылка всем студентам</b>\n\n"
        "Введи текст уведомления, которое получат все студенты, "
        "привязавшие Telegram к боту.\n\n"
        "HTML-разметка поддерживается.",
        kb([[InlineKeyboardButton(text="❌ Отмена", callback_data="adm")]])
    )

@router.message(Broadcast.text)
async def broadcast_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("❌ Уведомление не может быть пустым. Отправь текст ещё раз.")
        return

    await state.update_data(text=text)
    await state.clear()

    conn = await db()
    cur = await conn.execute(
        "SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL"
    )
    recipients = [r["telegram_id"] for r in await cur.fetchall()]
    await conn.close()

    sent = 0
    failed = 0
    for uid in recipients:
        try:
            await message.bot.send_message(
                uid,
                "📣 <b>Уведомление</b>\n\n" + esc(text),
            )
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1

    await replace_message(
        message,
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"📨 Отправлено: <b>{sent}</b>\n"
        f"⚠️ Не доставлено: <b>{failed}</b>",
        kb([[InlineKeyboardButton(text="🔐 Админ-панель", callback_data="adm")]])
    )

@router.callback_query(F.data == "adm:hws")
async def admin_hws(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn = await db()
    cur = await conn.execute("""
        SELECT h.id, h.published_date, h.due_date, d.name, d.emoji
        FROM homework h JOIN disciplines d ON d.id=h.discipline_id
        ORDER BY h.id DESC
        LIMIT 50
    """)
    rows_ = await cur.fetchall()
    await conn.close()
    rows = [[InlineKeyboardButton(
        text=f'{r["emoji"]} {r["name"]} | до {r["due_date"]}',
        callback_data=f'adhw:{r["id"]}'
    )] for r in rows_]
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")])
    text = "📋 <b>Домашние задания</b>\n\nВыбери ДЗ для удаления:" if rows_ else "📋 <b>Домашние задания</b>\n\nДЗ нет."
    await replace_callback(c, text, kb(rows))

@router.callback_query(F.data.startswith("adhw:"))
async def admin_hw_delete_confirm(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    hid = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("""
        SELECT h.*, d.name, d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?
    """, (hid,))
    h = await cur.fetchone()
    await conn.close()
    if not h:
        await c.answer("Не найдено.", show_alert=True); return
    await replace_callback(
        c,
        f'🗑 <b>Удалить ДЗ?</b>\n\n{h["emoji"]} {esc(h["name"])}\n{esc(h["text"])}\n\n'
        f'Опубликовано: {h["published_date"]}\nСдать до: {h["due_date"]}',
        kb([
            [InlineKeyboardButton(text="🗄 Скрыть в архив", callback_data=f"hidehw:{hid}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:hws")]
        ])
    )

@router.callback_query(F.data.startswith("hidehw:"))
async def hide_hw(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    hid = int(c.data.split(":")[1])
    conn = await db()
    await conn.execute("UPDATE homework SET hidden=1 WHERE id=?", (hid,))
    await conn.commit()
    await conn.close()
    await admin_hws(c)

@router.callback_query(F.data.startswith("delhw:"))
async def admin_hw_delete(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    hid = int(c.data.split(":")[1])
    conn = await db()
    await conn.execute("DELETE FROM homework WHERE id=?", (hid,))
    await conn.commit()
    await conn.close()
    await admin_hws(c)

@router.callback_query(F.data == "adm:disc")
async def admin_disc(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn = await db()
    cur = await conn.execute("SELECT * FROM disciplines ORDER BY active DESC, name")
    ds = await cur.fetchall()
    await conn.close()
    rows = [[InlineKeyboardButton(
        text=f'{d["emoji"]} {d["name"]}',
        callback_data=f"editdisc:{d['id']}"
    )] for d in ds]
    rows.append([InlineKeyboardButton(text="➕ Добавить дисциплину", callback_data="newdisc")])
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")])
    await replace_callback(c, "📚 <b>Дисциплины</b>\n\nВыбери дисциплину:", kb(rows))

class NewDisc(StatesGroup):
    name = State()

@router.callback_query(F.data == "newdisc")
async def newdisc(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(NewDisc.name)
    await replace_callback(c, "➕ Введи название новой дисциплины:", kb([[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:disc")]]))

@router.message(NewDisc.name)
async def newdisc_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым."); return
    conn = await db()
    try:
        await conn.execute("INSERT INTO disciplines(name,emoji) VALUES(?,?)", (name, EMOJIS.get(name, "📚")))
        await conn.commit()
        ok = True
    except aiosqlite.IntegrityError:
        ok = False
    await conn.close()
    await state.clear()
    await message.answer("✅ Дисциплина добавлена." if ok else "❌ Такая дисциплина уже есть.")
    # Show admin menu again.
    await replace_message(message, "📚 <b>Дисциплины</b>", kb([[InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")]]))

@router.callback_query(F.data.startswith("editdisc:"))
async def editdisc(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    did = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM disciplines WHERE id=?", (did,))
    d = await cur.fetchone()
    await conn.close()
    if not d: return
    await replace_callback(c, f'{d["emoji"]} <b>{esc(d["name"])}</b>', kb([
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"renamedisc:{did}")],
        [InlineKeyboardButton(text="🔄 Включить/выключить", callback_data=f"toggledisc:{did}")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"deldiscq:{did}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:disc")]
    ]))

class RenameDisc(StatesGroup):
    name = State()

@router.callback_query(F.data.startswith("renamedisc:"))
async def renamedisc(c: CallbackQuery, state: FSMContext):
    did = int(c.data.split(":")[1])
    await state.update_data(discipline_id=did)
    await state.set_state(RenameDisc.name)
    await replace_callback(c, "✏️ Введи новое название:", kb([[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm:disc")]]))

@router.message(RenameDisc.name)
async def renamedisc_name(message: Message, state: FSMContext):
    data = await state.get_data()
    name = (message.text or "").strip()
    conn = await db()
    try:
        await conn.execute("UPDATE disciplines SET name=?, emoji=? WHERE id=?", (name, EMOJIS.get(name,"📚"), data["discipline_id"]))
        await conn.commit()
        result = "✅ Название изменено."
    except aiosqlite.IntegrityError:
        result = "❌ Такая дисциплина уже существует."
    await conn.close()
    await state.clear()
    await message.answer(result)
    await replace_message(message, "📚 <b>Дисциплины</b>", kb([[InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")]]))

@router.callback_query(F.data.startswith("toggledisc:"))
async def toggledisc(c: CallbackQuery):
    did = int(c.data.split(":")[1])
    conn = await db()
    await conn.execute("UPDATE disciplines SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (did,))
    await conn.commit()
    await conn.close()
    await admin_disc(c)

@router.callback_query(F.data.startswith("deldiscq:"))
async def deldiscq(c: CallbackQuery):
    did = int(c.data.split(":")[1])
    await replace_callback(c, "⚠️ Удаление дисциплины удалит её ДЗ и учебники.\n\nПродолжить?", kb([
        [InlineKeyboardButton(text="🗑 Да", callback_data=f"deldisc:{did}")],
        [InlineKeyboardButton(text="⬅️ Нет", callback_data=f"editdisc:{did}")]
    ]))

@router.callback_query(F.data.startswith("deldisc:"))
async def deldisc(c: CallbackQuery):
    did = int(c.data.split(":")[1])
    conn = await db()
    await conn.execute("DELETE FROM disciplines WHERE id=?", (did,))
    await conn.commit()
    await conn.close()
    await admin_disc(c)

@router.callback_query(F.data == "adm:students")
async def admin_students(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn = await db()
    cur = await conn.execute("SELECT * FROM students ORDER BY full_name")
    ss = await cur.fetchall()
    await conn.close()
    registered = sum(1 for s in ss if s["telegram_id"])
    await replace_callback(c, f"👥 <b>Студенты</b>\n\nВсего: {len(ss)}\nПривязано Telegram: {registered}\n\n"
        "Чтобы добавить студента: добавь его ФИО в столбец A файла students.xlsx и перезапусти/переразверни бота.\n\n"
        "Столбец B оставь пустым, дисциплины находятся в столбце C.",
        kb([[InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")]])
    )

@router.callback_query(F.data == "adm:books")
async def admin_books(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn = await db()
    cur = await conn.execute("""
        SELECT d.*, COUNT(t.id) AS book_count
        FROM disciplines d
        LEFT JOIN textbooks t ON t.discipline_id=d.id
        WHERE d.active=1
        GROUP BY d.id
        ORDER BY d.name COLLATE NOCASE
    """)
    ds = await cur.fetchall()
    await conn.close()
    rows = []
    for d in ds:
        status = "🟢 ВКЛ" if d["textbooks_enabled"] else "⚪ ВЫКЛ"
        rows.append([InlineKeyboardButton(
            text=f'{d["emoji"]} {d["name"]} | {status} | 📕 {d["book_count"]}',
            callback_data=f"admbooks:{d['id']}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")])
    await replace_callback(c, "📖 <b>Учебники</b>\n\n🟢 ВКЛ — дисциплина показывается ученикам.\n⚪ ВЫКЛ — скрыта из раздела учебников.\n\nВыбери дисциплину:", kb(rows))

class BookAdd(StatesGroup):
    discipline = State()
    title = State()
    file = State()

@router.callback_query(F.data.startswith("admbooks:"))
async def admbooks(c: CallbackQuery):
    did = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM disciplines WHERE id=?", (did,))
    d = await cur.fetchone()
    cur = await conn.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id DESC", (did,))
    bs = await cur.fetchall()
    await conn.close()
    if not d:
        await c.answer("Дисциплина не найдена.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=f'📕 {b["title"]}', callback_data=f"delbookq:{b['id']}")] for b in bs]
    rows.append([InlineKeyboardButton(text=f'{"🟢 Выключить" if d["textbooks_enabled"] else "⚪ Включить"} раздел для учеников', callback_data=f"togglebooks:{did}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить учебник", callback_data=f"addbook:{did}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:books")])
    text = f'{d["emoji"]} <b>Учебники — {esc(d["name"])}</b>\n\nСтатус для учеников: <b>{"ВКЛ" if d["textbooks_enabled"] else "ВЫКЛ"}</b>\nУчебников: <b>{len(bs)}</b>'
    await replace_callback(c, text, kb(rows))

@router.callback_query(F.data.startswith("togglebooks:"))
async def togglebooks(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    did = int(c.data.split(":")[1])
    conn = await db()
    await conn.execute("UPDATE disciplines SET textbooks_enabled=CASE textbooks_enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (did,))
    await conn.commit()
    await conn.close()
    await admbooks(c)

@router.callback_query(F.data.startswith("addbook:"))
async def addbook(c: CallbackQuery, state: FSMContext):
    did = int(c.data.split(":")[1])
    await state.clear()
    await state.update_data(discipline_id=did)
    await state.set_state(BookAdd.title)
    await replace_callback(c, "📕 Введи название учебника:", kb([[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"admbooks:{did}")]]))

@router.message(BookAdd.title)
async def addbook_title(message: Message, state: FSMContext):
    await state.update_data(title=(message.text or "").strip())
    await state.set_state(BookAdd.file)
    await message.answer("📎 Теперь отправь сам учебник как файл (PDF/DOCX/архив и т.п.).")

@router.message(BookAdd.file)
async def addbook_file(message: Message, state: FSMContext):
    data = await state.get_data()
    kind, fid = None, None
    if message.document:
        kind, fid = "document", message.document.file_id
    elif message.photo:
        kind, fid = "photo", message.photo[-1].file_id
    elif message.video:
        kind, fid = "video", message.video.file_id
    if not fid:
        await message.answer("Нужен файл, фото или видео.")
        return
    conn = await db()
    await conn.execute(
        "INSERT INTO textbooks(discipline_id,title,kind,file_id,created_at) VALUES(?,?,?,?,?)",
        (data["discipline_id"], data["title"], kind, fid, datetime.now().isoformat())
    )
    await conn.commit()
    await conn.close()
    await state.clear()
    await message.answer("✅ Учебник добавлен.")
    await replace_message(message, "📖 <b>Учебники</b>", kb([
        [InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")]
    ]))

@router.callback_query(F.data.startswith("delbookq:"))
async def delbookq(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM textbooks WHERE id=?", (bid,))
    b = await cur.fetchone()
    await conn.close()
    if not b: return
    await replace_callback(c, f'🗑 Удалить учебник <b>{esc(b["title"])}</b>?', kb([
        [InlineKeyboardButton(text="🗄 Скрыть в архив", callback_data=f"delbook:{bid}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admbooks:{b['discipline_id']}")]
    ]))

@router.callback_query(F.data.startswith("delbook:"))
async def delbook(c: CallbackQuery):
    bid = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT discipline_id FROM textbooks WHERE id=?", (bid,))
    b = await cur.fetchone()
    if b:
        did = b["discipline_id"]
        await conn.execute("DELETE FROM textbooks WHERE id=?", (bid,))
        await conn.commit()
    else:
        did = None
    await conn.close()
    if did:
        conn = await db()
        cur = await conn.execute("SELECT * FROM disciplines WHERE id=?", (did,))
        d = await cur.fetchone()
        cur = await conn.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id DESC", (did,))
        bs = await cur.fetchall()
        await conn.close()
        rows = [[InlineKeyboardButton(text=f'📕 {b["title"]}', callback_data=f"delbookq:{b['id']}")] for b in bs]
        rows.append([InlineKeyboardButton(text="➕ Добавить учебник", callback_data=f"addbook:{did}")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:books")])
        text = f'{d["emoji"]} <b>Учебники — {esc(d["name"])}</b>\n\n'
        text += "Нажми на учебник, чтобы удалить его." if bs else "Пока нет учебников."
        await replace_callback(c, text, kb(rows))
    else:
        await admin_books(c)

# Correct callback helper for delete-book navigation
@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()

async def main():
    await init_db()
    await import_xlsx()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
