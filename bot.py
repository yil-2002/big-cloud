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

BOT_TOKEN     = os.getenv("BOT_TOKEN")
ADMIN_PASSWORD= os.getenv("ADMIN_PASSWORD")
ADMIN_ID      = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL  = os.getenv("DATABASE_URL")

if not BOT_TOKEN:     raise ValueError("BOT_TOKEN required")
if not ADMIN_PASSWORD:raise ValueError("ADMIN_PASSWORD required")
if not DATABASE_URL:  raise ValueError("DATABASE_URL required")

bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(bot, storage=storage)

db_pool   = None
PAGE_SIZE = 5

# ─── Cache ────────────────────────────────────────────────────────────────────
# Render Free 512MB RAM — cache DB yukini 70-90% kamaytiradi

account_cache  = TTLCache(maxsize=500, ttl=300)   # tg_id → account_id  (5 min)
username_cache = TTLCache(maxsize=500, ttl=300)   # account_id → username (5 min)
folders_cache  = TTLCache(maxsize=1,   ttl=60)    # papkalar ro'yxati    (1 min)
stats_cache    = TTLCache(maxsize=200, ttl=30)    # statistika           (30 sec)

# ─── Rate limit ───────────────────────────────────────────────────────────────

user_last_action  = defaultdict(float)
RATE_LIMIT_SECONDS = 1.5

def is_rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - user_last_action[user_id] < RATE_LIMIT_SECONDS:
        return True
    user_last_action[user_id] = now
    return False

# ─── Admin sessiya ────────────────────────────────────────────────────────────

admin_sessions     = {}   # {user_id: login_timestamp}
ADMIN_SESSION_HOURS = 24

# ─── States ───────────────────────────────────────────────────────────────────

class AuthState(StatesGroup):
    waiting_reg_username  = State()
    waiting_reg_password  = State()
    waiting_login_username= State()
    waiting_login_password= State()

class AdminAuthState(StatesGroup):
    waiting_password = State()

class SearchState(StatesGroup):
    waiting_query = State()

class NewFolderState(StatesGroup):
    waiting_name = State()

# ─── Password ─────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt    = secrets.token_hex(16)
    pwdhash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
    return salt + pwdhash.hex()

def verify_password(stored: str, provided: str) -> bool:
    salt    = stored[:32]
    pwdhash = hashlib.pbkdf2_hmac('sha256', provided.encode(), salt.encode(), 100000)
    return stored[32:] == pwdhash.hex()

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\|?*]', '_', name)
    return name[:255]

# ─── Database ─────────────────────────────────────────────────────────────────

async def create_db():
    global db_pool
    # 🚀 Render Free uchun optimal: min=1, max=3 (~30MB RAM tejash)
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    async with db_pool.acquire() as conn:

        # Jadvallar
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id            SERIAL PRIMARY KEY,
                username      TEXT UNIQUE,
                password_hash TEXT,
                created_at    BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_bindings (
                id              SERIAL PRIMARY KEY,
                account_id      INTEGER REFERENCES accounts(id),
                telegram_user_id BIGINT UNIQUE,
                bound_at        BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id         SERIAL PRIMARY KEY,
                file_id    TEXT,
                file_name  TEXT,
                category   TEXT,
                size       BIGINT,
                date       BIGINT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id   SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                date BIGINT
            )
        """)

        # Eski ustunlarni qo'shish (migration)
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS account_id INTEGER")
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS folder TEXT DEFAULT 'umumiy'")
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS pinned INTEGER DEFAULT 0")

        # 🛠️ ASOSIY TUZATISH: Eski TEXT ustunlarni BIGINT ga o'tkazish
        # Sabab: avvalgi versiyalarda date/created_at/bound_at TEXT edi,
        # hozir int(time.time()) — BIGINT yuborilmoqda.
        await conn.execute("""
            DO $$
            BEGIN
                -- folders.date
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'folders' AND column_name = 'date' 
                    AND data_type IN ('text', 'character varying')
                ) THEN
                    ALTER TABLE folders ALTER COLUMN date TYPE BIGINT USING date::bigint;
                END IF;

                -- files.date
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'files' AND column_name = 'date' 
                    AND data_type IN ('text', 'character varying')
                ) THEN
                    ALTER TABLE files ALTER COLUMN date TYPE BIGINT USING date::bigint;
                END IF;

                -- accounts.created_at
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'accounts' AND column_name = 'created_at' 
                    AND data_type IN ('text', 'character varying')
                ) THEN
                    ALTER TABLE accounts ALTER COLUMN created_at TYPE BIGINT USING created_at::bigint;
                END IF;

                -- telegram_bindings.bound_at
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'telegram_bindings' AND column_name = 'bound_at' 
                    AND data_type IN ('text', 'character varying')
                ) THEN
                    ALTER TABLE telegram_bindings ALTER COLUMN bound_at TYPE BIGINT USING bound_at::bigint;
                END IF;
            END $$;
        """)

        # 🚀 Indexlar — so'rovlar 10x tezlashadi
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_account  ON files(account_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_category ON files(category)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_pinned   ON files(pinned)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder   ON files(folder)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_date     ON files(date DESC)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bindings_tg    ON telegram_bindings(telegram_user_id)")

        await conn.execute(
            "INSERT INTO folders (name, date) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
            "umumiy", int(time.time())
        )
    logger.info("✅ Database ready")

# ─── DB helpers (cache bilan) ─────────────────────────────────────────────────

async def get_account_id(telegram_user_id: int):
    """Cache → DB (5 daqiqa cache)"""
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
    account_cache.pop(telegram_user_id, None)

async def get_account_username(account_id: int):
    """Cache → DB (5 daqiqa cache)"""
    if account_id in username_cache:
        return username_cache[account_id]
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT username FROM accounts WHERE id = $1", account_id)
    username_cache[account_id] = result
    return result

def invalidate_username_cache(account_id: int):
    username_cache.pop(account_id, None)

async def get_folders():
    """Cache → DB (1 daqiqa cache) — papkalar ko'p o'zgarmaydi"""
    if "all" in folders_cache:
        return folders_cache["all"]
    async with db_pool.acquire() as conn:
        result = list(await conn.fetch("SELECT name FROM folders ORDER BY name"))
    folders_cache["all"] = result
    return result

def invalidate_folders_cache():
    folders_cache.clear()

async def save_file(account_id, file_id, file_name, category, size):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO files (account_id, file_id, file_name, category, size, date, folder, pinned) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            account_id, file_id, file_name, category, size,
            int(time.time()),  # 🚀 UNIX timestamp — TEXT o'rniga 4x kichik
            "umumiy", 0
        )

def format_date(ts) -> str:
    """UNIX timestamp → ko'rinadigan sana"""
    try:
        if isinstance(ts, str):
            return ts  # eski TEXT format
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)

