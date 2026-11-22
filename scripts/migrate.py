#!/usr/bin/env python3
"""Schema migration for the ``hugecloud`` upgrade - **destructive-free**.

Why a hand-rolled migrator?
---------------------------
The project creates tables with ``Base.metadata.create_all()``, which adds
*missing tables* but never *missing columns*. Upgrading an existing deployment
therefore needs ``ALTER TABLE ... ADD COLUMN`` statements. This script:

1. optionally **backs up** a SQLite database file first (``--backup``);
2. inspects every table with the dialect's own reflection
   (``PRAGMA table_info`` on SQLite, ``information_schema`` on PostgreSQL);
3. adds only the columns that are really missing - it is **idempotent**, so
   running it twice is safe;
4. creates the brand-new ``audit_logs`` table plus the new indexes;
5. never issues ``DROP``/``DELETE``: existing rows keep their data and receive
   the declared defaults (``0`` / ``false`` / ``NULL``).

Usage::

    python scripts/migrate.py                 # apply
    python scripts/migrate.py --dry-run       # print the plan only
    python scripts/migrate.py --backup        # copy the .sqlite3 file first
    DATABASE_URL=postgresql+asyncpg://... python scripts/migrate.py

Exit code ``0`` = success (or nothing to do), ``1`` = failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Make the project importable when run as ``python scripts/migrate.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from config import get_settings
from db.database import Base

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("migrate")

__all__ = ["COLUMN_PLAN", "ensure_sqlite_directory", "main", "migrate"]


# ---------------------------------------------------------------------------
# The plan: table -> [(column, DDL fragment used when missing)]
# ---------------------------------------------------------------------------
#: Every column added by the ``hugecloud`` upgrade. The SQL type is written out
#: explicitly so the statement works on both SQLite and PostgreSQL.
#: ``ALTER TABLE ... ADD COLUMN`` SQLite'da ham, PostgreSQL'da ham ishlaydi va
#: mavjud qatorlarga tegmaydi (yangi ustun default qiymatni oladi) — shuning
#: uchun migratsiya **hech qachon ma'lumot o'chirmaydi**.
COLUMN_PLAN: dict[str, list[tuple[str, str]]] = {
    "users": [
        # --- hugecloud yangiliklari: hisob (login/parol) qatlami --------------
        ("login", "VARCHAR(64)"),
        ("password_hash", "VARCHAR(255)"),
        ("is_authenticated", "BOOLEAN NOT NULL DEFAULT 0"),
        ("active_profile_id", "BIGINT"),
        ("session_owner_id", "BIGINT"),
        ("profile_creator_id", "BIGINT"),
        ("failed_login_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("locked_until", "TIMESTAMP"),
        ("last_login_at", "TIMESTAMP"),
        ("last_seen_at", "TIMESTAMP"),
        ("admin_note", "VARCHAR(255)"),
        # --- ORM'da boshidanoq bor, ammo eski deployment'larda yetishmasligi
        #     mumkin (legacy faqat telegram_id + is_authorized saqlagan).
        ("is_authorized", "BOOLEAN NOT NULL DEFAULT 0"),
        ("is_blocked", "BOOLEAN NOT NULL DEFAULT 0"),
        ("is_premium", "BOOLEAN NOT NULL DEFAULT 0"),
        ("username", "VARCHAR(64)"),
        ("phone", "VARCHAR(32)"),
        ("first_name", "VARCHAR(64)"),
        ("last_name", "VARCHAR(64)"),
        ("language_code", "VARCHAR(8)"),
        ("settings", "JSON"),
        ("storage_used", "BIGINT NOT NULL DEFAULT 0"),
        ("storage_quota", "BIGINT"),
    ],
    "files": [
        # --- hugecloud yangiliklari: shaxsiy arxiv kanali nusxasi -------------
        ("archive_chat_id", "VARCHAR(64)"),
        ("archive_message_id", "INTEGER"),
        ("archive_file_id", "VARCHAR(255)"),
        ("archived_at", "TIMESTAMP"),
        ("is_pinned", "BOOLEAN NOT NULL DEFAULT 0"),
        # --- dedup va metadata (legacy fayl yozuvlarida bo'lmasligi mumkin) ---
        ("file_unique_id", "VARCHAR(255)"),
        ("mime_type", "VARCHAR(128)"),
        ("category", "VARCHAR(32)"),
        ("caption", "TEXT"),
        ("duration", "INTEGER"),
        ("width", "INTEGER"),
        ("height", "INTEGER"),
        ("thumbnail_file_id", "VARCHAR(255)"),
        ("deleted_at", "DATETIME"),  # 🗑 savat (soft delete) — 6-bosqich
        ("is_public", "BOOLEAN NOT NULL DEFAULT 0"),
        ("visibility", "VARCHAR(16) NOT NULL DEFAULT 'private'"),
        ("views", "INTEGER NOT NULL DEFAULT 0"),
        ("downloads", "INTEGER NOT NULL DEFAULT 0"),
        ("folder_id", "CHAR(32)"),
    ],
}

#: Indexes created after the columns exist (name -> DDL).
INDEX_PLAN: dict[str, str] = {
    "ix_users_login": "CREATE INDEX IF NOT EXISTS ix_users_login ON users (login)",
    "ix_users_is_authenticated": (
        "CREATE INDEX IF NOT EXISTS ix_users_is_authenticated ON users (is_authenticated)"
    ),
    "ix_users_is_blocked": "CREATE INDEX IF NOT EXISTS ix_users_is_blocked ON users (is_blocked)",
    "ix_files_archived": "CREATE INDEX IF NOT EXISTS ix_files_archived ON files (archive_message_id)",
    "ix_audit_actor": "CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_logs (actor_id)",
    "ix_audit_target": "CREATE INDEX IF NOT EXISTS ix_audit_target ON audit_logs (target_id)",
    "ix_audit_action_created": (
        "CREATE INDEX IF NOT EXISTS ix_audit_action_created ON audit_logs (action, created_at)"
    ),
}


async def existing_tables(engine: AsyncEngine) -> set[str]:
    """Names of all tables in the target database (dialect independent)."""

    def _inspect(connection: Any) -> set[str]:
        from sqlalchemy import inspect

        return set(inspect(connection).get_table_names())

    async with engine.connect() as connection:
        return set(await connection.run_sync(_inspect))


async def existing_columns(engine: AsyncEngine, table: str) -> set[str]:
    """Column names of ``table`` (empty set when the table does not exist)."""

    def _inspect(connection: Any) -> set[str]:
        from sqlalchemy import inspect

        inspector = inspect(connection)
        if table not in inspector.get_table_names():
            return set()
        return {column["name"] for column in inspector.get_columns(table)}

    async with engine.connect() as connection:
        return await connection.run_sync(_inspect)


def backup_sqlite(url: str) -> Path | None:
    """Copy a SQLite file next to itself with a timestamp before touching it."""
    if not url.startswith("sqlite"):
        log.info("Backup skipped (not a SQLite database)")
        return None
    path = url.split("///")[-1]
    if not path or path == ":memory:":
        log.info("Backup skipped (in-memory database)")
        return None
    source = Path(path)
    if not source.exists():
        log.info("Backup skipped (%s does not exist yet)", source)
        return None
    # UTC: server vaqti mintaqasi har xil bo'lsa ham backup nomlari izchil.
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = source.with_suffix(f".backup-{stamp}{source.suffix}")
    shutil.copy2(source, target)
    log.info("Backup written to %s", target)
    return target


def ensure_sqlite_directory(database_url: str) -> None:
    """SQLite fayli uchun ota papkani yaratish.

    ``sqlite+aiosqlite:///./data/hugecloud.sqlite3`` ko'rinishidagi URL'da
    ``data/`` papkasi bo'lmasa SQLite ``unable to open database file`` xatosini
    beradi (PostgreSQL'da bunday muammo yo'q).
    """
    if "sqlite" not in database_url:
        return
    path = database_url.split(":///", 1)[-1]
    if not path or path in {":memory:", ""}:
        return
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    log.info("SQLite directory ready: %s", parent)


async def migrate(database_url: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Apply the plan; returns a summary dict (also used by the tests)."""
    ensure_sqlite_directory(database_url)
    engine = create_async_engine(database_url, future=True)
    summary: dict[str, Any] = {
        "database": database_url.split("@")[-1],
        "tables_created": [],
        "columns_added": [],
        "indexes_created": [],
        "skipped": [],
        "dry_run": dry_run,
    }

    try:
        tables = await existing_tables(engine)
        log.info("Tables found: %s", ", ".join(sorted(tables)) or "<none>")

        # 1) Missing tables (audit_logs on an old deployment, or everything on a
        #    fresh database).
        missing_tables = [
            name for name in Base.metadata.tables if name not in tables
        ]
        if missing_tables:
            summary["tables_created"] = missing_tables
            log.info("Tables to create: %s", ", ".join(missing_tables))
            if not dry_run:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                tables = await existing_tables(engine)

        # 2) Missing columns on pre-existing tables.
        for table, plan in COLUMN_PLAN.items():
            if table not in tables:
                continue
            present = await existing_columns(engine, table)
            for column, ddl in plan:
                if column in present:
                    summary["skipped"].append(f"{table}.{column}")
                    continue
                statement = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                summary["columns_added"].append(f"{table}.{column}")
                log.info("%s", statement)
                if not dry_run:
                    async with engine.begin() as connection:
                        await connection.execute(text(statement))

        # 3) Indexes (SQLite/PostgreSQL both support IF NOT EXISTS).
        if not dry_run:
            tables = await existing_tables(engine)
        for name, ddl in INDEX_PLAN.items():
            table = ddl.split(" ON ", 1)[1].split(" ", 1)[0]
            if dry_run and table not in tables and table not in summary["tables_created"]:
                continue
            summary["indexes_created"].append(name)
            log.info("%s;", ddl)
            if not dry_run:
                async with engine.begin() as connection:
                    await connection.execute(text(ddl))

        # 4) Report the resulting shape so the operator can eyeball it.
        if not dry_run:
            for table in ("users", "files", "audit_logs"):
                columns = await existing_columns(engine, table)
                log.info("%-11s -> %d columns", table, len(columns))
    finally:
        await engine.dispose()

    log.info(
        "%s: +%d tables, +%d columns, +%d indexes (%d already present)",
        "PLAN" if dry_run else "DONE",
        len(summary["tables_created"]),
        len(summary["columns_added"]),
        len(summary["indexes_created"]),
        len(summary["skipped"]),
    )
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=None, help="Overrides DATABASE_URL / .env")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without touching the DB")
    parser.add_argument("--backup", action="store_true", help="Copy a SQLite file before migrating")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``python scripts/migrate.py``)."""
    args = _parse_args(argv)
    settings = get_settings()
    url = args.database_url or os.getenv("DATABASE_URL") or settings.database_url

    if args.backup:
        backup_sqlite(url)

    log.info("Migrating %s", url.split("@")[-1])
    try:
        asyncio.run(migrate(url, dry_run=args.dry_run))
    except Exception as exc:
        log.error("Migration failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
