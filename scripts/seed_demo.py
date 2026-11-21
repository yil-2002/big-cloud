"""Demo ma'lumotlarini yuklash — Mini App'ni real steks bilan ko'rsatish uchun.

Nima qiladi
-----------
1. Jadvallarni yaratadi (:func:`db.database.init_db` — mavjud yozuvlarni o'chirmaydi).
2. Demo hisob ochadi: ``demo`` / ``demo1234`` (Telegram ID ``777000001``).
3. Papkalar va turli toifadagi fayllarni qo'shadi (video, audio, hujjat, arxiv, APK/IPA).
4. Tanlangan fayllarni muqimlaydi (pin) hamda ochiq va parolli ulashish havolalarini yaratadi.

Skript **idempotent**: fayllar ``file_unique_id`` bo'yicha takrorlanmaydi, papkalar
qayta yaratilmaydi — bir necha marta ishga tushirish xavfsiz.

Har bir fayl bitta :class:`DemoFile` yozuvida to'liq tasvirlanadi (nom, papka, pin,
ulashish roli). Nomlar ikkinchi joyda takrorlanmaydi — shuning uchun matn xatosi
(yozuvda "darsi"/"dersi" kabi) imkonsiz.

Muhim cheklov
-------------
``file_id`` qiymatlari demo uchun soxta. Media oqimi (stream) Telegram serverlaridan
tortiladi, shuning uchun ijro tugmasi bosilganda pleer ``telegram_file_not_found``
xatosini ko'rsatadi — bu kutilgan holat. Haqiqiy ijro uchun ``.env`` fayliga o'z
``BOT_TOKEN``'ingizni yozing va fayllarni bot orqali yuklang: interfeys, mavzu
almashtirgichi, saralash, havolalar va pleer boshqaruvi shu holatida ham to'liq ishlaydi.

Ishga tushirish
---------------
    python scripts/seed_demo.py

Parametrlar (env orqali): ``DEMO_LOGIN``, ``DEMO_PASSWORD``, ``DEMO_TELEGRAM_ID``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ``python scripts/seed_demo.py`` ko'rinishida ishga tushirilganda loyiha ildizi
# ``sys.path``'da bo'lmaydi — ``scripts/migrate.py`` bilan bir xil yondashuv.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from db.database import init_db, session_scope
from db.models import File, Folder
from services.auth import AuthService
from services.share import ShareService
from services.storage import StorageService

#: Demo hisob ma'lumotlari (env orqali almashtirish mumkin).
DEMO_TELEGRAM_ID = int(os.getenv("DEMO_TELEGRAM_ID", "777000001"))
DEMO_LOGIN = os.getenv("DEMO_LOGIN", "demo")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "demo1234")

#: Ulashish havolasi muddati: 7 kun.
LINK_TTL_SECONDS = 7 * 24 * 3600

#: Parolli havolaning maxfiy kodi (demo uchun ochiq ko'rsatiladi).
DEMO_PASS_CODE = "2026"


@dataclass(frozen=True, slots=True)
class DemoFile:
    """Bitta demo faylning to'liq tavsifi."""

    name: str
    size: int
    mime: str
    folder: str | None = None
    pinned: bool = False
    #: ``"public"`` — ochiq havola, ``"secured"`` — parolli havola, ``None`` — havolasiz.
    share: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


