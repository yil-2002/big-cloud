import os
import asyncio
import asyncpg
import re
import hashlib
import secrets
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor

# ─── Config (faqat environment variables) ─────────────────────────────────────

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
admin_sessions = set()
PAGE_SIZE = 5

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

# ─── Database ─────────────────────────────────────────────────────────────────

async def create_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with db_pool.acquire() as conn:
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
                account_id INTEGER,
                file_id TEXT,
                file_name TEXT,
                category TEXT,
                size BIGINT,
                date TEXT,
                folder TEXT DEFAULT 'umumiy',
                pinned INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                date TEXT
            )
        """)
        await conn.execute(
            "INSERT INTO folders (name, date) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
            "umumiy", datetime.now().strftime("%Y-%m-%d %H:%M")
        )

async def get_account_id(telegram_user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT account_id FROM telegram_bindings WHERE telegram_user_id = $1",
            telegram_user_id
        )

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
    return telegram_user_id in admin_sessions or telegram_user_id == ADMIN_ID

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

def pagination_kb(ctx: str, page: int, total: int):
    kb = InlineKeyboardMarkup(row_width=3)
    total_pages = max(1, (total - 1) // PAGE_SIZE + 1)
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("⬅️", callback_data=f"{ctx}:{page - 1}"))
    btns.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if (page + 1) * PAGE_SIZE < total:
        btns.append(InlineKeyboardButton("➡️", callback_data=f"{ctx}:{page + 1}"))
    kb.add(*btns)
    kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))
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
async def cmd_start(message: Message):
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
    
    await state.finish()
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
    
    await state.finish()
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
async def cmd_admin(message: Message):
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
        admin_sessions.add(message.from_user.id)
        await state.finish()
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
    user = call.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await call.message.edit_text(
            "☁️ <b>Shaxsiy Bulut Xotira</b>\n"
            "<b>Личное Облачное Хранилище</b>\n\n"
            "🔐 Hisobga kirish / Войти:\n\n"
            "⚠️ <i>Login va parolingizni eslab qoling! / Запомните логин и пароль!</i>",
            parse_mode="HTML",
            reply_markup=auth_kb()
        )
        await call.answer()
        return
    is_admin_flag = is_admin_mode(user.id)
    username = await get_account_username(account_id)
    await call.message.edit_text(
        f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n"
        f"<b>Личное Облачное Хранилище</b>\n\n"
        f"👤 Hisob / Аккаунт: <b>@{username}</b>\n"
        f"Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_flag)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:menu")
async def cb_admin_menu(call: CallbackQuery):
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q / Нет доступа!", show_alert=True)
        return
    await call.message.edit_text(
        "🔰 <b>Admin panel / Админ панель</b>",
        parse_mode="HTML",
        reply_markup=admin_menu_kb()
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:logout")
async def cb_admin_logout(call: CallbackQuery):
    admin_sessions.discard(call.from_user.id)
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
        await message.answer(
            "🔐 Avval /start orqali kiring / Сначала войдите через /start",
            reply_markup=auth_kb()
        )
        return
    v = message.video
    name = v.file_name or f"video_{v.file_id[:8]}.mp4"
    await save_file(account_id, v.file_id, name, "video", v.file_size or 0)
    mb = round((v.file_size or 0) / 1024 / 1024, 2)
    await message.answer(
        f"🎬 <b>Saqlandi! / Сохранено!</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML"
    )

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def handle_photo(message: Message):
    user = message.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await message.answer(
            "🔐 Avval /start orqali kiring / Сначала войдите через /start",
            reply_markup=auth_kb()
        )
        return
    p = message.photo[-1]
    name = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    await save_file(account_id, p.file_id, name, "photo", p.file_size or 0)
    mb = round((p.file_size or 0) / 1024 / 1024, 2)
    await message.answer(
        f"🖼️ <b>Saqlandi! / Сохранено!</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML"
    )

@dp.message_handler(content_types=types.ContentType.DOCUMENT)
async def handle_document(message: Message):
    user = message.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await message.answer(
            "🔐 Avval /start orqali kiring / Сначала войдите через /start",
            reply_markup=auth_kb()
        )
        return
    d = message.document
    name = d.file_name or "nomsiz_fayl / безымянный"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    category = get_category(ext)
    await save_file(account_id, d.file_id, name, category, d.file_size or 0)
    mb = round((d.file_size or 0) / 1024 / 1024, 2)
    icon = get_icon(category)
    await message.answer(
        f"{icon} <b>Saqlandi! / Сохранено!</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML"
    )

# ─── Show files ─────────────────────────────────────────────────────────────────

async def send_file_safe(chat_id, file_id, cat, caption, reply_markup=None):
    try:
        if cat == "video":
            await bot.send_video(chat_id, file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML")
        elif cat == "photo":
            await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await bot.send_document(chat_id, file_id, caption=caption, reply_markup=reply_markup, parse_mode="HTML")
    except Exception:
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
        else:
            await send_file_safe(chat_id, file_id, cat, caption, reply_markup=kb)

# ─── User category callbacks ─────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("cat:"))
async def cb_category(call: CallbackQuery):
    user = call.from_user
    account_id = await get_account_id(user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring! / Сначала войдите!", show_alert=True)
        return
    
    _, cat, page = call.data.split(":")
    page = int(page)

    cat_map = {
        "video": ("f.category = $1", ("video",), "cat:video"),
        "photo": ("f.category = $1", ("photo",), "cat:photo"),
        "apps": ("f.category IN ($1, $2)", ("apk", "ipa"), "cat:apps"),
        "other": ("f.category = $1", ("other",), "cat:other"),
        "all": ("", (), "cat:all"),
        "pinned": ("f.pinned = $1", (1,), "cat:pinned"),
    }
    cat_names = {
        "video": "🎬 Videolar / Видео", "photo": "🖼️ Rasmlar / Фото",
        "apps": "🤖 APK/IPA", "other": "📄 Boshqalar / Другое",
        "all": "📋 Barcha fayllar / Все файлы", "pinned": "📌 Muhim fayllar / Важное",
    }

    if cat not in cat_map:
        await call.answer()
        return

    where, params, ctx = cat_map[cat]
    await call.message.delete()
    await bot.send_message(
        call.message.chat.id,
        f"<b>{cat_names[cat]}</b>\n"
        f"{page * PAGE_SIZE + 1}-{(page + 1) * PAGE_SIZE} ko'rsatilmoqda / показывается:",
        parse_mode="HTML"
    )
    await show_files_page(call.message.chat.id, account_id, False, ctx, page, where, params)
    await call.answer()

# ─── Admin callbacks ──────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("admin:cat:"))
async def cb_admin_category(call: CallbackQuery):
    user = call.from_user
    if not is_admin_mode(user.id):
        await call.answer("❌ Ruxsat yo'q / Нет доступа!", show_alert=True)
        return
    
    _, _, cat, page = call.data.split(":")
    page = int(page)

    cat_map = {
        "video": ("f.category = $1", ("video",), "admin:cat:video"),
        "photo": ("f.category = $1", ("photo",), "admin:cat:photo"),
        "apps": ("f.category IN ($1, $2)", ("apk", "ipa"), "admin:cat:apps"),
        "other": ("f.category = $1", ("other",), "admin:cat:other"),
        "all": ("", (), "admin:cat:all"),
        "pinned": ("f.pinned = $1", (1,), "admin:cat:pinned"),
    }
    cat_names = {
        "video": "🎬 Barcha videolar / Все видео", "photo": "🖼️ Barcha rasmlar / Все фото",
        "apps": "🤖 Barcha APK/IPA", "other": "📄 Barcha boshqalar / Другое",
        "all": "☁️ Barcha fayllar / Все файлы", "pinned": "📌 Barcha muhimlar / Все важное",
    }

    if cat not in cat_map:
        await call.answer()
        return

    where, params, ctx = cat_map[cat]
    await call.message.delete()
    await bot.send_message(
        call.message.chat.id,
        f"<b>{cat_names[cat]}</b>\n"
        f"{page * PAGE_SIZE + 1}-{(page + 1) * PAGE_SIZE} ko'rsatilmoqda / показывается:",
        parse_mode="HTML"
    )
    await show_files_page(call.message.chat.id, 0, True, ctx, page, where, params)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin:accounts:"))
async def cb_admin_accounts(call: CallbackQuery):
    user = call.from_user
    if not is_admin_mode(user.id):
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
    
    await call.message.edit_text(
        f"👥 <b>Hisoblar ro'yxati / Список аккаунтов</b> (jami / всего: {total}):",
        parse_mode="HTML",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin:account:"))
async def cb_admin_account_files(call: CallbackQuery):
    user = call.from_user
    if not is_admin_mode(user.id):
        await call.answer("❌ Ruxsat yo'q / Нет доступа!", show_alert=True)
        return
    
    parts = call.data.split(":")
    target_account_id = int(parts[2])
    page = int(parts[3])
    
    async with db_pool.acquire() as conn:
        target = await conn.fetchrow("SELECT username FROM accounts WHERE id = $1", target_account_id)
    
    name = target["username"] if target else "noma'lum / неизвестно"
    await call.message.delete()
    await bot.send_message(
        call.message.chat.id,
        f"👤 <b>@{name}</b> ning fayllari / файлы\n"
        f"{page * PAGE_SIZE + 1}-{(page + 1) * PAGE_SIZE}:",
        parse_mode="HTML"
    )
    await show_files_page(
        call.message.chat.id, 0, True,
        f"admin:account:{target_account_id}", page,
        extra_where="f.account_id = $1", extra_params=(target_account_id,)
    )
    await call.answer()

# ─── Folder callbacks ───────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "folders:0" or c.data.startswith("folders:"))
async def cb_folders(call: CallbackQuery):
    async with db_pool.acquire() as conn:
        folders = await conn.fetch("SELECT name FROM folders ORDER BY name")
    
    await call.message.edit_text(
        "📁 <b>Papkalar / Папки</b>\n\nPapkani tanlang / Выберите папку:",
        parse_mode="HTML",
        reply_markup=folders_kb(folders)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("folder:"))
async def cb_folder(call: CallbackQuery):
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
    
    await call.message.delete()
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

# ─── File actions (pin / unpin / delete / move) ─────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("pin:"))
async def cb_pin(call: CallbackQuery):
    user = call.from_user
    db_id = int(call.data.split(":")[1])
    is_admin_flag = is_admin_mode(user.id)
    account_id = await get_account_id(user.id)
    
    async with db_pool.acquire() as conn:
        file_account = await conn.fetchval("SELECT account_id FROM files WHERE id = $1", db_id)
        if file_account is None:
            await call.answer("Fayl topilmadi! / Файл не найден!", show_alert=True)
            return
        if not is_admin_flag and file_account != account_id:
            await call.answer("❌ Bu sizning faylingiz emas! / Это не ваш файл!", show_alert=True)
            return
        
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET pinned = 1 WHERE id = $1", db_id)
    
    await call.answer(f"📌 {name} muhim belgilandi! / закреплено!", show_alert=False)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, pinned, folder FROM files WHERE id = $1", db_id)
    if row:
        new_kb = file_actions_kb(row["id"], row["pinned"], row["folder"])
        try:
            await call.message.edit_reply_markup(reply_markup=new_kb)
        except Exception:
            pass

@dp.callback_query_handler(lambda c: c.data.startswith("unpin:"))
async def cb_unpin(call: CallbackQuery):
    user = call.from_user
    db_id = int(call.data.split(":")[1])
    is_admin_flag = is_admin_mode(user.id)
    account_id = await get_account_id(user.id)
    
    async with db_pool.acquire() as conn:
        file_account = await conn.fetchval("SELECT account_id FROM files WHERE id = $1", db_id)
        if file_account is None:
            await call.answer("Fayl topilmadi! / Файл не найден!", show_alert=True)
            return
        if not is_admin_flag and file_account != account_id:
            await call.answer("❌ Bu sizning faylingiz emas! / Это не ваш файл!", show_alert=True)
            return
        
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET pinned = 0 WHERE id = $1", db_id)
    
    await call.answer(f"✅ {name} dan pin olindi! / откреплено!", show_alert=False)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, pinned, folder FROM files WHERE id = $1", db_id)
    if row:
        new_kb = file_actions_kb(row["id"], row["pinned"], row["folder"])
        try:
            await call.message.edit_reply_markup(reply_markup=new_kb)
        except Exception:
            pass

@dp.callback_query_handler(lambda c: c.data.startswith("delete:"))
async def cb_delete(call: CallbackQuery):
    user = call.from_user
    db_id = int(call.data.split(":")[1])
    is_admin_flag = is_admin_mode(user.id)
    account_id = await get_account_id(user.id)
    
    async with db_pool.acquire() as conn:
        file_account = await conn.fetchval("SELECT account_id FROM files WHERE id = $1", db_id)
        if file_account is None:
            await call.answer("Fayl topilmadi! / Файл не найден!", show_alert=True)
            return
        if not is_admin_flag and file_account != account_id:
            await call.answer("❌ Bu sizning faylingiz emas! / Это не ваш файл!", show_alert=True)
            return
        
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
    
    await call.message.reply(
        f"🗑️ <b>{name}</b> ni o'chirishni tasdiqlaysizmi?\n"
        f"Подтвердите удаление <b>{name}</b>:",
        parse_mode="HTML",
        reply_markup=confirm_delete_kb(db_id)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("confirmdelete:"))
async def cb_confirm_delete(call: CallbackQuery):
    user = call.from_user
    db_id = int(call.data.split(":")[1])
    is_admin_flag = is_admin_mode(user.id)
    account_id = await get_account_id(user.id)
    
    async with db_pool.acquire() as conn:
        file_account = await conn.fetchval("SELECT account_id FROM files WHERE id = $1", db_id)
        if file_account is None:
            await call.answer("Fayl topilmadi! / Файл не найден!", show_alert=True)
            return
        if not is_admin_flag and file_account != account_id:
            await call.answer("❌ Ruxsat yo'q! / Нет доступа!", show_alert=True)
            return
        
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("DELETE FROM files WHERE id = $1", db_id)
    
    await call.message.edit_text(
        f"🗑️ <b>{name}</b> o'chirildi! / удален!",
        parse_mode="HTML"
    )
    await call.answer("O'chirildi! / Удалено!")

@dp.callback_query_handler(lambda c: c.data.startswith("move:"))
async def cb_move(call: CallbackQuery):
    user = call.from_user
    db_id = int(call.data.split(":")[1])
    is_admin_flag = is_admin_mode(user.id)
    account_id = await get_account_id(user.id)
    
    async with db_pool.acquire() as conn:
        file_account = await conn.fetchval("SELECT account_id FROM files WHERE id = $1", db_id)
        if file_account is None:
            await call.answer("Fayl topilmadi! / Файл не найден!", show_alert=True)
            return
        if not is_admin_flag and file_account != account_id:
            await call.answer("❌ Bu sizning faylingiz emas! / Это не ваш файл!", show_alert=True)
            return
        
        folders = await conn.fetch("SELECT name FROM folders ORDER BY name")
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
    
    await call.message.reply(
        f"📁 <b>{name}</b> ni qaysi papkaga ko'chirish?\n"
        f"В какую папку переместить <b>{name}</b>:",
        parse_mode="HTML",
        reply_markup=move_folders_kb(db_id, folders)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("domove:"))
async def cb_do_move(call: CallbackQuery):
    user = call.from_user
    parts = call.data.split(":", 2)
    db_id = int(parts[1])
    folder_name = parts[2]
    is_admin_flag = is_admin_mode(user.id)
    account_id = await get_account_id(user.id)
    
    async with db_pool.acquire() as conn:
        file_account = await conn.fetchval("SELECT account_id FROM files WHERE id = $1", db_id)
        if file_account is None:
            await call.answer("Fayl topilmadi! / Файл не найден!", show_alert=True)
            return
        if not is_admin_flag and file_account != account_id:
            await call.answer("❌ Ruxsat yo'q! / Нет доступа!", show_alert=True)
            return
        
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
    await call.message.answer(
        "📁 Yangi papka nomini yozing / Введите название новой папки:"
    )
    await NewFolderState.waiting_name.set()
    await call.answer()

@dp.message_handler(state=NewFolderState.waiting_name)
async def process_newfolder(message: Message, state: FSMContext):
    name = message.text.strip()
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
    await call.message.answer(
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
                await message.answer(
                    "🔐 Avval kiring / Сначала войдите",
                    reply_markup=auth_kb()
                )
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
            total = await conn.fetchrow("SELECT COUNT(*), SUM(size) FROM files")
            vid_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE category = 'video'") or 0
            photo_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE category = 'photo'") or 0
            app_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE category IN ('apk', 'ipa')") or 0
            other_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE category = 'other'") or 0
            pin_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE pinned = 1") or 0
            fold_cnt = await conn.fetchval("SELECT COUNT(*) FROM folders") or 0
            acc_cnt = await conn.fetchval("SELECT COUNT(*) FROM accounts") or 0
        else:
            if not account_id:
                await call.answer("🔐 Avval kiring! / Сначала войдите!", show_alert=True)
                return
            total = await conn.fetchrow("SELECT COUNT(*), SUM(size) FROM files WHERE account_id = $1", account_id)
            vid_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE account_id = $1 AND category = 'video'", account_id) or 0
            photo_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE account_id = $1 AND category = 'photo'", account_id) or 0
            app_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE account_id = $1 AND category IN ('apk', 'ipa')", account_id) or 0
            other_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE account_id = $1 AND category = 'other'", account_id) or 0
            pin_cnt = await conn.fetchval("SELECT COUNT(*) FROM files WHERE account_id = $1 AND pinned = 1", account_id) or 0
            fold_cnt = await conn.fetchval("SELECT COUNT(*) FROM folders") or 0
            acc_cnt = 1

    total_count = total[0] or 0
    total_size = total[1] or 0
    mb = round(total_size / 1024 / 1024, 2)
    gb = round(mb / 1024, 3)

    if is_admin_flag:
        text = (
            f"📊 <b>Admin Statistika / Админ статистика</b>\n\n"
            f"👥 Hisoblar / Аккаунты: <b>{acc_cnt}</b>\n"
            f"📄 Jami fayllar / Всего файлов: <b>{total_count}</b>\n"
            f"💾 Umumiy hajm / Общий объем: <b>{mb} MB ({gb} GB)</b>\n\n"
            f"🎬 Videolar / Видео: <b>{vid_cnt}</b>\n"
            f"🖼️ Rasmlar / Фото: <b>{photo_cnt}</b>\n"
            f"🤖 APK/IPA: <b>{app_cnt}</b>\n"
            f"📄 Boshqalar / Другое: <b>{other_cnt}</b>\n\n"
            f"📌 Muhim fayllar / Важное: <b>{pin_cnt}</b>\n"
            f"📁 Papkalar / Папки: <b>{fold_cnt + 1}</b>"
        )
    else:
        text = (
            f"📊 <b>Statistika / Статистика</b>\n\n"
            f"📄 Jami fayllar / Всего файлов: <b>{total_count}</b>\n"
            f"💾 Umumiy hajm / Общий объем: <b>{mb} MB ({gb} GB)</b>\n\n"
            f"🎬 Videolar / Видео: <b>{vid_cnt}</b>\n"
            f"🖼️ Rasmlar / Фото: <b>{photo_cnt}</b>\n"
            f"🤖 APK/IPA: <b>{app_cnt}</b>\n"
            f"📄 Boshqalar / Другое: <b>{other_cnt}</b>\n\n"
            f"📌 Muhim fayllar / Важное: <b>{pin_cnt}</b>\n"
            f"📁 Papkalar / Папки: <b>{fold_cnt + 1}</b>"
        )
    
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
        )
    )
    await call.answer()

# ─── /menu command ────────────────────────────────────────────────────────────

@dp.message_handler(commands=["menu"])
async def cmd_menu(message: Message):
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
    print(f"✅ Bot ishga tushdi! / Бот запущен! Port: {port}")

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
