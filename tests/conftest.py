"""Umumiy test fixture'lari.

Muhim: ``config.get_settings`` ``lru_cache`` bilan keshlanadi, shuning uchun
barcha env o'zgaruvchilari **importdan oldin** o'rnatiladi. Har test o'zining
SQLite bazasini oladi (``sqlite+aiosqlite:///<tmp>``) — testlar bir-biriga
ta'sir qilmaydi va tashqi servis (Postgres/Redis/Telegram) talab qilinmaydi.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Loyiha ildizini sys.path'ga qo'shamiz (pytest rootdir'dan ishga tushadi).
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- production .env testlarga ta'sir qilmasin -------------------------------
# VPS'da `/root/big-cloud/.env` bor. Uni testlar o'qib qolsa, sozlamalar
# (masalan ``ARCHIVE_ENABLED=false``) natijani o'zgartiradi va testlar
# mashinaga bog'liq bo'lib qoladi. Shu sababli dotenv manbasi faqat ataylab
# ``LOAD_DOTENV=1`` berilganda yoqiladi.
from pydantic_settings import BaseSettings

_orig_customise_sources = BaseSettings.settings_customise_sources


@classmethod
def _test_customise_sources(
    cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
):
    if os.environ.get("LOAD_DOTENV") != "1":
        return (init_settings, env_settings, file_secret_settings)
    return _orig_customise_sources(
        settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    )


BaseSettings.settings_customise_sources = _test_customise_sources

# --- env: importdan OLDIN ---------------------------------------------------
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("BOT_USERNAME", "Huge_cloud_bot")
os.environ.setdefault("ADMIN_ID", "111")
os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("ARCHIVE_CHANNEL_ID", "-1001234567890")
os.environ.setdefault("AUTH_REQUIRED", "true")
os.environ.setdefault("STORAGE_QUOTA", "0")            # cheksiz
os.environ.setdefault("APP_LANGUAGE", "uz")
os.environ.setdefault("REDIS_REQUIRED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

# ``main.py`` kabi: barcha matnlarni bir marta o'rnatamiz. Middleware testlari
# ``bot.texts.auth_gate_title`` kabi funksiyalarni chaqiradi — ular importdan
# keyin mavjud bo'lishi kerak (aks holda AttributeError).
from bot.texts_hugecloud import install_language
from db import database

install_language(os.environ.get("APP_LANGUAGE", "uz"))

from config import Settings, get_settings
from db.models import File, User
from services.storage import StorageService


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Har test uchun alohida SQLite fayli bilan sozlamalar."""
    db_file = tmp_path / "test.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    get_settings.cache_clear()
    # Engine/factory singletonlarini tozalaymiz (oldingi testdan qolmasin).
    database._engine = None
    database._session_factory = None
    return get_settings()


@pytest.fixture()
async def session(settings: Settings):
    """Jadvallar yaratilgan async sessiya.

    Haqiqiy ``init_db`` ishlatiladi: ``database._engine`` test bazasiga
    bog'lanadi, shuning uchun API ``TestClient`` ham (``get_session_factory``
    orqali) aynan shu bazani ko'radi.
    """
    await database.init_db(create_tables=True)
    factory = database.get_session_factory()
    async with factory() as session:
        yield session

    await database.dispose_engine()


@pytest.fixture()
def storage(session: Any, settings: Settings) -> StorageService:
    """StorageService (sessiya bilan bog'langan)."""
    return StorageService(session, settings=settings)


@pytest.fixture()
async def user(storage: StorageService) -> User:
    """Oddiy (tasdiqlangan) foydalanuvchi.

    6-bosqich darvozasi (``is_authenticated``) API'ni yopadi — shu sabab
    test hisobini sukut bo'yicha tasdiqlangan holatda tayyorlaymiz.
    Darvozaning o'zini tekshiradigan testlar bayroqni qo'lda o'chiradi.
    """
    created, _ = await storage.get_or_create_user(
        telegram_id=777, username="tester", first_name="Tester", language_code="uz"
    )
    if not created.is_authenticated:
        created.is_authenticated = True
        storage.session.add(created)
        await storage.session.commit()
    return created


