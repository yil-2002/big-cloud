import os
import asyncio
import asyncpg
import re
import hashlib
import secrets
import logging
import time
from datetime import datetime
from collections import defaultdict
from cachetools import TTLCache
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not ADMIN_PASSWORD:
    raise ValueError("ADMIN_PASSWORD environment variable is required")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

db_pool = None
PAGE_SIZE = 5

# ─── Cache & Rate limit ───────────────────────────────────────────────────────

# account_id cache — 5 daqiqa saqlanadi, DB yukini ~70% kamaytiradi
account_cache = TTLCache(maxsize=1000, ttl=300)

# Admin sessiyalar — {user_id: login_timestamp}
admin_sessions = {}
ADMIN_SESSION_HOURS = 24

# Rate limiting — foydalanuvchi boshqaruvlari orasidagi min. vaqt
user_last_action = defaultdict(float)
RATE_LIMIT_SECONDS = 1.5

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - user_last_action[user_id] < RATE_LIMIT_SECONDS:
        return True
    user_last_action[user_id] = now
    return False

# ─── States ───────────────────────────────────────────────────────────────────

class AuthState(StatesGroup):
    waiting_reg_username = State()
    waiting_reg_password = State()
    waiting_login_username = State()
    waiting_login_password = State()

class AdminAuthState(StatesGroup):
    waiting_password = State()

class SearchState(StatesGroup):
    waiting_query = State()

class NewFolderState(StatesGroup):
    waiting_name = State()

# ─── Password utils ───────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    pwdhash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return salt + pwdhash.hex()

def verify_password(stored: str, provided: str) -> bool:
    salt = stored[:32]
    pwdhash = hashlib.pbkdf2_hmac('sha256', provided.encode(), salt.encode(), 100000)
    return stored[32:] == pwdhash.hex()

def sanitize_filename(name: str) -> str:
    """Fayl nomidan xavfli belgilarni olib tashlash"""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name[:255]

# ─── Database ─────────────────────────────────────────────────────────────────

async def create_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
        # ─── Jadvallar yaratish ───
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                password_hash TEXT,
                created_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_bindings (
                id SERIAL PRIMARY KEY,
                account_id INTEGER REFERENCES accounts(id),
                telegram_user_id BIGINT UNIQUE,
                bound_at TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id SERIAL PRIMARY KEY,
                file_id TEXT,
                file_name TEXT,
                category TEXT,
                size BIGINT,
                date TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                date TEXT
            )
        """)

        # ─── Migration: eski bazalarga ustunlar qo'shish ───
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS account_id INTEGER")
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS folder TEXT DEFAULT 'umumiy'")
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS pinned INTEGER DEFAULT 0")

        # ─── Indexlar — ustunlar mavjudligi kafolatlangandan keyin ───
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_account ON files(account_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_category ON files(category)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_pinned ON files(pinned)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bindings_tg ON telegram_bindings(telegram_user_id)")

        await conn.execute(
            "INSERT INTO folders (name, date) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
            "umumiy", datetime.now().strftime("%Y-%m-%d %H:%M")
        )
    logger.info("✅ Database ready")

async def get_account_id(telegram_user_id: int):
    """Cache bilan account_id olish — DB yukini kamaytiradi"""
    if telegram_user_id in account_cache:
        return account_cache[telegram_user_id]
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT account_id FROM telegram_bindings WHERE telegram_user_id = $1",
            telegram_user_id
        )
    account_cache[telegram_user_id] = result
    return result

def invalidate_account_cache(telegram_user_id: int):
    """Login/logout da cacheni tozalash"""
    account_cache.pop(telegram_user_id, None)

async def get_account_username(account_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT username FROM accounts WHERE id = $1", account_id)

async def save_file(account_id, file_id, file_name, category, size):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO files (account_id, file_id, file_name, category, size, date, folder, pinned) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            account_id, file_id, file_name, category, size,
            datetime.now().strftime("%Y-%m-%d %H:%M"), "umumiy", 0
        )

SELECT_SQL = (
    "SELECT f.id, f.file_id, f.file_name, f.category, f.size, f.date, f.folder, f.pinned, f.account_id, "
    "a.username as owner_name "
    "FROM files f LEFT JOIN accounts a ON f.account_id = a.id"
)

def shift_placeholders(sql: str, shift: int) -> str:
    if not sql or shift == 0:
        return sql
    return re.sub(r'\$(\d+)', lambda m: f"${int(m.group(1)) + shift}", sql)

async def get_files(account_id: int, is_admin: bool, extra_where: str = "", extra_params: tuple = (),
                    order: str = "f.date DESC", limit: int = PAGE_SIZE, offset: int = 0):
    async with db_pool.acquire() as conn:
        if is_admin:
            if extra_where:
                q = f"{SELECT_SQL} WHERE {extra_where} ORDER BY {order} LIMIT {limit} OFFSET {offset}"
                return await conn.fetch(q, *extra_params)
            else:
                q = f"{SELECT_SQL} ORDER BY {order} LIMIT {limit} OFFSET {offset}"
                return await conn.fetch(q)
        else:
            if extra_where:
                shifted = shift_placeholders(extra_where, 1)
                q = f"{SELECT_SQL} WHERE f.account_id = $1 AND {shifted} ORDER BY {order} LIMIT {limit} OFFSET {offset}"
                return await conn.fetch(q, account_id, *extra_params)
            else:
                q = f"{SELECT_SQL} WHERE f.account_id = $1 ORDER BY {order} LIMIT {limit} OFFSET {offset}"
                return await conn.fetch(q, account_id)

async def get_total(account_id: int, is_admin: bool, extra_where: str = "", extra_params: tuple = ()) -> int:
    async with db_pool.acquire() as conn:
        if is_admin:
            if extra_where:
                q = f"SELECT COUNT(*) FROM files f WHERE {extra_where}"
                return await conn.fetchval(q, *extra_params) or 0
            else:
                return await conn.fetchval("SELECT COUNT(*) FROM files f") or 0
        else:
            if extra_where:
                shifted = shift_placeholders(extra_where, 1)
                q = f"SELECT COUNT(*) FROM files f WHERE f.account_id = $1 AND {shifted}"
                return await conn.fetchval(q, account_id, *extra_params) or 0
            else:
                return await conn.fetchval("SELECT COUNT(*) FROM files f WHERE f.account_id = $1", account_id) or 0

# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_admin_mode(telegram_user_id: int) -> bool:
    if telegram_user_id == ADMIN_ID:
        return True
    if telegram_user_id in admin_sessions:
        # 24 soatdan keyin sessiya tugaydi
        if time.time() - admin_sessions[telegram_user_id] < ADMIN_SESSION_HOURS * 3600:
            return True
        else:
            del admin_sessions[telegram_user_id]
            logger.info(f"Admin session expired for user {telegram_user_id}")
    return False

def get_icon(cat: str) -> str:
    return {"video": "🎬", "photo": "🖼️", "apk": "🤖", "ipa": "🍎"}.get(cat, "📄")

def get_category(ext: str) -> str:
    ext = ext.lower()
    if ext == "apk":
        return "apk"
    if ext == "ipa":
        return "ipa"
    if ext in ["mp4", "mov", "avi", "mkv", "webm"]:
        return "video"
    if ext in ["jpg", "jpeg", "png", "gif", "webp", "bmp"]:
        return "photo"
    return "other"

async def safe_edit(call: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    """Xabarni edit qilish — media bo'lsa delete + yangisi"""
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await call.message.delete()
        except Exception:
            pass
        await bot.send_message(call.message.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)

