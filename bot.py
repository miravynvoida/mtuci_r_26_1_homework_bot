import asyncio
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv
from openpyxl import load_workbook

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity

# Rich Messages / Rich Markdown появились в Bot API 10.1 и поддерживаются aiogram 3.29+.
# Если на хостинге стоит более старая aiogram, бот всё равно запустится, но rich markdown
# будет автоматически отправляться как обычный HTML/текст.
try:
    from aiogram.types import InputRichMessage
    RICH_AVAILABLE = True
except ImportError:
    InputRichMessage = None
    RICH_AVAILABLE = False

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db")
XLSX_PATH = os.getenv("STUDENTS_XLSX", "students.xlsx")
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в .env")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
router = Router()

BOT_VERSION_DEFAULT = "v1.5.1"
BOT_UPDATED_DEFAULT = "23.09.2026"

EMOJIS = {
    "Биология": "🧬", "География": "🌍", "Иностранный язык": "🌐",
    "Информатика": "💻", "История": "🏛️", "Литература": "📚",
    "Математика": "📐", "ОБЖиЗР": "🛡️", "Обществознание": "⚖️",
    "ОПД": "📖", "Родной язык": "🗣️", "Русский язык": "✍️",
    "Физика": "⚛️", "Физическая культура": "🏃", "Химия": "🧪",
}


def norm(s: str) -> str:
    s = (s or "").replace("ё", "е").replace("Ё", "Е")
    return " ".join(s.strip().lower().split())


