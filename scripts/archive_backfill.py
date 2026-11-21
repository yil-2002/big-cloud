#!/usr/bin/env python3
"""Arxiv kanalini to'ldirish (backfill) — katta fayllar MTProto orqali oqishi uchun.

Nega kerak: MTProto oqimi faylni **arxiv kanalidagi** xabardan o'qiydi
(`files.archive_message_id`). Arxiv 2026-09-22 da yoqilgan, shuning uchun undan
oldin yuklangan katta fayllarda nusxa yo'q va ular 413 oladi
("Bu fayl onlayn ijro uchun juda katta") — jonli shikoyat: 1.6 GB video.

Skript server-tomonlama ishlaydi: mavjud `file_id` ni kanalga **qayta yuboradi**
(yuklash emas — Telegram o'zi ko'chiradi), so'ng `archive_message_id` ni yozadi.
Idempotent: faqat nusxasi yo'qlarni oladi, shuning uchun to'xtatib qayta
ishga tushirish xavfsiz.

MUHIM: bu mashinada eski Supabase `DATABASE_URL` muhitda turibdi va u
pydantic'da `.env` dan ustun turadi — shuning uchun lokal baza bilan ishlash uchun
`env -u DATABASE_URL` bilan yugurtiring (aks holda Postgres'ga ulanmoqchi bo'ladi):

Ishlatish:
    env -u DATABASE_URL python scripts/archive_backfill.py --dry-run
    python scripts/archive_backfill.py --limit 5
    python scripts/archive_backfill.py                 # hammasi (sukut: >20 MB)
    python scripts/archive_backfill.py --file-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot  # noqa: E402
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError  # noqa: E402
from sqlalchemy import select  # noqa: E402

from config import Settings  # noqa: E402
from db.database import get_session_factory  # noqa: E402
from db.models import File as FileRecord  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("archive_backfill")


async def main() -> int:
    ap = argparse.ArgumentParser(description="Arxiv kanalini to'ldirish")
    ap.add_argument("--dry-run", action="store_true", help="faqat ro'yxatni ko'rsatish")
    ap.add_argument("--limit", type=int, default=0, help="nechta fayl (0 = hammasi)")
    ap.add_argument("--file-id", default="", help="bitta yozuv UUID si")
    ap.add_argument("--min-size", type=int, default=20 * 1024 * 1024, help="eng kichik hajm")
    ap.add_argument("--delay", type=float, default=0.6, help="har yuborish orasidagi pauza (s)")
    args = ap.parse_args()

    settings = Settings()
    db_url = settings.db.url
    if not db_url.startswith("sqlite"):
        log.error(
            "DB manzili sqlite emas (%s). Muhitdagi DATABASE_URL ustunlik qilyapti — "
            "`env -u DATABASE_URL` bilan qayta yugurtiring.",
            db_url.split("@")[-1],
        )
        return 2
    archive = settings.archive
    if not archive.configured:
        log.error("Arxiv sozlanmagan (ARCHIVE_ENABLED/ARCHIVE_CHANNEL_ID) — to'xtatildi")
        return 2

    factory = get_session_factory()
    async with factory() as session:
        query = select(FileRecord).where(FileRecord.archive_message_id.is_(None))
        query = query.where(FileRecord.file_size > args.min_size) if not args.file_id else query.where(
            FileRecord.id == uuid.UUID(args.file_id)
        )
        query = query.order_by(FileRecord.file_size.desc())
        if args.limit:
            query = query.limit(args.limit)
        rows = list((await session.execute(query)).scalars())

        if not rows:
            log.info("Arxivlanmagan fayl yo'q — hech narsa qilinmadi")
            return 0
        total = sum(int(r.file_size or 0) for r in rows)
        log.info("%d fayl (%.1f GB) navbatda", len(rows), total / 1073741824)
        if args.dry_run:
            for r in rows[:20]:
                log.info("  %s %s %.1f MB", r.id, r.file_name[:40], int(r.file_size or 0) / 1048576)
            return 0

        bot = Bot(token=settings.bot.token.get_secret_value())
        done = skipped = failed = 0
        try:
            for index, record in enumerate(rows, 1):
                caption = (
                    f"{archive.caption_prefix}\nuser={record.user_id}\nname={record.file_name}"
                )[:1024]
                try:
                    message = await bot.send_document(
                        chat_id=archive.numeric_id or archive.channel_id,
                        document=record.file_id,
                        caption=caption,
                        disable_notification=True,
                    )
                except TelegramRetryAfter as exc:
                    log.warning("FloodWait %ss — kutilmoqda", exc.retry_after)
                    await asyncio.sleep(exc.retry_after + 1)
                    continue
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    # file_id eskirgan/o'chirilgan bo'lishi mumkin — o'tkazib yuboramiz.
                    skipped += 1
                    log.warning("  [%d/%d] %s -> o'tkazildi: %s", index, len(rows), record.file_name[:32], exc)
                    continue
                except Exception as exc:  # noqa: BLE001 - bittasi qolganini to'xtatmasin
                    failed += 1
                    log.error("  [%d/%d] %s -> xato: %s", index, len(rows), record.file_name[:32], exc)
                    continue

                record.mark_archived(chat_id=archive.numeric_id or archive.channel_id, message_id=message.message_id)
                if message.document:
                    record.archive_file_id = message.document.file_id
                await session.commit()
                done += 1
                if index % 10 == 0 or index == len(rows):
                    log.info(
                        "  [%d/%d] arxivlandi=%d o'tkazildi=%d xato=%d (oxirgi: %s)",
                        index, len(rows), done, skipped, failed, record.file_name[:36],
                    )
                await asyncio.sleep(args.delay)
        finally:
            await bot.session.close()

        log.info("TAYYOR: arxivlandi=%d o'tkazildi=%d xato=%d", done, skipped, failed)
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
