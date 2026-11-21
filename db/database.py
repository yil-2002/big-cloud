"""Async SQLAlchemy engine, session factory and declarative base.

The module exposes a tiny, dependency-injection friendly API:

* :func:`init_db`            - create the engine / session factory (+ tables).
* :func:`get_engine`         - the lazily created :class:`AsyncEngine`.
* :func:`get_session_factory`- the ``async_sessionmaker[AsyncSession]``.
* :func:`session_scope`      - ``async with`` helper with commit/rollback.
* :func:`dispose_engine`     - graceful shutdown hook.

Engine options differ between PostgreSQL (pooling) and SQLite (single file /
in-memory, ``check_same_thread=False``), which is handled transparently.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "Base",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "init_db",
    "session_scope",
]

#: Deterministic constraint names make Alembic autogenerate diffs stable.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(url: str) -> dict[str, Any]:
    """Build backend specific ``create_async_engine`` keyword arguments."""
    settings = get_settings()
    kwargs: dict[str, Any] = {"echo": settings.db.echo, "future": True}

    if url.startswith("sqlite"):
        # SQLite: no pooling knobs, but in-memory DBs need a shared connection.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in url or url.endswith("://"):
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(
            pool_size=settings.db.pool_size,
            max_overflow=settings.db.max_overflow,
            pool_timeout=settings.db.pool_timeout,
            pool_recycle=settings.db.pool_recycle,
            pool_pre_ping=settings.db.pool_pre_ping,
        )
    return kwargs


def _ensure_sqlite_directory(url: str) -> None:
    """Create the parent folder of a SQLite file (``./data/…`` may not exist)."""
    if "sqlite" not in url:
        return
    from pathlib import Path

    path = url.split(":///", 1)[-1]
    if not path or path == ":memory:":
        return
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Har bir **yangi SQLite ulanishida** pragmalarni o'rnatadi.

    Nega har ulanishda? ``busy_timeout``, ``synchronous`` va ``foreign_keys``
    — ulanish darajasidagi sozlamalar: pool'dagi har bir ulanish uchun qayta
    berilishi shart. Avval ``foreign_keys=ON`` faqat ``init_db`` ichidagi bitta
    ulanishga qo'yilar edi, ya'ni qolgan ulanishlarda tashqi kalitlar
    **tekshirilmasdi**.

    ``journal_mode`` bazaning o'zida saqlanadi (bir marta yetarli), lekin uni
    har ulanishda so'rash zararsiz — qiymatni qaytarib olamiz va kutilganidan
    farq qilsa (masalan baza NFS'da yoki ``:memory:``) ogohlantiramiz.

    ``connect_args={"timeout": 30}`` bilan farqi: ``timeout`` pysqlite'ning
    qulf kutishini, ``busy_timeout`` esa SQLite'ning ichki qayta urinish
    siyosatini boshqaradi — ikkalasi bir-birini to'ldiradi.
    """
    settings = get_settings()
    if not settings.db.is_sqlite:
        return

    journal_mode = settings.db.journal_mode
    busy_timeout = int(settings.db.busy_timeout)
    synchronous = settings.db.synchronous
    is_memory = ":memory:" in settings.db.url or settings.db.url.endswith("://")
    warned: list[bool] = []  # ogohlantirishni bir marta chiqarish uchun

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA journal_mode={journal_mode}")
            row = cursor.fetchone()
            applied = str(row[0]).upper() if row else ""
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout}")
            cursor.execute(f"PRAGMA synchronous={synchronous}")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

        # Xotira bazasi WAL'ni qabul qilmaydi ("memory" qaytaradi) — bu norma.
        if applied and applied != journal_mode and not is_memory and not warned:
            warned.append(True)
            logger.warning(
                "SQLite journal_mode=%s qo'llanmadi (bazadan qaytgan qiymat: %s). "
                "Fayl tarmoq diskida (NFS/SMB) bo'lsa DB_JOURNAL_MODE=DELETE qo'ying.",
                journal_mode,
                applied,
            )

    logger.info(
        "SQLite pragmalari: journal_mode=%s busy_timeout=%dms synchronous=%s foreign_keys=ON",
        journal_mode,
        busy_timeout,
        synchronous,
    )


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _ensure_sqlite_directory(settings.db.url)
        _engine = create_async_engine(settings.db.url, **_engine_kwargs(settings.db.url))
        _apply_sqlite_pragmas(_engine)
        logger.info("SQLAlchemy async engine created for %s", settings.db.url.split("@")[-1])
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory bound to :func:`get_engine`."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,   # keep loaded attributes usable after commit
            autoflush=False,
            autocommit=False,
        )
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on any exception.

    >>> async with session_scope() as session:
    ...     session.add(User(telegram_id=1))
    """
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db(*, create_tables: bool | None = None) -> None:
    """Initialise the engine and (optionally) create missing tables.

    ``create_tables`` defaults to ``settings.db.auto_create_tables``. In
    production prefer Alembic migrations and set ``DB_AUTO_CREATE=false``.
    """
    settings = get_settings()
    should_create = settings.db.auto_create_tables if create_tables is None else create_tables

    # Import models so they register themselves on Base.metadata.
    from db import models  # noqa: F401 - import yon ta'siri (metadata registratsiyasi)

    if should_create:
        engine = get_engine()
        async with engine.begin() as conn:
            if settings.db.is_sqlite:
                # SQLite does not support ALTER for most schema changes, but
                # foreign keys must be switched on per connection.
                await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema verified/created (%d tables)", len(Base.metadata.tables))

    # Operator uchun dalil: jurnal rejimi haqiqatan qo'llandimi?
    if settings.db.is_sqlite and ":memory:" not in settings.db.url:
        async with get_engine().connect() as conn:
            mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
            foreign_keys = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        logger.info(
            "SQLite holati: journal_mode=%s busy_timeout=%sms foreign_keys=%s",
            mode, timeout, foreign_keys,
        )


async def dispose_engine() -> None:
    """Close all pooled connections (call during application shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _session_factory = None
