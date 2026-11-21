#!/usr/bin/env python3
"""VPS'dagi SQLite bazani bulutli PostgreSQL'ga (Neon/Supabase) ko'chirish.

VPS'siz rejimda bot GitHub Actions runner'ida ishlaydi va runner diski har
~6 soatda o'chadi — shuning uchun baza tashqi Postgres'da bo'lishi shart.
Bu skript barcha jadvallarni (foydalanuvchilar, papkalar, fayllar ``file_id``
lari, ulashish havolalari, audit, flaglar) bir martada nusxalaydi.

Ishlatish (VPS'da yoki bazaning nusxasi bor kompyuterda)::

    python scripts/sqlite_to_postgres.py \\
        --source sqlite:///./data/hugecloud.sqlite3 \\
        --target "postgresql://user:pass@ep-xxx.neon.tech/db?sslmode=require"

Xavfsizlik: maqsad bazada ma'lumot bo'lsa skript to'xtaydi (``--force``
bilan jadvallar avval tozalanadi). Manba baza o'zgartirilmaydi.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Settings yuklanishi uchun BOT_TOKEN shart — skript uchun ahamiyatsiz.
os.environ.setdefault("BOT_TOKEN", "0:migration-only")

from datetime import datetime, timezone  # noqa: E402

from sqlalchemy import DateTime, func, inspect, insert, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from config import DatabaseSettings  # noqa: E402
from db.database import Base  # noqa: E402
import db.models  # noqa: E402,F401  (jadvallarni metadata'ga ro'yxatdan o'tkazadi)

BATCH = 500


async def copy(source_url: str, target_url: str, *, force: bool) -> None:
    source_url = DatabaseSettings(DATABASE_URL=source_url).url
    target_url = DatabaseSettings(DATABASE_URL=target_url).url
    if not target_url.startswith("postgresql+asyncpg://"):
        raise SystemExit("--target PostgreSQL bo'lishi kerak")

    src = create_async_engine(source_url)
    dst = create_async_engine(target_url)
    tables = Base.metadata.sorted_tables  # FK tartibida (ota jadval birinchi)
    try:
        async with dst.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with dst.begin() as conn:
            for table in tables:
                count = (await conn.execute(select(func.count()).select_from(table))).scalar_one()
                if count and not force:
                    raise SystemExit(
                        f"Maqsad bazadagi '{table.name}' jadvalida {count} qator bor. "
                        "Tozalab ko'chirish uchun --force qo'shing."
                    )
            if force:
                for table in reversed(tables):
                    await conn.execute(table.delete())

        async with src.connect() as s_conn, dst.begin() as d_conn:
            src_tables = set(await s_conn.run_sync(lambda c: inspect(c).get_table_names()))
            for table in tables:
                if table.name not in src_tables:
                    print(f"  {table.name:<16} manbada yo'q — o'tkazib yuborildi")
                    continue
                src_cols = await s_conn.run_sync(
                    lambda c, t=table.name: {col["name"] for col in inspect(c).get_columns(t)}
                )
                cols = [c for c in table.columns if c.name in src_cols]
                # SQLite vaqtni "naive" saqlaydi; Postgres timestamptz uchun UTC qo'shamiz.
                aware = {
                    c.name for c in cols if isinstance(c.type, DateTime) and c.type.timezone
                }
                result = await s_conn.stream(select(*cols))
                total = 0
                async for chunk in result.partitions(BATCH):
                    rows = [dict(row._mapping) for row in chunk]
                    for row in rows:
                        for name in aware:
                            value = row.get(name)
                            if isinstance(value, datetime) and value.tzinfo is None:
                                row[name] = value.replace(tzinfo=timezone.utc)
                    await d_conn.execute(insert(table), rows)
                    total += len(rows)
                print(f"  {table.name:<16} {total} qator")

            # Avtoinkrement ketma-ketliklarini eng katta id'ga moslash.
            for table in tables:
                pk = list(table.primary_key.columns)
                if len(pk) != 1 or not pk[0].autoincrement or pk[0].type.python_type is not int:
                    continue
                await d_conn.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                        f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 0) + 1, false)"
                    )
                )
        print("Tayyor ✅")
    finally:
        await src.dispose()
        await dst.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="SQLite DSN, masalan sqlite:///./data/hugecloud.sqlite3")
    parser.add_argument("--target", required=True, help="PostgreSQL DSN (Neon/Supabase)")
    parser.add_argument("--force", action="store_true", help="Maqsad jadvallarni avval tozalash")
    args = parser.parse_args(argv)
    asyncio.run(copy(args.source, args.target, force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
