import os
import asyncio
import asyncpg
import re
import hashlib
import hmac
import json
import secrets
import logging
import time
import mimetypes
from urllib.parse import parse_qsl
from datetime import datetime
from collections import defaultdict
from cachetools import TTLCache
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    Message, CallbackQuery, InputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor
import aiohttp
from aiohttp import web

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
WEBAPP_BASE_URL = os.getenv("WEBAPP_BASE_URL", "").strip().rstrip("/")

if not BOT_TOKEN:     raise ValueError("BOT_TOKEN required")
if not ADMIN_PASSWORD:raise ValueError("ADMIN_PASSWORD required")
if not DATABASE_URL:  raise ValueError("DATABASE_URL required")
if not WEBAPP_BASE_URL:
    raise ValueError(
        "WEBAPP_BASE_URL required — Render'dagi haqiqiy domeningizni kiriting, "
        "masalan: https://sizning-servis.onrender.com (oxirida / bo'lmasin)"
    )
if not WEBAPP_BASE_URL.startswith("https://"):
    raise ValueError("WEBAPP_BASE_URL 'https://' bilan boshlanishi shart")

bot     = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp      = Dispatcher(bot, storage=storage)

db_pool   = None
PAGE_SIZE = 5
MAX_TAGS_PER_FILE = 10

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

# ─── 2FA (Two-Factor Authentication) ─────────────────────────────────────────

TWO_FA_CODE_TTL      = 300        # 5 daqiqa
TWO_FA_MAX_ATTEMPTS  = 3
TWO_FA_BLOCK_SECONDS = 15 * 60    # 15 daqiqa

# Foydalanuvchi 2FA — account_id bo'yicha (DB'ga bog'liq)
two_fa_attempts      = defaultdict(int)    # account_id -> xato urinishlar soni
two_fa_blocked_until = defaultdict(float)  # account_id -> bloklash tugash vaqti (unix)

# Admin panel 2FA — telegram_user_id bo'yicha (xotirada, DB'siz)
admin_2fa_pending        = {}              # {tg_id: {"code": str, "expires": ts}}
admin_2fa_attempts       = defaultdict(int)
admin_2fa_blocked_until  = defaultdict(float)

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

class TagState(StatesGroup):
    waiting_tag = State()

class TwoFAState(StatesGroup):
    waiting_code    = State()   # login / admin panel uchun kod tasdiqlash
    waiting_disable = State()   # 2FA ni o'chirish uchun kod tasdiqlash

class EmailAuthState(StatesGroup):
    waiting_email = State()
    waiting_code  = State()

class PhoneAuthState(StatesGroup):
    waiting_phone = State()
    waiting_code  = State()

class ResetPasswordState(StatesGroup):
    waiting_contact      = State()
    waiting_code         = State()
    waiting_new_password = State()

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

# ─── Email / Phone validatsiya ─────────────────────────────────────────────────

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')
PHONE_REGEX = re.compile(r'^\+998\d{9}$')          # +998901234567
VERIFICATION_CODE_TTL = 600                         # 10 daqiqa

def is_valid_email(email: str) -> bool:
    return bool(EMAIL_REGEX.match(email.strip()))

def is_valid_phone(phone: str) -> bool:
    return bool(PHONE_REGEX.match(phone.strip()))

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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS file_tags (
                id      SERIAL PRIMARY KEY,
                file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
                tag     TEXT,
                UNIQUE(file_id, tag)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS two_fa_codes (
                id          SERIAL PRIMARY KEY,
                account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
                code        TEXT,
                expires_at  BIGINT,
                used        BOOLEAN DEFAULT FALSE
            )
        """)

        # Eski ustunlarni qo'shish (migration)
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS account_id INTEGER")
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS folder TEXT DEFAULT 'umumiy'")
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS pinned INTEGER DEFAULT 0")

        # 2FA ustunlari (migration)
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS two_fa_enabled BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS two_fa_secret TEXT")
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS two_fa_method TEXT DEFAULT 'telegram'")

        # Email / Telefon auth ustunlari (migration)
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email TEXT UNIQUE")
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS phone TEXT UNIQUE")
        await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS auth_method TEXT DEFAULT 'username'")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS verification_codes (
                id          SERIAL PRIMARY KEY,
                contact     TEXT,
                code        TEXT,
                type        TEXT,
                expires_at  BIGINT,
                used        BOOLEAN DEFAULT FALSE
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS login_history (
                id               SERIAL PRIMARY KEY,
                account_id       INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
                telegram_user_id BIGINT,
                action           TEXT,
                created_at       BIGINT,
                device_info      TEXT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_login_history_account ON login_history(account_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_login_history_created ON login_history(created_at DESC)")

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
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_file      ON file_tags(file_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_tag       ON file_tags(tag)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_2fa_account    ON two_fa_codes(account_id)")

        await conn.execute(
            "INSERT INTO folders (name, date) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
            "umumiy", int(time.time())
        )

        # ─── Duplicate Detection (migration) ─────────────────────────────────
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS file_hash TEXT")
        await conn.execute("ALTER TABLE files ADD COLUMN IF NOT EXISTS storage_path TEXT")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS file_owners (
                id          SERIAL PRIMARY KEY,
                file_hash   TEXT,
                account_id  INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
                file_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
                custom_name TEXT,
                added_at    BIGINT,
                UNIQUE(file_hash, account_id)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_owners_hash    ON file_owners(file_hash)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_file_owners_account ON file_owners(account_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash          ON files(file_hash)")

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

# ─── Duplicate Detection helpers ──────────────────────────────────────────────

def calculate_file_hash(file_data: bytes) -> str:
    """Fayl mazmunidan SHA256 hash hisoblaydi"""
    return hashlib.sha256(file_data).hexdigest()

async def find_duplicate_file(file_hash: str):
    """Bazada bir xil hash bor-yo'qligini tekshiradi. Topilsa file row qaytaradi."""
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, file_id, file_name, category, size FROM files WHERE file_hash = $1 LIMIT 1",
            file_hash
        )

async def add_file_owner(file_hash: str, account_id: int, file_id: int, custom_name: str):
    """file_owners jadvaliga yangi ega qo'shadi"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO file_owners (file_hash, account_id, file_id, custom_name, added_at) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (file_hash, account_id) DO NOTHING",
            file_hash, account_id, file_id, custom_name, int(time.time())
        )

async def remove_file_owner(file_hash: str, account_id: int):
    """file_owners dan egani o'chiradi (soft delete)"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM file_owners WHERE file_hash = $1 AND account_id = $2",
            file_hash, account_id
        )

async def get_file_owners_count(file_hash: str) -> int:
    """Berilgan hash uchun ega sonini qaytaradi"""
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM file_owners WHERE file_hash = $1", file_hash
        ) or 0

async def save_file(account_id, file_id, file_name, category, size, file_data: bytes = None):
    """
    Faylni saqlaydi. Duplikat aniqlansa yangi satr yaratmaydi —
    mavjud files.id ni virtual ega sifatida file_owners ga qo'shadi.
    Qaytadi: (db_id, is_duplicate)
    """
    file_hash = calculate_file_hash(file_data) if file_data else None

    async with db_pool.acquire() as conn:
        # Duplikat tekshiruvi
        if file_hash:
            existing = await conn.fetchrow(
                "SELECT id FROM files WHERE file_hash = $1 LIMIT 1", file_hash
            )
            if existing:
                # Virtual ega sifatida qo'shish (yangi satr yaratilmaydi)
                await conn.execute(
                    "INSERT INTO file_owners (file_hash, account_id, file_id, custom_name, added_at) "
                    "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (file_hash, account_id) DO NOTHING",
                    file_hash, account_id, existing["id"], file_name, int(time.time())
                )
                return existing["id"], True  # (id, is_duplicate=True)

        # Yangi fayl saqlash
        row_id = await conn.fetchval(
            "INSERT INTO files (account_id, file_id, file_name, category, size, date, folder, pinned, file_hash) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id",
            account_id, file_id, file_name, category, size,
            int(time.time()),
            "umumiy", 0, file_hash
        )
        # Egani ham file_owners ga yozish
        if file_hash:
            await conn.execute(
                "INSERT INTO file_owners (file_hash, account_id, file_id, custom_name, added_at) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (file_hash, account_id) DO NOTHING",
                file_hash, account_id, row_id, file_name, int(time.time())
            )
        return row_id, False  # (id, is_duplicate=False)

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

# ─── Tags ─────────────────────────────────────────────────────────────────────

def sanitize_tag(raw: str) -> str:
    """Teg nomini tozalash: kichik harf, faqat harf/raqam/_ , 30 belgigacha"""
    tag = raw.strip().lstrip("#").lower()
    tag = re.sub(r"[^\w]+", "", tag, flags=re.UNICODE)
    return tag[:30]

async def add_file_tag(file_id: int, tag: str) -> bool:
    """Faylga teg qo'shish. Limitdan oshsa yoki xato bo'lsa False qaytadi."""
    tag = sanitize_tag(tag)
    if not tag:
        return False
    async with db_pool.acquire() as conn:
        existing = await conn.fetchval("SELECT COUNT(*) FROM file_tags WHERE file_id = $1", file_id)
        if existing and existing >= MAX_TAGS_PER_FILE:
            return False
        try:
            await conn.execute(
                "INSERT INTO file_tags (file_id, tag) VALUES ($1, $2) "
                "ON CONFLICT (file_id, tag) DO NOTHING",
                file_id, tag
            )
            return True
        except Exception as e:
            logger.error(f"add_file_tag error: {e}")
            return False

async def remove_file_tag(file_id: int, tag: str):
    """Fayldan tegni o'chirish (fayl o'zi qoladi)"""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM file_tags WHERE file_id = $1 AND tag = $2", file_id, tag)

async def get_file_tags(file_id: int):
    """Faylning barcha teglari"""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT tag FROM file_tags WHERE file_id = $1 ORDER BY tag", file_id)
    return [r["tag"] for r in rows]

async def get_tags_map(file_ids: list):
    """Bir nechta fayl uchun teglarni bitta so'rovda olish: {file_id: [tag, ...]}"""
    if not file_ids:
        return {}
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT file_id, tag FROM file_tags WHERE file_id = ANY($1::int[]) ORDER BY tag",
            list(file_ids)
        )
    tags_map = defaultdict(list)
    for r in rows:
        tags_map[r["file_id"]].append(r["tag"])
    return tags_map

async def get_files_by_tag(tag: str, account_id: int, is_admin: bool, limit: int = 50):
    """Teg bo'yicha fayllarni topish"""
    tag = sanitize_tag(tag)
    async with db_pool.acquire() as conn:
        if is_admin:
            rows = await conn.fetch(
                f"{SELECT_SQL} JOIN file_tags t ON t.file_id = f.id "
                "WHERE t.tag = $1 ORDER BY f.date DESC LIMIT $2",
                tag, limit
            )
        else:
            rows = await conn.fetch(
                f"{SELECT_SQL} JOIN file_tags t ON t.file_id = f.id "
                "WHERE t.tag = $1 AND f.account_id = $2 ORDER BY f.date DESC LIMIT $3",
                tag, account_id, limit
            )
    return rows

async def get_all_tags(account_id: int, is_admin: bool = False, limit: int = 60):
    """Foydalanuvchining (yoki barcha, admin uchun) teglari, chastota bo'yicha"""
    async with db_pool.acquire() as conn:
        if is_admin:
            rows = await conn.fetch(
                "SELECT tag, COUNT(*) AS cnt FROM file_tags "
                "GROUP BY tag ORDER BY cnt DESC, tag LIMIT $1",
                limit
            )
        else:
            rows = await conn.fetch(
                "SELECT t.tag AS tag, COUNT(*) AS cnt FROM file_tags t "
                "JOIN files f ON f.id = t.file_id "
                "WHERE f.account_id = $1 "
                "GROUP BY t.tag ORDER BY cnt DESC, t.tag LIMIT $2",
                account_id, limit
            )
    return rows

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

# ─── 2FA DB helpers ───────────────────────────────────────────────────────────

def _gen_code() -> str:
    """6 xonali tasodifiy kod (kriptografik xavfsiz)"""
    return f"{secrets.randbelow(1_000_000):06d}"

async def generate_2fa_code(account_id: int) -> str:
    """Yangi 2FA kod yaratadi, eski ishlatilmagan kodlarni bekor qiladi"""
    code       = _gen_code()
    expires_at = int(time.time()) + TWO_FA_CODE_TTL
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE two_fa_codes SET used = TRUE WHERE account_id = $1 AND used = FALSE",
            account_id
        )
        await conn.execute(
            "INSERT INTO two_fa_codes (account_id, code, expires_at, used) VALUES ($1,$2,$3,FALSE)",
            account_id, code, expires_at
        )
    return code

async def verify_2fa_code(account_id: int, code: str) -> bool:
    """Kodni tekshiradi va ishlatilgan deb belgilaydi (bir marta ishlatiladi)"""
    now = int(time.time())
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM two_fa_codes "
            "WHERE account_id = $1 AND code = $2 AND used = FALSE AND expires_at > $3 "
            "ORDER BY id DESC LIMIT 1",
            account_id, code.strip(), now
        )
        if not row:
            return False
        await conn.execute("UPDATE two_fa_codes SET used = TRUE WHERE id = $1", row["id"])
    return True

async def enable_2fa(account_id: int, method: str = "telegram"):
    """2FA ni yoqadi"""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE accounts SET two_fa_enabled = TRUE, two_fa_method = $2 WHERE id = $1",
            account_id, method
        )

async def disable_2fa(account_id: int):
    """2FA ni o'chiradi"""
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE accounts SET two_fa_enabled = FALSE WHERE id = $1", account_id)

async def is_2fa_enabled(account_id: int) -> bool:
    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT two_fa_enabled FROM accounts WHERE id = $1", account_id)
    return bool(val)

# ─── 2FA urinishlar / bloklash ────────────────────────────────────────────────

def is_2fa_blocked(account_id: int):
    """(blocked: bool, qolgan_soniya: int)"""
    until = two_fa_blocked_until.get(account_id, 0)
    now   = time.time()
    if until > now:
        return True, int(until - now)
    return False, 0

def register_2fa_fail(account_id: int):
    two_fa_attempts[account_id] += 1
    if two_fa_attempts[account_id] >= TWO_FA_MAX_ATTEMPTS:
        two_fa_blocked_until[account_id] = time.time() + TWO_FA_BLOCK_SECONDS
        two_fa_attempts[account_id] = 0

def reset_2fa_attempts(account_id: int):
    two_fa_attempts.pop(account_id, None)
    two_fa_blocked_until.pop(account_id, None)

def is_2fa_blocked_admin(tg_id: int):
    until = admin_2fa_blocked_until.get(tg_id, 0)
    now   = time.time()
    if until > now:
        return True, int(until - now)
    return False, 0

