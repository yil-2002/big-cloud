"""🧹 Migratsiya: eski sxemani yangilash **ma'lumotni o'chirmasdan**."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts.migrate import COLUMN_PLAN, existing_columns, existing_tables, migrate

# Eski (legacy) sxema: faqat shu ustunlar bor edi.
LEGACY_USERS = """
CREATE TABLE users (
    telegram_id BIGINT NOT NULL PRIMARY KEY,
    username VARCHAR(64),
    first_name VARCHAR(64),
    is_authorized BOOLEAN NOT NULL DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""

LEGACY_FILES = """
CREATE TABLE files (
    id CHAR(32) NOT NULL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    file_id VARCHAR(255) NOT NULL,
    file_name VARCHAR(255),
    file_size BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""


def _seed_legacy(path: Path) -> None:
    """Legacy baza + 3 ta mavjud yozuv (migratsiyadan keyin ham turishi shart)."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(LEGACY_USERS)
        connection.execute(LEGACY_FILES)
        connection.execute(
            "INSERT INTO users (telegram_id, username, first_name, is_authorized) "
            "VALUES (777, 'sobir', 'Sobir', 1)"
        )
        connection.execute(
            "INSERT INTO files (id, user_id, file_id, file_name, file_size) "
            "VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 777, 'AgAD-1', 'eski.mp4', 1024)"
        )
        connection.execute(
            "INSERT INTO files (id, user_id, file_id, file_name, file_size) "
            "VALUES ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 777, 'AgAD-2', 'eski.pdf', 2048)"
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def legacy_db(tmp_path: Path) -> str:
    """Legacy SQLite bazasi va uning URL'i."""
    path = tmp_path / "legacy.sqlite3"
    _seed_legacy(path)
    return f"sqlite+aiosqlite:///{path}"


async def test_migrate_creates_missing_tables(legacy_db: str) -> None:
    """Yangi jadvallar (audit_logs, system_flags, folders, shared_links) qo'shiladi."""
    summary = await migrate(legacy_db)
    assert "audit_logs" in summary["tables_created"]
    assert "system_flags" in summary["tables_created"]
    assert "folders" in summary["tables_created"]
    assert "shared_links" in summary["tables_created"]


async def test_migrate_adds_auth_and_archive_columns(legacy_db: str) -> None:
    """🔐 auth va 📦 arxiv ustunlari eski jadvallarga qo'shiladi."""
    await migrate(legacy_db)

    engine_url = legacy_db
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(engine_url, future=True)
    try:
        users = await existing_columns(engine, "users")
        files = await existing_columns(engine, "files")
    finally:
        await engine.dispose()

    for column in ("login", "password_hash", "is_authenticated", "is_blocked", "last_seen_at"):
        assert column in users, f"users.{column} yetishmayapti"
    for column in ("archive_chat_id", "archive_message_id", "archive_file_id", "file_unique_id"):
        assert column in files, f"files.{column} yetishmayapti"


async def test_migrate_preserves_existing_rows(legacy_db: str) -> None:
    """Asosiysi: eski yozuvlar **o'chmaydi** va qiymatlari saqlanadi."""
    await migrate(legacy_db)

    path = legacy_db.replace("sqlite+aiosqlite:///", "")
    connection = sqlite3.connect(path)
    try:
        users = connection.execute(
            "SELECT telegram_id, username, first_name, is_authorized FROM users"
        ).fetchall()
        files = connection.execute(
            "SELECT id, file_id, file_name, file_size FROM files ORDER BY file_name"
        ).fetchall()
    finally:
        connection.close()

    assert users == [(777, "sobir", "Sobir", 1)]
    assert len(files) == 2
    assert files[0][2] == "eski.mp4" and files[0][3] == 1024
    assert files[1][2] == "eski.pdf" and files[1][3] == 2048


async def test_migrate_is_idempotent(legacy_db: str) -> None:
    """Ikki marta ishga tushirish xato bermaydi (ikkinchida hammasi 'skipped')."""
    first = await migrate(legacy_db)
    second = await migrate(legacy_db)

    assert second["tables_created"] == []
    assert second["columns_added"] == []
    assert len(second["skipped"]) == len(first["columns_added"]) + len(first["skipped"])


async def test_migrate_dry_run_changes_nothing(legacy_db: str) -> None:
    """``--dry-run``: reja ko'rsatiladi, baza tegilmaydi."""
    plan = await migrate(legacy_db, dry_run=True)
    assert plan["dry_run"] is True
    assert plan["columns_added"]                       # reja bor

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(legacy_db, future=True)
    try:
        columns = await existing_columns(engine, "users")
        tables = await existing_tables(engine)
    finally:
        await engine.dispose()

    assert "login" not in columns                      # hech narsa qo'shilmadi
    assert "audit_logs" not in tables


async def test_migrate_fresh_database(tmp_path: Path) -> None:
    """Bo'sh bazada ham ishlaydi: barcha jadvallar yaratiladi."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'fresh.sqlite3'}"
    summary = await migrate(url)

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, future=True)
    try:
        tables = await existing_tables(engine)
    finally:
        await engine.dispose()

    assert {"users", "files", "folders", "shared_links", "audit_logs", "system_flags"} <= tables
    assert summary["columns_added"] == []              # jadvallar yangi — ALTER kerak emas


async def test_column_plan_matches_orm() -> None:
    """Rejadagi har bir ustun ORM'da ham bor (nom xatosi bo'lmasligi uchun)."""
    from db.models import File, User

    models = {"users": User, "files": File}
    for table, plan in COLUMN_PLAN.items():
        if table not in models:
            continue
        orm_columns = {column.name for column in models[table].__table__.columns}
        for column, ddl in plan:
            assert column in orm_columns, f"{table}.{column} ORM'da yo'q"
            assert ddl.strip(), f"{table}.{column} uchun DDL bo'sh"


async def test_migrate_creates_missing_sqlite_directory(tmp_path: Path) -> None:
    """``./data/...`` papkasi bo'lmasa migratsiya uni yaratadi (yiqilmaydi)."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'nested' / 'deep' / 'hugecloud.sqlite3'}"
    assert not (tmp_path / "nested").exists()

    summary = await migrate(url)
    assert summary["tables_created"]
    assert (tmp_path / "nested" / "deep" / "hugecloud.sqlite3").exists()