async def safe_edit_or_caption(call: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    """Fayl xabarlarida caption yoki text edit"""
    try:
        await call.message.edit_caption(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await call.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            try:
                await call.message.delete()
            except Exception:
                pass
            await bot.send_message(call.message.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)

# ─── Keyboards ────────────────────────────────────────────────────────────────

def auth_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔐 Kirish / Вход", callback_data="auth:login"),
        InlineKeyboardButton("📝 Ro'yxatdan o'tish / Регистрация", callback_data="auth:register")
    )
    return kb

def user_menu_kb(show_admin: bool = False):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🎬 Videolar / Видео", callback_data="cat:video:0"),
        InlineKeyboardButton("🖼️ Rasmlar / Фото", callback_data="cat:photo:0"),
        InlineKeyboardButton("🤖 APK/IPA", callback_data="cat:apps:0"),
        InlineKeyboardButton("📄 Boshqalar / Другое", callback_data="cat:other:0"),
    )
    kb.add(
        InlineKeyboardButton("📋 Barchasi / Все", callback_data="cat:all:0"),
        InlineKeyboardButton("📌 Muhimlar / Важное", callback_data="cat:pinned:0"),
    )
    kb.add(
        InlineKeyboardButton("📁 Papkalar / Папки", callback_data="folders:0"),
        InlineKeyboardButton("🔍 Qidirish / Поиск", callback_data="search"),
    )
    kb.add(
        InlineKeyboardButton("📊 Statistika / Статистика", callback_data="stats"),
        InlineKeyboardButton("➕ Papka qo'sh / + Папка", callback_data="newfolder"),
    )
    kb.add(InlineKeyboardButton("🔒 Chiqish / Выход", callback_data="logout"))
    if show_admin:
        kb.add(InlineKeyboardButton("🔐 Admin paneli / Админ панель", callback_data="admin:menu"))
    return kb

def admin_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("☁️ Barcha fayllar / Все файлы", callback_data="admin:cat:all:0"),
        InlineKeyboardButton("👥 Hisoblar / Аккаунты", callback_data="admin:accounts:0"),
    )
    kb.add(
        InlineKeyboardButton("🎬 Barcha videolar / Все видео", callback_data="admin:cat:video:0"),
        InlineKeyboardButton("🖼️ Barcha rasmlar / Все фото", callback_data="admin:cat:photo:0"),
        InlineKeyboardButton("🤖 Barcha APK/IPA", callback_data="admin:cat:apps:0"),
        InlineKeyboardButton("📄 Barcha boshqalar / Другое", callback_data="admin:cat:other:0"),
    )
    kb.add(
        InlineKeyboardButton("📌 Barcha muhimlar / Все важное", callback_data="admin:cat:pinned:0"),
        InlineKeyboardButton("📊 Statistika / Статистика", callback_data="stats"),
    )
    kb.add(InlineKeyboardButton("🔒 Admin rejimdan chiqish / Выйти из админ", callback_data="admin:logout"))
    return kb

def file_actions_kb(file_id_db: int, pinned: int, folder: str):
    kb = InlineKeyboardMarkup(row_width=3)
    pin_btn = (
        InlineKeyboardButton("📌 Pin olish / Открепить", callback_data=f"unpin:{file_id_db}")
        if pinned else
        InlineKeyboardButton("📌 Pin / Закрепить", callback_data=f"pin:{file_id_db}")
    )
    kb.add(
        pin_btn,
        InlineKeyboardButton("📁 Ko'chirish / Переместить", callback_data=f"move:{file_id_db}"),
        InlineKeyboardButton("🗑️ O'chirish / Удалить", callback_data=f"delete:{file_id_db}"),
    )
    return kb

def after_upload_kb(cat: str):
    """Fayl yuborilgandan keyin qulay tugmalar"""
    cat_map = {
        "video": ("cat:video:0", "🎬 Videolar"),
        "photo": ("cat:photo:0", "🖼️ Rasmlar"),
        "apk": ("cat:apps:0", "🤖 APK/IPA"),
        "ipa": ("cat:apps:0", "🤖 APK/IPA"),
        "other": ("cat:other:0", "📄 Boshqalar"),
    }
    cb, label = cat_map.get(cat, ("cat:all:0", "📋 Barchasi"))
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(label, callback_data=cb),
        InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"),
    )
    return kb