def register_2fa_fail_admin(tg_id: int):
    admin_2fa_attempts[tg_id] += 1
    if admin_2fa_attempts[tg_id] >= TWO_FA_MAX_ATTEMPTS:
        admin_2fa_blocked_until[tg_id] = time.time() + TWO_FA_BLOCK_SECONDS
        admin_2fa_attempts[tg_id] = 0

def reset_2fa_attempts_admin(tg_id: int):
    admin_2fa_attempts.pop(tg_id, None)
    admin_2fa_blocked_until.pop(tg_id, None)
    admin_2fa_pending.pop(tg_id, None)

# ─── Email / Telefon auth — DB helpers ────────────────────────────────────────
# ESLATMA: Bot haqiqiy email/SMS xizmatiga ulanmagan (SMTP/SMS-gateway yo'q).
# Kod "yuborish" — kodni shu Telegram chatning o'zida ko'rsatish orqali amalga
# oshiriladi (xuddi mavjud 2FA mexanizmi kabi). Agar kelajakda haqiqiy email/SMS
# yuborish kerak bo'lsa, send_verification_code() funksiyasini shu joyga qo'shib,
# generate_verification_code() dan keyin chaqirish kifoya.

async def generate_verification_code(contact: str, code_type: str) -> str:
    """Yangi tasdiqlash kodi yaratadi, shu contact+type uchun eski kodlarni bekor qiladi"""
    code       = _gen_code()
    expires_at = int(time.time()) + VERIFICATION_CODE_TTL
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE verification_codes SET used = TRUE "
            "WHERE contact = $1 AND type = $2 AND used = FALSE",
            contact, code_type
        )
        await conn.execute(
            "INSERT INTO verification_codes (contact, code, type, expires_at, used) "
            "VALUES ($1,$2,$3,$4,FALSE)",
            contact, code, code_type, expires_at
        )
    return code

async def verify_code(contact: str, code: str, code_type: str) -> bool:
    """Tasdiqlash kodini tekshiradi va ishlatilgan deb belgilaydi (bir marta ishlatiladi)"""
    now = int(time.time())
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM verification_codes "
            "WHERE contact = $1 AND type = $2 AND code = $3 "
            "AND used = FALSE AND expires_at > $4 "
            "ORDER BY id DESC LIMIT 1",
            contact, code_type, code.strip(), now
        )
        if not row:
            return False
        await conn.execute("UPDATE verification_codes SET used = TRUE WHERE id = $1", row["id"])
    return True

# ─── Login History helpers ────────────────────────────────────────────────────

ACTION_LABELS = {
    "login":           "✅ Kirish",
    "login_2fa":       "✅ Kirish (2FA)",
    "login_email":     "✅ Kirish (Email)",
    "login_phone":     "✅ Kirish (Telefon)",
    "logout":          "🔒 Chiqish",
    "failed_login":    "❌ Noto'g'ri parol",
    "password_change": "🔑 Parol o'zgardi",
    "register":        "📝 Ro'yxatdan o'tish",
}

async def log_login_event(account_id: int, telegram_user_id: int, action: str, device_info: str = "Telegram Bot"):
    """Login hodisasini yozib qo'yadi"""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO login_history (account_id, telegram_user_id, action, created_at, device_info) "
                "VALUES ($1,$2,$3,$4,$5)",
                account_id, telegram_user_id, action, int(time.time()), device_info
            )
    except Exception as e:
        logger.warning(f"login_history yozishda xato: {e}")

async def get_login_history(account_id: int, limit: int = 20):
    """Foydalanuvchining o'z login tarixi"""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT action, created_at, device_info FROM login_history "
            "WHERE account_id = $1 ORDER BY created_at DESC LIMIT $2",
            account_id, limit
        )

async def get_all_login_history(limit: int = 50):
    """Admin: barcha foydalanuvchilarning login tarixi"""
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT lh.action, lh.created_at, lh.device_info, a.username "
            "FROM login_history lh "
            "LEFT JOIN accounts a ON lh.account_id = a.id "
            "ORDER BY lh.created_at DESC LIMIT $1",
            limit
        )

async def detect_suspicious_activity(account_id: int):
    """Shubhali faoliyatni aniqlaydi"""
    now = int(time.time())
    five_min_ago = now - 300  # 5 daqiqa
    async with db_pool.acquire() as conn:
        # 5 daqiqada 5 marta noto'g'ri parol
        failed_count = await conn.fetchval(
            "SELECT COUNT(*) FROM login_history "
            "WHERE account_id = $1 AND action = 'failed_login' AND created_at > $2",
            account_id, five_min_ago
        )
        # So'nggi 10 ta hodisa — yangi qurilmadan kirish tekshiruvi
        recent_logins = await conn.fetch(
            "SELECT action, device_info FROM login_history "
            "WHERE account_id = $1 AND action LIKE 'login%' "
            "ORDER BY created_at DESC LIMIT 10",
            account_id
        )
    alerts = []
    if failed_count >= 5:
        alerts.append(f"⚠️ 5 daqiqada {failed_count} marta noto'g'ri parol!")
    return alerts

def format_history_row(row, show_username=False) -> str:
    """Bitta tarix qatorini formatlaydi"""
    ts     = row["created_at"]
    dt     = datetime.fromtimestamp(int(ts)).strftime("%d.%m %H:%M")
    action = ACTION_LABELS.get(row["action"], row["action"])
    device = row.get("device_info") or "—"
    if show_username:
        uname = row.get("username") or "?"
        return f"<code>{dt}</code> | @{uname} | {action}"
    return f"<code>{dt}</code> | {action} | {device}"

async def get_account_by_email(email: str):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, username, password_hash FROM accounts WHERE email = $1", email
        )

async def get_account_by_phone(phone: str):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, username, password_hash FROM accounts WHERE phone = $1", phone
        )

async def bind_telegram(account_id: int, telegram_user_id: int):
    """Telegram foydalanuvchisini hisobga bog'lash (login yakunlash)"""
    now = int(time.time())
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO telegram_bindings (account_id, telegram_user_id, bound_at) VALUES ($1,$2,$3) "
            "ON CONFLICT (telegram_user_id) DO UPDATE SET account_id=$1, bound_at=$3",
            account_id, telegram_user_id, now
        )
    invalidate_account_cache(telegram_user_id)

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
    kb.add(
        InlineKeyboardButton("📧 Email orqali",   callback_data="auth:email"),
        InlineKeyboardButton("📱 Telefon orqali", callback_data="auth:phone"),
    )
    kb.add(InlineKeyboardButton("🔑 Parolni unutdingizmi? / Забыли пароль?", callback_data="auth:reset"))
    return kb

def user_menu_kb(show_admin: bool = False):
    kb = InlineKeyboardMarkup(row_width=2)
    # ─── Mini App (WebApp) tugmasi ───────────────────────────────────────────
    # WEBAPP_BASE_URL global config'dan olinadi (yuqorida validatsiya qilingan)
    kb.add(InlineKeyboardButton(
        "☁️ Cloud Drive (Mini App)",
        web_app=WebAppInfo(url=f"{WEBAPP_BASE_URL}/webapp")
    ))
    # ────────────────────────────────────────────────────────────────────────
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
        InlineKeyboardButton("🏷️ Teglar / Теги",     callback_data="tagcloud:0"),
        InlineKeyboardButton("📊 Statistika",         callback_data="stats"),
    )
    kb.add(
        InlineKeyboardButton("➕ Yangi papka",        callback_data="newfolder"),
    )
    kb.add(InlineKeyboardButton("⚙️ Sozlamalar / Настройки", callback_data="settings"))
    kb.add(InlineKeyboardButton("📜 Faoliyat tarixi", callback_data="login_history"))
    kb.add(InlineKeyboardButton("🔒 Chiqish / Выход", callback_data="logout"))
    if show_admin:
        kb.add(InlineKeyboardButton("🔐 Admin paneli / Админ панель", callback_data="admin:menu"))
    return kb

def settings_kb(two_fa_enabled: bool, email: str = None, phone: str = None):
    kb = InlineKeyboardMarkup(row_width=1)
    if two_fa_enabled:
        kb.add(InlineKeyboardButton("🔓 2FA ni o'chirish / Отключить 2FA", callback_data="disable2fa"))
    else:
        kb.add(InlineKeyboardButton("🔐 2FA ni yoqish / Включить 2FA", callback_data="enable2fa"))
    kb.add(InlineKeyboardButton(
        "📧 Emailni o'zgartirish" if email else "📧 Email bog'lash",
        callback_data="attach:email"
    ))
    kb.add(InlineKeyboardButton(
        "📱 Telefonni o'zgartirish" if phone else "📱 Telefon bog'lash",
        callback_data="attach:phone"
    ))
    kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))
    return kb