SELECT_SQL = (
    "SELECT f.id, f.file_id, f.file_name, f.category, f.size, f.date, "
    "f.folder, f.pinned, f.account_id, a.username as owner_name "
    "FROM files f LEFT JOIN accounts a ON f.account_id = a.id"
)

def shift_placeholders(sql: str, shift: int) -> str:
    if not sql or shift == 0:
        return sql
    return re.sub(r'\$(\d+)', lambda m: f"${int(m.group(1)) + shift}", sql)

async def get_files_with_total(
    account_id: int, is_admin: bool,
    extra_where: str = "", extra_params: tuple = (),
    limit: int = PAGE_SIZE, offset: int = 0
):
    """
    🚀 COUNT + SELECT — bitta so'rovda (COUNT(*) OVER())
    LIMIT va OFFSET ham parameterizatsiya qilingan (xavfsizlik)
    """
    async with db_pool.acquire() as conn:
        params = []
        where_parts = []
        param_idx = 1

        if not is_admin:
            where_parts.append(f"sub.account_id = ${param_idx}")
            params.append(account_id)
            param_idx += 1

        if extra_where:
            shifted = shift_placeholders(extra_where, param_idx - 1)
            where_parts.append(shifted)
            params.extend(extra_params)
            param_idx += len(extra_params)

        where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        params.extend([limit, offset])
        limit_idx = param_idx
        offset_idx = param_idx + 1

        q = f"""
            SELECT *, COUNT(*) OVER() AS total_count
            FROM ({SELECT_SQL}) sub
            {where_clause}
            ORDER BY sub.date DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
        """
        rows = await conn.fetch(q, *params)

        total = rows[0]["total_count"] if rows else 0
        return rows, total

async def get_stats(account_id: int, is_admin: bool):
    """
    🚀 Bitta so'rovda barcha statistika (7 ta alohida so'rov o'rniga)
    Cache bilan — 30 soniya saqlanadi
    """
    cache_key = f"admin" if is_admin else f"user_{account_id}"
    if cache_key in stats_cache:
        return stats_cache[cache_key]

    async with db_pool.acquire() as conn:
        if is_admin:
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(*)                                           AS total,
                    COALESCE(SUM(size), 0)                            AS total_size,
                    COUNT(*) FILTER (WHERE category = 'video')        AS vid_cnt,
                    COUNT(*) FILTER (WHERE category = 'photo')        AS photo_cnt,
                    COUNT(*) FILTER (WHERE category IN ('apk','ipa')) AS app_cnt,
                    COUNT(*) FILTER (WHERE category = 'other')        AS other_cnt,
                    COUNT(*) FILTER (WHERE pinned = 1)                AS pin_cnt
                FROM files
            """)
            fold_cnt = await conn.fetchval("SELECT COUNT(*) FROM folders") or 0
            acc_cnt  = await conn.fetchval("SELECT COUNT(*) FROM accounts") or 0
        else:
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(*)                                           AS total,
                    COALESCE(SUM(size), 0)                            AS total_size,
                    COUNT(*) FILTER (WHERE category = 'video')        AS vid_cnt,
                    COUNT(*) FILTER (WHERE category = 'photo')        AS photo_cnt,
                    COUNT(*) FILTER (WHERE category IN ('apk','ipa')) AS app_cnt,
                    COUNT(*) FILTER (WHERE category = 'other')        AS other_cnt,
                    COUNT(*) FILTER (WHERE pinned = 1)                AS pin_cnt
                FROM files WHERE account_id = $1
            """, account_id)
            fold_cnt = await conn.fetchval("SELECT COUNT(*) FROM folders") or 0
            acc_cnt  = 1

    result = dict(stats), fold_cnt, acc_cnt
    stats_cache[cache_key] = result
    return result

# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_admin_mode(telegram_user_id: int) -> bool:
    if telegram_user_id == ADMIN_ID:
        return True
    if telegram_user_id in admin_sessions:
        if time.time() - admin_sessions[telegram_user_id] < ADMIN_SESSION_HOURS * 3600:
            return True
        del admin_sessions[telegram_user_id]
        logger.info(f"Admin session expired: {telegram_user_id}")
    return False

