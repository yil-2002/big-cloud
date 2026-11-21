"""🔧 SQLite pragmalari: WAL, ``busy_timeout``, ``synchronous``, ``foreign_keys``.

Audit muammosi: ``journal_mode=delete`` ostida bot va API bir vaqtda yozganda
``database is locked`` xatosi yuzaga kelardi. Bundan tashqari
``PRAGMA foreign_keys=ON`` avval **faqat** ``init_db`` ichidagi bitta ulanishga
qo'yilar edi — pragma ulanish darajasida bo'lgani uchun qolgan (pool'dagi)
ulanishlarda tashqi kalitlar umuman tekshirilmasdi.

Bu modul ikkala holatni ham **xatti-harakat** orqali tekshiradi: qiymatni
bazadan qayta o'qiydi va FK haqiqatan kuchga kirganini buzilgan yozuv bilan
sinaydi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config import Settings, get_settings
from db import database
from db.models import File, User
from tests.conftest import make_file

pytestmark = pytest.mark.asyncio


async def _pragma(target: Any, name: str) -> Any:
    """``PRAGMA <name>`` qiymatini o'qish (sessiya yoki ulanishdan)."""
    connection = target if hasattr(target, "exec_driver_sql") else await target.connection()
    return (await connection.exec_driver_sql(f"PRAGMA {name}")).scalar()


async def test_wal_journal_mode_is_active(session: Any, settings: Settings) -> None:
    """Fayl bazasida ``journal_mode`` haqiqatan ``wal`` bo'ladi."""
    assert settings.db.journal_mode == "WAL", "sukut bo'yicha WAL kutiladi"
    mode = str(await _pragma(session, "journal_mode")).lower()
    assert mode == "wal", f"bazadan qaytgan qiymat: {mode}"


async def test_wal_sidecar_file_appears(session: Any, storage: Any, settings: Settings) -> None:
    """WAL yon fayli (``-wal``) paydo bo'ladi — operator uchun ko'rinadigan dalil."""
    db_path = Path(settings.db.url.split(":///", 1)[-1])
    assert db_path.exists(), "baza fayli yaratilishi kerak"

    user = (await storage.get_or_create_user(telegram_id=555_101))[0]
    await make_file(storage, user.telegram_id, name="wal.mp4")
    await storage.session.commit()

    assert db_path.with_name(db_path.name + "-wal").exists(), "-wal fayli yo'q"


async def test_busy_timeout_and_synchronous_are_applied(session: Any, settings: Settings) -> None:
    """``busy_timeout`` va ``synchronous`` sozlamalardagi qiymatga teng."""
    assert int(await _pragma(session, "busy_timeout")) == settings.db.busy_timeout
    # synchronous: 0=OFF 1=NORMAL 2=FULL 3=EXTRA
    numeric = {"OFF": 0, "NORMAL": 1, "FULL": 2, "EXTRA": 3}
    assert int(await _pragma(session, "synchronous")) == numeric[settings.db.synchronous]


async def test_foreign_keys_enabled_on_every_connection(session: Any) -> None:
    """FK pool'dagi **har bir** ulanishda yoqilgan (avval faqat bittasida edi)."""
    assert int(await _pragma(session, "foreign_keys")) == 1

    engine = database.get_engine()
    async with engine.connect() as second:
        assert int(await _pragma(second, "foreign_keys")) == 1, "ikkinchi ulanishda FK o'chiq"


async def test_foreign_keys_are_actually_enforced(session: Any) -> None:
    """Yoqilgan FK haqiqatan ishlaydi: yetim yozuv ``IntegrityError`` beradi."""
    with pytest.raises(IntegrityError):
        session.add(
            File(
                user_id=999_999_999,  # bunday foydalanuvchi yo'q
                file_id="AgAD-orphan",
                file_unique_id="uniq-orphan",
                file_name="orphan.mp4",
                file_size=1,
            )
        )
        await session.flush()
    await session.rollback()


async def test_cascade_delete_removes_files_with_user(storage: Any) -> None:
    """``ondelete=CASCADE`` FK yoqilganda haqiqatan ishlaydi."""
    user = (await storage.get_or_create_user(telegram_id=555_002))[0]
    record = await make_file(storage, user.telegram_id, name="cascade.mp4")
    await storage.session.commit()
    file_id = record.id

    await storage.session.delete(user)
    await storage.session.commit()
    # ORM identity-map keshini tozalaymiz — aks holda o'chirilgan qator
    # xotiradan qaytadi va test yolg'on "muvaffaqiyat" ko'rsatadi.
    storage.session.expire_all()

    remaining = await storage.session.scalar(select(File).where(File.id == file_id))
    assert remaining is None, "fayl user bilan o'chirilmadi (CASCADE ishlamadi)"


def test_pragma_defaults_are_production_safe() -> None:
    """Sukut qiymatlar: WAL, 5000ms, NORMAL."""
    get_settings.cache_clear()
    settings = get_settings()
    assert (settings.db.journal_mode, settings.db.busy_timeout, settings.db.synchronous) == (
        "WAL",
        5000,
        "NORMAL",
    )


def test_invalid_pragma_values_are_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Noto'g'ri pragma qiymati ishga tushishda rad etiladi (jim buzilmaydi)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'x.sqlite3'}")
    monkeypatch.setenv("BOT_TOKEN", "1:A" + "x" * 33)

    for env, value in (("DB_JOURNAL_MODE", "SUPERFAST"), ("DB_SYNCHRONOUS", "MAYBE")):
        monkeypatch.setenv(env, value)
        get_settings.cache_clear()
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.delenv(env)

    monkeypatch.setenv("DB_BUSY_TIMEOUT", "-1")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings()
    get_settings.cache_clear()


def test_pragmas_are_skipped_for_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    """PostgreSQL DSN'da SQLite pragmalari qo'llanmaydi (listener o'rnatilmaydi)."""
    engine = MagicMock()

    settings = get_settings()
    monkeypatch.setattr(settings.db, "url", "postgresql+asyncpg://u:p@localhost/db")
    database._apply_sqlite_pragmas(engine)
    assert engine.mock_calls == [], "SQLite bo'lmagan backend'da hech narsa qilmasligi kerak"
    assert settings.db.is_sqlite is False


def test_user_model_is_importable_for_fk_checks() -> None:
    """FK nishoni (``users.telegram_id``) modelda mavjud — migratsiya uchun dalil."""
    column = File.__table__.columns["user_id"]
    assert any(fk.column.table.name == User.__tablename__ for fk in column.foreign_keys)