def twofa_cancel_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ Bekor qilish / Отмена", callback_data="cancel2fa"))
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
    kb.add(InlineKeyboardButton("🏷️ Teglar",       callback_data="tagcloud:0"))
    kb.add(
        InlineKeyboardButton("📜 Kirish tarixi",    callback_data="admin:login_history"),
        InlineKeyboardButton("🚨 Shubhali faoliyat", callback_data="admin:suspicious"),
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
    kb.add(InlineKeyboardButton("🏷️ Teglar", callback_data=f"tags:{file_id_db}"))
    return kb

def tags_view_kb(file_id_db: int, tags: list):
    """Faylning teglari + o'chirish (tegga bosilsa o'chadi) + qo'shish tugmasi"""
    kb = InlineKeyboardMarkup(row_width=3)
    tag_buttons = [
        InlineKeyboardButton(f"#{t} ✕", callback_data=f"rmtag:{file_id_db}:{t}")
        for t in tags
    ]
    if tag_buttons:
        kb.add(*tag_buttons)
    if len(tags) < MAX_TAGS_PER_FILE:
        kb.add(InlineKeyboardButton("➕ Teg qo'shish / Добавить тег", callback_data=f"addtag:{file_id_db}"))
    kb.add(InlineKeyboardButton("🔙 Orqaga / Назад", callback_data="menu"))
    return kb

def tag_cloud_kb(tags_rows):
    """Barcha teglar ro'yxati (tag cloud) — 3 ustunli"""
    kb = InlineKeyboardMarkup(row_width=3)
    tag_buttons = [
        InlineKeyboardButton(f"#{r['tag']} ({r['cnt']})", callback_data=f"searchtag:{r['tag']}:0")
        for r in tags_rows
    ]
    kb.add(*tag_buttons)
    kb.add(InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu"))
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

@dp.message_handler(commands=["start", "menu"], state="*")
async def cmd_start(message: Message, state: FSMContext):
    await state.finish()
    user       = message.from_user
    account_id = await get_account_id(user.id)

    if account_id:
        username      = await get_account_username(account_id)
        is_admin_flag = is_admin_mode(user.id)
        await message.answer(
            f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n"
            f"<b>Личное Облачное Хранилище</b>\n\n"
            f"👤 Hisob / Аккаунт: <b>@{username}</b>\n"
            f"📤 Fayl yuboring — saqlanadi!\n\n"
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
            "<i>Login va parolingizni eslab qoling!</i>\n"
            "<i>Запомните логин и пароль!</i>",
            parse_mode="HTML",
            reply_markup=auth_kb()
        )

@dp.message_handler(commands=["cancel"], state="*")
async def cmd_cancel(message: Message, state: FSMContext):
    """Har qanday state da /cancel — state ni tozalaydi"""
    current = await state.get_state()
    await state.finish()
    if current:
        await message.answer(
            "❌ Bekor qilindi / Отменено.\n\n"
            "Davom etish uchun /start bosing.",
        )
    else:
        # State yo'q edi — oddiy /start ga yo'naltirish
        account_id = await get_account_id(message.from_user.id)
        if account_id:
            username      = await get_account_username(account_id)
            is_admin_flag = is_admin_mode(message.from_user.id)
            await message.answer(
                f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n\n"
                f"👤 @{username}\n\nMenyudan tanlang:",
                parse_mode="HTML",
                reply_markup=user_menu_kb(show_admin=is_admin_flag)
            )
        else:
            await message.answer(
                "☁️ <b>Shaxsiy Bulut Xotira</b>\n\n"
                "🔐 Hisobga kirish yoki ro'yxatdan o'tish:",
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
    await log_login_event(account_id, message.from_user.id, "register")
    await state.finish()
    logger.info(f"Registered: @{username} tg={message.from_user.id}")
    await message.answer(
        f"✅ <b>Hisob yaratildi! / Аккаунт создан!</b>\n\n👤 @{username}\n\n"
        "Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
    )

@dp.callback_query_handler(lambda c: c.data == "auth:login")
async def cb_login(call: CallbackQuery):
    await call.message.edit_text(
        "🔐 <b>Hisobga kirish / Вход</b>\n\nFoydalanuvchi nomini kiriting / Введите логин:",
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
            "❌ Login yoki parol noto'g'ri / Неверный логин или пароль.\n/start"
        )
        # Agar foydalanuvchi mavjud bo'lsa, failed_login loglaymiz
        if account:
            await log_login_event(account["id"], message.from_user.id, "failed_login")
        await state.finish()
        return

    account_id = account["id"]

    # ─── 2FA tekshiruvi ───────────────────────────────────────────────────
    async with db_pool.acquire() as conn:
        two_fa_enabled_flag = await conn.fetchval(
            "SELECT two_fa_enabled FROM accounts WHERE id = $1", account_id
        )

    if two_fa_enabled_flag:
        blocked, remaining = is_2fa_blocked(account_id)
        if blocked:
            mins = remaining // 60 + 1
            await message.answer(
                f"🔒 Juda ko'p xato urinish. {mins} daqiqadan keyin qayta urining.\n"
                f"Слишком много неверных попыток. Повторите через {mins} мин."
            )
            await state.finish()
            return

        code = await generate_2fa_code(account_id)
        await message.answer(
            "2FA kod yuborildi. 5 daqiqa ichida kiriting:\n\n"
            f"<code>{code}</code>",
            parse_mode="HTML",
            reply_markup=twofa_cancel_kb()
        )
        await state.update_data(purpose="login", account_id=account_id, username=username)
        await TwoFAState.waiting_code.set()
        return

    # ─── 2FA yo'q — oddiy kirish ──────────────────────────────────────────
    now = int(time.time())
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO telegram_bindings (account_id, telegram_user_id, bound_at) VALUES ($1,$2,$3) "
            "ON CONFLICT (telegram_user_id) DO UPDATE SET account_id=$1, bound_at=$3",
            account_id, message.from_user.id, now
        )
    invalidate_account_cache(message.from_user.id)
    await log_login_event(account_id, message.from_user.id, "login")
    await state.finish()
    logger.info(f"Login: @{username} tg={message.from_user.id}")
    await message.answer(
        f"✅ <b>Xush kelibsiz! / Добро пожаловать!</b>\n\n👤 @{username}\n\n"
        "Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
    )

@dp.message_handler(state=TwoFAState.waiting_code)
async def process_2fa_code(message: Message, state: FSMContext):
    """Login yoki admin panel uchun yuborilgan 2FA kodni tekshiradi"""
    data    = await state.get_data()
    purpose = data.get("purpose")
    code    = message.text.strip()

    # ─── Admin panel 2FA ──────────────────────────────────────────────────
    if purpose == "admin":
        tg_id = message.from_user.id
        blocked, remaining = is_2fa_blocked_admin(tg_id)
        if blocked:
            mins = remaining // 60 + 1
            await state.finish()
            await message.answer(
                f"🔒 Juda ko'p xato urinish. {mins} daqiqadan keyin qayta urining. /admin"
            )
            return

        pending = admin_2fa_pending.get(tg_id)
        if not pending or pending["expires"] < time.time():
            admin_2fa_pending.pop(tg_id, None)
            await state.finish()
            await message.answer("❌ Kod eskirgan / Код истёк. /admin orqali qayta urining.")
            return

        if code == pending["code"]:
            admin_2fa_pending.pop(tg_id, None)
            reset_2fa_attempts_admin(tg_id)
            admin_sessions[tg_id] = time.time()
            await state.finish()
            logger.info(f"Admin 2FA passed: tg={tg_id}")
            await message.answer(
                "✅ <b>Admin rejimi faollashdi!</b>",
                parse_mode="HTML", reply_markup=admin_menu_kb()
            )
        else:
            register_2fa_fail_admin(tg_id)
            blocked, remaining = is_2fa_blocked_admin(tg_id)
            if blocked:
                admin_2fa_pending.pop(tg_id, None)
                await state.finish()
                await message.answer(
                    "🔒 3 marta noto'g'ri kod. 15 daqiqaga bloklandingiz. /admin"
                )
            else:
                left = TWO_FA_MAX_ATTEMPTS - admin_2fa_attempts.get(tg_id, 0)
                await message.answer(
                    f"❌ Noto'g'ri kod. Qolgan urinishlar: {left}",
                    reply_markup=twofa_cancel_kb()
                )
        return

    # ─── Login 2FA ────────────────────────────────────────────────────────
    account_id = data.get("account_id")
    username   = data.get("username")
    if not account_id:
        await state.finish()
        await message.answer("❌ Xatolik yuz berdi. /start orqali qaytadan urining.")
        return

    blocked, remaining = is_2fa_blocked(account_id)
    if blocked:
        mins = remaining // 60 + 1
        await state.finish()
        await message.answer(
            f"🔒 Juda ko'p xato urinish. {mins} daqiqadan keyin qayta urining. /start"
        )
        return

    ok = await verify_2fa_code(account_id, code)
    if ok:
        reset_2fa_attempts(account_id)
        now = int(time.time())
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO telegram_bindings (account_id, telegram_user_id, bound_at) VALUES ($1,$2,$3) "
                "ON CONFLICT (telegram_user_id) DO UPDATE SET account_id=$1, bound_at=$3",
                account_id, message.from_user.id, now
            )
        invalidate_account_cache(message.from_user.id)
        await log_login_event(account_id, message.from_user.id, "login_2fa")
        await state.finish()
        logger.info(f"Login+2FA: @{username} tg={message.from_user.id}")
        await message.answer(
            f"✅ <b>Xush kelibsiz! / Добро пожаловать!</b>\n\n👤 @{username}\n\n"
            "Menyudan tanlang / Выберите из меню:",
            parse_mode="HTML",
            reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
        )
    else:
        await log_login_event(account_id, message.from_user.id, "failed_login", "2FA xato kod")
        register_2fa_fail(account_id)
        blocked, remaining = is_2fa_blocked(account_id)
        if blocked:
            await state.finish()
            await message.answer("🔒 3 marta noto'g'ri kod. 15 daqiqaga bloklandingiz. /start")
        else:
            left = TWO_FA_MAX_ATTEMPTS - two_fa_attempts.get(account_id, 0)
            await message.answer(
                f"❌ Noto'g'ri kod. Qolgan urinishlar: {left}",
                reply_markup=twofa_cancel_kb()
            )

@dp.message_handler(state=TwoFAState.waiting_disable)
async def process_2fa_disable_code(message: Message, state: FSMContext):
    """2FA ni o'chirish uchun tasdiqlash kodini tekshiradi"""
    data       = await state.get_data()
    account_id = data.get("account_id")
    code       = message.text.strip()

    if not account_id:
        await state.finish()
        await message.answer("❌ Xatolik yuz berdi.")
        return

    ok = await verify_2fa_code(account_id, code)
    if ok:
        await disable_2fa(account_id)
        await state.finish()
        logger.info(f"2FA disabled: account={account_id}")
        await message.answer(
            "✅ 2FA o'chirildi! / 2FA отключена!",
            reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
        )
    else:
        await message.answer(
            "❌ Noto'g'ri kod / Неверный код. Qaytadan kiriting yoki bekor qiling:",
            reply_markup=twofa_cancel_kb()
        )

@dp.callback_query_handler(lambda c: c.data == "cancel2fa", state="*")
async def cb_cancel_2fa(call: CallbackQuery, state: FSMContext):
    await state.finish()
    await call.answer("Bekor qilindi / Отменено")
    await safe_edit(
        call,
        "❌ Bekor qilindi / Отменено.\n\n/start orqali qayta urining.",
    )

@dp.callback_query_handler(lambda c: c.data == "logout")
async def cb_logout(call: CallbackQuery):
    account_id = await get_account_id(call.from_user.id)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM telegram_bindings WHERE telegram_user_id = $1", call.from_user.id
        )
    if account_id:
        await log_login_event(account_id, call.from_user.id, "logout")
    invalidate_account_cache(call.from_user.id)
    logger.info(f"Logout: tg={call.from_user.id}")
    await call.answer("🔒 Hisobdan chiqildi / Выход выполнен")
    await call.message.edit_text(
        "☁️ <b>Shaxsiy Bulut Xotira</b>\n<b>Личное Облачное Хранилище</b>\n\n"
        "🔐 Hisobga kirish yoki yangi hisob ochish\n\n"
        "⚠️ <i>Login va parolingizni eslab qoling! / Запомните логин и пароль!</i>",
        parse_mode="HTML", reply_markup=auth_kb()
    )

# ─── Email / Telefon orqali kirish ────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "auth:email")
async def cb_email_auth(call: CallbackQuery, state: FSMContext):
    await state.update_data(mode="login")
    await call.message.edit_text(
        "📧 <b>Email orqali kirish / Вход через email</b>\n\n"
        "Hisobingizga bog'langan emailni kiriting / Введите email:",
        parse_mode="HTML"
    )
    await EmailAuthState.waiting_email.set()
    await call.answer()

@dp.message_handler(state=EmailAuthState.waiting_email)
async def process_email(message: Message, state: FSMContext):
    email = message.text.strip().lower()
    if not is_valid_email(email):
        await message.answer("❌ Email formati noto'g'ri / Неверный формат email. Qaytadan kiriting:")
        return

    data    = await state.get_data()
    mode    = data.get("mode", "login")

    if mode == "attach":
        account_id = data.get("account_id")
        if not account_id:
            await state.finish()
            await message.answer("❌ Xatolik yuz berdi. /start orqali qaytadan urining.")
            return
        existing = await get_account_by_email(email)
        if existing and existing["id"] != account_id:
            await message.answer("❌ Bu email boshqa hisobga bog'langan / Этот email уже используется. Boshqa email kiriting:")
            return
        code = await generate_verification_code(email, "attach_email")
        await message.answer(
            "Tasdiqlash kodi yuborildi. 10 daqiqa ichida kiriting:\n\n"
            f"<code>{code}</code>",
            parse_mode="HTML", reply_markup=twofa_cancel_kb()
        )
        await state.update_data(purpose="attach_email", contact=email, account_id=account_id)
        await EmailAuthState.waiting_code.set()
        return

    # ─── mode == "login" ───────────────────────────────────────────────────
    account = await get_account_by_email(email)
    if not account:
        await message.answer(
            "❌ Bu emailga bog'langan hisob topilmadi / Аккаунт с таким email не найден.\n/start"
        )
        await state.finish()
        return

    code = await generate_verification_code(email, "login_email")
    await message.answer(
        "Tasdiqlash kodi yuborildi. 10 daqiqa ichida kiriting:\n\n"
        f"<code>{code}</code>",
        parse_mode="HTML", reply_markup=twofa_cancel_kb()
    )
    await state.update_data(purpose="login_email", contact=email, account_id=account["id"], username=account["username"])
    await EmailAuthState.waiting_code.set()

@dp.callback_query_handler(lambda c: c.data == "auth:phone")
async def cb_phone_auth(call: CallbackQuery, state: FSMContext):
    await state.update_data(mode="login")
    await call.message.edit_text(
        "📱 <b>Telefon orqali kirish / Вход через телефон</b>\n\n"
        "Hisobingizga bog'langan telefon raqamini kiriting (+998901234567):",
        parse_mode="HTML"
    )
    await PhoneAuthState.waiting_phone.set()
    await call.answer()