def esc(s: str) -> str:
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
        archived INTEGER NOT NULL DEFAULT 0,
        text_entities TEXT,
        explanation_entities TEXT,
        FOREIGN KEY(discipline_id) REFERENCES disciplines(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS homework_media (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        homework_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        file_id TEXT NOT NULL,
        caption TEXT,
        caption_entities TEXT,
        FOREIGN KEY(homework_id) REFERENCES homework(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS homework_answer (
        homework_id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        file_id TEXT,
        text TEXT,
        caption TEXT,
        entities TEXT,
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
    CREATE TABLE IF NOT EXISTS bot_info (
        id INTEGER PRIMARY KEY CHECK(id=1),
        version TEXT NOT NULL,
        updated_date TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vip_users (
        telegram_id INTEGER PRIMARY KEY,
        full_name TEXT NOT NULL,
        added_at TEXT NOT NULL
    );
    """)

    # Safe migrations for old bot.db.
    async def add_column(table, column, definition):
        cur = await conn.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cur.fetchall()}
        if column not in cols:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    await add_column("disciplines", "textbooks_enabled", "INTEGER NOT NULL DEFAULT 1")
    await add_column("homework", "archived", "INTEGER NOT NULL DEFAULT 0")
    await add_column("homework", "text_entities", "TEXT")
    await add_column("homework", "explanation_entities", "TEXT")
    await add_column("homework_media", "caption_entities", "TEXT")

    await conn.execute(
        "INSERT OR IGNORE INTO bot_info(id,version,updated_date) VALUES(1,?,?)",
        (BOT_VERSION_DEFAULT, BOT_UPDATED_DEFAULT),
    )
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
                "INSERT INTO students(full_name,normalized_name,created_at) VALUES(?,?,?)",
                (name, norm(name), datetime.now().isoformat()),
            )
        except aiosqlite.IntegrityError:
            pass
    for name in disciplines:
        emoji = EMOJIS.get(name, "📚")
        try:
            await conn.execute("INSERT INTO disciplines(name,emoji) VALUES(?,?)", (name, emoji))
        except aiosqlite.IntegrityError:
            await conn.execute("UPDATE disciplines SET emoji=? WHERE name=?", (emoji, name))
    await conn.commit()
    await conn.close()
    log.info("Excel import: %s students, %s disciplines", len(students), len(disciplines))


async def set_ui(chat_id: int, message_id: int):
    conn = await db()
    await conn.execute(
        "INSERT INTO ui_state(telegram_id,message_id) VALUES(?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET message_id=excluded.message_id",
        (chat_id, message_id),
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


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def replace_message(message: Message, text: str, keyboard):
    await delete_previous(message.bot, message.chat.id)
    sent = await message.answer(text, reply_markup=keyboard)
    await set_ui(message.chat.id, sent.message_id)
    return sent


async def replace_callback(c: CallbackQuery, text: str, keyboard):
    await delete_previous(c.bot, c.message.chat.id)
    sent = await c.bot.send_message(c.message.chat.id, text, reply_markup=keyboard)
    await set_ui(c.message.chat.id, sent.message_id)
    await c.answer()
    return sent


async def replace_callback_rich(c: CallbackQuery, markdown: str, keyboard):
    await delete_previous(c.bot, c.message.chat.id)
    sent = await send_rich_markdown(c.bot, c.message.chat.id, markdown, keyboard)
    await set_ui(c.message.chat.id, sent.message_id)
    await c.answer()
    return sent


async def student_for(tg_id: int):
    conn = await db()
    cur = await conn.execute("SELECT * FROM students WHERE telegram_id=?", (tg_id,))
    row = await cur.fetchone()
    await conn.close()
    return row


async def vip_for(tg_id: int):
    conn = await db()
    cur = await conn.execute("SELECT * FROM vip_users WHERE telegram_id=?", (tg_id,))
    row = await cur.fetchone()
    await conn.close()
    return row


async def authorized(user_id: int) -> bool:
    return is_admin(user_id) or bool(await student_for(user_id))


async def active_hw_count(discipline_id: int) -> int:
    conn = await db()
    cur = await conn.execute(
        "SELECT COUNT(*) FROM homework WHERE discipline_id=? AND archived=0 AND due_date>=?",
        (discipline_id, date.today().strftime("%d.%m.%Y")),
    )
    n = (await cur.fetchone())[0]
    await conn.close()
    return n


async def archive_expired():
    today = date.today().strftime("%d.%m.%Y")
    conn = await db()
    # Dates are stored DD.MM.YYYY; SQLite cannot compare them chronologically as text.
    cur = await conn.execute("SELECT id,due_date FROM homework WHERE archived=0")
    rows = await cur.fetchall()
    changed = 0
    for r in rows:
        try:
            due = datetime.strptime(r["due_date"], "%d.%m.%Y").date()
            if due < date.today():
                await conn.execute("UPDATE homework SET archived=1 WHERE id=?", (r["id"],))
                changed += 1
        except ValueError:
            pass
    await conn.commit()
    await conn.close()
    if changed:
        log.info("Archived expired homework: %s", changed)


async def disciplines_keyboard():
    await archive_expired()
    conn = await db()
    cur = await conn.execute("""
        SELECT d.id,d.name,d.emoji,
               EXISTS(SELECT 1 FROM homework h WHERE h.discipline_id=d.id AND h.archived=0) AS has_hw
        FROM disciplines d
        WHERE d.active=1
        ORDER BY has_hw DESC, d.name COLLATE NOCASE
    """)
    rows_ = await cur.fetchall()
    await conn.close()
    buttons = []
    for r in rows_:
        suffix = " | 📝 Есть ДЗ" if r["has_hw"] else ""
        buttons.append(InlineKeyboardButton(text=f'{r["emoji"]} {r["name"]}{suffix}', callback_data=f'disc:{r["id"]}'))
    return kb([buttons[i:i+2] for i in range(0, len(buttons), 2)])


async def main_text_for(user_id: int):
    student = await student_for(user_id)
    name = student["full_name"] if student else ""
    greeting = f'👋 <b>Привет, {esc(name)}!</b>\n\n' if name else '👋 <b>Добро пожаловать!</b>\n\n'
    return greeting + '📚 Здесь ты можешь быстро посмотреть домашние задания и учебники.\n\nВыбери нужный раздел:'


async def top_menu_keyboard():
    return kb([
        [InlineKeyboardButton(text="📖 Учебники", callback_data="books_main"), InlineKeyboardButton(text="📝 Домашнее задание", callback_data="main_disciplines")],
        [InlineKeyboardButton(text="🗓 Расписание", url="https://t.me/vvfsched_bot"), InlineKeyboardButton(text="🆘 Помощь", url="https://t.me/miravynvoida")],
    ])


async def main_menu_keyboard():
    return await top_menu_keyboard()


async def show_main_message(message: Message):
    return await replace_message(message, await main_text_for(message.from_user.id), await main_menu_keyboard())


async def show_main_callback(c: CallbackQuery):
    return await replace_callback(c, await main_text_for(c.from_user.id), await main_menu_keyboard())


# ---------- /info ----------
@router.message(Command("info"))
async def info_cmd(message: Message):
    if not await authorized(message.from_user.id):
        return
    conn = await db()
    cur = await conn.execute("SELECT version,updated_date FROM bot_info WHERE id=1")
    info = await cur.fetchone()
    await conn.close()
    version = info["version"] if info else BOT_VERSION_DEFAULT
    updated = info["updated_date"] if info else BOT_UPDATED_DEFAULT
    await message.answer(
        "ℹ️ <b>Информация о боте</b>\n\n"
        f"🤖 Версия: <b>{esc(version)}</b>\n"
        f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        f"📅 Дата обновления: <b>{esc(updated)}</b>"
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    student = await student_for(message.from_user.id)
    if student:
        await show_main_message(message)
        return
    await replace_message(
        message,
        "👋 <b>Добро пожаловать!</b>\n\nДля доступа к боту введи свою <b>фамилию и имя</b> ровно как в списке группы.\n\nНапример: <i>Иванов Фёдор</i>",
        kb([[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]]),
    )
    await state.set_state(Register.waiting_name)


class Register(StatesGroup):
    waiting_name = State()


@router.message(Register.waiting_name)
async def register_name(message: Message, state: FSMContext):
    entered = norm(message.text or "")
    conn = await db()
    cur = await conn.execute("SELECT * FROM students WHERE normalized_name=?", (entered,))
    student = await cur.fetchone()
    if not student:
        await conn.close()
        await message.answer("❌ Не нашёл такое ФИО. Проверь написание и попробуй ещё раз.")
        return
    if student["telegram_id"] and student["telegram_id"] != message.from_user.id:
        await conn.close()
        await message.answer("❌ Это ФИО уже привязано к другому Telegram-аккаунту.")
        return
    await conn.execute("UPDATE students SET telegram_id=? WHERE id=?", (message.from_user.id, student["id"]))
    await conn.commit()
    await conn.close()
    await state.clear()
    await message.answer("✅ <b>Готово!</b> Ты добавлен в список группы.")
    await show_main_message(message)


@router.callback_query(F.data == "cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_main_callback(c)


@router.callback_query(F.data == "main")
async def main_cb(c: CallbackQuery):
    await show_main_callback(c)


async def disciplines_keyboard_with_back():
    base = await disciplines_keyboard()
    rows = list(base.inline_keyboard)
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")])
    return kb(rows)


@router.callback_query(F.data == "main_disciplines")
async def main_disciplines_cb(c: CallbackQuery):
    if not await authorized(c.from_user.id):
        await c.answer("Сначала пройди проверку по ФИО.", show_alert=True)
        return
    await replace_callback(c, "📝 <b>Домашнее задание</b>\n\nВыбери дисциплину:", await disciplines_keyboard_with_back())


@router.callback_query(F.data.startswith("disc:"))
async def discipline(c: CallbackQuery):
    if not await authorized(c.from_user.id):
        await c.answer("Сначала пройди проверку по ФИО.", show_alert=True)
        return
    await archive_expired()
    did = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM disciplines WHERE id=? AND active=1", (did,))
    d = await cur.fetchone()
    cur = await conn.execute("SELECT * FROM homework WHERE discipline_id=? AND archived=0 ORDER BY due_date ASC,id ASC", (did,))
    hws = await cur.fetchall()
    cur = await conn.execute("SELECT textbooks_enabled,(SELECT COUNT(*) FROM textbooks WHERE discipline_id=?) AS book_count FROM disciplines WHERE id=?", (did, did))
    book_info = await cur.fetchone()
    await conn.close()
    if not d:
        await c.answer("Дисциплина не найдена.", show_alert=True)
        return
    rows = []
    for h in hws:
        rows.append([InlineKeyboardButton(text=f'📝 ДЗ — сдать до {h["due_date"]}', callback_data=f'hw:{h["id"]}')])
    if book_info and book_info["textbooks_enabled"] and book_info["book_count"]:
        rows.append([InlineKeyboardButton(text="📖 Учебники", callback_data=f'books:{did}')])
    rows.append([InlineKeyboardButton(text="⬅️ К дисциплинам", callback_data="main_disciplines")])
    text = f'{d["emoji"]} <b>{esc(d["name"])}</b>\n\n' + ("Выберите домашнее задание:" if hws else "Пока нет активных домашних заданий.")
    await replace_callback(c, text, kb(rows))


@router.callback_query(F.data == "books_main")
async def books_main(c: CallbackQuery):
    if not await authorized(c.from_user.id):
        await c.answer("Сначала пройди проверку по ФИО.", show_alert=True)
        return
    conn = await db()
    cur = await conn.execute("""
        SELECT d.id,d.name,d.emoji,COUNT(t.id) AS book_count
        FROM disciplines d JOIN textbooks t ON t.discipline_id=d.id
        WHERE d.active=1 AND d.textbooks_enabled=1
        GROUP BY d.id HAVING COUNT(t.id)>0 ORDER BY d.name COLLATE NOCASE
    """)
    ds = await cur.fetchall()
    await conn.close()
    buttons = [InlineKeyboardButton(text=f'{d["emoji"]} {d["name"]}', callback_data=f'books:{d["id"]}') for d in ds]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="⬅️ Главное меню", callback_data="main")])
    await replace_callback(c, "📖 <b>Учебники</b>\n\nВыбери дисциплину:" if ds else "📖 <b>Учебники</b>\n\nПока нет доступных учебников.", kb(rows))


# ---------- message formatting / premium emoji ----------
def entity_json(entities):
    if not entities:
        return None
    return json.dumps([e.model_dump(exclude_none=True) for e in entities], ensure_ascii=False)


def entities_from_json(raw):
    if not raw:
        return None
    try:
        return [MessageEntity(**x) for x in json.loads(raw)]
    except Exception:
        return None


async def send_text_preserving_entities(bot: Bot, chat_id: int, text: str, entities_json=None, reply_markup=None):
    entities = entities_from_json(entities_json)
    return await bot.send_message(chat_id, text, entities=entities, reply_markup=reply_markup, parse_mode=None)


async def send_media(bot: Bot, chat_id: int, kind: str, file_id: str, caption=None, caption_entities=None):
    ents = entities_from_json(caption_entities)
    kwargs = {"caption": caption, "caption_entities": ents, "parse_mode": None}
    if kind == "document": return await bot.send_document(chat_id, file_id, **kwargs)
    if kind == "photo": return await bot.send_photo(chat_id, file_id, **kwargs)
    if kind == "video": return await bot.send_video(chat_id, file_id, **kwargs)
    if kind == "audio": return await bot.send_audio(chat_id, file_id, **kwargs)
    if kind == "voice": return await bot.send_voice(chat_id, file_id, **kwargs)
    if kind == "animation": return await bot.send_animation(chat_id, file_id, **kwargs)
    if kind == "video_note": return await bot.send_video_note(chat_id, file_id)
    if kind == "sticker": return await bot.send_sticker(chat_id, file_id)
    return None


async def send_rich_markdown(bot: Bot, chat_id: int, markdown: str, reply_markup=None):
    """Use Telegram Rich Markdown (tables, lists, formulas, custom emoji, etc.) when supported."""
    if RICH_AVAILABLE:
        return await bot.send_rich_message(
            chat_id,
            InputRichMessage(markdown=markdown),
            reply_markup=reply_markup,
        )
    # Compatibility fallback: ordinary Telegram MarkdownV2 is still useful for basic formatting.
    return await bot.send_message(chat_id, markdown, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)


async def detect_media(message: Message):
    if message.document:
        return "document", message.document.file_id, message.caption, message.caption_entities
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption, message.caption_entities
    if message.video:
        return "video", message.video.file_id, message.caption, message.caption_entities
    if message.audio:
        return "audio", message.audio.file_id, message.caption, message.caption_entities
    if message.voice:
        return "voice", message.voice.file_id, message.caption, message.caption_entities
    if message.animation:
        return "animation", message.animation.file_id, message.caption, message.caption_entities
    if message.video_note:
        return "video_note", message.video_note.file_id, None, None
    if message.sticker:
        return "sticker", message.sticker.file_id, None, None
    return None


@router.callback_query(F.data.startswith("hw:"))
async def homework_view(c: CallbackQuery):
    if not await authorized(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await archive_expired()
    hid = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT h.*,d.name AS discipline,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?", (hid,))
    h = await cur.fetchone()
    cur = await conn.execute("SELECT * FROM homework_media WHERE homework_id=? ORDER BY id", (hid,))
    media = await cur.fetchall()
    cur = await conn.execute("SELECT * FROM homework_answer WHERE homework_id=?", (hid,))
    answer = await cur.fetchone()
    await conn.close()
    if not h or (h["archived"] and not is_admin(c.from_user.id)):
        await c.answer("ДЗ не найдено или уже в архиве.", show_alert=True)
        return

    rows = []
    if await vip_for(c.from_user.id):
        # Кнопка есть у всех заданий; если ответ ещё не добавлен, бот покажет уведомление.
        rows.append([InlineKeyboardButton(text="💡 Посмотреть ответ", callback_data=f'answer:{hid}')])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f'disc:{h["discipline_id"]}')])

    # Если администратор вводил текст как обычное сообщение с Telegram-entities
    # (включая custom/premium emoji), отправляем entities напрямую. Иначе используем
    # Rich Markdown, который поддерживает таблицы, списки, формулы и т.д.
    if h["text_entities"] or h["explanation_entities"]:
        text = (
            f'{h["emoji"]} <b>{esc(h["discipline"])}</b>\n\n'
            f'📝 <b>Домашнее задание</b>\n{esc(h["text"])}\n\n'
            f'⏳ Сдать до: <b>{esc(h["due_date"])}</b>'
        )
        if h["explanation"]:
            text += f'\n\n💬 <b>Пояснение:</b>\n{esc(h["explanation"])}'
        await replace_callback(c, text, kb(rows))
    else:
        markdown = (
            f'# {h["emoji"]} {h["discipline"]}\n\n'
            f'**📝 Домашнее задание**\n{h["text"]}\n\n'
            f'⏳ **Сдать до:** {h["due_date"]}'
        )
        if h["explanation"]:
            markdown += f'\n\n💬 **Пояснение:**\n{h["explanation"]}'
        try:
            await replace_callback_rich(c, markdown, kb(rows))
        except TelegramBadRequest:
            # Если конкретный клиент/API не принял Rich Markdown, возвращаемся к обычному HTML.
            text = (
                f'{h["emoji"]} <b>{esc(h["discipline"])}</b>\n\n'
                f'📝 <b>Домашнее задание</b>\n{esc(h["text"])}\n\n'
                f'⏳ Сдать до: <b>{esc(h["due_date"])}</b>'
            )
            if h["explanation"]:
                text += f'\n\n💬 <b>Пояснение:</b>\n{esc(h["explanation"])}'
            await replace_callback(c, text, kb(rows))
    for m in media:
        try:
            await send_media(c.bot, c.message.chat.id, m["kind"], m["file_id"], m["caption"], m["caption_entities"])
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("answer:"))
async def homework_answer(c: CallbackQuery):
    if not await vip_for(c.from_user.id):
        await c.answer("🔒 Раздел доступен только VIP-пользователям.", show_alert=True)
        return
    hid = int(c.data.split(":")[1])
    conn = await db()
    cur = await conn.execute("SELECT * FROM homework_answer WHERE homework_id=?", (hid,))
    ans = await cur.fetchone()
    await conn.close()
    if not ans:
        await c.answer("Готового ответа пока нет.", show_alert=True)
        return
    await c.answer()
    try:
        if ans["kind"] == "text":
            await send_text_preserving_entities(c.bot, c.message.chat.id, ans["text"] or "", ans["entities"])
        else:
            await send_media(c.bot, c.message.chat.id, ans["kind"], ans["file_id"], ans["caption"], ans["entities"])
    except TelegramBadRequest:
        await c.message.answer("Не удалось отправить готовый ответ.")


@router.callback_query(F.data.startswith("books:"))
async def books(c: CallbackQuery):
    if not await authorized(c.from_user.id):
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
    buttons = [InlineKeyboardButton(text=f'📕 {b["title"]}', callback_data=f'book:{b["id"]}') for b in books_]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f'disc:{did}')])
    await replace_callback(c, f'{d["emoji"]} <b>Учебники — {esc(d["name"])}</b>\n\nВыберите учебник:' if books_ else 'Учебников пока нет.', kb(rows))


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
    await replace_callback(c, f'📕 <b>{esc(b["title"])}</b>', kb([[InlineKeyboardButton(text="⬅️ К учебникам", callback_data=f'books:{b["discipline_id"]}')]]))
    try:
        await send_media(c.bot, c.message.chat.id, b["kind"], b["file_id"], b["title"])
    except TelegramBadRequest:
        await c.message.answer("Не удалось отправить учебник.")


# ---------- ADMIN ----------
def admin_menu():
    return kb([
        [InlineKeyboardButton(text="➕ Добавить ДЗ", callback_data="adm:addhw"), InlineKeyboardButton(text="📋 Управление ДЗ", callback_data="adm:hws")],
        [InlineKeyboardButton(text="📚 Дисциплины", callback_data="adm:disc"), InlineKeyboardButton(text="📖 Учебники", callback_data="adm:books")],
        [InlineKeyboardButton(text="👥 Студенты", callback_data="adm:students"), InlineKeyboardButton(text="📣 Уведомление всем", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="⭐ VIP список", callback_data="adm:vip")],
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


# ---------- ADD HOMEWORK ----------
class AddHW(StatesGroup):
    discipline = State(); text = State(); due = State(); explanation = State(); media = State()


@router.callback_query(F.data == "adm:addhw")
async def addhw_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return
    conn = await db(); cur = await conn.execute("SELECT * FROM disciplines WHERE active=1 ORDER BY name"); ds = await cur.fetchall(); await conn.close()
    buttons = [InlineKeyboardButton(text=f'{d["emoji"]} {d["name"]}', callback_data=f'adddid:{d["id"]}') for d in ds]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")])
    await state.clear(); await state.set_state(AddHW.discipline)
    await replace_callback(c, "➕ <b>Новое ДЗ</b>\n\nВыбери дисциплину:", kb(rows))


@router.callback_query(AddHW.discipline, F.data.startswith("adddid:"))
async def addhw_discipline(c: CallbackQuery, state: FSMContext):
    await state.update_data(discipline_id=int(c.data.split(":")[1]))
    await state.set_state(AddHW.text)
    await replace_callback(c, "✏️ Введи текст домашнего задания.\n\nПоддерживаются обычный текст и Telegram-разметка/кастомные эмодзи из отправленного сообщения.", kb([[InlineKeyboardButton(text="❌ Отмена", callback_data="adm")]]))


@router.message(AddHW.text)
async def addhw_text(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужно отправить именно текстовое сообщение.")
        return
    await state.update_data(text=message.text, text_entities=entity_json(message.entities))
    await state.set_state(AddHW.due)
    await message.answer("📅 Введи дату сдачи в формате <b>ДД.ММ.ГГГГ</b>:")


def valid_date(s):
    try: return datetime.strptime(s.strip(), "%d.%m.%Y").date()
    except ValueError: return None


@router.message(AddHW.due)
async def addhw_due(message: Message, state: FSMContext):
    d = valid_date(message.text or "")
    if not d:
        await message.answer("❌ Неверная дата. Пример: <b>25.09.2026</b>")
        return
    await state.update_data(due=d.strftime("%d.%m.%Y")); await state.set_state(AddHW.explanation)
    await message.answer("💬 Введи пояснение или отправь <b>—</b>, если оно не нужно:")


@router.message(AddHW.explanation)
async def addhw_explanation(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    await state.update_data(explanation="" if value in {"—", "-", ""} else value, explanation_entities=entity_json(message.entities) if value not in {"—", "-", ""} else None, media=[])
    await state.set_state(AddHW.media)
    await message.answer("📎 Теперь можешь прислать любое количество файлов, фото, видео и других медиа.\n\nКогда закончишь, нажми кнопку ниже.", reply_markup=kb([[InlineKeyboardButton(text="✅ Готово", callback_data="addhw:finish")]]))


@router.message(AddHW.media)
async def addhw_media(message: Message, state: FSMContext):
    item = await detect_media(message)
    if not item:
        await message.answer("Пришли файл/фото/видео/медиа. Для завершения нажми «✅ Готово».")
        return
    data = await state.get_data(); media = data.get("media", [])
    kind, fid, caption, ents = item
    media.append((kind, fid, caption, entity_json(ents)))
    await state.update_data(media=media)
    await message.answer(f"✅ Материал добавлен. Сейчас материалов: {len(media)}")


@router.callback_query(AddHW.media, F.data == "addhw:finish")
async def addhw_finish(c: CallbackQuery, state: FSMContext):
    data = await state.get_data(); published = date.today().strftime("%d.%m.%Y")
    conn = await db()
    cur = await conn.execute(
        "INSERT INTO homework(discipline_id,text,explanation,published_date,due_date,text_entities,explanation_entities) VALUES(?,?,?,?,?,?,?)",
        (data["discipline_id"], data["text"], data.get("explanation", ""), published, data["due"], data.get("text_entities"), data.get("explanation_entities")),
    )
    hid = cur.lastrowid
    for kind, fid, caption, ents in data.get("media", []):
        await conn.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption,caption_entities) VALUES(?,?,?,?,?)", (hid,kind,fid,caption,ents))
    await conn.commit()
    cur = await conn.execute("SELECT name,emoji FROM disciplines WHERE id=?", (data["discipline_id"],)); disc = await cur.fetchone(); await conn.close()
    await state.clear()
    await notify_new_hw(c.bot, hid, disc, published, data)
    await replace_callback(c, f"✅ <b>ДЗ опубликовано!</b>\n\n{disc['emoji']} {esc(disc['name'])}\n📅 {published} → ⏳ {data['due']}", kb([[InlineKeyboardButton(text="🔐 Админ-панель", callback_data="adm")]]))


async def notify_new_hw(bot, hid, disc, published, data):
    conn = await db(); cur = await conn.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL"); recipients = [r["telegram_id"] for r in await cur.fetchall()]; await conn.close()
    notify = f'🔔 <b>Новое домашнее задание!</b>\n\n{disc["emoji"]} <b>{esc(disc["name"])}</b>\n⏳ Сдать до: <b>{data["due"]}</b>\n\n{esc(data["text"])}'
    for uid in recipients:
        try:
            await bot.send_message(uid, notify, reply_markup=kb([[InlineKeyboardButton(text="📖 Открыть ДЗ", callback_data=f"hw:{hid}")]]))
        except (TelegramForbiddenError, TelegramBadRequest):
            pass


# ---------- BROADCAST ----------
class Broadcast(StatesGroup): text = State()

@router.callback_query(F.data == "adm:broadcast")
async def broadcast_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return
    await state.clear(); await state.set_state(Broadcast.text)
    await replace_callback(c, "📣 <b>Рассылка всем студентам</b>\n\nОтправь текст сообщения. Telegram-разметка и кастомные эмодзи из отправленного сообщения сохраняются.", kb([[InlineKeyboardButton(text="❌ Отмена", callback_data="adm")]]))

@router.message(Broadcast.text)
async def broadcast_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if not message.text:
        await message.answer("Нужно отправить текстовое сообщение."); return
    conn = await db(); cur = await conn.execute("SELECT telegram_id FROM students WHERE telegram_id IS NOT NULL"); recipients = [r["telegram_id"] for r in await cur.fetchall()]; await conn.close()
    sent = failed = 0
    ents = message.entities
    for uid in recipients:
        try:
            await send_text_preserving_entities(message.bot, uid, message.text, entity_json(ents))
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest): failed += 1
    await state.clear()
    await replace_message(message, f"✅ <b>Рассылка завершена</b>\n\n📨 Отправлено: <b>{sent}</b>\n⚠️ Не доставлено: <b>{failed}</b>", kb([[InlineKeyboardButton(text="🔐 Админ-панель", callback_data="adm")]]))


# ---------- HOMEWORK MANAGEMENT ----------
async def hw_list_page(c, archived=False, page=0):
    await archive_expired()
    conn = await db()
    cur = await conn.execute("""
        SELECT h.id,h.published_date,h.due_date,h.archived,d.name,d.emoji
        FROM homework h JOIN disciplines d ON d.id=h.discipline_id
        WHERE h.archived=? ORDER BY h.id ASC LIMIT 5 OFFSET ?
    """, (1 if archived else 0, page*5))
    rows_ = await cur.fetchall()
    cur = await conn.execute("SELECT COUNT(*) FROM homework WHERE archived=?", (1 if archived else 0)); total = (await cur.fetchone())[0]
    await conn.close()
    buttons = [InlineKeyboardButton(text=f'{r["emoji"]} {r["name"]} | до {r["due_date"]}', callback_data=f'adhw:{r["id"]}:{page}:{1 if archived else 0}') for r in rows_]
    rows = [buttons[i:i+2] for i in range(0,len(buttons),2)]
    nav=[]
    if page>0: nav.append(InlineKeyboardButton(text="⬅️ Предыдущая", callback_data=f'adhwpage:{1 if archived else 0}:{page-1}'))
    if (page+1)*5<total: nav.append(InlineKeyboardButton(text="Следующая ➡️", callback_data=f'adhwpage:{1 if archived else 0}:{page+1}'))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="🗄 Архив" if not archived else "📋 Все ДЗ", callback_data="adm:archive" if not archived else "adm:hws")])
    rows.append([InlineKeyboardButton(text="⬅️ Админ-панель", callback_data="adm")])
    title = "🗄 <b>Архив ДЗ</b>" if archived else "📋 <b>Все ДЗ</b>"
    await replace_callback(c, f'{title}\n\nСтраница {page+1}. Выбери ДЗ:' if rows_ else f'{title}\n\nПусто.', kb(rows))


@router.callback_query(F.data == "adm:hws")
async def admin_hws(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    await hw_list_page(c, False, 0)

@router.callback_query(F.data == "adm:archive")
async def admin_archive(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    await hw_list_page(c, True, 0)

@router.callback_query(F.data.startswith("adhwpage:"))
async def admin_hw_page(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _, archived, page = c.data.split(":")
    await hw_list_page(c, bool(int(archived)), int(page))


@router.callback_query(F.data.startswith("adhw:"))
async def admin_hw_detail(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _, hid, page, archived = c.data.split(":")
    hid=int(hid); page=int(page); archived=bool(int(archived))
    conn=await db(); cur=await conn.execute("SELECT h.*,d.name,d.emoji FROM homework h JOIN disciplines d ON d.id=h.discipline_id WHERE h.id=?",(hid,)); h=await cur.fetchone(); await conn.close()
    if not h: await c.answer("ДЗ не найдено.",show_alert=True); return
    text=f'{h["emoji"]} <b>{esc(h["name"])}</b>\n\n📝 {esc(h["text"])}\n\n📅 Опубликовано: {h["published_date"]}\n⏳ Сдать до: {h["due_date"]}'
    if h["explanation"]: text += f'\n\n💬 {esc(h["explanation"])}'
    rows=[
        [InlineKeyboardButton(text="✏️ Редактировать",callback_data=f'edit_hw:{hid}:{page}:{1 if archived else 0}'),InlineKeyboardButton(text="🗄 Скрыть" if not archived else "♻️ Вернуть",callback_data=f'archive_hw:{hid}:{page}:{1 if archived else 0}')],
        [InlineKeyboardButton(text="🗑 Удалить",callback_data=f'delhwq:{hid}:{page}:{1 if archived else 0}')],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data=f'adhwpage:{1 if archived else 0}:{page}')]
    ]
    await replace_callback(c,text,kb(rows))


@router.callback_query(F.data.startswith("archive_hw:"))
async def archive_hw(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":"); hid=int(hid); page=int(page); archived=bool(int(archived))
    conn=await db(); await conn.execute("UPDATE homework SET archived=? WHERE id=?", (0 if archived else 1,hid)); await conn.commit(); await conn.close()
    await hw_list_page(c, archived, page)


@router.callback_query(F.data.startswith("delhwq:"))
async def delhwq(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":")
    await replace_callback(c,"🗑 <b>Удалить ДЗ окончательно?</b>\n\nЭто удалит задание и все его материалы.",kb([
        [InlineKeyboardButton(text="🗑 Да, удалить",callback_data=f'delhw:{hid}:{page}:{archived}')],
        [InlineKeyboardButton(text="⬅️ Отмена",callback_data=f'adhw:{hid}:{page}:{archived}')]
    ]))

@router.callback_query(F.data.startswith("delhw:"))
async def delhw(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":"); page=int(page); archived=bool(int(archived))
    conn=await db(); await conn.execute("DELETE FROM homework WHERE id=?",(int(hid),)); await conn.commit(); await conn.close(); await hw_list_page(c,archived,page)


# ---------- EDIT HOMEWORK ----------
class EditHW(StatesGroup):
    value=State(); media=State(); answer=State()

@router.callback_query(F.data.startswith("edit_hw:"))
async def edit_hw_menu(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":")
    await replace_callback(c,"✏️ <b>Редактирование ДЗ</b>\n\nВыбери, что изменить:",kb([
        [InlineKeyboardButton(text="📝 Задание",callback_data=f'ehwfield:text:{hid}:{page}:{archived}'),InlineKeyboardButton(text="📅 Дата публикации",callback_data=f'ehwfield:pub:{hid}:{page}:{archived}')],
        [InlineKeyboardButton(text="⏳ Дата сдачи",callback_data=f'ehwfield:due:{hid}:{page}:{archived}'),InlineKeyboardButton(text="💬 Пояснение",callback_data=f'ehwfield:exp:{hid}:{page}:{archived}')],
        [InlineKeyboardButton(text="📎 Файлы",callback_data=f'ehwmedia:{hid}:{page}:{archived}'),InlineKeyboardButton(text="💡 Готовый ответ",callback_data=f'ehwanswer:{hid}:{page}:{archived}')],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data=f'adhw:{hid}:{page}:{archived}')]
    ]))

@router.callback_query(F.data.startswith("ehwfield:"))
async def edit_hw_field(c: CallbackQuery,state:FSMContext):
    if not is_admin(c.from_user.id): return
    _,field,hid,page,archived=c.data.split(":")
    await state.clear(); await state.update_data(field=field,hid=int(hid),page=int(page),archived=int(archived)); await state.set_state(EditHW.value)
    prompts={'text':'📝 Отправь новое задание. Можно использовать разметку и кастомные эмодзи.','pub':'📅 Новая дата публикации (ДД.ММ.ГГГГ):','due':'⏳ Новая дата сдачи (ДД.ММ.ГГГГ):','exp':'💬 Новое пояснение или — чтобы убрать его:'}
    await replace_callback(c,prompts[field],kb([[InlineKeyboardButton(text="❌ Отмена",callback_data=f'edit_hw:{hid}:{page}:{archived}')]]))

@router.message(EditHW.value)
async def edit_hw_value(message:Message,state:FSMContext):
    data=await state.get_data(); field=data['field']; value=(message.text or '').strip()
    if field in {'pub','due'} and not valid_date(value): await message.answer("❌ Неверная дата. Формат: ДД.ММ.ГГГГ"); return
    conn=await db()
    if field=='text':
        await conn.execute("UPDATE homework SET text=?,text_entities=? WHERE id=?",(message.text or '',entity_json(message.entities),data['hid']))
    elif field=='pub': await conn.execute("UPDATE homework SET published_date=? WHERE id=?",(value,data['hid']))
    elif field=='due': await conn.execute("UPDATE homework SET due_date=? WHERE id=?",(value,data['hid']))
    elif field=='exp': await conn.execute("UPDATE homework SET explanation=?,explanation_entities=? WHERE id=?",('' if value in {'—','-',''} else value, None if value in {'—','-',''} else entity_json(message.entities),data['hid']))
    await conn.commit(); await conn.close(); await state.clear()
    await message.answer("✅ Изменение сохранено.")
    await replace_message(message,"✏️ <b>Редактирование ДЗ</b>",kb([[InlineKeyboardButton(text="⬅️ К ДЗ",callback_data=f'adhw:{data["hid"]}:{data["page"]}:{data["archived"]}')]]))

@router.callback_query(F.data.startswith("ehwmedia:"))
async def edit_hw_media(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":")
    conn=await db(); cur=await conn.execute("SELECT * FROM homework_media WHERE homework_id=? ORDER BY id",(int(hid),)); ms=await cur.fetchall(); await conn.close()
    rows=[]
    for m in ms:
        rows.append([InlineKeyboardButton(text=f'🗑 {m["kind"]} #{m["id"]}',callback_data=f'delmedia:{m["id"]}:{hid}:{page}:{archived}')])
    rows.append([InlineKeyboardButton(text="➕ Добавить файл",callback_data=f'addmedia:{hid}:{page}:{archived}')])
    rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data=f'edit_hw:{hid}:{page}:{archived}')])
    await replace_callback(c,"📎 <b>Файлы ДЗ</b>\n\nНажми на файл, чтобы удалить его.",kb(rows))

class EditMedia(StatesGroup): media=State()
@router.callback_query(F.data.startswith("addmedia:"))
async def addmedia(c:CallbackQuery,state:FSMContext):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":"); await state.clear(); await state.update_data(hid=int(hid),page=int(page),archived=int(archived)); await state.set_state(EditMedia.media)
    await replace_callback(c,"📎 Отправь файл/фото/видео/медиа для добавления:",kb([[InlineKeyboardButton(text="❌ Отмена",callback_data=f'ehwmedia:{hid}:{page}:{archived}')]]))

@router.message(EditMedia.media)
async def addmedia_message(message:Message,state:FSMContext):
    item=await detect_media(message)
    if not item: await message.answer("Отправь файл, фото, видео или другое поддерживаемое медиа."); return
    data=await state.get_data(); kind,fid,cap,ents=item
    conn=await db(); await conn.execute("INSERT INTO homework_media(homework_id,kind,file_id,caption,caption_entities) VALUES(?,?,?,?,?)",(data['hid'],kind,fid,cap,entity_json(ents))); await conn.commit(); await conn.close(); await state.clear()
    await message.answer("✅ Файл добавлен.")
    await replace_message(message,"📎 <b>Файлы ДЗ</b>",kb([[InlineKeyboardButton(text="⬅️ К редактированию",callback_data=f'edit_hw:{data["hid"]}:{data["page"]}:{data["archived"]}')]]))

@router.callback_query(F.data.startswith("delmedia:"))
async def delmedia(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,mid,hid,page,archived=c.data.split(":"); conn=await db(); await conn.execute("DELETE FROM homework_media WHERE id=?",(int(mid),)); await conn.commit(); await conn.close()
    await edit_hw_media(c)


# ---------- READY ANSWER ----------
@router.callback_query(F.data.startswith("ehwanswer:"))
async def ehwanswer(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":")
    conn=await db(); cur=await conn.execute("SELECT * FROM homework_answer WHERE homework_id=?",(int(hid),)); ans=await cur.fetchone(); await conn.close()
    status="Есть готовый ответ." if ans else "Готового ответа пока нет."
    await replace_callback(c,f"💡 <b>Готовый ответ</b>\n\n{status}\n\nОтправь любое одно сообщение: текст, фото, видео, документ, аудио, голосовое, стикер и т.п. Оно заменит предыдущий ответ.",kb([
        [InlineKeyboardButton(text="➕/🔄 Добавить ответ",callback_data=f'setanswer:{hid}:{page}:{archived}')],
        *([[InlineKeyboardButton(text="🗑 Удалить ответ",callback_data=f'delanswer:{hid}:{page}:{archived}')]] if ans else []),
        [InlineKeyboardButton(text="⬅️ Назад",callback_data=f'edit_hw:{hid}:{page}:{archived}')]
    ]))

@router.callback_query(F.data.startswith("setanswer:"))
async def setanswer(c:CallbackQuery,state:FSMContext):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":"); await state.clear(); await state.update_data(hid=int(hid),page=int(page),archived=int(archived)); await state.set_state(EditHW.answer)
    await replace_callback(c,"💡 Отправь готовый ответ одним сообщением. Можно текст, фото, видео, документ, аудио, голосовое, анимацию, стикер и т.д.",kb([[InlineKeyboardButton(text="❌ Отмена",callback_data=f'ehwanswer:{hid}:{page}:{archived}')]]))

@router.message(EditHW.answer)
async def setanswer_message(message:Message,state:FSMContext):
    data=await state.get_data(); item=await detect_media(message)
    conn=await db()
    await conn.execute("DELETE FROM homework_answer WHERE homework_id=?",(data['hid'],))
    if message.text:
        await conn.execute("INSERT INTO homework_answer(homework_id,kind,text,entities) VALUES(?,?,?,?)",(data['hid'],'text',message.text,entity_json(message.entities)))
    elif item:
        kind,fid,cap,ents=item
        await conn.execute("INSERT INTO homework_answer(homework_id,kind,file_id,caption,entities) VALUES(?,?,?,?,?)",(data['hid'],kind,fid,cap,entity_json(ents)))
    else:
        await conn.close(); await message.answer("Это сообщение пока не поддерживается для готового ответа."); return
    await conn.commit(); await conn.close(); await state.clear(); await message.answer("✅ Готовый ответ сохранён.")
    await replace_message(message,"💡 <b>Готовый ответ</b>",kb([[InlineKeyboardButton(text="⬅️ К редактированию",callback_data=f'edit_hw:{data["hid"]}:{data["page"]}:{data["archived"]}')]]))

@router.callback_query(F.data.startswith("delanswer:"))
async def delanswer(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,hid,page,archived=c.data.split(":"); conn=await db(); await conn.execute("DELETE FROM homework_answer WHERE homework_id=?",(int(hid),)); await conn.commit(); await conn.close(); await ehwanswer(c)


# ---------- DISCIPLINES ----------
@router.callback_query(F.data == "adm:disc")
async def admin_disc(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn=await db(); cur=await conn.execute("SELECT * FROM disciplines ORDER BY active DESC,name"); ds=await cur.fetchall(); await conn.close()
    buttons=[InlineKeyboardButton(text=f'{d["emoji"]} {d["name"]}',callback_data=f'editdisc:{d["id"]}') for d in ds]
    rows=[buttons[i:i+2] for i in range(0,len(buttons),2)]
    rows += [[InlineKeyboardButton(text="➕ Добавить дисциплину",callback_data="newdisc")],[InlineKeyboardButton(text="⬅️ Админ-панель",callback_data="adm")]]
    await replace_callback(c,"📚 <b>Дисциплины</b>\n\nВыбери дисциплину:",kb(rows))

class NewDisc(StatesGroup): name=State()
@router.callback_query(F.data == "newdisc")
async def newdisc(c:CallbackQuery,state:FSMContext):
    await state.clear(); await state.set_state(NewDisc.name); await replace_callback(c,"➕ Введи название новой дисциплины:",kb([[InlineKeyboardButton(text="⬅️ Отмена",callback_data="adm:disc")]]))
@router.message(NewDisc.name)
async def newdisc_name(message:Message,state:FSMContext):
    name=(message.text or '').strip()
    if not name: await message.answer("Название не может быть пустым."); return
    conn=await db()
    try: await conn.execute("INSERT INTO disciplines(name,emoji) VALUES(?,?)",(name,EMOJIS.get(name,'📚'))); await conn.commit(); result="✅ Дисциплина добавлена."
    except aiosqlite.IntegrityError: result="❌ Такая дисциплина уже есть."
    await conn.close(); await state.clear(); await message.answer(result); await replace_message(message,"📚 <b>Дисциплины</b>",kb([[InlineKeyboardButton(text="⬅️ Админ-панель",callback_data="adm")]]))

@router.callback_query(F.data.startswith("editdisc:"))
async def editdisc(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    did=int(c.data.split(":")[1]); conn=await db(); cur=await conn.execute("SELECT * FROM disciplines WHERE id=?",(did,)); d=await cur.fetchone(); await conn.close()
    if not d: return
    await replace_callback(c,f'{d["emoji"]} <b>{esc(d["name"])}</b>',kb([
        [InlineKeyboardButton(text="✏️ Переименовать",callback_data=f'renamedisc:{did}'),InlineKeyboardButton(text="🔄 Вкл/выкл",callback_data=f'toggledisc:{did}')],
        [InlineKeyboardButton(text="🗑 Удалить",callback_data=f'deldiscq:{did}')],[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:disc")]
    ]))

class RenameDisc(StatesGroup): name=State()
@router.callback_query(F.data.startswith("renamedisc:"))
async def renamedisc(c:CallbackQuery,state:FSMContext):
    did=int(c.data.split(":")[1]); await state.update_data(discipline_id=did); await state.set_state(RenameDisc.name); await replace_callback(c,"✏️ Введи новое название:",kb([[InlineKeyboardButton(text="⬅️ Отмена",callback_data="adm:disc")]]))
@router.message(RenameDisc.name)
async def renamedisc_name(message:Message,state:FSMContext):
    data=await state.get_data(); name=(message.text or '').strip(); conn=await db()
    try: await conn.execute("UPDATE disciplines SET name=?,emoji=? WHERE id=?",(name,EMOJIS.get(name,'📚'),data['discipline_id'])); await conn.commit(); result="✅ Название изменено."
    except aiosqlite.IntegrityError: result="❌ Такая дисциплина уже есть."
    await conn.close(); await state.clear(); await message.answer(result); await replace_message(message,"📚 <b>Дисциплины</b>",kb([[InlineKeyboardButton(text="⬅️ Админ-панель",callback_data="adm")]]))
@router.callback_query(F.data.startswith("toggledisc:"))
async def toggledisc(c:CallbackQuery):
    did=int(c.data.split(":")[1]); conn=await db(); await conn.execute("UPDATE disciplines SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(did,)); await conn.commit(); await conn.close(); await admin_disc(c)
@router.callback_query(F.data.startswith("deldiscq:"))
async def deldiscq(c:CallbackQuery):
    did=int(c.data.split(":")[1]); await replace_callback(c,"⚠️ Удаление дисциплины удалит её ДЗ и учебники.\n\nПродолжить?",kb([[InlineKeyboardButton(text="🗑 Да",callback_data=f'deldisc:{did}')],[InlineKeyboardButton(text="⬅️ Нет",callback_data=f'editdisc:{did}')]]))
@router.callback_query(F.data.startswith("deldisc:"))
async def deldisc(c:CallbackQuery):
    did=int(c.data.split(":")[1]); conn=await db(); await conn.execute("DELETE FROM disciplines WHERE id=?",(did,)); await conn.commit(); await conn.close(); await admin_disc(c)


# ---------- STUDENTS ----------
@router.callback_query(F.data == "adm:students")
async def admin_students(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn=await db(); cur=await conn.execute("SELECT * FROM students ORDER BY full_name"); ss=await cur.fetchall(); await conn.close(); registered=sum(1 for s in ss if s['telegram_id'])
    await replace_callback(c,f"👥 <b>Студенты</b>\n\nВсего: {len(ss)}\nПривязано Telegram: {registered}\n\nДобавление/изменение списка группы выполняется через students.xlsx.",kb([[InlineKeyboardButton(text="⬅️ Админ-панель",callback_data="adm")]]))


# ---------- BOOKS ----------
@router.callback_query(F.data == "adm:books")
async def admin_books(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn=await db(); cur=await conn.execute("SELECT d.*,COUNT(t.id) AS book_count FROM disciplines d LEFT JOIN textbooks t ON t.discipline_id=d.id WHERE d.active=1 GROUP BY d.id ORDER BY d.name COLLATE NOCASE"); ds=await cur.fetchall(); await conn.close()
    buttons=[InlineKeyboardButton(text=f'{d["emoji"]} {d["name"]} | 📕 {d["book_count"]}',callback_data=f'admbooks:{d["id"]}') for d in ds]
    rows=[buttons[i:i+2] for i in range(0,len(buttons),2)]+[[InlineKeyboardButton(text="⬅️ Админ-панель",callback_data="adm")]]
    await replace_callback(c,"📖 <b>Учебники</b>\n\nВыбери дисциплину:",kb(rows))

class BookAdd(StatesGroup): title=State(); file=State()
@router.callback_query(F.data.startswith("admbooks:"))
async def admbooks(c:CallbackQuery):
    did=int(c.data.split(":")[1]); conn=await db(); cur=await conn.execute("SELECT * FROM disciplines WHERE id=?",(did,)); d=await cur.fetchone(); cur=await conn.execute("SELECT * FROM textbooks WHERE discipline_id=? ORDER BY id DESC",(did,)); bs=await cur.fetchall(); await conn.close()
    if not d: return
    buttons=[InlineKeyboardButton(text=f'📕 {b["title"]}',callback_data=f'delbookq:{b["id"]}') for b in bs]; rows=[buttons[i:i+2] for i in range(0,len(buttons),2)]
    rows += [[InlineKeyboardButton(text=f'{"🟢 Выключить" if d["textbooks_enabled"] else "⚪ Включить"} раздел',callback_data=f'togglebooks:{did}')],[InlineKeyboardButton(text="➕ Добавить учебник",callback_data=f'addbook:{did}')],[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:books")]]
    await replace_callback(c,f'{d["emoji"]} <b>Учебники — {esc(d["name"])}</b>\n\nУчебников: <b>{len(bs)}</b>',kb(rows))
@router.callback_query(F.data.startswith("togglebooks:"))
async def togglebooks(c:CallbackQuery):
    did=int(c.data.split(":")[1]); conn=await db(); await conn.execute("UPDATE disciplines SET textbooks_enabled=CASE textbooks_enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(did,)); await conn.commit(); await conn.close(); await admbooks(c)
@router.callback_query(F.data.startswith("addbook:"))
async def addbook(c:CallbackQuery,state:FSMContext):
    did=int(c.data.split(":")[1]); await state.clear(); await state.update_data(discipline_id=did); await state.set_state(BookAdd.title); await replace_callback(c,"📕 Введи название учебника:",kb([[InlineKeyboardButton(text="⬅️ Отмена",callback_data=f'admbooks:{did}')]]))
@router.message(BookAdd.title)
async def addbook_title(message:Message,state:FSMContext):
    await state.update_data(title=(message.text or '').strip()); await state.set_state(BookAdd.file); await message.answer("📎 Теперь отправь сам учебник как файл/фото/видео.")
@router.message(BookAdd.file)
async def addbook_file(message:Message,state:FSMContext):
    data=await state.get_data(); item=await detect_media(message)
    if not item: await message.answer("Нужен файл, фото, видео или другое медиа."); return
    kind,fid,cap,ents=item; conn=await db(); await conn.execute("INSERT INTO textbooks(discipline_id,title,kind,file_id,created_at) VALUES(?,?,?,?,?)",(data['discipline_id'],data['title'],kind,fid,datetime.now().isoformat())); await conn.commit(); await conn.close(); await state.clear(); await message.answer("✅ Учебник добавлен."); await replace_message(message,"📖 <b>Учебники</b>",kb([[InlineKeyboardButton(text="⬅️ Админ-панель",callback_data="adm")]]))
@router.callback_query(F.data.startswith("delbookq:"))
async def delbookq(c:CallbackQuery):
    bid=int(c.data.split(":")[1]); conn=await db(); cur=await conn.execute("SELECT * FROM textbooks WHERE id=?",(bid,)); b=await cur.fetchone(); await conn.close()
    if not b:return
    await replace_callback(c,f'🗑 Удалить учебник <b>{esc(b["title"])}</b>?',kb([[InlineKeyboardButton(text="🗑 Да, удалить",callback_data=f'delbook:{bid}')],[InlineKeyboardButton(text="⬅️ Назад",callback_data=f'admbooks:{b["discipline_id"]}')]]))
@router.callback_query(F.data.startswith("delbook:"))
async def delbook(c:CallbackQuery):
    bid=int(c.data.split(":")[1]); conn=await db(); cur=await conn.execute("SELECT discipline_id FROM textbooks WHERE id=?",(bid,)); b=await cur.fetchone();
    if b: did=b['discipline_id']; await conn.execute("DELETE FROM textbooks WHERE id=?",(bid,)); await conn.commit()
    else: did=None
    await conn.close()
    if did: await admbooks(c)
    else: await admin_books(c)


# ---------- VIP ----------
class VipAdd(StatesGroup): telegram_id=State(); full_name=State()

@router.callback_query(F.data == "adm:vip")
async def admin_vip(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    conn=await db(); cur=await conn.execute("SELECT * FROM vip_users ORDER BY full_name COLLATE NOCASE"); vs=await cur.fetchall(); await conn.close()
    buttons=[InlineKeyboardButton(text=f'⭐ {v["full_name"]}',callback_data=f'vipview:{v["telegram_id"]}') for v in vs]
    rows=[buttons[i:i+2] for i in range(0,len(buttons),2)]
    rows += [[InlineKeyboardButton(text="➕ Добавить ID",callback_data="vipadd")],[InlineKeyboardButton(text="⬅️ Админ-панель",callback_data="adm")]]
    await replace_callback(c,"⭐ <b>VIP список</b>\n\nVIP-пользователи имеют доступ к готовым ответам.",kb(rows))

@router.callback_query(F.data == "vipadd")
async def vipadd(c:CallbackQuery,state:FSMContext):
    if not is_admin(c.from_user.id): return
    await state.clear(); await state.set_state(VipAdd.telegram_id); await replace_callback(c,"➕ Введи Telegram ID пользователя (только цифры):",kb([[InlineKeyboardButton(text="❌ Отмена",callback_data="adm:vip")]]))
@router.message(VipAdd.telegram_id)
async def vipadd_id(message:Message,state:FSMContext):
    value=(message.text or '').strip()
    if not value.isdigit(): await message.answer("❌ ID должен состоять только из цифр."); return
    await state.update_data(telegram_id=int(value)); await state.set_state(VipAdd.full_name); await message.answer("👤 Введи имя и фамилию владельца этого Telegram ID:")
@router.message(VipAdd.full_name)
async def vipadd_name(message:Message,state:FSMContext):
    name=(message.text or '').strip()
    if not name: await message.answer("Имя и фамилия не могут быть пустыми."); return
    data=await state.get_data(); conn=await db(); await conn.execute("INSERT OR REPLACE INTO vip_users(telegram_id,full_name,added_at) VALUES(?,?,?)",(data['telegram_id'],name,datetime.now().isoformat())); await conn.commit(); await conn.close(); await state.clear(); await message.answer("✅ Пользователь добавлен в VIP список."); await replace_message(message,"⭐ <b>VIP список</b>",kb([[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:vip")]]))

@router.callback_query(F.data.startswith("vipview:"))
async def vipview(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    uid=int(c.data.split(":")[1]); conn=await db(); cur=await conn.execute("SELECT * FROM vip_users WHERE telegram_id=?",(uid,)); v=await cur.fetchone(); await conn.close()
    if not v:return
    await replace_callback(c,f'⭐ <b>{esc(v["full_name"])}</b>\n\n🆔 Telegram ID: <code>{uid}</code>',kb([[InlineKeyboardButton(text="🗑 Удалить из VIP",callback_data=f'vipdelq:{uid}')],[InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:vip")]]))
@router.callback_query(F.data.startswith("vipdelq:"))
async def vipdelq(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    uid=int(c.data.split(":")[1]); await replace_callback(c,"⚠️ Удалить пользователя из VIP списка?\n\nОн сразу потеряет доступ к готовым ответам.",kb([[InlineKeyboardButton(text="🗑 Да, удалить",callback_data=f'vipdel:{uid}')],[InlineKeyboardButton(text="⬅️ Нет",callback_data=f'vipview:{uid}')]]))
@router.callback_query(F.data.startswith("vipdel:"))
async def vipdel(c:CallbackQuery):
    if not is_admin(c.from_user.id): return
    uid=int(c.data.split(":")[1]); conn=await db(); await conn.execute("DELETE FROM vip_users WHERE telegram_id=?",(uid,)); await conn.commit(); await conn.close(); await admin_vip(c)


@router.callback_query(F.data == "noop")
async def noop(c:CallbackQuery): await c.answer()


async def main():
    await init_db(); await import_xlsx(); await archive_expired()
    if not RICH_AVAILABLE:
        log.warning("Installed aiogram does not support Rich Messages. Upgrade to aiogram>=3.29 for Telegram Rich Markdown/tables.")
    bot=Bot(TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp=Dispatcher(); dp.include_router(router)
    log.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