def folders_kb(folders_list):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📂 umumiy / общая", callback_data="folder:umumiy:0"))
    for f in folders_list:
        kb.add(InlineKeyboardButton(f"📂 {f['name']}", callback_data=f"folder:{f['name']}:0"))
    kb.add(InlineKeyboardButton("➕ Yangi papka / Новая папка", callback_data="newfolder"))
    kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))
    return kb

def confirm_delete_kb(file_id_db: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ha, o'chir / Да, удалить", callback_data=f"confirmdelete:{file_id_db}"),
        InlineKeyboardButton("❌ Yo'q / Нет", callback_data="menu"),
    )
    return kb

def move_folders_kb(file_id_db: int, folders_list):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📂 umumiy / общая", callback_data=f"domove:{file_id_db}:umumiy"))
    for f in folders_list:
        kb.add(InlineKeyboardButton(f"📂 {f['name']}", callback_data=f"domove:{file_id_db}:{f['name']}"))
    kb.add(InlineKeyboardButton("🔙 Orqaga / Назад", callback_data="menu"))
    return kb

# ─── Start / Auth ─────────────────────────────────────────────────────────────

@dp.message_handler(commands=["start"])
async def cmd_start(message: Message, state: FSMContext):
    await state.finish()
    user = message.from_user
    account_id = await get_account_id(user.id)

    if account_id:
        username = await get_account_username(account_id)
        is_admin_flag = is_admin_mode(user.id)
        await message.answer(
            f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n"
            f"<b>Личное Облачное Хранилище</b>\n\n"
            f"👤 Hisob / Аккаунт: <b>@{username}</b>\n"
            f"📤 Fayl yuboring — saqlanadi!\n"
            f"Отправьте файл — он сохранится!\n\n"
            "Menyudan tanlang / Выберите из меню:",
            parse_mode="HTML",
            reply_markup=user_menu_kb(show_admin=is_admin_flag)
        )
    else:
        await message.answer(
            "☁️ <b>Shaxsiy Bulut Xotira</b>\n"
            "<b>Личное Облачное Хранилище</b>\n\n"
            "🔐 Hisobga kirish yoki yangi hisob ochish\n"
            "Войти в аккаунт или создать новый\n\n"
            "⚠️ <b>Muhim / Важно:</b>\n"
            "<i>Login va parolingizni eslab qoling! U sizga boshqa barcha Telegram akkauntdan kirish imkonini beradi.</i>\n\n"
            "<i>Запомните логин и пароль! Он позволяет вам войти с любого Telegram аккаунта.</i>",
            parse_mode="HTML",
            reply_markup=auth_kb()
        )

@dp.callback_query_handler(lambda c: c.data == "auth:register")
async def cb_register(call: CallbackQuery):
    await call.message.edit_text(
        "📝 <b>Yangi hisob ochish / Регистрация</b>\n\n"
        "Foydalanuvchi nomini kiriting / Введите логин:",
        parse_mode="HTML"
    )
    await AuthState.waiting_reg_username.set()
    await call.answer()

@dp.message_handler(state=AuthState.waiting_reg_username)
async def process_reg_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if not username or len(username) < 3:
        await message.answer(
            "❌ Nom kamida 3 ta belgi / Логин минимум 3 символа. Qayta / Ещё раз:"
        )
        return
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        await message.answer(
            "❌ Faqat lotin harflari, raqam va _ / Только латиница, цифры и _. Qayta / Ещё раз:"
        )
        return

    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM accounts WHERE username = $1", username)
    if exists:
        await message.answer(
            "❌ Bu nom band / Этот логин занят. Boshqa / Другой:"
        )
        return

    await state.update_data(username=username)
    await message.answer(
        "🔑 Parolni kiriting (kamida 4 ta belgi) / Введите пароль (минимум 4 символа):"
    )
    await AuthState.waiting_reg_password.set()