@dp.message_handler(state=PhoneAuthState.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    if not is_valid_phone(phone):
        await message.answer("❌ Format noto'g'ri / Неверный формат. Namuna: +998901234567. Qaytadan kiriting:")
        return

    data = await state.get_data()
    mode = data.get("mode", "login")

    if mode == "attach":
        account_id = data.get("account_id")
        if not account_id:
            await state.finish()
            await message.answer("❌ Xatolik yuz berdi. /start orqali qaytadan urining.")
            return
        existing = await get_account_by_phone(phone)
        if existing and existing["id"] != account_id:
            await message.answer("❌ Bu raqam boshqa hisobga bog'langan / Этот номер уже используется. Boshqa raqam kiriting:")
            return
        code = await generate_verification_code(phone, "attach_phone")
        await message.answer(
            "Tasdiqlash kodi yuborildi. 10 daqiqa ichida kiriting:\n\n"
            f"<code>{code}</code>",
            parse_mode="HTML", reply_markup=twofa_cancel_kb()
        )
        await state.update_data(purpose="attach_phone", contact=phone, account_id=account_id)
        await PhoneAuthState.waiting_code.set()
        return

    # ─── mode == "login" ───────────────────────────────────────────────────
    account = await get_account_by_phone(phone)
    if not account:
        await message.answer(
            "❌ Bu raqamga bog'langan hisob topilmadi / Аккаунт с таким номером не найден.\n/start"
        )
        await state.finish()
        return

    code = await generate_verification_code(phone, "login_phone")
    await message.answer(
        "Tasdiqlash kodi yuborildi. 10 daqiqa ichida kiriting:\n\n"
        f"<code>{code}</code>",
        parse_mode="HTML", reply_markup=twofa_cancel_kb()
    )
    await state.update_data(purpose="login_phone", contact=phone, account_id=account["id"], username=account["username"])
    await PhoneAuthState.waiting_code.set()

@dp.message_handler(state=[EmailAuthState.waiting_code, PhoneAuthState.waiting_code, ResetPasswordState.waiting_code])
async def process_verification_code(message: Message, state: FSMContext):
    """Email/telefon orqali kirish, hisobga bog'lash yoki parol tiklash uchun kodni tekshiradi"""
    data       = await state.get_data()
    purpose    = data.get("purpose")
    contact    = data.get("contact")
    account_id = data.get("account_id")
    code       = message.text.strip()

    if not purpose or not contact:
        await state.finish()
        await message.answer("❌ Xatolik yuz berdi. /start orqali qaytadan urining.")
        return

    code_type_map = {
        "login_email":  "login_email",
        "login_phone":  "login_phone",
        "attach_email": "attach_email",
        "attach_phone": "attach_phone",
        "reset":        "password_reset",
    }
    code_type = code_type_map.get(purpose)
    ok = await verify_code(contact, code, code_type)

    if not ok:
        await message.answer(
            "❌ Noto'g'ri yoki eskirgan kod / Неверный или истёкший код. Qaytadan kiriting:",
            reply_markup=twofa_cancel_kb()
        )
        return

    # ─── Email/telefon orqali kirish ───────────────────────────────────────
    if purpose in ("login_email", "login_phone"):
        await bind_telegram(account_id, message.from_user.id)
        username = data.get("username")
        await log_login_event(account_id, message.from_user.id, purpose)
        await state.finish()
        logger.info(f"Login via {purpose}: @{username} tg={message.from_user.id}")
        await message.answer(
            f"✅ <b>Xush kelibsiz! / Добро пожаловать!</b>\n\n👤 @{username}\n\n"
            "Menyudan tanlang / Выберите из меню:",
            parse_mode="HTML",
            reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
        )
        return

    # ─── Emailni/telefonni hisobga bog'lash ────────────────────────────────
    if purpose in ("attach_email", "attach_phone"):
        column = "email" if purpose == "attach_email" else "phone"
        async with db_pool.acquire() as conn:
            await conn.execute(f"UPDATE accounts SET {column} = $1 WHERE id = $2", contact, account_id)
        await state.finish()
        logger.info(f"{column} attached: account={account_id}")
        label = "Email" if purpose == "attach_email" else "Telefon"
        await message.answer(
            f"✅ {label} muvaffaqiyatli bog'landi / Успешно привязан!\n\n<code>{contact}</code>",
            parse_mode="HTML",
            reply_markup=user_menu_kb(show_admin=is_admin_mode(message.from_user.id))
        )
        return

    # ─── Parol tiklash — kod tasdiqlandi, yangi parol so'raladi ────────────
    if purpose == "reset":
        await message.answer("✅ Kod tasdiqlandi. Yangi parolni kiriting (kamida 4 belgi):")
        await state.update_data(account_id=account_id)
        await ResetPasswordState.waiting_new_password.set()
        return

@dp.callback_query_handler(lambda c: c.data == "auth:reset")
async def cb_reset_password(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(
        "🔑 <b>Parolni tiklash / Сброс пароля</b>\n\n"
        "Hisobingizga bog'langan email yoki telefon raqamini kiriting:",
        parse_mode="HTML"
    )
    await ResetPasswordState.waiting_contact.set()
    await call.answer()

@dp.message_handler(state=ResetPasswordState.waiting_contact)
async def process_reset_contact(message: Message, state: FSMContext):
    contact = message.text.strip()
    account = None
    contact_type = None

    if is_valid_email(contact.lower()):
        contact = contact.lower()
        account = await get_account_by_email(contact)
        contact_type = "email"
    elif is_valid_phone(contact.replace(" ", "")):
        contact = contact.replace(" ", "")
        account = await get_account_by_phone(contact)
        contact_type = "phone"
    else:
        await message.answer(
            "❌ Email yoki telefon formati noto'g'ri / Неверный формат.\nQaytadan kiriting:"
        )
        return

    if not account:
        await message.answer(
            "❌ Bunday email/telefonga bog'langan hisob topilmadi / Аккаунт не найден.\n/start"
        )
        await state.finish()
        return

    code = await generate_verification_code(contact, "password_reset")
    await message.answer(
        "Tasdiqlash kodi yuborildi. 10 daqiqa ichida kiriting:\n\n"
        f"<code>{code}</code>",
        parse_mode="HTML", reply_markup=twofa_cancel_kb()
    )
    await state.update_data(purpose="reset", contact=contact, contact_type=contact_type, account_id=account["id"])
    await ResetPasswordState.waiting_code.set()

@dp.message_handler(state=ResetPasswordState.waiting_new_password)
async def process_new_password(message: Message, state: FSMContext):
    password = message.text.strip()
    if len(password) < 4:
        await message.answer("❌ Parol juda qisqa / Пароль слишком короткий (kamida 4 belgi):")
        return

    data       = await state.get_data()
    account_id = data.get("account_id")
    if not account_id:
        await state.finish()
        await message.answer("❌ Xatolik yuz berdi. /start orqali qaytadan urining.")
        return

    password_hash = hash_password(password)
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE accounts SET password_hash = $1 WHERE id = $2", password_hash, account_id)
    await log_login_event(account_id, message.from_user.id, "password_change")
    await state.finish()
    logger.info(f"Password reset: account={account_id}")
    await message.answer(
        "✅ Parol muvaffaqiyatli o'zgartirildi! / Пароль успешно изменён!\n\n"
        "Endi yangi parol bilan kirishingiz mumkin / Теперь войдите с новым паролем.\n/start",
        reply_markup=auth_kb()
    )

@dp.callback_query_handler(lambda c: c.data == "attach:email")
async def cb_attach_email(call: CallbackQuery, state: FSMContext):
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    await state.update_data(mode="attach", account_id=account_id)
    await call.message.edit_text(
        "📧 <b>Emailni bog'lash / Привязка email</b>\n\nEmail manzilingizni kiriting:",
        parse_mode="HTML"
    )
    await EmailAuthState.waiting_email.set()
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "attach:phone")
async def cb_attach_phone(call: CallbackQuery, state: FSMContext):
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    await state.update_data(mode="attach", account_id=account_id)
    await call.message.edit_text(
        "📱 <b>Telefonni bog'lash / Привязка телефона</b>\n\nTelefon raqamingizni kiriting (+998901234567):",
        parse_mode="HTML"
    )
    await PhoneAuthState.waiting_phone.set()
    await call.answer()

# ─── Login History Handlers ───────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "login_history")
async def cb_login_history(call: CallbackQuery):
    """Foydalanuvchining o'z faoliyat tarixi"""
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return

    rows    = await get_login_history(account_id, limit=20)
    alerts  = await detect_suspicious_activity(account_id)

    lines = []
    if alerts:
        lines.append("🚨 <b>Shubhali faoliyat aniqlandi!</b>")
        for a in alerts:
            lines.append(a)
        lines.append("")

    if rows:
        lines.append("📜 <b>So'nggi faoliyat tarixi:</b>\n")
        for r in rows:
            lines.append(format_history_row(r))
    else:
        lines.append("📭 Faoliyat tarixi bo'sh.")

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🏠 Menyu", callback_data="menu"))

    await safe_edit(call, "\n".join(lines), reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:login_history")
async def cb_admin_login_history(call: CallbackQuery):
    """Admin: barcha foydalanuvchilarning kirish tarixi"""
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return

    rows = await get_all_login_history(limit=50)

    lines = ["📜 <b>Barcha foydalanuvchilar kirish tarixi:</b>\n"]
    if rows:
        for r in rows:
            lines.append(format_history_row(r, show_username=True))
    else:
        lines.append("📭 Tarix bo'sh.")

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("🚨 Shubhali faoliyat", callback_data="admin:suspicious"),
        InlineKeyboardButton("🔙 Admin menyu",        callback_data="admin:menu"),
    )

    await safe_edit(call, "\n".join(lines), reply_markup=kb)
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "admin:suspicious")
async def cb_suspicious_activity(call: CallbackQuery):
    """Admin: barcha hisoblar bo'yicha shubhali faoliyat"""
    if not is_admin_mode(call.from_user.id):
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return

    now          = int(time.time())
    five_min_ago = now - 300
    async with db_pool.acquire() as conn:
        # 5 daqiqada 5+ marta noto'g'ri parol kiritgan hisoblar
        suspicious = await conn.fetch("""
            SELECT a.username, COUNT(*) AS cnt
            FROM login_history lh
            JOIN accounts a ON lh.account_id = a.id
            WHERE lh.action = 'failed_login' AND lh.created_at > $1
            GROUP BY a.username, lh.account_id
            HAVING COUNT(*) >= 5
            ORDER BY cnt DESC
        """, five_min_ago)

    if suspicious:
        lines = ["🚨 <b>Shubhali faoliyat (5 daqiqada 5+ noto'g'ri urinish):</b>\n"]
        for r in suspicious:
            lines.append(f"⚠️ @{r['username']} — {r['cnt']} ta urinish")
    else:
        lines = ["✅ <b>Hozircha shubhali faoliyat aniqlanmadi.</b>"]

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("📜 Kirish tarixi", callback_data="admin:login_history"),
        InlineKeyboardButton("🔙 Admin menyu",   callback_data="admin:menu"),
    )

    await safe_edit(call, "\n".join(lines), reply_markup=kb)
    await call.answer()

# ─── Admin ────────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["admin"], state="*")
async def cmd_admin(message: Message, state: FSMContext):
    await state.finish()
    if is_admin_mode(message.from_user.id):
        await message.answer(
            "🔰 <b>Admin panel / Админ панель</b>\n\nBarcha hisoblar va fayllar ko'rinadi.",
            parse_mode="HTML", reply_markup=admin_menu_kb()
        )
    else:
        await message.answer("🔐 <b>Admin parolini kiriting:</b>", parse_mode="HTML")
        await AdminAuthState.waiting_password.set()

@dp.message_handler(state=AdminAuthState.waiting_password)
async def process_admin_password(message: Message, state: FSMContext):
    if message.text != ADMIN_PASSWORD:
        await message.answer("❌ Noto'g'ri parol / Неверный пароль. Qayta:")
        return

    # ─── Admin panel uchun 2FA har doim talab qilinadi ───────────────────
    tg_id = message.from_user.id
    blocked, remaining = is_2fa_blocked_admin(tg_id)
    if blocked:
        mins = remaining // 60 + 1
        await state.finish()
        await message.answer(
            f"🔒 Juda ko'p xato urinish. {mins} daqiqadan keyin qayta urining. /admin"
        )
        return

    code = _gen_code()
    admin_2fa_pending[tg_id] = {"code": code, "expires": time.time() + TWO_FA_CODE_TTL}
    await message.answer(
        "2FA kod yuborildi. 5 daqiqa ichida kiriting:\n\n"
        f"<code>{code}</code>",
        parse_mode="HTML",
        reply_markup=twofa_cancel_kb()
    )
    await state.update_data(purpose="admin")
    await TwoFAState.waiting_code.set()

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
            "☁️ <b>Shaxsiy Bulut Xotira</b>\n\n🔐 Hisobga kirish / Войти:",
            reply_markup=auth_kb()
        )
        await call.answer()
        return
    username      = await get_account_username(account_id)
    is_admin_flag = is_admin_mode(call.from_user.id)
    await safe_edit(
        call,
        f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n\n"
        f"👤 Hisob / Аккаунт: <b>@{username}</b>\n"
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

# ─── Sozlamalar / 2FA ─────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "settings")
async def cb_settings(call: CallbackQuery):
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT two_fa_enabled, email, phone FROM accounts WHERE id = $1", account_id
        )
    enabled = row["two_fa_enabled"]
    email   = row["email"]
    phone   = row["phone"]
    status  = "✅ Yoqilgan / Включено" if enabled else "❌ O'chirilgan / Отключено"
    await safe_edit(
        call,
        f"⚙️ <b>Sozlamalar / Настройки</b>\n\n"
        f"🔐 2FA holati / Статус 2FA: <b>{status}</b>\n"
        f"📧 Email: <b>{email or '—'}</b>\n"
        f"📱 Telefon: <b>{phone or '—'}</b>\n\n"
        "2FA yoqilganda, har safar tizimga kirishda qo'shimcha 6 xonali "
        "tasdiqlash kodi so'raladi.\n"
        "При включении 2FA каждый вход потребует дополнительный код подтверждения.",
        reply_markup=settings_kb(bool(enabled), email, phone)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data == "enable2fa")
async def cb_enable_2fa(call: CallbackQuery):
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    await enable_2fa(account_id, "telegram")
    logger.info(f"2FA enabled: account={account_id}")
    await call.answer("✅ 2FA yoqildi! / 2FA включена!")
    await cb_settings(call)

@dp.callback_query_handler(lambda c: c.data == "disable2fa")
async def cb_disable_2fa(call: CallbackQuery, state: FSMContext):
    account_id = await get_account_id(call.from_user.id)
    if not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return

    # Xavfsizlik uchun o'chirishdan oldin tasdiqlash kodi yuboriladi
    code = await generate_2fa_code(account_id)
    await call.answer()
    await bot.send_message(
        call.message.chat.id,
        "2FA ni o'chirish uchun tasdiqlash kodi yuborildi. "
        "5 daqiqa ichida kiriting:\n\n"
        f"<code>{code}</code>",
        parse_mode="HTML",
        reply_markup=twofa_cancel_kb()
    )
    await state.update_data(account_id=account_id)
    await TwoFAState.waiting_disable.set()

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
    _, is_dup = await save_file(account_id, v.file_id, name, "video", v.file_size or 0)
    mb   = round((v.file_size or 0) / 1024 / 1024, 2)
    logger.info(f"Video saved: {name} ({mb}MB) acc={account_id} dup={is_dup}")
    dup_note = " <i>(diskda mavjud edi)</i>" if is_dup else ""
    await message.answer(
        f"🎬 <b>Saqlandi!{dup_note}</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML", reply_markup=after_upload_kb("video")
    )

@dp.message_handler(content_types=types.ContentType.PHOTO)
async def handle_photo(message: Message):
    account_id = await _check_auth(message)
    if not account_id: return
    p    = message.photo[-1]
    name = f"photo_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    _, is_dup = await save_file(account_id, p.file_id, name, "photo", p.file_size or 0)
    mb   = round((p.file_size or 0) / 1024 / 1024, 2)
    dup_note = " <i>(diskda mavjud edi)</i>" if is_dup else ""
    await message.answer(
        f"🖼️ <b>Saqlandi!{dup_note}</b>\n📄 {name}\n💾 {mb} MB",
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
    _, is_dup = await save_file(account_id, d.file_id, name, category, d.file_size or 0)
    mb   = round((d.file_size or 0) / 1024 / 1024, 2)
    icon = get_icon(category)
    logger.info(f"Doc saved: {name} ({category},{mb}MB) acc={account_id} dup={is_dup}")
    dup_note = " <i>(diskda mavjud edi)</i>" if is_dup else ""
    await message.answer(
        f"{icon} <b>Saqlandi!{dup_note}</b>\n📄 {name}\n💾 {mb} MB",
        parse_mode="HTML", reply_markup=after_upload_kb(category)
    )

@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_unknown(message: Message):
    account_id = await get_account_id(message.from_user.id)
    if account_id:
        await message.answer(
            "❓ <b>Bu fayl turi saqlanmaydi.</b>\n"
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
            "😔 Hozircha fayl yo'q. / Пока файлов нет.\n📤 Fayl yuboring!",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu / Меню", callback_data="menu")
            )
        )
        return

    tags_map = await get_tags_map([r["id"] for r in rows])

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
            owner_text = f"\n👤 <b>@{owner_name_str}</b>"

        file_tags = tags_map.get(db_id, [])
        tags_text = ("\n🏷️ " + " ".join(f"#{t}" for t in file_tags)) if file_tags else ""

        caption = (
            f"{pin_icon}{get_icon(cat)} <b>{row['file_name']}</b>\n"
            f"💾 {mb} MB  |  📅 {date_str}\n"
            f"📁 {folder}{owner_text}{tags_text}"
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
        "📁 <b>Papkalar / Папки</b>\n\nPapkani tanlang / Выберите папку:",
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
        f"🗑️ <b>{name}</b> ni o'chirishni tasdiqlaysizmi?\nПодтвердите удаление:",
        reply_markup=confirm_delete_kb(db_id)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("confirmdelete:"))
async def cb_confirm_delete(call: CallbackQuery):
    db_id                        = int(call.data.split(":")[1])
    account_id, file_account_id, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return

    # Faylning egalari va hash ma'lumotini olish
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT file_name, file_hash FROM files WHERE id = $1", db_id)
    if not row:
        await call.answer("Fayl topilmadi!", show_alert=True)
        return

    name      = row["file_name"]
    file_hash = row["file_hash"]
    deleter_account = account_id  # o'chirayotgan foydalanuvchi

    if file_hash:
        # Duplikat tizimi: egani o'chirish
        await remove_file_owner(file_hash, deleter_account)
        owners_left = await get_file_owners_count(file_hash)

        if owners_left > 0:
            # Boshqa egalar bor — faqat file_owners dan olib tashlandi (soft delete)
            logger.info(f"Soft-deleted: id={db_id} ({name}) tg={call.from_user.id}, owners_left={owners_left}")
            await call.message.edit_text(
                f"🗑️ <b>{name}</b> sizning ro'yxatingizdan o'chirildi! / удалён из вашего списка!",
                parse_mode="HTML"
            )
            await call.answer("O'chirildi!")
            return
        # Oxirgi egasi — toliq o'chirish
    # hash yo'q yoki oxirgi ega — to'liq o'chirish
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM files WHERE id = $1", db_id)
    logger.info(f"Hard-deleted: id={db_id} ({name}) tg={call.from_user.id}")
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
        f"📁 <b>{name}</b> ni qaysi papkaga ko'chirish?\nВ какую папку переместить:",
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
    await safe_edit(
        call,
        "🔍 Qidiruv so'zini yozing / Введите слово для поиска:\n"
        "🏷️ Teg bo'yicha qidirish uchun <b>#teg</b> deb yozing / Для поиска по тегу напишите <b>#tag</b>"
    )
    await SearchState.waiting_query.set()
    await call.answer()