#: Demo to'plam: turli toifalar, papkalar, pin va ulashish holatlari.
DEMO_FILES: tuple[DemoFile, ...] = (
    DemoFile("HugeCloud — taqdimot.mp4", 50_331_648, "video/mp4", folder="Loyihalar",
             pinned=True, extra={"duration": 214, "width": 1920, "height": 1080}),
    DemoFile("Dasturlash darsi 01.mp4", 327_155_712, "video/mp4", folder="Loyihalar",
             share="public", extra={"duration": 1876, "width": 1280, "height": 720}),
    DemoFile("Yomg'ir ovozi.mp3", 7_340_032, "audio/mpeg", folder="Musiqa",
             extra={"duration": 187}),
    DemoFile("Tungi yo'l — instrumental.flac", 31_457_280, "audio/flac", folder="Musiqa",
             extra={"duration": 245}),
    DemoFile("Shartnoma_2026.pdf", 1_048_576, "application/pdf",
             pinned=True, share="secured"),
    DemoFile("Oylik hisobot.xlsx", 524_288,
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    DemoFile("Eslatmalar.txt", 12_288, "text/plain", extra={"caption": "Kundalik reja"}),
    DemoFile("loyiha_arxiv.zip", 104_857_600, "application/zip"),
    DemoFile("HugeCloud.apk", 24_117_248, "application/vnd.android.package-archive"),
    DemoFile("HugeCloud.ipa", 68_157_440, "application/octet-stream"),
    DemoFile("cover_art.png", 2_621_440, "image/png", extra={"width": 1400, "height": 1400}),
)


async def _ensure_folder(storage: StorageService, name: str) -> uuid.UUID:
    """Papkani topadi yoki yaratadi (takroriy ishga tushirishda xavfsiz)."""
    existing = await storage.session.scalar(
        select(Folder.id).where(
            Folder.user_id == DEMO_TELEGRAM_ID,
            Folder.name == name,
            Folder.parent_id.is_(None),
        )
    )
    if existing is not None:
        return existing
    folder = await storage.create_folder(name, DEMO_TELEGRAM_ID)
    return folder.id


async def seed() -> None:
    """Demo ma'lumotlar bazasini shakllantiradi."""
    await init_db()

    async with session_scope() as session:
        storage = StorageService(session)
        auth = AuthService(session, storage=storage)
        share = ShareService(session)

        await storage.get_or_create_user(
            DEMO_TELEGRAM_ID,
            username="demo_user",
            first_name="Demo",
            last_name="Foydalanuvchi",
            language_code="uz",
        )

        result = await auth.register(
            DEMO_TELEGRAM_ID, DEMO_LOGIN, DEMO_PASSWORD, source="seed_demo"
        )
        if not result.ok:
            # Hisob allaqachon mavjud — bu xato emas, idempotentlik.
            print(f"· register: {result.message}")

        folders: dict[str, uuid.UUID] = {}
        links: dict[str, str] = {}
        created = 0

        for item in DEMO_FILES:
            folder_id: uuid.UUID | None = None
            if item.folder is not None:
                if item.folder not in folders:
                    folders[item.folder] = await _ensure_folder(storage, item.folder)
                folder_id = folders[item.folder]

            stored = await storage.add_file(
                telegram_id=DEMO_TELEGRAM_ID,
                file_id=f"DEMO_{abs(hash(item.name)):020d}",
                file_unique_id=f"demo-{uuid.uuid5(uuid.NAMESPACE_URL, item.name)}",
                file_name=item.name,
                file_size=item.size,
                mime_type=item.mime,
                folder_id=folder_id,
                **item.extra,
            )
            record: File = stored.record
            created += int(stored.is_new)

            if item.pinned and not record.is_pinned:
                await storage.toggle_pin(record.id, DEMO_TELEGRAM_ID)

            if item.share == "public":
                link = await share.create_link(
                    record.id, DEMO_TELEGRAM_ID,
                    ttl_seconds=LINK_TTL_SECONDS, make_public=True,
                )
                links["ochiq"] = link.token
            elif item.share == "secured":
                link = await share.create_link(
                    record.id, DEMO_TELEGRAM_ID,
                    ttl_seconds=LINK_TTL_SECONDS, pass_code=DEMO_PASS_CODE,
                )
                links["parolli"] = link.token

        await session.flush()

    print("Demo tayyor ✓")
    print(f"  Login / parol  : {DEMO_LOGIN} / {DEMO_PASSWORD}")
    print(f"  Telegram ID    : {DEMO_TELEGRAM_ID}")
    print(f"  Yangi fayllar  : {created} (jami {len(DEMO_FILES)})")
    print(f"  Papkalar       : {', '.join(sorted(folders)) or '—'}")
    if "ochiq" in links:
        print(f"  Ochiq havola   : /s/{links['ochiq']}")
    if "parolli" in links:
        print(f"  Parolli havola : /s/{links['parolli']}  (kod: {DEMO_PASS_CODE})")


if __name__ == "__main__":
    asyncio.run(seed())