def get_icon(cat: str) -> str:
    return {"video": "🎬", "photo": "🖼️", "apk": "🤖", "ipa": "🍎"}.get(cat, "📄")

def get_category(ext: str) -> str:
    ext = ext.lower()
    if ext == "apk": return "apk"
    if ext == "ipa": return "ipa"
    if ext in ["mp4","mov","avi","mkv","webm"]:         return "video"
    if ext in ["jpg","jpeg","png","gif","webp","bmp"]:  return "photo"
    return "other"

async def safe_edit(call: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        try:
            await call.message.delete()
        except Exception:
            pass
        await bot.send_message(call.message.chat.id, text, reply_markup=reply_markup, parse_mode=parse_mode)

async def safe_edit_or_caption(call: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
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

async def check_file_access(call: CallbackQuery, db_id: int):
    account_id   = await get_account_id(call.from_user.id)
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

# ─── Keyboards ────────────────────────────────────────────────────────────────

def auth_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔐 Kirish / Вход",               callback_data="auth:login"),
        InlineKeyboardButton("📝 Ro'yxatdan o'tish / Регистрация", callback_data="auth:register")
    )
    return kb

def user_menu_kb(show_admin: bool = False):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🎬 Videolar / Видео",    callback_data="cat:video:0"),
        InlineKeyboardButton("🖼️ Rasmlar / Фото",     callback_data="cat:photo:0"),
        InlineKeyboardButton("🤖 APK/IPA",             callback_data="cat:apps:0"),
        InlineKeyboardButton("📄 Boshqalar / Другое", callback_data="cat:other:0"),
    )
    kb.add(
        InlineKeyboardButton("📋 Barchasi / Все",     callback_data="cat:all:0"),
        InlineKeyboardButton("📌 Muhimlar / Важное",  callback_data="cat:pinned:0"),
    )
    kb.add(
        InlineKeyboardButton("📁 Papkalar / Папки",   callback_data="folders:0"),
        InlineKeyboardButton("🔍 Qidirish / Поиск",  callback_data="search"),
    )
    kb.add(
        InlineKeyboardButton("📊 Statistika",         callback_data="stats"),
        InlineKeyboardButton("➕ Yangi papka",        callback_data="newfolder"),
    )
    kb.add(InlineKeyboardButton("🔒 Chiqish / Выход", callback_data="logout"))
    if show_admin:
        kb.add(InlineKeyboardButton("🔐 Admin paneli / Админ панель", callback_data="admin:menu"))
    return kb

def admin_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("☁️ Barcha fayllar",  callback_data="admin:cat:all:0"),
        InlineKeyboardButton("👥 Hisoblar",         callback_data="admin:accounts:0"),
    )
    kb.add(
        InlineKeyboardButton("🎬 Videolar",         callback_data="admin:cat:video:0"),
        InlineKeyboardButton("🖼️ Rasmlar",          callback_data="admin:cat:photo:0"),
        InlineKeyboardButton("🤖 APK/IPA",          callback_data="admin:cat:apps:0"),
        InlineKeyboardButton("📄 Boshqalar",        callback_data="admin:cat:other:0"),
    )
    kb.add(
        InlineKeyboardButton("📌 Muhimlar",         callback_data="admin:cat:pinned:0"),
        InlineKeyboardButton("📊 Statistika",       callback_data="stats"),
    )
    kb.add(InlineKeyboardButton("🔒 Admin rejimdan chiqish", callback_data="admin:logout"))
    return kb

def file_actions_kb(file_id_db: int, pinned: int, folder: str):
    kb = InlineKeyboardMarkup(row_width=3)
    pin_btn = (
        InlineKeyboardButton("📌 Pin olish", callback_data=f"unpin:{file_id_db}")
        if pinned else
        InlineKeyboardButton("📌 Pin",       callback_data=f"pin:{file_id_db}")
    )
    kb.add(
        pin_btn,
        InlineKeyboardButton("📁 Ko'chirish", callback_data=f"move:{file_id_db}"),
        InlineKeyboardButton("🗑️ O'chirish",  callback_data=f"delete:{file_id_db}"),
    )
    return kb

def after_upload_kb(cat: str):
    cat_map = {
        "video": ("cat:video:0", "🎬 Videolar"),
        "photo": ("cat:photo:0", "🖼️ Rasmlar"),
        "apk":   ("cat:apps:0",  "🤖 APK/IPA"),
        "ipa":   ("cat:apps:0",  "🤖 APK/IPA"),
        "other": ("cat:other:0", "📄 Boshqalar"),
    }
    cb, label = cat_map.get(cat, ("cat:all:0", "📋 Barchasi"))
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(label,               callback_data=cb),
        InlineKeyboardButton("🏠 Menyu / Меню",  callback_data="menu"),
    )
    return kb

def folders_kb(folders_list):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📂 umumiy / общая", callback_data="folder:umumiy:0"))
    for f in folders_list:
        if f["name"] != "umumiy":
            kb.add(InlineKeyboardButton(f"📂 {f['name']}", callback_data=f"folder:{f['name']}:0"))
    kb.add(InlineKeyboardButton("➕ Yangi papka / Новая папка", callback_data="newfolder"))
    kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))
    return kb