@pytest.fixture()
async def admin_user(storage: StorageService, settings: Settings) -> User:
    """Administrator (``ADMIN_ID``)."""
    created, _ = await storage.get_or_create_user(
        telegram_id=111, username="admin", first_name="Admin"
    )
    return created


async def make_file(
    storage: StorageService,
    telegram_id: int,
    *,
    name: str,
    size: int = 1024,
    unique_id: str | None = None,
    category: str | None = None,
    pinned: bool = False,
    archive_message_id: int | None = None,
) -> File:
    """Test faylini yaratish yordamchisi."""
    stored = await storage.add_file(
        telegram_id=telegram_id,
        file_id=f"AgAD-{unique_id or name}",
        file_unique_id=unique_id or f"uniq-{name}",
        file_name=name,
        file_size=size,
    )
    record = stored.record
    if category:
        from db.models import FileCategory

        record.category = FileCategory(category)
    if pinned:
        record.is_pinned = True
    if archive_message_id is not None:
        record.archive_message_id = archive_message_id
    if category or pinned or archive_message_id is not None:
        storage.session.add(record)
        await storage.session.commit()
        await storage.session.refresh(record)
    return record


@pytest.fixture()
def client(settings: Settings):
    """FastAPI ``TestClient`` — haqiqiy router'lar, test bazasi bilan.

    Lifespan ``init_db`` ni qayta chaqiradi, lekin engine allaqachon test
    bazasiga bog'langan (yuqoridagi ``session`` fixture'i bilan bir xil fayl).
    """
    from fastapi.testclient import TestClient

    from api.app import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def init_data(settings: Settings):
    """Mini App ``initData`` yasaduvchi funksiya (bot_token bilan imzolanadi)."""
    from api.security import build_init_data

    def factory(telegram_id: int = 777, **kwargs: Any) -> str:
        return build_init_data(
            telegram_id=telegram_id,
            bot_token=settings.bot_token,
            **kwargs,
        )

    return factory


@pytest.fixture(scope="session")
def shared_dispatcher(tmp_path_factory: pytest.TempPathFactory):
    """Butun sessiya uchun **bitta** dispetcher.

    ``aiogram`` router'lari modul darajasidagi singletonlar: bir marta
    biriktirilgan router ikkinchi dispetcherga qo'shilmaydi
    (``RuntimeError: Router is already attached``). Shuning uchun bot yig'ilishini
    tekshiruvchi testlar ham, webhook testlari ham shu bitta nusxani bo'lishadi.
    """
    import os

    from bot.setup import build_dispatcher
    from config import get_settings

    db_path = tmp_path_factory.mktemp("shared-dispatcher") / "shared.sqlite3"
    previous_db = os.environ.get("DATABASE_URL")
    previous_password = os.environ.get("BOT_PASSWORD")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    # Legacy parol darvozasi middleware'ini ham ko'rish uchun yoqamiz.
    os.environ["BOT_PASSWORD"] = "test-parol"
    get_settings.cache_clear()
    database._engine = None
    database._session_factory = None
    try:
        dispatcher = build_dispatcher(get_settings())
    finally:
        if previous_db is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_db
        if previous_password is None:
            os.environ.pop("BOT_PASSWORD", None)
        else:
            os.environ["BOT_PASSWORD"] = previous_password
        get_settings.cache_clear()
        database._engine = None
        database._session_factory = None
    return dispatcher


# --------------------------------------------------------------------------- #
# Webhook testlari uchun umumiy qiymatlar
# --------------------------------------------------------------------------- #
#: Webhook marshruti (``WEBHOOK_PATH``) — testlar va real konfiguratsiya bir xil.
WEBHOOK_PATH = "/api/webhook"
#: Webhook sekret sarlavhasi qiymati.
WEBHOOK_SECRET = "maxfiy-token-123"


@pytest.fixture()
def webhook_settings(settings: Settings) -> Settings:
    """Webhook rejimi yoqilgan sozlamalar nusxasi (bir nechta test fayl uchun)."""
    settings.api.webhook_enabled = True
    settings.api.webhook_url = "https://hugecloud.example.com"
    settings.api.webhook_path = WEBHOOK_PATH
    settings.api.webhook_secret = WEBHOOK_SECRET
    return settings