@dp.message_handler(state=AuthState.waiting_reg_password)
async def process_reg_password(message: Message, state: FSMContext):
    password = message.text.strip()
    if len(password) < 4:
        await message.answer(
            "❌ Parol juda qisqa / Пароль слишком короткий. Qayta / Ещё раз:"
        )
        return

    data = await state.get_data()
    username = data["username"]
    password_hash = hash_password(password)

    async with db_pool.acquire() as conn:
        account_id = await conn.fetchval(
            "INSERT INTO accounts (username, password_hash, created_at) VALUES ($1, $2, $3) RETURNING id",
            username, password_hash, datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        await conn.execute(
            "INSERT INTO telegram_bindings (account_id, telegram_user_id, bound_at) VALUES ($1, $2, $3)",
            account_id, message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M")
        )

    invalidate_account_cache(message.from_user.id)
    await state.finish()
    logger.info(f"New account registered: @{username} (tg_id={message.from_user.id})")
    await message.answer(
        f"✅ <b>Hisob yaratildi! / Аккаунт создан!</b>\n\n"
        f"👤 @{username}\n\n"
        "Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
    )

@dp.callback_query_handler(lambda c: c.data == "auth:login")
async def cb_login(call: CallbackQuery):
    await call.message.edit_text(
        "🔐 <b>Hisobga kirish / Вход</b>\n\n"
        "Foydalanuvchi nomini kiriting / Введите логин:",
        parse_mode="HTML"
    )
    await AuthState.waiting_login_username.set()
    await call.answer()

@dp.message_handler(state=AuthState.waiting_login_username)
async def process_login_username(message: Message, state: FSMContext):
    username = message.text.strip()
    await state.update_data(username=username)
    await message.answer(
        "🔑 Parolni kiriting / Введите пароль:"
    )
    await AuthState.waiting_login_password.set()

@dp.message_handler(state=AuthState.waiting_login_password)
async def process_login_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    username = data["username"]

    async with db_pool.acquire() as conn:
        account = await conn.fetchrow("SELECT id, password_hash FROM accounts WHERE username = $1", username)

    if not account or not verify_password(account["password_hash"], password):
        await message.answer(
            "❌ Login yoki parol noto'g'ri / Неверный логин или пароль.\n"
            "Qayta urinib ko'ring / Попробуйте снова: /start"
        )
        await state.finish()
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO telegram_bindings (account_id, telegram_user_id, bound_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (telegram_user_id) DO UPDATE SET account_id = $1, bound_at = $3",
            account["id"], message.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M")
        )

    invalidate_account_cache(message.from_user.id)
    await state.finish()
    logger.info(f"Login: @{username} (tg_id={message.from_user.id})")
    await message.answer(
        f"✅ <b>Xush kelibsiz! / Добро пожаловать!</b>\n\n"
        f"👤 @{username}\n\n"
        "Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
    )

@dp.callback_query_handler(lambda c: c.data == "logout")
async def cb_logout(call: CallbackQuery):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM telegram_bindings WHERE telegram_user_id = $1", call.from_user.id)
    invalidate_account_cache(call.from_user.id)
    logger.info(f"Logout: tg_id={call.from_user.id}")
    await call.answer("🔒 Hisobdan chiqildi / Выход выполнен")
    await call.message.edit_text(
        "☁️ <b>Shaxsiy Bulut Xotira</b>\n"
        "<b>Личное Облачное Хранилище</b>\n\n"
        "🔐 Hisobga kirish yoki yangi hisob ochish\n"
        "Войти в аккаунт или создать новый\n\n"
        "⚠️ <b>Muhim / Важно:</b>\n"
        "<i>Login va parolingizni eslab qoling! U sizga boshqa barcha Telegram akkauntdan kirish imkonini beradi.</i>\n\n"
        "<i>Запомните логин и пароль! Он позволяет вам войти с любого Telegram аккаунта.</i>",
        parse_mode="HTML",
        reply_markup=auth_kb()
    )

# ─── Admin ────────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["admin"])
async def cmd_admin(message: Message, state: FSMContext):
    await state.finish()
    user = message.from_user
    if is_admin_mode(user.id):
        await message.answer(
            "🔰 <b>Admin panel / Админ панель</b>\n\n"
            "Barcha hisoblar va fayllar ko'rinadi.\n"
            "Все аккаунты и файлы видны.",
            parse_mode="HTML",
            reply_markup=admin_menu_kb()
        )
    else:
        await message.answer(
            "🔐 <b>Admin parolini kiriting / Введите админ пароль:</b>",
            parse_mode="HTML"
        )
        await AdminAuthState.waiting_password.set()

@dp.message_handler(state=AdminAuthState.waiting_password)
async def process_admin_password(message: Message, state: FSMContext):
    if message.text == ADMIN_PASSWORD:
        admin_sessions[message.from_user.id] = time.time()
        await state.finish()
        logger.info(f"Admin login: tg_id={message.from_user.id}")
        await message.answer(
            "✅ <b>Admin rejimi faollashdi! / Админ режим активирован!</b>\n\n"
            "🔰 Admin panel / Админ панель:",
            parse_mode="HTML",
            reply_markup=admin_menu_kb()
        )
    else:
        await message.answer(
            "❌ Noto'g'ri parol / Неверный пароль. Qayta / Ещё раз:",
            parse_mode="HTML"
        )

# ─── Menu callbacks ───────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "menu")
async def cb_menu(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq! / Медленнее!")
        return
    user = call.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await safe_edit(
            call,
            "☁️ <b>Shaxsiy Bulut Xotira</b>\n"
            "<b>Личное Облачное Хранилище</b>\n\n"
            "🔐 Hisobga kirish / Войти:\n\n"
            "⚠️ <i>Login va parolingizni eslab qoling! / Запомните логин и пароль!</i>",
            reply_markup=auth_kb()
        )
        await call.answer()
        return
    is_admin_flag = is_admin_mode(user.id)
    username = await get_account_username(account_id)
    await safe_edit(
        call,
        f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n"
        f"<b>Личное Облачное Хранилище</b>\n\n"
        f"👤 Hisob / Аккаунт: <b>@{username}</b>\n"
        f"Menyudan tanlang / Выберите из меню:",
        reply_markup=user_menu_kb(show_admin=is_admin_flag)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:menu")
async def cb_admin_menu(call: CallbackQuery):
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q / Нет доступа!", show_alert=True)
        return
    await safe_edit(
        call,
        "🔰 <b>Admin panel / Админ панель</b>",
        reply_markup=admin_menu_kb()
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:logout")
async def cb_admin_logout(call: CallbackQuery):
    admin_sessions.pop(call.from_user.id, None)
    logger.info(f"Admin logout: tg_id={call.from_user.id}")
    await call.answer("🔒 Admin rejimi yopildi / Админ режим закрыт")
    await cb_menu(call)

@dp.callback_query_handler(lambda c: c.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()

# ─── File upload handlers ─────────────────────────────────────────────────────

@dp.message_handler(content_types=types.ContentType.VIDEO)
async def handle_video(message: Message):
    user = message.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await message.answer("🔐 Avval /start orqali kiring / Сначала войдите через /start", reply_markup=auth_kb())
        return
    v = message.video
    name = sanitize_filename(v.file_name or f"video_{v.file_id[:8]}.mp4")
    await save_file(account_id, v.file_id, name, "video", v.file_size or 0)
    mb = round((v.file_size or 0) / 1024 / 1024, 2)
    logger.info(f"Video saved: {name} ({mb}MB) by account_id={account_id}")
    await message.answer(
        f"🎬 <b>Saqlandi! / Сохранено!</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML",
        reply_markup=after_upload_kb("video")
    )

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def handle_photo(message: Message):
    user = message.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await message.answer("🔐 Avval /start orqali kiring / Сначала войдите через /start", reply_markup=auth_kb())
        return
    p = message.photo[-1]
    name = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    await save_file(account_id, p.file_id, name, "photo", p.file_size or 0)
    mb = round((p.file_size or 0) / 1024 / 1024, 2)
    logger.info(f"Photo saved: {name} by account_id={account_id}")
    await message.answer(
        f"🖼️ <b>Saqlandi! / Сохранено!</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML",
        reply_markup=after_upload_kb("photo")
    )

@dp.message_handler(content_types=types.ContentType.DOCUMENT)
async def handle_document(message: Message):
    user = message.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await message.answer("🔐 Avval /start orqali kiring / Сначала войдите через /start", reply_markup=auth_kb())
        return
    d = message.document
    name = sanitize_filename(d.file_name or "nomsiz_fayl")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    category = get_category(ext)
    await save_file(account_id, d.file_id, name, category, d.file_size or 0)
    mb = round((d.file_size or 0) / 1024 / 1024, 2)
    icon = get_icon(category)
    logger.info(f"Document saved: {name} ({category}, {mb}MB) by account_id={account_id}")
    await message.answer(
        f"{icon} <b>Saqlandi! / Сохранено!</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML",
        reply_markup=after_upload_kb(category)
    )

@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_unknown(message: Message):
    """Noto'g'ri fayl turi"""
    account_id = await get_account_id(message.from_user.id)
    if account_id:
        await message.answer(
            "❓ <b>Bu fayl turi saqlanmaydi.</b>\n"
            "Video, rasm, APK yoki hujjat yuboring!\n\n"
            "Этот тип файла не поддерживается.\n"
            "Отправьте видео, фото, APK или документ!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
            )
        )

# ─── Show files ───────────────────────────────────────────────────────────────

async def send_file_safe(chat_id, file_id, cat, caption, reply_markup=None):
    try:
        if cat == "video":
            await bot.send_video(chat_id, file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML")
        elif cat == "photo":
            await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await bot.send_document(chat_id, file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"send_file_safe fallback: {e}")
        await bot.send_message(chat_id, caption, reply_markup=reply_markup, parse_mode="HTML")

async def show_files_page(chat_id, account_id, is_admin, ctx, page=0, extra_where="", extra_params=(), order="f.date DESC"):
    total = await get_total(account_id, is_admin, extra_where, extra_params)
    if total == 0:
        await bot.send_message(
            chat_id,
            "😔 Hozircha fayl yo'q. / Пока файлов нет.\n"
            "📤 Fayl yuboring — saqlanadi! / Отправьте файл — сохранится!",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
            )
        )
        return

    offset = page * PAGE_SIZE
    rows = await get_files(account_id, is_admin, extra_where, extra_params, order, limit=PAGE_SIZE, offset=offset)

    for i, row in enumerate(rows):
        db_id = row["id"]
        file_id = row["file_id"]
        name = row["file_name"]
        cat = row["category"]
        size = row["size"] or 0
        date = row["date"]
        folder = row["folder"]
        pinned = row["pinned"]
        owner_account_id = row["account_id"]
        owner_name = row["owner_name"]

        mb = round(size / 1024 / 1024, 2)
        pin_icon = "📌 " if pinned else ""

        owner_text = ""
        if is_admin and owner_account_id != account_id:
            owner_name_str = owner_name or "noma'lum"
            owner_text = f"\n👤 <b>@{owner_name_str}</b>"

        caption = (
            f"{pin_icon}{get_icon(cat)} <b>{name}</b>\n"
            f"💾 {mb} MB  |  📅 {date}\n"
            f"📁 {folder}{owner_text}"
        )

        kb = file_actions_kb(db_id, pinned, folder)

        if i == len(rows) - 1:
            total_pages = max(1, (total - 1) // PAGE_SIZE + 1)
            nav_btns = []
            if page > 0:
                nav_btns.append(InlineKeyboardButton("⬅️", callback_data=f"{ctx}:{page - 1}"))
            nav_btns.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
            if (page + 1) * PAGE_SIZE < total:
                nav_btns.append(InlineKeyboardButton("➡️", callback_data=f"{ctx}:{page + 1}"))
            if nav_btns:
                kb.add(*nav_btns)
            kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))

        await send_file_safe(chat_id, file_id, cat, caption, reply_markup=kb)

# ─── Universal category handler ───────────────────────────────────────────────

CAT_MAP = {
    "video": ("f.category = $1", ("video",)),
    "photo": ("f.category = $1", ("photo",)),
    "apps":  ("f.category IN ($1, $2)", ("apk", "ipa")),
    "other": ("f.category = $1", ("other",)),
    "all":   ("", ()),
    "pinned":("f.pinned = $1", (1,)),
}

CAT_NAMES_USER = {
    "video": "🎬 Videolar / Видео", "photo": "🖼️ Rasmlar / Фото",
    "apps": "🤖 APK/IPA", "other": "📄 Boshqalar / Другое",
    "all": "📋 Barcha fayllar / Все файлы", "pinned": "📌 Muhim fayllar / Важное",
}

CAT_NAMES_ADMIN = {
    "video": "🎬 Barcha videolar / Все видео", "photo": "🖼️ Barcha rasmlar / Все фото",
    "apps": "🤖 Barcha APK/IPA", "other": "📄 Barcha boshqalar / Другое",
    "all": "☁️ Barcha fayllar / Все файлы", "pinned": "📌 Barcha muhimlar / Все важное",
}

@dp.callback_query_handler(lambda c: c.data.startswith("cat:"))
async def cb_category(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq! / Медленнее!")
        return
    user = call.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring! / Сначала войдите!", show_alert=True)
        return

    _, cat, page = call.data.split(":")
    page = int(page)
    if cat not in CAT_MAP:
        await call.answer()
        return

    where, params = CAT_MAP[cat]
    ctx = f"cat:{cat}"
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(
        call.message.chat.id,
        f"<b>{CAT_NAMES_USER[cat]}</b>",
        parse_mode="HTML"
    )
    await show_files_page(call.message.chat.id, account_id, False, ctx, page, where, params)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin:cat:"))
async def cb_admin_category(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq! / Медленнее!")
        return
    user = call.from_user
    if not is_admin_mode(user.id):
        await call.answer("❌ Ruxsat yo'q / Нет доступа!", show_alert=True)
        return

    _, _, cat, page = call.data.split(":")
    page = int(page)
    if cat not in CAT_MAP:
        await call.answer()
        return

    where, params = CAT_MAP[cat]
    ctx = f"admin:cat:{cat}"
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(
        call.message.chat.id,
        f"<b>{CAT_NAMES_ADMIN[cat]}</b>",
        parse_mode="HTML"
    )
    await show_files_page(call.message.chat.id, 0, True, ctx, page, where, params)
    await call.answer()

# ─── Admin accounts ───────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("admin:accounts:"))
async def cb_admin_accounts(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq!")
        return
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q / Нет доступа!", show_alert=True)
        return

    page = int(call.data.split(":")[2])
    async with db_pool.acquire() as conn:
        accounts = await conn.fetch(
            "SELECT id, username, created_at FROM accounts ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            PAGE_SIZE, page * PAGE_SIZE
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM accounts") or 0

    if not accounts:
        await call.answer("Hisoblar yo'q / Аккаунтов нет!")
        return

    kb = InlineKeyboardMarkup(row_width=2)
    for acc in accounts:
        kb.add(InlineKeyboardButton(f"👤 @{acc['username']}", callback_data=f"admin:account:{acc['id']}:0"))

    total_pages = max(1, (total - 1) // PAGE_SIZE + 1)
    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton("⬅️", callback_data=f"admin:accounts:{page - 1}"))
    nav_btns.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if (page + 1) * PAGE_SIZE < total:
        nav_btns.append(InlineKeyboardButton("➡️", callback_data=f"admin:accounts:{page + 1}"))
    if nav_btns:
        kb.add(*nav_btns)
    kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))

    await safe_edit(
        call,
        f"👥 <b>Hisoblar ro'yxati / Список аккаунтов</b> (jami / всего: {total}):",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin:account:"))
async def cb_admin_account_files(call: CallbackQuery):
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q / Нет доступа!", show_alert=True)
        return

    parts = call.data.split(":")
    target_account_id = int(parts[2])
    page = int(parts[3])

    async with db_pool.acquire() as conn:
        target = await conn.fetchrow("SELECT username FROM accounts WHERE id = $1", target_account_id)

    name = target["username"] if target else "noma'lum"
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(
        call.message.chat.id,
        f"👤 <b>@{name}</b> ning fayllari / файлы:",
        parse_mode="HTML"
    )
    await show_files_page(
        call.message.chat.id, 0, True,
        f"admin:account:{target_account_id}", page,
        extra_where="f.account_id = $1", extra_params=(target_account_id,)
    )
    await call.answer()

# ─── Folder callbacks ─────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "folders:0" or c.data.startswith("folders:"))
async def cb_folders(call: CallbackQuery):
    async with db_pool.acquire() as conn:
        folders = await conn.fetch("SELECT name FROM folders ORDER BY name")

    await safe_edit(
        call,
        "📁 <b>Papkalar / Папки</b>\n\nPapkani tanlang / Выберите папку:",
        reply_markup=folders_kb(folders)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("folder:"))
async def cb_folder(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq!")
        return
    user = call.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring! / Сначала войдите!", show_alert=True)
        return
    is_admin_flag = is_admin_mode(user.id)
    parts = call.data.split(":")
    folder_name = parts[1]
    page = int(parts[2])
    ctx = f"folder:{folder_name}"

    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(
        call.message.chat.id,
        f"📂 <b>{folder_name}</b> papkasi / папка:",
        parse_mode="HTML"
    )
    await show_files_page(
        call.message.chat.id, account_id, is_admin_flag, ctx, page,
        extra_where="f.folder = $1", extra_params=(folder_name,)
    )
    await call.answer()

# ─── File actions ─────────────────────────────────────────────────────────────

async def check_file_access(call: CallbackQuery, db_id: int):
    """Fayl mavjudligi va ruxsatni tekshirish"""
    account_id = await get_account_id(call.from_user.id)
    is_admin_flag = is_admin_mode(call.from_user.id)
    async with db_pool.acquire() as conn:
        file_account = await conn.fetchval("SELECT account_id FROM files WHERE id = $1", db_id)
    if file_account is None:
        await call.answer("Fayl topilmadi! / Файл не найден!", show_alert=True)
        return None, None, False
    if not is_admin_flag and file_account != account_id:
        await call.answer("❌ Bu sizning faylingiz emas! / Это не ваш файл!", show_alert=True)
        return None, None, False
    return account_id, file_account, is_admin_flag

@dp.callback_query_handler(lambda c: c.data.startswith("pin:"))
async def cb_pin(call: CallbackQuery):
    db_id = int(call.data.split(":")[1])
    account_id, file_account, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag:
        return

    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET pinned = 1 WHERE id = $1", db_id)
        row = await conn.fetchrow("SELECT id, pinned, folder FROM files WHERE id = $1", db_id)

    await call.answer(f"📌 {name} muhim belgilandi!", show_alert=False)
    if row:
        try:
            await call.message.edit_reply_markup(reply_markup=file_actions_kb(row["id"], row["pinned"], row["folder"]))
        except Exception:
            pass

@dp.callback_query_handler(lambda c: c.data.startswith("unpin:"))
async def cb_unpin(call: CallbackQuery):
    db_id = int(call.data.split(":")[1])
    account_id, file_account, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag:
        return

    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET pinned = 0 WHERE id = $1", db_id)
        row = await conn.fetchrow("SELECT id, pinned, folder FROM files WHERE id = $1", db_id)

    await call.answer(f"✅ {name} dan pin olindi!", show_alert=False)
    if row:
        try:
            await call.message.edit_reply_markup(reply_markup=file_actions_kb(row["id"], row["pinned"], row["folder"]))
        except Exception:
            pass

@dp.callback_query_handler(lambda c: c.data.startswith("delete:"))
async def cb_delete(call: CallbackQuery):
    db_id = int(call.data.split(":")[1])
    account_id, file_account, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag:
        return

    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)

    await safe_edit_or_caption(
        call,
        f"🗑️ <b>{name}</b> ni o'chirishni tasdiqlaysizmi?\n"
        f"Подтвердите удаление <b>{name}</b>:",
        reply_markup=confirm_delete_kb(db_id)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("confirmdelete:"))
async def cb_confirm_delete(call: CallbackQuery):
    db_id = int(call.data.split(":")[1])
    account_id, file_account, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag:
        return

    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("DELETE FROM files WHERE id = $1", db_id)

    logger.info(f"File deleted: id={db_id} ({name}) by tg_id={call.from_user.id}")
    await call.message.edit_text(
        f"🗑️ <b>{name}</b> o'chirildi! / удален!",
        parse_mode="HTML"
    )
    await call.answer("O'chirildi! / Удалено!")

@dp.callback_query_handler(lambda c: c.data.startswith("move:"))
async def cb_move(call: CallbackQuery):
    db_id = int(call.data.split(":")[1])
    account_id, file_account, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag:
        return

    async with db_pool.acquire() as conn:
        folders = await conn.fetch("SELECT name FROM folders ORDER BY name")
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)

    await safe_edit_or_caption(
        call,
        f"📁 <b>{name}</b> ni qaysi papkaga ko'chirish?\n"
        f"В какую папку переместить <b>{name}</b>:",
        reply_markup=move_folders_kb(db_id, folders)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("domove:"))
async def cb_do_move(call: CallbackQuery):
    parts = call.data.split(":", 2)
    db_id = int(parts[1])
    folder_name = parts[2]
    account_id, file_account, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag:
        return

    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET folder = $1 WHERE id = $2", folder_name, db_id)

    await call.message.edit_text(
        f"✅ <b>{name}</b> → 📁 {folder_name}",
        parse_mode="HTML"
    )
    await call.answer()

# ─── New folder ───────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "newfolder")
async def cb_newfolder(call: CallbackQuery):
    await safe_edit(
        call,
        "📁 Yangi papka nomini yozing / Введите название новой папки:"
    )
    await NewFolderState.waiting_name.set()
    await call.answer()

@dp.message_handler(state=NewFolderState.waiting_name)
async def process_newfolder(message: Message, state: FSMContext):
    name = sanitize_filename(message.text.strip())
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO folders (name, date) VALUES ($1, $2)",
                name, datetime.now().strftime("%Y-%m-%d %H:%M")
            )
            admin_flag = is_admin_mode(message.from_user.id)
            await message.answer(
                f"✅ <b>{name}</b> papkasi yaratildi! / папка создана!",
                parse_mode="HTML",
                reply_markup=(admin_menu_kb() if admin_flag else user_menu_kb(show_admin=admin_flag))
            )
        except Exception:
            await message.answer(
                f"❌ <b>{name}</b> papkasi allaqachon mavjud! / папка уже существует!",
                parse_mode="HTML"
            )
    await state.finish()

# ─── Search ───────────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "search")
async def cb_search(call: CallbackQuery):
    await safe_edit(
        call,
        "🔍 Qidiruv so'zini yozing / Введите слово для поиска:"
    )
    await SearchState.waiting_query.set()
    await call.answer()

@dp.message_handler(state=SearchState.waiting_query)
async def process_search(message: Message, state: FSMContext):
    user = message.from_user
    account_id = await get_account_id(user.id)
    is_admin_flag = is_admin_mode(user.id)
    keyword = f"%{message.text.strip()}%"

    async with db_pool.acquire() as conn:
        if is_admin_flag:
            rows = await conn.fetch(
                f"{SELECT_SQL} WHERE f.file_name ILIKE $1 ORDER BY f.date DESC",
                keyword
            )
        else:
            if not account_id:
                await message.answer("🔐 Avval kiring / Сначала войдите", reply_markup=auth_kb())
                await state.finish()
                return
            rows = await conn.fetch(
                f"{SELECT_SQL} WHERE f.account_id = $1 AND f.file_name ILIKE $2 ORDER BY f.date DESC",
                account_id, keyword
            )

    await state.finish()
    if not rows:
        await message.answer(
            "🔍 Hech narsa topilmadi. / Ничего не найдено.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
            )
        )
        return

    await message.answer(
        f"🔍 <b>{len(rows)} ta natija / результатов:</b>",
        parse_mode="HTML"
    )
    for row in rows:
        db_id = row["id"]
        file_id = row["file_id"]
        name = row["file_name"]
        cat = row["category"]
        size = row["size"] or 0
        date = row["date"]
        folder = row["folder"]
        pinned = row["pinned"]
        owner_account_id = row["account_id"]
        owner_name = row["owner_name"]

        mb = round(size / 1024 / 1024, 2)
        pin_icon = "📌 " if pinned else ""

        owner_text = ""
        if is_admin_flag and owner_account_id != account_id:
            owner_name_str = owner_name or "noma'lum"
            owner_text = f"\n👤 <b>@{owner_name_str}</b>"

        caption = (
            f"{pin_icon}{get_icon(cat)} <b>{name}</b>\n"
            f"💾 {mb} MB  |  📅 {date}\n"
            f"📁 {folder}{owner_text}"
        )
        await send_file_safe(
            message.chat.id, file_id, cat, caption,
            reply_markup=file_actions_kb(db_id, pinned, folder)
        )

# ─── Stats ────────────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "stats")
async def cb_stats(call: CallbackQuery):
    user = call.from_user
    account_id = await get_account_id(user.id)
    is_admin_flag = is_admin_mode(user.id)

    async with db_pool.acquire() as conn:
        if is_admin_flag:
            # 🚀 Bitta so'rov bilan barcha statistika
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total,
                    COALESCE(SUM(size), 0) as total_size,
                    COUNT(*) FILTER (WHERE category = 'video') as vid_cnt,
                    COUNT(*) FILTER (WHERE category = 'photo') as photo_cnt,
                    COUNT(*) FILTER (WHERE category IN ('apk', 'ipa')) as app_cnt,
                    COUNT(*) FILTER (WHERE category = 'other') as other_cnt,
                    COUNT(*) FILTER (WHERE pinned = 1) as pin_cnt
                FROM files
            """)
            fold_cnt = await conn.fetchval("SELECT COUNT(*) FROM folders") or 0
            acc_cnt = await conn.fetchval("SELECT COUNT(*) FROM accounts") or 0
        else:
            if not account_id:
                await call.answer("🔐 Avval kiring! / Сначала войдите!", show_alert=True)
                return
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total,
                    COALESCE(SUM(size), 0) as total_size,
                    COUNT(*) FILTER (WHERE category = 'video') as vid_cnt,
                    COUNT(*) FILTER (WHERE category = 'photo') as photo_cnt,
                    COUNT(*) FILTER (WHERE category IN ('apk', 'ipa')) as app_cnt,
                    COUNT(*) FILTER (WHERE category = 'other') as other_cnt,
                    COUNT(*) FILTER (WHERE pinned = 1) as pin_cnt
                FROM files WHERE account_id = $1
            """, account_id)
            fold_cnt = await conn.fetchval("SELECT COUNT(*) FROM folders") or 0
            acc_cnt = 1

    mb = round((stats["total_size"] or 0) / 1024 / 1024, 2)
    gb = round(mb / 1024, 3)

    if is_admin_flag:
        text = (
            f"📊 <b>Admin Statistika / Админ статистика</b>\n\n"
            f"👥 Hisoblar / Аккаунты: <b>{acc_cnt}</b>\n"
            f"📄 Jami fayllar / Всего файлов: <b>{stats['total']}</b>\n"
            f"💾 Umumiy hajm / Общий объем: <b>{mb} MB ({gb} GB)</b>\n\n"
            f"🎬 Videolar / Видео: <b>{stats['vid_cnt']}</b>\n"
            f"🖼️ Rasmlar / Фото: <b>{stats['photo_cnt']}</b>\n"
            f"🤖 APK/IPA: <b>{stats['app_cnt']}</b>\n"
            f"📄 Boshqalar / Другое: <b>{stats['other_cnt']}</b>\n\n"
            f"📌 Muhim fayllar / Важное: <b>{stats['pin_cnt']}</b>\n"
            f"📁 Papkalar / Папки: <b>{fold_cnt + 1}</b>"
        )
    else:
        text = (
            f"📊 <b>Statistika / Статистика</b>\n\n"
            f"📄 Jami fayllar / Всего файлов: <b>{stats['total']}</b>\n"
            f"💾 Umumiy hajm / Общий объем: <b>{mb} MB ({gb} GB)</b>\n\n"
            f"🎬 Videolar / Видео: <b>{stats['vid_cnt']}</b>\n"
            f"🖼️ Rasmlar / Фото: <b>{stats['photo_cnt']}</b>\n"
            f"🤖 APK/IPA: <b>{stats['app_cnt']}</b>\n"
            f"📄 Boshqalar / Другое: <b>{stats['other_cnt']}</b>\n\n"
            f"📌 Muhim fayllar / Важное: <b>{stats['pin_cnt']}</b>\n"
            f"📁 Papkalar / Папки: <b>{fold_cnt + 1}</b>"
        )

    await safe_edit(call, text, reply_markup=InlineKeyboardMarkup().add(
        InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
    ))
    await call.answer()

# ─── /menu command ────────────────────────────────────────────────────────────

@dp.message_handler(commands=["menu"])
async def cmd_menu(message: Message, state: FSMContext):
    await state.finish()
    user = message.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await message.answer(
            "☁️ <b>Shaxsiy Bulut Xotira</b>\n"
            "<b>Личное Облачное Хранилище</b>\n\n"
            "🔐 Hisobga kirish / Войти:\n\n"
            "⚠️ <i>Login va parolingizni eslab qoling! / Запомните логин и пароль!</i>",
            parse_mode="HTML",
            reply_markup=auth_kb()
        )
        return
    is_admin_flag = is_admin_mode(user.id)
    username = await get_account_username(account_id)
    await message.answer(
        f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n"
        f"<b>Личное Облачное Хранилище</b>\n\n"
        f"👤 Hisob / Аккаунт: <b>@{username}</b>\n"
        f"Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_flag)
    )

# ─── Startup ──────────────────────────────────────────────────────────────────

async def on_startup(dp):
    await create_db()
    from aiohttp import web
    async def health(request):
        return web.Response(text="OK")
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ Bot ishga tushdi! Port: {port}")

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