def confirm_delete_kb(file_id_db: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Ha, o'chir / Да", callback_data=f"confirmdelete:{file_id_db}"),
        InlineKeyboardButton("❌ Yo'q / Нет",      callback_data="menu"),
    )
    return kb

def move_folders_kb(file_id_db: int, folders_list):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("📂 umumiy", callback_data=f"domove:{file_id_db}:umumiy"))
    for f in folders_list:
        if f["name"] != "umumiy":
            kb.add(InlineKeyboardButton(f"📂 {f['name']}", callback_data=f"domove:{file_id_db}:{f['name']}"))
    kb.add(InlineKeyboardButton("🔙 Orqaga / Назад", callback_data="menu"))
    return kb

# ─── Start / Auth ─────────────────────────────────────────────────────────────

@dp.message_handler(commands=["start"])
async def cmd_start(message: Message, state: FSMContext):
    await state.finish()
    user       = message.from_user
    account_id = await get_account_id(user.id)

    if account_id:
        username      = await get_account_username(account_id)
        is_admin_flag = is_admin_mode(user.id)
        await message.answer(
            f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>
"
            f"<b>Личное Облачное Хранилище</b>

"
            f"👤 Hisob / Аккаунт: <b>@{username}</b>
"
            f"📤 Fayl yuboring — saqlanadi!

"
            "Menyudan tanlang / Выберите из меню:",
            parse_mode="HTML",
            reply_markup=user_menu_kb(show_admin=is_admin_flag)
        )
    else:
        await message.answer(
            "☁️ <b>Shaxsiy Bulut Xotira</b>
"
            "<b>Личное Облачное Хранилище</b>

"
            "🔐 Hisobga kirish yoki yangi hisob ochish
"
            "Войти в аккаунт или создать новый

"
            "⚠️ <b>Muhim / Важно:</b>
"
            "<i>Login va parolingizni eslab qoling!</i>
"
            "<i>Запомните логин и пароль!</i>",
            parse_mode="HTML",
            reply_markup=auth_kb()
        )

@dp.callback_query_handler(lambda c: c.data == "auth:register")
async def cb_register(call: CallbackQuery):
    await call.message.edit_text(
        "📝 <b>Yangi hisob ochish / Регистрация</b>

"
        "Foydalanuvchi nomini kiriting / Введите логин:",
        parse_mode="HTML"
    )
    await AuthState.waiting_reg_username.set()
    await call.answer()

@dp.message_handler(state=AuthState.waiting_reg_username)
async def process_reg_username(message: Message, state: FSMContext):
    username = message.text.strip()
    if len(username) < 3:
        await message.answer("❌ Kamida 3 ta belgi / Минимум 3 символа:")
        return
    if not re.match(r'^[a-zA-Z0-9_]+$', username):
        await message.answer("❌ Faqat lotin, raqam va _ / Только латиница, цифры, _:")
        return
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM accounts WHERE username = $1", username)
    if exists:
        await message.answer("❌ Bu nom band / Логин занят. Boshqa / Другой:")
        return
    await state.update_data(username=username)
    await message.answer("🔑 Parolni kiriting (kamida 4 belgi) / Пароль (минимум 4 символа):")
    await AuthState.waiting_reg_password.set()

@dp.message_handler(state=AuthState.waiting_reg_password)
async def process_reg_password(message: Message, state: FSMContext):
    password = message.text.strip()
    if len(password) < 4:
        await message.answer("❌ Parol juda qisqa / Пароль слишком короткий:")
        return
    data          = await state.get_data()
    username      = data["username"]
    password_hash = hash_password(password)
    now           = int(time.time())
    async with db_pool.acquire() as conn:
        account_id = await conn.fetchval(
            "INSERT INTO accounts (username, password_hash, created_at) VALUES ($1,$2,$3) RETURNING id",
            username, password_hash, now
        )
        await conn.execute(
            "INSERT INTO telegram_bindings (account_id, telegram_user_id, bound_at) VALUES ($1,$2,$3)",
            account_id, message.from_user.id, now
        )
    invalidate_account_cache(message.from_user.id)
    await state.finish()
    logger.info(f"Registered: @{username} tg={message.from_user.id}")
    await message.answer(
        f"✅ <b>Hisob yaratildi! / Аккаунт создан!</b>

👤 @{username}

"
        "Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
    )

@dp.callback_query_handler(lambda c: c.data == "auth:login")
async def cb_login(call: CallbackQuery):
    await call.message.edit_text(
        "🔐 <b>Hisobga kirish / Вход</b>

Foydalanuvchi nomini kiriting / Введите логин:",
        parse_mode="HTML"
    )
    await AuthState.waiting_login_username.set()
    await call.answer()

@dp.message_handler(state=AuthState.waiting_login_username)
async def process_login_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await message.answer("🔑 Parolni kiriting / Введите пароль:")
    await AuthState.waiting_login_password.set()

@dp.message_handler(state=AuthState.waiting_login_password)
async def process_login_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data     = await state.get_data()
    username = data["username"]
    async with db_pool.acquire() as conn:
        account = await conn.fetchrow(
            "SELECT id, password_hash FROM accounts WHERE username = $1", username
        )
    if not account or not verify_password(account["password_hash"], password):
        await message.answer(
            "❌ Login yoki parol noto'g'ri / Неверный логин или пароль.
/start"
        )
        await state.finish()
        return
    now = int(time.time())
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO telegram_bindings (account_id, telegram_user_id, bound_at) VALUES ($1,$2,$3) "
            "ON CONFLICT (telegram_user_id) DO UPDATE SET account_id=$1, bound_at=$3",
            account["id"], message.from_user.id, now
        )
    invalidate_account_cache(message.from_user.id)
    await state.finish()
    logger.info(f"Login: @{username} tg={message.from_user.id}")
    await message.answer(
        f"✅ <b>Xush kelibsiz! / Добро пожаловать!</b>

👤 @{username}

"
        "Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
    )