@dp.message_handler(state=SearchState.waiting_query)
async def process_search(message: Message, state: FSMContext):
    account_id    = await get_account_id(message.from_user.id)
    is_admin_flag = is_admin_mode(message.from_user.id)
    query_text    = message.text.strip()
    await state.finish()

    # 🏷️ "#tag" formati — teg bo'yicha qidiruv
    if query_text.startswith("#"):
        tag = sanitize_tag(query_text)
        if not tag:
            await message.answer("❌ Teg nomi noto'g'ri / Неверное имя тега.")
            return
        if not is_admin_flag and not account_id:
            await message.answer("🔐 Avval kiring / Сначала войдите", reply_markup=auth_kb())
            return
        rows   = await get_files_by_tag(tag, account_id, is_admin_flag, limit=50)
        header = f"🏷️ <b>#{tag}</b> bo'yicha {len(rows)} ta natija / результатов:"
    else:
        keyword = f"%{query_text}%"
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
        header = f"🔍 <b>{len(rows)} ta natija / результатов:</b>"

    if not rows:
        await message.answer(
            "🔍 Hech narsa topilmadi. / Ничего не найдено.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu", callback_data="menu")
            )
        )
        return

    await message.answer(header, parse_mode="HTML")

    tags_map = await get_tags_map([r["id"] for r in rows])

    for row in rows:
        mb       = round((row["size"] or 0) / 1024 / 1024, 2)
        pin_icon = "📌 " if row["pinned"] else ""
        date_str = format_date(row["date"])
        owner_text = ""
        if is_admin_flag and row["account_id"] != account_id:
            owner_name_str = row["owner_name"] or "noma'lum"
            owner_text = f"\n👤 <b>@{owner_name_str}</b>"
        file_tags = tags_map.get(row["id"], [])
        tags_text = ("\n🏷️ " + " ".join(f"#{t}" for t in file_tags)) if file_tags else ""
        caption = (
            f"{pin_icon}{get_icon(row['category'])} <b>{row['file_name']}</b>\n"
            f"💾 {mb} MB  |  📅 {date_str}\n"
            f"📁 {row['folder']}{owner_text}{tags_text}"
        )
        await send_file_safe(
            message.chat.id, row["file_id"], row["category"], caption,
            reply_markup=file_actions_kb(row["id"], row["pinned"], row["folder"])
        )

# ─── Tags (handlers) ──────────────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("tags:"))
async def cb_tags(call: CallbackQuery):
    db_id                        = int(call.data.split(":")[1])
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    async with db_pool.acquire() as conn:
        name = await conn.fetchval("SELECT file_name FROM files WHERE id = $1", db_id)
    tags = await get_file_tags(db_id)
    tags_line = " ".join(f"#{t}" for t in tags) if tags else "Hali teg yo'q / Пока нет тегов"
    text = f"🏷️ <b>{name}</b> teglari / теги:\n\n{tags_line}"
    await safe_edit_or_caption(call, text, reply_markup=tags_view_kb(db_id, tags))
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("addtag:"))
async def cb_add_tag(call: CallbackQuery, state: FSMContext):
    db_id                        = int(call.data.split(":")[1])
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    tags = await get_file_tags(db_id)
    if len(tags) >= MAX_TAGS_PER_FILE:
        await call.answer(f"❌ Maksimum {MAX_TAGS_PER_FILE} ta teg!", show_alert=True)
        return
    await state.update_data(tag_file_id=db_id)
    await safe_edit_or_caption(
        call,
        "🏷️ Yangi teg nomini yozing (masalan: muhim) / Введите новый тег (например: важное):"
    )
    await TagState.waiting_tag.set()
    await call.answer()

@dp.message_handler(state=TagState.waiting_tag)
async def process_tag(message: Message, state: FSMContext):
    data  = await state.get_data()
    db_id = data.get("tag_file_id")
    await state.finish()
    if not db_id:
        await message.answer("❌ Xatolik yuz berdi, qaytadan urinib ko'ring.")
        return

    tag = sanitize_tag(message.text)
    if not tag:
        await message.answer("❌ Noto'g'ri teg nomi / Неверное имя тега. Qaytadan urining:")
        return

    ok   = await add_file_tag(db_id, tag)
    tags = await get_file_tags(db_id)
    if ok:
        await message.answer(
            f"✅ <b>#{tag}</b> qo'shildi! / добавлен!",
            parse_mode="HTML",
            reply_markup=tags_view_kb(db_id, tags)
        )
    elif len(tags) >= MAX_TAGS_PER_FILE:
        await message.answer(f"❌ Bu faylda allaqachon {MAX_TAGS_PER_FILE} ta teg bor!")
    else:
        await message.answer("❌ Xatolik yuz berdi / Произошла ошибка.")

@dp.callback_query_handler(lambda c: c.data.startswith("rmtag:"))
async def cb_remove_tag(call: CallbackQuery):
    parts                        = call.data.split(":", 2)
    db_id                        = int(parts[1])
    tag                          = parts[2]
    account_id, _, is_admin_flag = await check_file_access(call, db_id)
    if account_id is None and not is_admin_flag: return
    await remove_file_tag(db_id, tag)
    tags = await get_file_tags(db_id)
    await call.answer(f"❌ #{tag} olib tashlandi / удалён")
    try:
        await call.message.edit_reply_markup(reply_markup=tags_view_kb(db_id, tags))
    except Exception:
        pass

@dp.callback_query_handler(lambda c: c.data.startswith("tagcloud"))
async def cb_tagcloud(call: CallbackQuery):
    account_id    = await get_account_id(call.from_user.id)
    is_admin_flag = is_admin_mode(call.from_user.id)
    if not is_admin_flag and not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    tags_rows = await get_all_tags(account_id, is_admin_flag)
    if not tags_rows:
        await safe_edit(
            call,
            "🏷️ Hali teglar yo'q. Fayl teglariga o'ting va teg qo'shing! / Тегов пока нет.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Menyu", callback_data="menu")
            )
        )
        await call.answer()
        return
    await safe_edit(
        call,
        "🏷️ <b>Teglar / Теги:</b>\nTegni tanlang / Выберите тег:",
        reply_markup=tag_cloud_kb(tags_rows)
    )
    await call.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("searchtag:"))
async def cb_search_by_tag(call: CallbackQuery):
    if is_rate_limited(call.from_user.id):
        await call.answer("⏳ Sekinroq!")
        return
    account_id    = await get_account_id(call.from_user.id)
    is_admin_flag = is_admin_mode(call.from_user.id)
    if not is_admin_flag and not account_id:
        await call.answer("🔐 Avval kiring!", show_alert=True)
        return
    parts = call.data.split(":")
    tag   = parts[1]
    page  = int(parts[2])
    try: await call.message.delete()
    except Exception: pass
    await bot.send_message(call.message.chat.id, f"🏷️ <b>#{tag}</b> bo'yicha fayllar:", parse_mode="HTML")
    await show_files_page(
        call.message.chat.id, account_id, is_admin_flag,
        f"searchtag:{tag}", page,
        extra_where="sub.id IN (SELECT file_id FROM file_tags WHERE tag = $1)",
        extra_params=(tag,)
    )
    await call.answer()

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

    # Tejalgan joy: duplikat fayllar tufayli qancha joy tejaldi
    async with db_pool.acquire() as conn:
        if is_admin_flag:
            dup_row = await conn.fetchrow("""
                SELECT COUNT(*) AS dup_refs,
                       COALESCE(SUM(f.size), 0) AS dup_size
                FROM file_owners fo
                JOIN files f ON fo.file_id = f.id
                WHERE (SELECT COUNT(*) FROM file_owners fo2 WHERE fo2.file_hash = fo.file_hash) > 1
            """)
        else:
            dup_row = await conn.fetchrow("""
                SELECT COUNT(*) AS dup_refs,
                       COALESCE(SUM(f.size), 0) AS dup_size
                FROM file_owners fo
                JOIN files f ON fo.file_id = f.id
                WHERE fo.account_id = $1
                  AND (SELECT COUNT(*) FROM file_owners fo2 WHERE fo2.file_hash = fo.file_hash) > 1
            """, account_id)
    saved_mb = round((dup_row["dup_size"] or 0) / 1024 / 1024, 2) if dup_row else 0
    saved_gb = round(saved_mb / 1024, 3)
    saved_text = (
        f"\n♻️ Tejalgan joy: <b>{saved_mb} MB ({saved_gb} GB)</b> <i>(duplikatlar hisobiga)</i>"
        if saved_mb > 0 else ""
    )

    prefix = "Admin " if is_admin_flag else ""
    text   = (
        f"📊 <b>{prefix}Statistika</b>\n\n"
        + (f"👥 Hisoblar: <b>{acc_cnt}</b>\n" if is_admin_flag else "")
        + f"📄 Jami fayllar: <b>{stats['total']}</b>\n"
        f"💾 Hajm: <b>{mb} MB ({gb} GB)</b>{saved_text}\n\n"
        f"🎬 Videolar: <b>{stats['vid_cnt']}</b>\n"
        f"🖼️ Rasmlar: <b>{stats['photo_cnt']}</b>\n"
        f"🤖 APK/IPA: <b>{stats['app_cnt']}</b>\n"
        f"📄 Boshqalar: <b>{stats['other_cnt']}</b>\n\n"
        f"📌 Muhimlar: <b>{stats['pin_cnt']}</b>\n"
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
            "☁️ <b>Shaxsiy Bulut Xotira</b>\n\n🔐 Hisobga kirish / Войти:\n\n"
            "⚠️ <i>Login va parolingizni eslab qoling!</i>",
            parse_mode="HTML", reply_markup=auth_kb()
        )
        return
    username      = await get_account_username(account_id)
    is_admin_flag = is_admin_mode(message.from_user.id)
    await message.answer(
        f"☁️ <b>Shaxsiy Bulut Xotirangiz</b>\n\n"
        f"👤 Hisob / Аккаунт: <b>@{username}</b>\n"
        f"Menyudan tanlang / Выберите из меню:",
        parse_mode="HTML",
        reply_markup=user_menu_kb(show_admin=is_admin_flag)
    )

# ─── WEB APP (Telegram Mini App) ──────────────────────────────────────────────
# t.me/HugeCloudBot/app orqali ochiladigan brauzer interfeysi.
# Auth: Telegram.WebApp.initData (HMAC bilan tekshiriladi, alohida sessiya kerak emas)

from io import BytesIO

WEBAPP_UPLOAD_MAX = 45 * 1024 * 1024  # 🚀 aiogram v2 / Bot API upload chegarasi (~50MB), xavfsizlik zaxirasi bilan

# ─── Media token (thumbnail/preview uchun) ────────────────────────────────────
# <img src> va <video src> teglari HTTP header yubora olmaydi,
# shuning uchun qisqa muddatli HMAC token URL ga qo'shiladi.

MEDIA_TOKEN_SECRET = (os.getenv("MEDIA_TOKEN_SECRET") or BOT_TOKEN or "fallback_secret").encode()
MEDIA_TOKEN_TTL    = 20 * 60   # 20 daqiqa

def _make_media_token(account_id: int, file_db_id: int) -> str:
    """account_id:file_db_id:exp ni HMAC-SHA256 bilan imzolaydi"""
    exp = int(time.time()) + MEDIA_TOKEN_TTL
    payload = f"{account_id}:{file_db_id}:{exp}"
    sig = hmac.new(MEDIA_TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
    import base64
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

def _verify_media_token(token: str, file_db_id: int):
    """
    Token ni tekshiradi.
    To'g'ri bo'lsa -> account_id (int), noto'g'ri/eskirgan bo'lsa -> None
    """
    try:
        import base64
        padded = token + "=" * (-len(token) % 4)
        raw    = base64.urlsafe_b64decode(padded).decode()
        parts  = raw.split(":")
        if len(parts) != 4:
            return None
        acc_id_s, fid_s, exp_s, sig = parts
        if int(fid_s) != file_db_id:
            return None
        if time.time() > int(exp_s):
            return None
        payload  = f"{acc_id_s}:{fid_s}:{exp_s}"
        expected = hmac.new(MEDIA_TOKEN_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expected, sig):
            return None
        return int(acc_id_s)
    except Exception:
        return None

def verify_webapp_init_data(init_data: str, max_age: int = 86400):
    """
    Telegram WebApp initData ni HMAC-SHA256 bilan tekshiradi.
    To'g'ri bo'lsa -> user dict (id, first_name, ...), noto'g'ri/eskirgan bo'lsa -> None.
    """
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key      = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash   = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", 0))
    except ValueError:
        auth_date = 0
    if max_age and auth_date and (time.time() - auth_date) > max_age:
        return None
    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except Exception:
        return None

def _extract_init_data(request: "web.Request") -> str:
    return request.headers.get("X-Telegram-Init-Data") or request.query.get("init_data", "") or ""

async def webapp_auth(request: "web.Request"):
    """
    initData ni tekshiradi va (telegram_user_id, account_id, is_admin) qaytaradi.
    Muvaffaqiyatsiz bo'lsa web.HTTPException ko'taradi.
    """
    user = verify_webapp_init_data(_extract_init_data(request))
    if not user or "id" not in user:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "unauthorized"}), content_type="application/json")
    tg_id      = int(user["id"])
    account_id = await get_account_id(tg_id)
    if not account_id:
        raise web.HTTPForbidden(text=json.dumps({"error": "no_account"}), content_type="application/json")
    return tg_id, account_id, is_admin_mode(tg_id)

def _file_row_to_json(row, tags=None, account_id: int = 0):
    db_id    = row["id"]
    category = row["category"]
    # photo/video uchun token-based media URL generatsiya
    media_url = None
    if category in ("photo", "video") and account_id:
        token     = _make_media_token(account_id, db_id)
        media_url = f"/webapp/media/{db_id}?token={token}"
    return {
        "id": db_id,
        "name": row["file_name"],
        "category": category,
        "icon": get_icon(category),
        "size": row["size"] or 0,
        "date": format_date(row["date"]),
        "timestamp": row["date"],
        "folder": row["folder"],
        "pinned": bool(row["pinned"]),
        "owner": row["owner_name"],
        "tags": tags or [],
        "media_url": media_url,   # thumbnail/preview uchun (None bo'lsa ko'rsatilmaydi)
    }