@dp.callback_query_handler(lambda c: c.data == "logout")
async def cb_logout(call: CallbackQuery):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM telegram_bindings WHERE telegram_user_id = $1", call.from_user.id
        )
    invalidate_account_cache(call.from_user.id)
    logger.info(f"Logout: tg={call.from_user.id}")
    await call.answer("🔒 Hisobdan chiqildi / Выход выполнен")
    await call.message.edit_text(
        "☁️ <b>Shaxsiy Bulut Xotira</b>
<b>Личное Облачное Хранилище</b>

"
        "🔐 Hisobga kirish yoki yangi hisob ochish

"
        "⚠️ <i>Login va parolingizni eslab qoling! / Запомните логин и пароль!</i>",
        parse_mode="HTML", reply_markup=auth_kb()
    )

# ─── Admin ────────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["admin"])
async def cmd_admin(message: Message, state: FSMContext):
    await state.finish()
    if is_admin_mode(message.from_user.id):
        await message.answer(
            "🔰 <b>Admin panel / Админ панель</b>

Barcha hisoblar va fayllar ko'rinadi.",
            parse_mode="HTML", reply_markup=admin_menu_kb()
        )
    else:
        await message.answer("🔐 <b>Admin parolini kiriting:</b>", parse_mode="HTML")
        await AdminAuthState.waiting_password.set()

@dp.message_handler(state=AdminAuthState.waiting_password)
async def process_admin_password(message: Message, state: FSMContext):
    if message.text == ADMIN_PASSWORD:
        admin_sessions[message.from_user.id] = time.time()
        await state.finish()
        logger.info(f"Admin login: tg={message.from_user.id}")
        await message.answer(
            "✅ <b>Admin rejimi faollashdi!</b>",
            parse_mode="HTML", reply_markup=admin_menu_kb()
        )
    else:
        await message.answer("❌ Noto'g'ri parol / Неверный пароль. Qayta:")

# ─── Menu ─────────────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "menu")
async def cb_menu(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq! / Медленнее!")
        return
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await safe_edit(
            call,
            "☁️ <b>Shaxsiy Bulut Xotira</b>

🔐 Hisobga kirish / Войти:",
            reply_markup=auth_kb()
        )
        await call.answer()
        return
    username      = await get_account_username(account_id)
    is_admin_flag = is_admin_mode(call.from_user.id)
    await safe_edit(
        call,
        f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>

"
        f"👤 Hisob / Аккаунт: <b>@{username}</b>
"
        f"Menyudan tanlang / Выберите из меню:",
        reply_markup=user_menu_kb(show_admin=is_admin_flag)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:menu")
async def cb_admin_menu(call: CallbackQuery):
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await safe_edit(call, "🔰 <b>Admin panel</b>", reply_markup=admin_menu_kb())
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:logout")
async def cb_admin_logout(call: CallbackQuery):
    admin_sessions.pop(call.from_user.id, None)
    logger.info(f"Admin logout: tg={call.from_user.id}")
    await call.answer("🔒 Admin rejimi yopildi")
    await cb_menu(call)

@dp.callback_query_handler(lambda c: c.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()

# ─── File upload ──────────────────────────────────────────────────────────────

async def _check_auth(message: Message):
    account_id = await get_account_id(message.from_user.id)
    if not account_id:
        await message.answer(
            "🔐 Avval /start orqali kiring / Сначала войдите через /start",
            reply_markup=auth_kb()
        )
    return account_id

@dp.message_handler(content_types=types.ContentType.VIDEO)
async def handle_video(message: Message):
    account_id = await _check_auth(message)
    if not account_id: return
    v    = message.video
    name = sanitize_filename(v.file_name or f"video_{v.file_id[:8]}.mp4")
    await save_file(account_id, v.file_id, name, "video", v.file_size or 0)
    mb   = round((v.file_size or 0) / 1024 / 1024, 2)
    logger.info(f"Video saved: {name} ({mb}MB) acc={account_id}")
    await message.answer(
        f"🎬 <b>Saqlandi!</b>
📄 {name}
💾 {mb} MB",
        parse_mode="HTML", reply_markup=after_upload_kb("video")
    )

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def handle_photo(message: Message):
    account_id = await _check_auth(message)
    if not account_id: return
    p    = message.photo[-1]
    name = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    await save_file(account_id, p.file_id, name, "photo", p.file_size or 0)
    mb   = round((p.file_size or 0) / 1024 / 1024, 2)
    await message.answer(
        f"🖼️ <b>Saqlandi!</b>
📄 {name}
💾 {mb} MB",
        parse_mode="HTML", reply_markup=after_upload_kb("photo")
    )

@dp.message_handler(content_types=types.ContentType.DOCUMENT)
async def handle_document(message: Message):
    account_id = await _check_auth(message)
    if not account_id: return
    d        = message.document
    name     = sanitize_filename(d.file_name or "nomsiz_fayl")
    ext      = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    category = get_category(ext)
    await save_file(account_id, d.file_id, name, category, d.file_size or 0)
    mb   = round((d.file_size or 0) / 1024 / 1024, 2)
    icon = get_icon(category)
    logger.info(f"Doc saved: {name} ({category},{mb}MB) acc={account_id}")
    await message.answer(
        f"{icon} <b>Saqlandi!</b>
📄 {name}
💾 {mb} MB",
        parse_mode="HTML", reply_markup=after_upload_kb(category)
    )

@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_unknown(message: Message):
    account_id = await get_account_id(message.from_user.id)
    if account_id:
        await message.answer(
            "❓ <b>Bu fayl turi saqlanmaydi.</b>
"
            "Video, rasm, APK yoki hujjat yuboring!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu", callback_data="menu")
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

async def show_files_page(chat_id, account_id, is_admin, ctx, page=0,
                           extra_where="", extra_params=()):
    offset        = page * PAGE_SIZE
    rows, total   = await get_files_with_total(
        account_id, is_admin, extra_where, extra_params, PAGE_SIZE, offset
    )

    if not rows:
        await bot.send_message(
            chat_id,
            "😔 Hozircha fayl yo'q. / Пока файлов нет.
📤 Fayl yuboring!",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
            )
        )
        return

    for i, row in enumerate(rows):
        db_id  = row["id"]
        cat    = row["category"]
        size   = row["size"] or 0
        pinned = row["pinned"]
        folder = row["folder"]

        mb        = round(size / 1024 / 1024, 2)
        pin_icon  = "📌 " if pinned else ""
        date_str  = format_date(row["date"])

        owner_text = ""
        if is_admin and row["account_id"] != account_id:
            owner_name_str = row["owner_name"] or "noma'lum"
            owner_text = f"
👤 <b>@{owner_name_str}</b>"

        caption = (
            f"{pin_icon}{get_icon(cat)} <b>{row['file_name']}</b>
"
            f"💾 {mb} MB  |  📅 {date_str}
"
            f"📁 {folder}{owner_text}"
        )

        kb = file_actions_kb(db_id, pinned, folder)

        # Sahifa tugmasini faqat oxirgi faylga qo'shish
        if i == len(rows) - 1:
            total_pages = max(1, (total - 1) // PAGE_SIZE + 1)
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️", callback_data=f"{ctx}:{page-1}"))
            nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
            if (page + 1) * PAGE_SIZE < total:
                nav.append(InlineKeyboardButton("➡️", callback_data=f"{ctx}:{page+1}"))
            if nav:
                kb.add(*nav)
            kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))

        await send_file_safe(chat_id, row["file_id"], cat, caption, reply_markup=kb)

# ─── Category map ─────────────────────────────────────────────────────────────

CAT_MAP = {
    "video":  ("sub.category = $1",          ("video",)),
    "photo":  ("sub.category = $1",          ("photo",)),
    "apps":   ("sub.category IN ($1, $2)",   ("apk", "ipa")),
    "other":  ("sub.category = $1",          ("other",)),
    "all":    ("",                            ()),
    "pinned": ("sub.pinned = $1",            (1,)),
}

CAT_NAMES_USER = {
    "video":"🎬 Videolar","photo":"🖼️ Rasmlar",
    "apps":"🤖 APK/IPA","other":"📄 Boshqalar",
    "all":"📋 Barcha fayllar","pinned":"📌 Muhim fayllar",
}
CAT_NAMES_ADMIN = {
    "video":"🎬 Barcha videolar","photo":"🖼️ Barcha rasmlar",
    "apps":"🤖 Barcha APK/IPA","other":"📄 Barcha boshqalar",
    "all":"☁️ Barcha fayllar","pinned":"📌 Barcha muhimlar",
}

# ─── Category callbacks ───────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("cat:"))
async def cb_category(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq!")
        return
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    _, cat, page = call.data.split(":")
    page = int(page)
    if cat not in CAT_MAP:
        await call.answer(); return
    where, params = CAT_MAP[cat]
    try: await call.message.delete()
    except Exception: pass
    await bot.send_message(call.message.chat.id, f"<b>{CAT_NAMES_USER[cat]}</b>", parse_mode="HTML")
    await show_files_page(call.message.chat.id, account_id, False, f"cat:{cat}", page, where, params)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin:cat:"))
async def cb_admin_category(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq!")
        return
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    _, _, cat, page = call.data.split(":")
    page = int(page)
    if cat not in CAT_MAP:
        await call.answer(); return
    where, params = CAT_MAP[cat]
    try: await call.message.delete()
    except Exception: pass
    await bot.send_message(call.message.chat.id, f"<b>{CAT_NAMES_ADMIN[cat]}</b>", parse_mode="HTML")
    await show_files_page(call.message.chat.id, 0, True, f"admin:cat:{cat}", page, where, params)
    await call.answer()

# ─── Admin accounts ───────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("admin:accounts:"))
async def cb_admin_accounts(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq!")
        return
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    page = int(call.data.split(":")[2])
    async with db_pool.acquire() as conn:
        accounts = await conn.fetch(
            "SELECT id, username FROM accounts ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            PAGE_SIZE, page * PAGE_SIZE
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM accounts") or 0
    if not accounts:
        await call.answer("Hisoblar yo'q!"); return

    kb = InlineKeyboardMarkup(row_width=1)
    for acc in accounts:
        kb.add(InlineKeyboardButton(f"👤 @{acc['username']}", callback_data=f"admin:account:{acc['id']}:0"))
    total_pages = max(1, (total - 1) // PAGE_SIZE + 1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admin:accounts:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admin:accounts:{page+1}"))
    if nav: kb.add(*nav)
    kb.add(InlineKeyboardButton("🏠 Menyu", callback_data="menu"))
    await safe_edit(call, f"👥 <b>Hisoblar</b> (jami: {total}):", reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("admin:account:"))
async def cb_admin_account_files(call: CallbackQuery):
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    parts             = call.data.split(":")
    target_account_id = int(parts[2])
    page              = int(parts[3])
    async with db_pool.acquire() as conn:
        target = await conn.fetchrow("SELECT username FROM accounts WHERE id = $1", target_account_id)
    name = target["username"] if target else "noma'lum"
    try: await call.message.delete()
    except Exception: pass
    await bot.send_message(call.message.chat.id, f"👤 <b>@{name}</b> fayllari:", parse_mode="HTML")
    await show_files_page(
        call.message.chat.id, 0, True,
        f"admin:account:{target_account_id}", page,
        extra_where="sub.account_id = $1", extra_params=(target_account_id,)
    )
    await call.answer()

# ─── Folders ──────────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("folders:"))
async def cb_folders(call: CallbackQuery):
    folders = await get_folders()  # cache bilan
    await safe_edit(
        call,
        "📁 <b>Papkalar / Папки</b>

Papkani tanlang / Выберите папку:",
        reply_markup=folders_kb(folders)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("folder:"))
async def cb_folder(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq!")
        return
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    is_admin_flag = is_admin_mode(call.from_user.id)
    parts         = call.data.split(":")
    folder_name   = parts[1]
    page          = int(parts[2])
    try: await call.message.delete()
    except Exception: pass
    await bot.send_message(call.message.chat.id, f"📂 <b>{folder_name}</b>:", parse_mode="HTML")
    await show_files_page(
        call.message.chat.id, account_id, is_admin_flag,
        f"folder:{folder_name}", page,
        extra_where="sub.folder = $1", extra_params=(folder_name,)
    )
    await call.answer()

# ─── File actions ─────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("pin:"))
async def cb_pin(call: CallbackQuery):
    db_id                        = int(call.data.split(":")[1])
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET pinned = 1 WHERE id = $1", db_id)
        row  = await conn.fetchrow("SELECT id, pinned, folder FROM files WHERE id = $1", db_id)
    await call.answer(f"📌 {name} muhim belgilandi!")
    if row:
        try: await call.message.edit_reply_markup(reply_markup=file_actions_kb(row["id"], row["pinned"], row["folder"]))
        except Exception: pass

@dp.callback_query_handler(lambda c: c.data.startswith("unpin:"))
async def cb_unpin(call: CallbackQuery):
    db_id                        = int(call.data.split(":")[1])
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET pinned = 0 WHERE id = $1", db_id)
        row  = await conn.fetchrow("SELECT id, pinned, folder FROM files WHERE id = $1", db_id)
    await call.answer(f"✅ {name} dan pin olindi!")
    if row:
        try: await call.message.edit_reply_markup(reply_markup=file_actions_kb(row["id"], row["pinned"], row["folder"]))
        except Exception: pass

@dp.callback_query_handler(lambda c: c.data.startswith("delete:"))
async def cb_delete(call: CallbackQuery):
    db_id                        = int(call.data.split(":")[1])
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
    await safe_edit_or_caption(
        call,
        f"🗑️ <b>{name}</b> ni o'chirishni tasdiqlaysizmi?
Подтвердите удаление:",
        reply_markup=confirm_delete_kb(db_id)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("confirmdelete:"))
async def cb_confirm_delete(call: CallbackQuery):
    db_id                        = int(call.data.split(":")[1])
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("DELETE FROM files WHERE id = $1", db_id)
    logger.info(f"Deleted: id={db_id} ({name}) tg={call.from_user.id}")
    await call.message.edit_text(f"🗑️ <b>{name}</b> o'chirildi! / удален!", parse_mode="HTML")
    await call.answer("O'chirildi!")

@dp.callback_query_handler(lambda c: c.data.startswith("move:"))
async def cb_move(call: CallbackQuery):
    db_id                        = int(call.data.split(":")[1])
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    folders = await get_folders()  # cache bilan
    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
    await safe_edit_or_caption(
        call,
        f"📁 <b>{name}</b> ni qaysi papkaga ko'chirish?
В какую папку переместить:",
        reply_markup=move_folders_kb(db_id, folders)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("domove:"))
async def cb_do_move(call: CallbackQuery):
    parts                        = call.data.split(":", 2)
    db_id                        = int(parts[1])
    folder_name                  = parts[2]
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
        await conn.execute("UPDATE files SET folder = $1 WHERE id = $2", folder_name, db_id)
    await call.message.edit_text(f"✅ <b>{name}</b> → 📁 {folder_name}", parse_mode="HTML")
    await call.answer()

# ─── New folder ───────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "newfolder")
async def cb_newfolder(call: CallbackQuery):
    await safe_edit(call, "📁 Yangi papka nomini yozing / Введите название новой папки:")
    await NewFolderState.waiting_name.set()
    await call.answer()

@dp.message_handler(state=NewFolderState.waiting_name)
async def process_newfolder(message: Message, state: FSMContext):
    name = sanitize_filename(message.text.strip())
    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO folders (name, date) VALUES ($1, $2)",
                name, int(time.time())
            )
            invalidate_folders_cache()  # cache yangilash
            admin_flag = is_admin_mode(message.from_user.id)
            await message.answer(
                f"✅ <b>{name}</b> papkasi yaratildi! / папка создана!",
                parse_mode="HTML",
                reply_markup=(admin_menu_kb() if admin_flag else user_menu_kb(show_admin=admin_flag))
            )
        except asyncpg.exceptions.UniqueViolationError:
            await message.answer(f"❌ <b>{name}</b> allaqachon mavjud! / уже существует!", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Folder creation error: {e}")
            await message.answer(f"❌ Xato yuz berdi: {e}", parse_mode="HTML")
    await state.finish()

# ─── Search ───────────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "search")
async def cb_search(call: CallbackQuery):
    await safe_edit(call, "🔍 Qidiruv so'zini yozing / Введите слово для поиска:")
    await SearchState.waiting_query.set()
    await call.answer()

@dp.message_handler(state=SearchState.waiting_query)
async def process_search(message: Message, state: FSMContext):
    account_id    = await get_account_id(message.from_user.id)
    is_admin_flag = is_admin_mode(message.from_user.id)
    keyword       = f"%{message.text.strip()}%"
    await state.finish()

    async with db_pool.acquire() as conn:
        if is_admin_flag:
            rows = await conn.fetch(
                f"{SELECT_SQL} WHERE f.file_name ILIKE $1 ORDER BY f.date DESC LIMIT 50",
                keyword
            )
        else:
            if not account_id:
                await message.answer("🔐 Avval kiring / Сначала войдите", reply_markup=auth_kb())
                return
            rows = await conn.fetch(
                f"{SELECT_SQL} WHERE f.account_id = $1 AND f.file_name ILIKE $2 ORDER BY f.date DESC LIMIT 50",
                account_id, keyword
            )

    if not rows:
        await message.answer(
            "🔍 Hech narsa topilmadi. / Ничего не найдено.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu", callback_data="menu")
            )
        )
        return

    await message.answer(f"🔍 <b>{len(rows)} ta natija / результатов:</b>", parse_mode="HTML")
    for row in rows:
        mb       = round((row["size"] or 0) / 1024 / 1024, 2)
        pin_icon = "📌 " if row["pinned"] else ""
        date_str = format_date(row["date"])
        owner_text = ""
        if is_admin_flag and row["account_id"] != account_id:
            owner_name_str = row["owner_name"] or "noma'lum"
            owner_text = f"
👤 <b>@{owner_name_str}</b>"
        caption = (
            f"{pin_icon}{get_icon(row['category'])} <b>{row['file_name']}</b>
"
            f"💾 {mb} MB  |  📅 {date_str}
"
            f"📁 {row['folder']}{owner_text}"
        )
        await send_file_safe(
            message.chat.id, row["file_id"], row["category"], caption,
            reply_markup=file_actions_kb(row["id"], row["pinned"], row["folder"])
        )

# ─── Stats ────────────────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "stats")
async def cb_stats(call: CallbackQuery):
    account_id    = await get_account_id(call.from_user.id)
    is_admin_flag = is_admin_mode(call.from_user.id)

    if not is_admin_flag and not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return

    # 🚀 Cache bilan — 30 soniyada bir yangilanadi
    stats, fold_cnt, acc_cnt = await get_stats(account_id, is_admin_flag)

    mb = round((stats["total_size"] or 0) / 1024 / 1024, 2)
    gb = round(mb / 1024, 3)

    prefix = "Admin " if is_admin_flag else ""
    text   = (
        f"📊 <b>{prefix}Statistika</b>

"
        + (f"👥 Hisoblar: <b>{acc_cnt}</b>
" if is_admin_flag else "")
        + f"📄 Jami fayllar: <b>{stats['total']}</b>
"
        f"💾 Hajm: <b>{mb} MB ({gb} GB)</b>

"
        f"🎬 Videolar: <b>{stats['vid_cnt']}</b>
"
        f"🖼️ Rasmlar: <b>{stats['photo_cnt']}</b>
"
        f"🤖 APK/IPA: <b>{stats['app_cnt']}</b>
"
        f"📄 Boshqalar: <b>{stats['other_cnt']}</b>

"
        f"📌 Muhimlar: <b>{stats['pin_cnt']}</b>
"
        f"📁 Papkalar: <b>{fold_cnt + 1}</b>"
    )
    await safe_edit(
        call, text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
        )
    )
    await call.answer()

# ─── /menu command ────────────────────────────────────────────────────────────

@dp.message_handler(commands=["menu"])
async def cmd_menu(message: Message, state: FSMContext):
    await state.finish()
    account_id = await get_account_id(message.from_user.id)
    if not account_id:
        await message.answer(
            "☁️ <b>Shaxsiy Bulut Xotira</b>

🔐 Hisobga kirish / Войти:

"
            "⚠️ <i>Login va parolingizni eslab qoling!</i>",
            parse_mode="HTML", reply_markup=auth_kb()
        )
        return
    username      = await get_account_username(account_id)
    is_admin_flag = is_admin_mode(message.from_user.id)
    await message.answer(
        f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>

"
        f"👤 Hisob / Аккаунт: <b>@{username}</b>
"
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
    app    = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port   = int(os.getenv("PORT", 8000))
    site   = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ Bot ishga tushdi! Port: {port}")

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