async def webapp_get_files(account_id, is_admin, category=None, folder=None,
                            search=None, limit=30, offset=0):
    conditions, params, idx = [], [], 1

    if not is_admin:
        conditions.append(f"sub.account_id = ${idx}"); params.append(account_id); idx += 1

    if category and category != "all":
        if category == "apps":
            conditions.append(f"sub.category IN (${idx}, ${idx+1})")
            params.extend(["apk", "ipa"]); idx += 2
        elif category == "pinned":
            conditions.append(f"sub.pinned = ${idx}"); params.append(1); idx += 1
        elif category in ("video", "photo", "other"):
            conditions.append(f"sub.category = ${idx}"); params.append(category); idx += 1

    if folder:
        conditions.append(f"sub.folder = ${idx}"); params.append(folder); idx += 1

    if search:
        conditions.append(f"sub.file_name ILIKE ${idx}"); params.append(f"%{search}%"); idx += 1

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    params.extend([limit, offset])
    limit_idx, offset_idx = idx, idx + 1

    q = f"""
        SELECT *, COUNT(*) OVER() AS total_count
        FROM ({SELECT_SQL}) sub
        {where_clause}
        ORDER BY sub.pinned DESC, sub.date DESC
        LIMIT ${limit_idx} OFFSET ${offset_idx}
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(q, *params)
    total = rows[0]["total_count"] if rows else 0
    return rows, total

# ─── WebApp: HTML page ─────────────────────────────────────────────────────────

async def webapp_handler(request):
    return web.Response(text=WEBAPP_HTML, content_type="text/html", charset="utf-8")

# ─── WebApp: /webapp/files (+ /webapp/search alias) ───────────────────────────

async def api_files_handler(request):
    try:
        tg_id, account_id, is_admin = await webapp_auth(request)
    except web.HTTPException as exc:
        return exc
    q = request.query
    category = q.get("category") or "all"
    folder   = q.get("folder") or None
    search   = (q.get("search") or "").strip() or None
    try:
        page = max(0, int(q.get("page", 0)))
    except ValueError:
        page = 0
    limit  = 30
    offset = page * limit

    rows, total = await webapp_get_files(account_id, is_admin, category, folder, search, limit, offset)
    tags_map = await get_tags_map([r["id"] for r in rows]) if rows else {}
    files = [_file_row_to_json(r, tags_map.get(r["id"], []), account_id) for r in rows]

    return web.json_response({
        "files": files,
        "total": total,
        "page": page,
        "has_more": offset + len(rows) < total,
        "is_admin": is_admin,
    })

# ─── WebApp: /webapp/folders ───────────────────────────────────────────────────

async def api_folders_handler(request):
    try:
        await webapp_auth(request)
    except web.HTTPException as exc:
        return exc
    folders = await get_folders()
    result  = [{"name": "umumiy", "label": "📂 Umumiy"}]
    result += [{"name": f["name"], "label": f"📂 {f['name']}"} for f in folders if f["name"] != "umumiy"]
    return web.json_response({"folders": result})

# ─── WebApp: /webapp/download/<db_id> ─────────────────────────────────────────

async def api_download_handler(request):
    try:
        tg_id, account_id, is_admin = await webapp_auth(request)
    except web.HTTPException as exc:
        return exc
    try:
        db_id = int(request.match_info["file_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad_id"}, status=400)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_id, file_name, category, account_id FROM files WHERE id = $1", db_id
        )
    if not row:
        return web.json_response({"error": "not_found"}, status=404)
    if not is_admin and row["account_id"] != account_id:
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        tg_file = await bot.get_file(row["file_id"])
    except Exception as e:
        logger.error(f"webapp download get_file error: {e}")
        return web.json_response({"error": "telegram_error"}, status=502)

    file_url     = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    filename     = row["file_name"] or "file"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # 🚀 Streaming — Render Free 512MB RAM'da katta fayllarni xavfsiz uzatish uchun
    session = aiohttp.ClientSession()
    try:
        tg_resp = await session.get(file_url)
        if tg_resp.status != 200:
            await tg_resp.release()
            await session.close()
            return web.json_response({"error": "download_failed"}, status=502)

        resp = web.StreamResponse(status=200, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type,
        })
        if tg_resp.content_length:
            resp.content_length = tg_resp.content_length
        await resp.prepare(request)
        async for chunk in tg_resp.content.iter_chunked(64 * 1024):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        await session.close()

# ─── WebApp: /webapp/media/<db_id>?token=... (thumbnail / preview) ────────────
# <img src> va <video src> header yubora olmaydi — token URL da bo'ladi.
# Rasm uchun to'liq fayl qaytariladi (Telegram allaqachon compress qilgan).
# Video uchun birinchi 64KB ni stream qilamiz (thumbnail emas, lekin thumbnail
# generatsiya uchun server-side ffmpeg kerak — hozircha to'liq stream).

async def api_media_handler(request):
    try:
        db_id = int(request.match_info["file_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad_id"}, status=400)

    token = request.query.get("token", "")
    account_id = _verify_media_token(token, db_id)
    if not account_id:
        return web.Response(status=401, text="Unauthorized")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT file_id, file_name, category, account_id FROM files WHERE id = $1", db_id
        )
    if not row:
        return web.Response(status=404, text="Not found")

    # Admin bo'lmagan foydalanuvchi faqat o'z faylini ko'rishi mumkin
    is_adm = is_admin_mode(
        (await conn.fetchval(
            "SELECT telegram_user_id FROM telegram_bindings WHERE account_id = $1 LIMIT 1",
            account_id
        ) or 0) if False else 0   # optimizatsiya: token allaqachon account_id ni tasdiqlagan
    )
    # account_id tekshiruvi: token ichida saqlangan account_id faylnikiga mos kelishi kerak
    if row["account_id"] != account_id:
        # Admin ekanini DB dan tekshiramiz (kalit so'rov)
        async with db_pool.acquire() as conn2:
            tg_id = await conn2.fetchval(
                "SELECT telegram_user_id FROM telegram_bindings WHERE account_id = $1 LIMIT 1",
                account_id
            )
        if not tg_id or not is_admin_mode(int(tg_id)):
            return web.Response(status=403, text="Forbidden")

    try:
        tg_file = await bot.get_file(row["file_id"])
    except Exception as e:
        logger.error(f"media handler get_file error: {e}")
        return web.Response(status=502, text="Telegram error")

    file_url     = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    filename     = row["file_name"] or "file"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    session = aiohttp.ClientSession()
    try:
        tg_resp = await session.get(file_url)
        if tg_resp.status != 200:
            await tg_resp.release()
            await session.close()
            return web.Response(status=502, text="Download failed")

        # Inline ko'rish (Content-Disposition: inline)
        resp = web.StreamResponse(status=200, headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Type": content_type,
            "Cache-Control": "private, max-age=900",   # 15 daqiqa kesh
        })
        if tg_resp.content_length:
            resp.content_length = tg_resp.content_length
        await resp.prepare(request)
        async for chunk in tg_resp.content.iter_chunked(64 * 1024):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        await session.close()

# ─── WebApp: /webapp/upload ────────────────────────────────────────────────────

async def api_upload_handler(request):
    try:
        tg_id, account_id, is_admin = await webapp_auth(request)
    except web.HTTPException as exc:
        return exc

    reader  = await request.multipart()
    folder  = "umumiy"
    saved   = []

    while True:
        field = await reader.next()
        if field is None:
            break

        if field.name == "folder":
            val = (await field.read(decode=True)).decode(errors="ignore").strip()
            if val:
                folder = sanitize_filename(val)[:50] or "umumiy"
            continue

        if field.name != "file":
            continue

        original_name = field.filename or "nomsiz_fayl"
        safe_name     = sanitize_filename(original_name)
        ext           = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        category      = get_category(ext)

        chunks, size = [], 0
        while True:
            chunk = await field.read_chunk(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > WEBAPP_UPLOAD_MAX:
                return web.json_response(
                    {"error": "too_large", "max_mb": WEBAPP_UPLOAD_MAX // 1024 // 1024},
                    status=413
                )
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content:
            continue

        input_file = InputFile(BytesIO(content), filename=safe_name)
        try:
            if category == "video":
                sent    = await bot.send_video(tg_id, input_file, caption=f"📤 {safe_name}")
                file_id = sent.video.file_id
            elif category == "photo":
                sent    = await bot.send_photo(tg_id, input_file, caption=f"📤 {safe_name}")
                file_id = sent.photo[-1].file_id
            else:
                sent    = await bot.send_document(tg_id, input_file, caption=f"📤 {safe_name}")
                file_id = sent.document.file_id
        except Exception as e:
            logger.error(f"webapp upload send error: {e}")
            return web.json_response({"error": "telegram_send_failed"}, status=502)

        row_id, is_dup = await save_file(account_id, file_id, safe_name, category, size, file_data=content)
        # Webapp upload uchun folder ni ham yangilash (agar yangi fayl bo'lsa)
        if not is_dup and folder != "umumiy":
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE files SET folder = $1 WHERE id = $2", folder, row_id)
        saved.append({
            "id": row_id, "name": safe_name, "category": category,
            "icon": get_icon(category), "size": size, "folder": folder,
            "duplicate": is_dup,
        })

    if not saved:
        return web.json_response({"error": "no_file"}, status=400)
    return web.json_response({"ok": True, "files": saved})

# ─── WebApp: frontend (HTML + CSS + JS, bitta fayl) ─────────────────────────
# Video player, image viewer, grid/list view, pull-to-refresh, infinite scroll,
# drag & drop upload, folder navigation, dark/light theme support.

WEBAPP_HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>☁️ Cloud Drive</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#0f1115; --bg2:#171a21; --card:#1e222b; --border:#2a2f3a;
  --text:#eef1f6; --muted:#8a92a3; --accent:#4f8cff; --accent2:#3f74d9;
  --danger:#ff5c6c; --success:#33c46d; --radius:14px; --fab-bottom:22px;
}
[data-theme="light"]{
  --bg:#f0f2f7; --bg2:#ffffff; --card:#ffffff; --border:#e3e7ee;
  --text:#171a21; --muted:#6b7280; --accent:#3f74d9; --accent2:#2f5fc0;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;margin:0;padding:0;}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background:var(--bg);color:var(--text);padding-bottom:90px;min-height:100vh;
  transition:background .2s,color .2s;
}
/* ── Topbar ── */
.topbar{
  position:sticky;top:0;z-index:20;background:var(--bg2);
  border-bottom:1px solid var(--border);padding:10px 14px;
}
.topbar-row{display:flex;align-items:center;gap:8px;}
.title{font-size:17px;font-weight:700;display:flex;align-items:center;gap:6px;flex:1;}
.icon-btn{
  background:var(--card);border:1px solid var(--border);color:var(--text);
  width:36px;height:36px;border-radius:10px;display:flex;align-items:center;
  justify-content:center;font-size:16px;cursor:pointer;flex-shrink:0;
}
.search-row{display:flex;gap:8px;margin-top:10px;}
.search-input{
  flex:1;background:var(--card);border:1px solid var(--border);color:var(--text);
  border-radius:10px;padding:9px 12px;font-size:14px;outline:none;
}
.search-input::placeholder{color:var(--muted);}
.chips{
  display:flex;gap:6px;overflow-x:auto;margin-top:10px;padding-bottom:2px;
  scrollbar-width:none;
}
.chips::-webkit-scrollbar{display:none;}
.chip{
  flex:0 0 auto;background:var(--card);border:1px solid var(--border);color:var(--muted);
  padding:6px 12px;border-radius:20px;font-size:13px;white-space:nowrap;cursor:pointer;
  transition:all .15s;
}
.chip.active{background:var(--accent);color:#fff;border-color:var(--accent);}
/* ── PTR / Offline ── */
#offline-banner{
  display:none;background:var(--danger);color:#fff;text-align:center;
  font-size:13px;padding:6px;position:sticky;top:0;z-index:30;
}
#ptr{text-align:center;font-size:12px;color:var(--muted);height:0;overflow:hidden;transition:height .15s;}
/* ── Content ── */
.content{padding:12px 14px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;}
.list{display:flex;flex-direction:column;gap:8px;}
/* ── Cards ── */
.card{
  background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
  padding:12px;cursor:pointer;position:relative;overflow:hidden;
  transition:transform .1s,box-shadow .1s;
}
.card:active{transform:scale(.97);}
.grid .card{display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px;}
.list .card{display:flex;flex-direction:row;align-items:center;gap:12px;text-align:left;}
.f-icon{font-size:30px;line-height:1;}
.list .f-icon{font-size:26px;}
.f-name{
  font-size:13px;font-weight:600;word-break:break-word;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
.list .f-name{-webkit-line-clamp:1;flex:1;}
.f-meta{font-size:11px;color:var(--muted);}
.f-pin{position:absolute;top:6px;right:8px;font-size:13px;}
/* ── Thumbnail / Preview ── */
.thumb-wrap{
  width:100%;aspect-ratio:1;background:var(--border);border-radius:8px;
  overflow:hidden;display:flex;align-items:center;justify-content:center;
  position:relative;flex-shrink:0;
}
.grid .thumb-wrap{width:100%;}
.list .thumb-wrap{width:56px;height:56px;aspect-ratio:unset;border-radius:10px;}
.thumb-img{
  width:100%;height:100%;object-fit:cover;
  transition:opacity .3s;
}
.thumb-img.loading{opacity:0;}
.thumb-img.loaded{opacity:1;}
.thumb-placeholder{font-size:28px;line-height:1;}
.list .thumb-placeholder{font-size:24px;}
/* video badge */
.thumb-wrap .play-badge{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.35);font-size:22px;pointer-events:none;
}
.list .thumb-wrap .play-badge{font-size:16px;}
/* skeleton animation */
@keyframes skeletonPulse{0%,100%{opacity:.6}50%{opacity:.3}}
.thumb-skeleton{animation:skeletonPulse 1.4s ease-in-out infinite;}
/* ── Empty / Loading ── */
.empty{text-align:center;color:var(--muted);padding:60px 20px;font-size:14px;}
.loading{text-align:center;color:var(--muted);padding:30px;font-size:13px;}
#loadingMore{display:none;text-align:center;padding:14px;color:var(--muted);font-size:13px;}
/* ── FAB ── */
.fab{
  position:fixed;right:18px;bottom:var(--fab-bottom);width:56px;height:56px;border-radius:50%;
  background:var(--accent);color:#fff;border:none;font-size:26px;
  box-shadow:0 6px 18px rgba(0,0,0,.35);
  display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:25;
  transition:transform .15s;
}
.fab:active{transform:scale(.92);}
/* ── Dropzone ── */
#dropzone{
  position:fixed;inset:0;background:rgba(79,140,255,.15);border:3px dashed var(--accent);
  display:none;align-items:center;justify-content:center;font-size:16px;font-weight:600;
  color:var(--accent);z-index:40;backdrop-filter:blur(2px);
}
#dropzone.show{display:flex;}
/* ── Toast ── */
.toast{
  position:fixed;left:50%;bottom:100px;transform:translateX(-50%) translateY(10px);
  background:var(--card);border:1px solid var(--border);color:var(--text);
  padding:10px 16px;border-radius:10px;font-size:13px;z-index:60;opacity:0;
  transition:opacity .25s,transform .25s;pointer-events:none;
  max-width:80%;text-align:center;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0);}
/* ── Bottom Sheet ── */
#sheetBg{
  position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:50;
  display:none;align-items:flex-end;
}
#sheetBg.show{display:flex;}
#sheetContent{
  background:var(--bg2);border-radius:20px 20px 0 0;padding:20px 18px 34px;
  width:100%;max-height:70vh;overflow-y:auto;
}
#sheetContent h3{font-size:16px;margin-bottom:14px;word-break:break-word;}
.sheet-row{
  display:flex;justify-content:space-between;align-items:center;
  padding:9px 0;border-bottom:1px solid var(--border);font-size:14px;
}
.sheet-row:last-of-type{border:none;}
.sheet-row span{color:var(--muted);}
.btn{
  background:var(--accent);color:#fff;border:none;border-radius:10px;
  padding:11px 16px;font-size:14px;font-weight:600;cursor:pointer;
  transition:opacity .15s;
}
.btn:active{opacity:.8;}
.btn.secondary{background:var(--card);color:var(--text);border:1px solid var(--border);}
.btn.danger{background:var(--danger);}
.sheet-actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;}
.sheet-actions .btn{flex:1;min-width:100px;text-align:center;}

/* ══════════════════════════════════════════════════════════
   VIDEO PLAYER
══════════════════════════════════════════════════════════ */
#videoOverlay{
  position:fixed;inset:0;background:#000;z-index:200;
  display:none;flex-direction:column;
}
#videoOverlay.show{display:flex;}
#videoHeader{
  position:absolute;top:0;left:0;right:0;
  padding:env(safe-area-inset-top,12px) 12px 12px;
  background:linear-gradient(to bottom,rgba(0,0,0,.8),transparent);
  display:flex;align-items:center;gap:10px;z-index:10;
  transition:opacity .3s;
}
#videoHeader.hide{opacity:0;pointer-events:none;}
#videoTitle{flex:1;color:#fff;font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.v-btn{background:none;border:none;color:#fff;cursor:pointer;padding:6px;font-size:20px;line-height:1;flex-shrink:0;}
#mainVideo{width:100%;height:100%;object-fit:contain;}
#videoPlayOverlay{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  pointer-events:none;
}
#playIcon{
  width:68px;height:68px;border-radius:50%;
  background:rgba(255,255,255,.18);backdrop-filter:blur(8px);
  border:2px solid rgba(255,255,255,.35);
  display:flex;align-items:center;justify-content:center;
  font-size:28px;transition:opacity .2s;
}
#playIcon.hidden{opacity:0;}
#videoControls{
  position:absolute;bottom:0;left:0;right:0;
  padding:8px 14px env(safe-area-inset-bottom,20px);
  background:linear-gradient(to top,rgba(0,0,0,.8),transparent);
  z-index:10;transition:opacity .3s;
}
#videoControls.hide{opacity:0;pointer-events:none;}
#progressBar{
  height:4px;background:rgba(255,255,255,.3);border-radius:2px;
  margin-bottom:10px;cursor:pointer;position:relative;
}
#progressFill{height:100%;background:#fff;border-radius:2px;width:0%;transition:width .1s linear;}
#progressThumb{
  position:absolute;top:50%;right:0;transform:translateY(-50%);
  width:12px;height:12px;border-radius:50%;background:#fff;
  margin-right:-6px;display:none;
}
#progressBar:hover #progressThumb{display:block;}
.ctrl-row{display:flex;align-items:center;gap:10px;}
#timeDisplay{color:rgba(255,255,255,.75);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap;}
.v-spacer{flex:1;}
#volumeSlider{width:72px;accent-color:#fff;cursor:pointer;}
/* ── Image Viewer ── */
#imageOverlay{
  position:fixed;inset:0;background:rgba(0,0,0,.95);z-index:200;
  display:none;flex-direction:column;
}
#imageOverlay.show{display:flex;}
#imageHeader{
  display:flex;align-items:center;gap:10px;padding:14px 14px 8px;flex-shrink:0;
}
#imageTitle{flex:1;color:#fff;font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
#imageBody{flex:1;display:flex;align-items:center;justify-content:center;padding:8px 12px 20px;}
#imageEl{max-width:100%;max-height:100%;object-fit:contain;border-radius:8px;}
</style>
</head>
<body>
<div id="offline-banner">📵 Internet yo'q / Нет соединения</div>
<div id="ptr">↓ Yangilash uchun torting...</div>

<!-- ══ TOPBAR ══ -->
<div class="topbar">
  <div class="topbar-row">
    <button class="icon-btn" id="backBtn" style="display:none">◀</button>
    <div class="title" id="pageTitle">☁️ Cloud Drive</div>
    <button class="icon-btn" id="themeBtn" title="Tema">🌙</button>
    <button class="icon-btn" id="viewBtn" title="Ko'rinish">⊞</button>
  </div>
  <div class="search-row">
    <input class="search-input" id="searchInput" placeholder="🔍 Fayl qidirish..." autocomplete="off">
  </div>
  <div class="chips" id="chips">
    <div class="chip active" data-cat="all">📋 Barchasi</div>
    <div class="chip" data-cat="video">🎬 Video</div>
    <div class="chip" data-cat="photo">🖼️ Rasm</div>
    <div class="chip" data-cat="apps">🤖 APK/IPA</div>
    <div class="chip" data-cat="other">📄 Boshqa</div>
    <div class="chip" data-cat="pinned">📌 Muhim</div>
  </div>
</div>

<!-- ══ CONTENT ══ -->
<div class="content">
  <div id="fileList" class="list"></div>
  <div id="loadingMore">⏳ Yuklanmoqda...</div>
</div>

<!-- ══ FAB ══ -->
<input type="file" id="fileInput" multiple style="display:none">
<button class="fab" id="uploadFab">＋</button>

<!-- ══ DROPZONE ══ -->
<div id="dropzone">📂 Faylni bu yerga tashlang</div>

<!-- ══ TOAST ══ -->
<div class="toast" id="toast"></div>

<!-- ══ BOTTOM SHEET ══ -->
<div id="sheetBg">
  <div id="sheetContent"></div>
</div>

<!-- ══ VIDEO PLAYER ══ -->
<div id="videoOverlay">
  <div id="videoHeader">
    <button class="v-btn" id="videoClose">✕</button>
    <span id="videoTitle"></span>
    <a class="v-btn" id="videoDownload" href="#" download>⬇</a>
  </div>
  <video id="mainVideo" playsinline preload="metadata"></video>
  <div id="videoPlayOverlay">
    <div id="playIcon">▶</div>
  </div>
  <div id="videoControls">
    <div id="progressBar">
      <div id="progressFill"></div>
      <div id="progressThumb"></div>
    </div>
    <div class="ctrl-row">
      <button class="v-btn" id="playPauseBtn">▶</button>
      <span id="timeDisplay">0:00 / 0:00</span>
      <div class="v-spacer"></div>
      <span style="color:rgba(255,255,255,.6);font-size:14px;">🔊</span>
      <input type="range" id="volumeSlider" min="0" max="1" step="0.05" value="1">
    </div>
  </div>
</div>

<!-- ══ IMAGE VIEWER ══ -->
<div id="imageOverlay">
  <div id="imageHeader">
    <button class="v-btn" id="imageClose">✕</button>
    <span id="imageTitle"></span>
    <a class="v-btn" id="imageDownload" href="#" download>⬇</a>
  </div>
  <div id="imageBody">
    <img id="imageEl" src="" alt="">
  </div>
</div>

<script>
// ══════════════════════════════════════════════════════════
// CONFIG & STATE
// ══════════════════════════════════════════════════════════
const tg = window.Telegram?.WebApp;
tg?.ready(); tg?.expand();
const initData = tg?.initData || '';
const isDark = () => document.documentElement.getAttribute('data-theme') !== 'light';

const state = {
  category:'all', folder:null, search:'', page:0,
  hasMore:true, loading:false, viewMode:'list'
};

// ── Theme ──────────────────────────────────────────────────────────────────────
(function initTheme(){
  const saved = localStorage.getItem('cld_theme');
  const scheme = saved || (tg?.colorScheme === 'light' ? 'light' : 'dark');
  document.documentElement.setAttribute('data-theme', scheme);
  document.getElementById('themeBtn').textContent = scheme === 'light' ? '🌙' : '☀️';
})();
document.getElementById('themeBtn').onclick = () => {
  const next = isDark() ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('cld_theme', next);
  document.getElementById('themeBtn').textContent = next === 'light' ? '🌙' : '☀️';
};

// ── View mode toggle ───────────────────────────────────────────────────────────
document.getElementById('viewBtn').onclick = () => {
  state.viewMode = state.viewMode === 'list' ? 'grid' : 'list';
  document.getElementById('viewBtn').textContent = state.viewMode === 'list' ? '⊞' : '☰';
  const fl = document.getElementById('fileList');
  fl.className = state.viewMode;
};

// ── Offline banner ─────────────────────────────────────────────────────────────
window.addEventListener('online',  () => { document.getElementById('offline-banner').style.display='none'; resetAndLoad(); });
window.addEventListener('offline', () => { document.getElementById('offline-banner').style.display='block'; });

// ══════════════════════════════════════════════════════════
// TOAST
// ══════════════════════════════════════════════════════════
let toastTimer;
function toast(msg, ms=3000){
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

// ══════════════════════════════════════════════════════════
// API
// ══════════════════════════════════════════════════════════
async function api(path, opts={}){
  const res = await fetch(path, {
    ...opts,
    headers:{ 'X-Telegram-Init-Data': initData, ...(opts.headers||{}) }
  });
  const data = await res.json().catch(()=>({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ══════════════════════════════════════════════════════════
// FOLDERS (navbar breadcrumb)
// ══════════════════════════════════════════════════════════
let allFolders = [];
async function loadFolders(){
  try{
    const d = await api('/webapp/folders');
    allFolders = d.folders || [];
    buildFolderChips();
  }catch(e){ console.warn('folders:', e); }
}

function buildFolderChips(){
  // Faqat chips dan tashqari folder chip qo'shamiz
  const existing = document.querySelectorAll('.chip[data-folder]');
  existing.forEach(c => c.remove());
  const chips = document.getElementById('chips');
  allFolders.forEach(f => {
    const c = document.createElement('div');
    c.className = 'chip' + (state.folder === f.name ? ' active' : '');
    c.dataset.folder = f.name;
    c.textContent = f.label;
    c.onclick = () => { setFolder(f.name === state.folder ? null : f.name); };
    chips.appendChild(c);
  });
}

function setFolder(name){
  state.folder = name;
  document.querySelectorAll('.chip[data-folder]').forEach(c => {
    c.classList.toggle('active', c.dataset.folder === name);
  });
  const title = document.getElementById('pageTitle');
  title.textContent = name ? `📁 ${name}` : '☁️ Cloud Drive';
  resetAndLoad();
}

// ── Category chips ────────────────────────────────────────────────────────────
document.getElementById('chips').addEventListener('click', e => {
  const chip = e.target.closest('.chip[data-cat]');
  if (!chip) return;
  state.category = chip.dataset.cat;
  document.querySelectorAll('.chip[data-cat]').forEach(c => c.classList.toggle('active', c === chip));
  resetAndLoad();
});

// ── Search (debounced) ────────────────────────────────────────────────────────
let searchTimer;
document.getElementById('searchInput').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = e.target.value.trim(); resetAndLoad(); }, 350);
});

// ══════════════════════════════════════════════════════════
// FILE LIST
// ══════════════════════════════════════════════════════════
function fmtSize(bytes){
  if (!bytes) return '';
  const mb = bytes/1024/1024;
  return mb >= 1024 ? (mb/1024).toFixed(2)+' GB' : mb.toFixed(2)+' MB';
}
function escapeHtml(s){
  const d = document.createElement('div'); d.textContent = s||''; return d.innerHTML;
}

function buildThumb(f){
  // media_url mavjud bo'lsa thumbnail, aks holda emoji icon
  if (f.media_url){
    const isVideo = f.category === 'video';
    const img = document.createElement('img');
    img.className = 'thumb-img loading thumb-skeleton';
    img.loading = 'lazy';
    img.decoding = 'async';
    // video uchun poster o'rniga birinchi kadrni ko'rsatamiz (img orqali)
    // rasm uchun to'g'ridan-to'g'ri
    if (!isVideo) {
      img.src = f.media_url;
    } else {
      // Video thumbnail: video elementi bilan birinchi kadrni olish
      const vid = document.createElement('video');
      vid.src = f.media_url;
      vid.preload = 'metadata';
      vid.muted = true;
      vid.style.cssText = 'position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;';
      vid.addEventListener('loadedmetadata', () => {
        vid.currentTime = 0.5;
      }, {once:true});
      vid.addEventListener('seeked', () => {
        const c = document.createElement('canvas');
        c.width = 200; c.height = 200;
        try{
          const ctx = c.getContext('2d');
          ctx.drawImage(vid, 0, 0, 200, 200);
          img.src = c.toDataURL('image/jpeg', 0.7);
        }catch(e){
          img.src = f.media_url;  // fallback
        }
        vid.remove();
      }, {once:true});
      document.body.appendChild(vid);
      // Agar seeked kelmasa, media_url ni to'g'ridan-to'g'ri ishlatamiz
      setTimeout(() => { if (!img.src) img.src = f.media_url; }, 3000);
    }
    img.onload = () => { img.classList.remove('loading','thumb-skeleton'); img.classList.add('loaded'); };
    img.onerror = () => { img.replaceWith(buildIconEl(f.icon)); };

    const wrap = document.createElement('div');
    wrap.className = 'thumb-wrap';
    wrap.appendChild(img);
    if (isVideo){
      const badge = document.createElement('div');
      badge.className = 'play-badge';
      badge.textContent = '▶';
      wrap.appendChild(badge);
    }
    return wrap;
  }
  // Media_url yo'q — oddiy icon
  const wrap = document.createElement('div');
  wrap.className = 'thumb-wrap';
  wrap.appendChild(buildIconEl(f.icon));
  return wrap;
}

function buildIconEl(icon){
  const el = document.createElement('div');
  el.className = 'thumb-placeholder';
  el.textContent = icon;
  return el;
}

function renderFiles(files, append){
  const container = document.getElementById('fileList');
  if (!append) container.innerHTML = '';
  if (!append && !files.length){
    container.innerHTML = '<div class="empty">😔 Fayl topilmadi<br><small>Botga fayl yuboring</small></div>';
    return;
  }
  const frag = document.createDocumentFragment();
  files.forEach(f => {
    const card = document.createElement('div');
    card.className = 'card';

    const thumb = buildThumb(f);
    const infoDiv = document.createElement('div');
    infoDiv.className = 'f-info';
    infoDiv.style.cssText = 'flex:1;min-width:0';
    infoDiv.innerHTML = `
      <div class="f-name">${escapeHtml(f.name)}</div>
      <div class="f-meta">${fmtSize(f.size)}${f.size && f.date ? ' · ' : ''}${f.date||''}</div>
      ${f.tags?.length ? '<div class="f-meta" style="margin-top:2px">'+f.tags.slice(0,3).map(t=>'#'+t).join(' ')+'</div>' : ''}
    `;

    if (f.pinned){
      const pin = document.createElement('div');
      pin.className = 'f-pin';
      pin.textContent = '📌';
      card.appendChild(pin);
    }
    card.appendChild(thumb);
    card.appendChild(infoDiv);
    card.onclick = () => openFile(f);
    frag.appendChild(card);
  });
  container.appendChild(frag);
}

async function loadFiles(append=false){
  if (state.loading || (append && !state.hasMore)) return;
  state.loading = true;
  document.getElementById('loadingMore').style.display = 'block';
  try{
    const p = new URLSearchParams({
      category: state.category,
      folder: state.folder||'',
      search: state.search,
      page: state.page
    });
    const data = await api('/webapp/files?' + p);
    renderFiles(data.files||[], append);
    state.hasMore = data.has_more;
  }catch(e){
    toast('❌ ' + e.message);
    if (!append) document.getElementById('fileList').innerHTML = '<div class="empty">Xatolik yuz berdi</div>';
  }finally{
    state.loading = false;
    document.getElementById('loadingMore').style.display = 'none';
  }
}

function resetAndLoad(){
  state.page = 0; state.hasMore = true; loadFiles(false);
}

// ── Infinite scroll ───────────────────────────────────────────────────────────
window.addEventListener('scroll', () => {
  if ((window.innerHeight + window.scrollY) >= document.body.offsetHeight - 200){
    if (state.hasMore && !state.loading){ state.page++; loadFiles(true); }
  }
});

// ── Pull to refresh ───────────────────────────────────────────────────────────
let touchStartY=0, pulling=false;
window.addEventListener('touchstart', e => { if (!window.scrollY) touchStartY = e.touches[0].clientY; }, {passive:true});
window.addEventListener('touchmove',  e => {
  if (!window.scrollY && touchStartY){
    if (e.touches[0].clientY - touchStartY > 60 && !pulling){
      pulling = true;
      document.getElementById('ptr').style.height = '30px';
    }
  }
},{passive:true});
window.addEventListener('touchend', () => {
  if (pulling){
    document.getElementById('ptr').style.height = '0';
    pulling = false; resetAndLoad(); loadFolders();
  }
  touchStartY = 0;
},{passive:true});

// ══════════════════════════════════════════════════════════
// VIDEO PLAYER
// ══════════════════════════════════════════════════════════
const videoOverlay = document.getElementById('videoOverlay');
const mainVideo    = document.getElementById('mainVideo');
const playIcon     = document.getElementById('playIcon');
const progressFill = document.getElementById('progressFill');
const progressBar  = document.getElementById('progressBar');
const timeDisplay  = document.getElementById('timeDisplay');
const playPauseBtn = document.getElementById('playPauseBtn');
let controlsTimer;

function fmtTime(s){
  s = Math.floor(s||0);
  const m = Math.floor(s/60); const sec = (s%60).toString().padStart(2,'0');
  return `${m}:${sec}`;
}

function showVideoControls(){
  document.getElementById('videoHeader').classList.remove('hide');
  document.getElementById('videoControls').classList.remove('hide');
  clearTimeout(controlsTimer);
  controlsTimer = setTimeout(() => {
    if (!mainVideo.paused){
      document.getElementById('videoHeader').classList.add('hide');
      document.getElementById('videoControls').classList.add('hide');
    }
  }, 3500);
}

async function openVideo(f){
  document.getElementById('videoTitle').textContent = f.name;
  const dlBtn = document.getElementById('videoDownload');
  dlBtn.href = '#'; dlBtn.download = f.name;
  dlBtn.onclick = (e) => { e.preventDefault(); downloadFile(f); };

  mainVideo.pause();
  mainVideo.src = '';
  videoOverlay.classList.add('show');
  showVideoControls();

  // Video header bilan fetch qilib blob URL yaratish
  // (media_url token bilan to'g'ridan-to'g'ri ham ishlaydi, lekin katta fayllar uchun
  //  download endpoint streaming uchun optimallashtirilgan)
  try {
    const res = await fetch('/webapp/download/' + f.id, {
      headers: { 'X-Telegram-Init-Data': initData }
    });
    if (!res.ok) throw new Error('Yuklab bo\'lmadi');
    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    mainVideo.src = blobUrl;
    mainVideo.onended = () => URL.revokeObjectURL(blobUrl);
    mainVideo.play().catch(()=>{});
  } catch(e) {
    toast('❌ Video yuklanmadi: ' + e.message);
    videoOverlay.classList.remove('show');
  }
}

document.getElementById('videoClose').onclick = () => {
  mainVideo.pause();
  if (mainVideo.src && mainVideo.src.startsWith('blob:')) URL.revokeObjectURL(mainVideo.src);
  mainVideo.src = '';
  videoOverlay.classList.remove('show');
};

mainVideo.addEventListener('timeupdate', () => {
  const pct = mainVideo.duration ? (mainVideo.currentTime / mainVideo.duration) * 100 : 0;
  progressFill.style.width = pct + '%';
  timeDisplay.textContent = fmtTime(mainVideo.currentTime) + ' / ' + fmtTime(mainVideo.duration);
});

mainVideo.addEventListener('play',  () => { playPauseBtn.textContent='⏸'; playIcon.classList.add('hidden'); });
mainVideo.addEventListener('pause', () => { playPauseBtn.textContent='▶'; playIcon.classList.remove('hidden'); });
mainVideo.addEventListener('ended', () => { playPauseBtn.textContent='▶'; playIcon.classList.remove('hidden'); });

function togglePlay(){
  if (mainVideo.paused) mainVideo.play().catch(()=>{});
  else mainVideo.pause();
  showVideoControls();
}
mainVideo.onclick = () => { togglePlay(); };
playPauseBtn.onclick = (e) => { e.stopPropagation(); togglePlay(); };
videoOverlay.addEventListener('touchstart', showVideoControls, {passive:true});
videoOverlay.addEventListener('mousemove',  showVideoControls);

// Progress seek
progressBar.addEventListener('click', e => {
  if (!mainVideo.duration) return;
  const r = progressBar.getBoundingClientRect();
  mainVideo.currentTime = ((e.clientX - r.left) / r.width) * mainVideo.duration;
  showVideoControls();
});

// Volume
document.getElementById('volumeSlider').addEventListener('input', e => {
  mainVideo.volume = +e.target.value;
});

// ══════════════════════════════════════════════════════════
// IMAGE VIEWER
// ══════════════════════════════════════════════════════════
const imageOverlay = document.getElementById('imageOverlay');

async function openImage(f){
  document.getElementById('imageTitle').textContent = f.name;
  const dlBtn = document.getElementById('imageDownload');
  dlBtn.href = '#'; dlBtn.download = f.name;
  dlBtn.onclick = (e) => { e.preventDefault(); downloadFile(f); };

  const imgEl = document.getElementById('imageEl');
  imgEl.src = '';
  imageOverlay.classList.add('show');

  // media_url mavjud bo'lsa (token bilan) — to'g'ridan-to'g'ri ishlatamiz
  // aks holda fetch+blob
  if (f.media_url) {
    imgEl.src = f.media_url;
  } else {
    try {
      const res = await fetch('/webapp/download/' + f.id, {
        headers: { 'X-Telegram-Init-Data': initData }
      });
      if (!res.ok) throw new Error('Yuklab bo\'lmadi');
      const blob = await res.blob();
      const blobUrl = URL.createObjectURL(blob);
      imgEl.src = blobUrl;
      imgEl.onload = () => {}; // keep blob alive
    } catch(e) {
      toast('❌ Rasm yuklanmadi: ' + e.message);
      imageOverlay.classList.remove('show');
    }
  }
}
document.getElementById('imageClose').onclick = () => {
  imageOverlay.classList.remove('show');
  const imgEl = document.getElementById('imageEl');
  if (imgEl.src && imgEl.src.startsWith('blob:')) URL.revokeObjectURL(imgEl.src);
  imgEl.src = '';
};

// ══════════════════════════════════════════════════════════
// OPEN FILE (dispatcher)
// ══════════════════════════════════════════════════════════
function openFile(f){
  if (f.category === 'video') { openVideo(f); return; }
  if (f.category === 'photo') { openImage(f); return; }
  openFileSheet(f);
}

// ══════════════════════════════════════════════════════════
// BOTTOM SHEET
// ══════════════════════════════════════════════════════════
function openFileSheet(f){
  const bg = document.getElementById('sheetBg');
  const content = document.getElementById('sheetContent');
  const isMedia = f.category === 'video' || f.category === 'photo';
  content.innerHTML = `
    <h3>${f.icon} ${escapeHtml(f.name)}</h3>
    <div class="sheet-row"><span>Hajmi</span><b>${fmtSize(f.size)||'—'}</b></div>
    <div class="sheet-row"><span>Sana</span><b>${f.date||'—'}</b></div>
    <div class="sheet-row"><span>Papka</span><b>${escapeHtml(f.folder||'umumiy')}</b></div>
    ${f.tags?.length ? `<div class="sheet-row"><span>Teglar</span><b>${f.tags.map(t=>'#'+t).join(' ')}</b></div>` : ''}
    <div class="sheet-actions">
      ${isMedia ? `<button class="btn" id="openMediaBtn">${f.category==='video'?'▶ Ko\'rish':'🖼 Ko\'rish'}</button>` : ''}
      <button class="btn secondary" id="dlBtn">⬇ Yuklab olish</button>
      <button class="btn secondary" id="closeSheet">✕ Yopish</button>
    </div>
  `;
  if (isMedia) document.getElementById('openMediaBtn').onclick = () => { closeSheet(); openFile(f); };
  document.getElementById('dlBtn').onclick = () => downloadFile(f);
  document.getElementById('closeSheet').onclick = closeSheet;
  bg.classList.add('show');
}

function closeSheet(){ document.getElementById('sheetBg').classList.remove('show'); }
document.getElementById('sheetBg').addEventListener('click', e => {
  if (e.target.id === 'sheetBg') closeSheet();
});

async function downloadFile(f){
  toast('⬇️ Yuklanmoqda...');
  try{
    const res = await fetch('/webapp/download/' + f.id, {
      headers:{ 'X-Telegram-Init-Data': initData }
    });
    if (!res.ok) throw new Error('Yuklab bo\'lmadi');
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = f.name;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    closeSheet();
    toast('✅ Yuklandi: ' + f.name);
  }catch(e){ toast('❌ ' + e.message); }
}

// ══════════════════════════════════════════════════════════
// UPLOAD
// ══════════════════════════════════════════════════════════
const fileInput = document.getElementById('fileInput');
document.getElementById('uploadFab').onclick = () => fileInput.click();
fileInput.onchange = () => { uploadFiles(fileInput.files); fileInput.value=''; };

const dropzone = document.getElementById('dropzone');
['dragenter','dragover'].forEach(evt =>
  window.addEventListener(evt, e => { e.preventDefault(); dropzone.classList.add('show'); })
);
['dragleave','drop'].forEach(evt =>
  window.addEventListener(evt, e => {
    e.preventDefault();
    if (evt === 'drop' && e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
    dropzone.classList.remove('show');
  })
);

async function uploadFiles(fileList){
  for (const file of fileList){
    toast('📤 Yuklanmoqda: ' + file.name);
    const fd = new FormData();
    fd.append('file', file);
    fd.append('folder', state.folder || 'umumiy');
    try{
      const res = await fetch('/webapp/upload', {
        method:'POST',
        headers:{ 'X-Telegram-Init-Data': initData },
        body: fd
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Xatolik');
      const msg = data.files?.[0]?.duplicate
        ? '✅ ' + file.name + ' saqlandi! (mavjud edi)'
        : '✅ ' + file.name + ' saqlandi!';
      toast(msg);
    }catch(e){ toast('❌ ' + file.name + ': ' + e.message); }
  }
  resetAndLoad();
}

// ══════════════════════════════════════════════════════════
// INIT
// ══════════════════════════════════════════════════════════
loadFolders();
loadFiles(false);
</script>
</body>
</html>
"""

# ─── Startup ──────────────────────────────────────────────────────────────────

async def keep_alive_task():
    """
    🚀 Render Free tarifida servis ~15 daqiqa tashqi HTTP so'rovsiz qolsa uxlab qoladi.
    Telegram getUpdates (polling) chiquvchi so'rov bo'lgani uchun buni hisobga olmaydi —
    natijada bot polling to'xtab qoladi, lekin /webapp'ga birov kirsa qayta uyg'onadi.
    Shu sabab o'z-o'ziga davriy ravishda HTTP so'rov yuborib, servisni doim uyg'oq ushlab turamiz.
    """
    await asyncio.sleep(30)  # server to'liq ko'tarilishini kutamiz
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(f"{WEBAPP_BASE_URL}/", timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    logger.info(f"🔄 Keep-alive ping: {resp.status}")
            except Exception as e:
                logger.warning(f"⚠️ Keep-alive ping xato: {e}")
            await asyncio.sleep(600)  # har 10 daqiqada

async def on_startup(dp):
    # 🛠️ Eski/qoldiq webhook bo'lsa o'chiramiz — aks holda polling doimiy
    # "Conflict: terminated by other getUpdates request" xatosi bilan yiqiladi
    # va bot Telegram xabarlariga umuman javob bermay qoladi.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Webhook tozalandi (agar mavjud bo'lsa), polling rejimi faollashtirildi")
    except Exception as e:
        logger.warning(f"⚠️ delete_webhook xato: {e}")

    await create_db()

    async def health(request):
        return web.Response(text="OK")

    app = web.Application(client_max_size=WEBAPP_UPLOAD_MAX + 1024 * 1024)  # +1MB — multipart overhead

    # Health-check
    app.router.add_get("/", health)

    # Web App (Mini App)
    app.router.add_get("/webapp", webapp_handler)
    app.router.add_get("/webapp/files", api_files_handler)
    app.router.add_get("/webapp/search", api_files_handler)   # alias, "search" query orqali ishlaydi
    app.router.add_get("/webapp/folders", api_folders_handler)
    app.router.add_get("/webapp/download/{file_id}", api_download_handler)
    app.router.add_get("/webapp/media/{file_id}",    api_media_handler)
    app.router.add_post("/webapp/upload", api_upload_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    port   = int(os.getenv("PORT", 8000))
    site   = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ Bot ishga tushdi! Port: {port} | Web App: {WEBAPP_BASE_URL}/webapp")

    # Keep-alive fon vazifasi sifatida ishga tushiriladi (asosiy pollingni bloklamaydi)
    asyncio.create_task(keep_alive_task())

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)
