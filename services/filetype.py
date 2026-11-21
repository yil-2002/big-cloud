"""Kontent bo'yicha fayl turini aniqlash (magic bytes) + bir martalik backfill.

Nega kerak: Telegram ba'zi hujjatlarni **kengaytmasiz** nom (``tmp_3295``) va
``application/octet-stream`` mime bilan qaytaradi. Bunday yozuvlarda
:meth:`db.models.file.File.detect_category` "other" beradi va Mini App pleeri
umuman ochilmaydi. Yechim — baytlarning **birinchi ~4 KB** ini o'qib, imzo
(magic bytes) bo'yicha tur va kategoriyani aniqlash.

Modul ataylab **faqat stdlib** bilan yozilgan (``python-magic`` kabi tashqi
paket yo'q): Python 3.10+ da ishlaydi va o'rnatish talab qilmaydi.

Asosiy API
----------
* :func:`detect_bytes` - baytlar bo'yicha ``(mime, category)``;
* :func:`detect_file`  - fayl yo'lidan faqat boshini o'qib aniqlash;
* :func:`detect_path`  - :func:`detect_file` ning aliasi (qulaylik uchun);
* :func:`category_for_mime` / :func:`is_generic_mime` - yordamchilar;
* :func:`effective_type` - API uchun: mavjud tur bo'lsa saqlaydi, bo'lmasa
  kengaytma/mime bo'yicha hisoblab beradi (diskka tegmaydi).

CLI (bir martalik backfill)::

    python -m services.filetype --backfill --dry-run
    python -m services.filetype --backfill
    python -m services.filetype --media-dir /var/lib/bigcloud/media --backfill

Backfill faqat ``mime_type`` va ``category`` maydonlarini yangilaydi; boshqa
hech narsa o'zgarmaydi. Diskda topilmagan fayllar jimgina o'tkazib yuboriladi
(xato bermaydi).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from mimetypes import guess_type
from pathlib import Path
from typing import Any

from db.models import File, FileCategory

logger = logging.getLogger(__name__)

__all__ = [
    "GENERIC_MIMES",
    "SNIFF_BYTES",
    "STREAMABLE_CATEGORIES",
    "BackfillStats",
    "category_for_mime",
    "detect_bytes",
    "detect_file",
    "detect_path",
    "effective_type",
    "is_generic_mime",
    "media_cache_dir",
    "read_head",
    "resolve_media_path",
    "run_backfill",
]

#: Aniqlash uchun o'qiladigan maksimal bayt soni. Imzolar fayl boshida bo'ladi,
#: shuning uchun butun faylni o'qish shart emas (katta videolar uchun muhim).
SNIFF_BYTES = 4096

#: "Tur noma'lum" deb hisoblanadigan mime qiymatlari.
GENERIC_MIMES: frozenset[str] = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "application/unknown",
        "unknown/unknown",
    }
)

#: Mini App pleeri ``<video>``/``<audio>`` orqali ijro eta oladigan kategoriyalar
#: (``db.models.file._STREAMABLE_CATEGORIES`` bilan bir xil).
STREAMABLE_CATEGORIES: tuple[FileCategory, ...] = (
    FileCategory.VIDEO,
    FileCategory.AUDIO,
    FileCategory.VOICE,
    FileCategory.ANIMATION,
)

_OCTET = "application/octet-stream"
_UNKNOWN: tuple[str, FileCategory] = (_OCTET, FileCategory.OTHER)

# --- ftyp brendlari ---------------------------------------------------------
_FTYP_AUDIO_BRANDS = frozenset({b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "})
_FTYP_IMAGE_BRANDS: dict[bytes, str] = {
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"hevc": "image/heic",
    b"hevx": "image/heic",
    b"mif1": "image/heic",
    b"msf1": "image/heic",
    b"avif": "image/avif",
    b"avis": "image/avif",
}
_FTYP_MOV_BRANDS = frozenset({b"qt  "})
_FTYP_VIDEO_BRANDS = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"msdh", b"msix"}
)


# ---------------------------------------------------------------------------
# Past darajali yordamchilar
# ---------------------------------------------------------------------------
def read_head(path: str | os.PathLike[str], size: int = SNIFF_BYTES) -> bytes:
    """Faylning faqat boshidan ``size`` bayt o'qish (xotira/trafik tejamkor).

    Diskda yo'q yoki o'qib bo'lmaydigan fayl uchun ``b""`` qaytaradi — chaqiruvchi
    buni "tur aniqlanmadi" deb talqin qiladi (xato ko'tarmaydi).
    """
    try:
        with open(path, "rb") as handle:
            return handle.read(max(0, int(size)))
    except (OSError, ValueError):
        return b""


def is_generic_mime(mime: str | None) -> bool:
    """``True`` — mime bo'sh yoki "tur noma'lum" (``octet-stream`` va h.k.)."""
    if mime is None:
        return True
    return mime.strip().lower() in GENERIC_MIMES


def category_for_mime(mime: str | None) -> FileCategory:
    """Mime'dan kategoriya (loyihadagi mavjud taqsimot orqali)."""
    if not mime:
        return FileCategory.OTHER
    return File.detect_category("", mime)


def _vint(data: bytes, pos: int) -> tuple[int | None, int]:
    """EBML variable-size integer'ni o'qish (``(qiymat, yangi pozitsiya)``)."""
    if pos >= len(data):
        return None, pos
    first = data[pos]
    if first == 0:  # pragma: no cover - buzilgan EBML
        return None, pos + 1
    length = 0
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
    length += 1
    if length > 8 or pos + length > len(data):
        return None, pos + length
    value = first & (mask - 1)
    for index in range(1, length):
        value = (value << 8) | data[pos + index]
    return value, pos + length


def _read_id(data: bytes, pos: int) -> tuple[int | None, int]:
    """EBML element ID'sini o'qish (Class-D, qiymat o'zi)."""
    if pos >= len(data):
        return None, pos
    first = data[pos]
    length = 0
    mask = 0x80
    while mask and not (first & mask):
        mask >>= 1
        length += 1
    length += 1
    if length > 4 or pos + length > len(data):
        return None, pos + length
    value = 0
    for index in range(length):
        value = (value << 8) | data[pos + index]
    return value, pos + length


def _ebml_doctype(data: bytes) -> str | None:
    """EBML (Matroska/WebM) sarlavhasidagi ``DocType`` qiymati."""
    if data[:4] != b"\x1aE\xdf\xa3":
        return None
    pos = 4
    size, pos = _vint(data, pos)
    end = len(data) if size is None else min(len(data), pos + size)
    while pos < end:
        element_id, pos = _read_id(data, pos)
        if element_id is None:
            break
        element_size, pos = _vint(data, pos)
        if element_size is None:
            break
        if element_id == 0x4282:  # DocType
            raw = data[pos : pos + element_size]
            return raw.decode("ascii", "replace").strip("\x00").strip().lower() or None
        pos += element_size
    return None


def _ftyp_type(head: bytes) -> tuple[str, FileCategory]:
    """ISO-BMFF (``ftyp``) brendiga qarab tur: mp4 / m4a / mov / heic / avif."""
    brand = head[8:12]
    compatible = head[8:32]
    # Major brand — hal qiluvchi. ``compatible`` ro'yxatiga tayanmaymiz: Apple
    # ``M4V `` videolari ko'pincha ``M4A `` ni ham mos brend sifatida sanaydi,
    # shuning uchun compatible bo'yicha audio deb xulosa qilish xato bo'lardi.
    if brand in _FTYP_AUDIO_BRANDS:
        return "audio/mp4", FileCategory.AUDIO
    if brand in _FTYP_IMAGE_BRANDS:
        return _FTYP_IMAGE_BRANDS[brand], FileCategory.PHOTO
    if brand in _FTYP_MOV_BRANDS or compatible[4:8] == b"qt  ":
        return "video/quicktime", FileCategory.VIDEO
    if brand in _FTYP_VIDEO_BRANDS or brand[:3] == b"M4V":
        return "video/mp4", FileCategory.VIDEO
    # Noma'lum ISO-BMFF brendi — eng ehtimoliy holat video (mp4).
    return "video/mp4", FileCategory.VIDEO


def _looks_like_mp3(head: bytes) -> bool:
    """``ID3`` teg yoki MPEG audio frame sync (``0xFFEx``/``0xFFFx``)."""
    if head[:3] == b"ID3":
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        version = (head[1] >> 3) & 0x03
        layer = (head[1] >> 1) & 0x03
        # version == 1 (reserved) yoki layer == 0 (reserved) — bu MP3 emas.
        return version != 1 and layer != 0
    return False


def _avi_or_riff(head: bytes) -> tuple[str, FileCategory]:
    """``RIFF`` konteynerini ichki ``form type`` bo'yicha ajratish."""
    form = head[8:12]
    if form == b"AVI ":
        return "video/x-msvideo", FileCategory.VIDEO
    if form == b"WAVE":
        return "audio/wav", FileCategory.AUDIO
    if form == b"WEBP":
        return "image/webp", FileCategory.PHOTO
    return _UNKNOWN


# ---------------------------------------------------------------------------
# Asosiy aniqlash
# ---------------------------------------------------------------------------
def detect_bytes(head: bytes | bytearray | memoryview) -> tuple[str, FileCategory]:
    """Imzo bo'yicha ``(mime, category)``.

    Aniqlanmasa yoki fayl juda qisqa bo'lsa — ``("application/octet-stream",
    FileCategory.OTHER)``. Bu funksiya **faqat** berilgan baytlarga qaraydi:
    kengaytma va fayl nomi hisobga olinmaydi.
    """
    data = bytes(head or b"")
    if len(data) < 4:
        return _UNKNOWN

    # --- rasmlar -----------------------------------------------------------
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", FileCategory.PHOTO
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", FileCategory.PHOTO
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", FileCategory.ANIMATION

    # --- hujjatlar ---------------------------------------------------------
    if data[:4] == b"%PDF":
        return "application/pdf", FileCategory.DOCUMENT

    # --- audio -------------------------------------------------------------
    if data[:4] == b"fLaC":
        return "audio/flac", FileCategory.AUDIO
    if data[:4] == b"OggS":
        return "audio/ogg", FileCategory.AUDIO
    if data[:4] == b"RIFF":
        return _avi_or_riff(data)
    if data[:4] == b"\x1aE\xdf\xa3":  # EBML
        doctype = _ebml_doctype(data)
        if doctype == "webm":
            return "video/webm", FileCategory.VIDEO
        return "video/x-matroska", FileCategory.VIDEO
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return _ftyp_type(data)
    if _looks_like_mp3(data):
        return "audio/mpeg", FileCategory.AUDIO

    # --- arxivlar ----------------------------------------------------------
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "application/zip", FileCategory.ARCHIVE
    if data[:6] == b"7z\xbc\xaf\x27\x1c":
        return "application/x-7z-compressed", FileCategory.ARCHIVE
    if data[:4] == b"Rar!":
        return "application/vnd.rar", FileCategory.ARCHIVE
    if data[:2] == b"\x1f\x8b":
        return "application/gzip", FileCategory.ARCHIVE

    return _UNKNOWN


def detect_file(path: str | os.PathLike[str], size: int = SNIFF_BYTES) -> tuple[str, FileCategory]:
    """Faylning **birinchi ~4 KB** ini o'qib tur aniqlash."""
    return detect_bytes(read_head(path, size))


#: Qulaylik uchun taxallus (chaqiruvchilar ikkala nomni ham ishlatishi mumkin).
detect_path = detect_file


def effective_type(record: Any) -> tuple[str | None, FileCategory]:
    """API uchun "haqiqiy" ``(mime, category)`` juftligi.

    Tartib:

    1. bazadagi ``mime_type`` mavjud va aniq bo'lsa — **saqlanadi**;
    2. aks holda kengaytmadan taxmin qilinadi (``mimetypes``);
    3. kategoriya bazada "other"/bo'sh bo'lsa, mime/kengaytma bo'yicha qayta
       hisoblanadi (:meth:`File.detect_category`).

    Diskka murojaat qilmaydi — ro'yxat so'rovlarida har bir fayl uchun IO
    bo'lmasligi shart.
    """
    name = getattr(record, "file_name", "") or ""
    stored_mime = getattr(record, "mime_type", None)
    mime = stored_mime if not is_generic_mime(stored_mime) else None
    if mime is None:
        guessed = guess_type(name)[0]
        if guessed:
            mime = guessed

    category = getattr(record, "category", None)
    if category is None or category is FileCategory.OTHER:
        category = File.detect_category(name, mime)
    return mime, category


def is_streamable_category(category: Any) -> bool:
    """Kategoriya Mini App pleerida ijro etilishi mumkinmi."""
    return category in STREAMABLE_CATEGORIES


# ---------------------------------------------------------------------------
# Backfill (bir martalik migratsiya)
# ---------------------------------------------------------------------------
def media_cache_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Lokal media kesh katalogi.

    ``MEDIA_CACHE_DIR`` env yoki aniq berilgan yo'l; sukut bo'yicha
    ``/var/lib/bigcloud/media`` (MTProto sessiyasi yonida).
    """
    if explicit:
        return Path(explicit).expanduser()
    return Path(os.environ.get("MEDIA_CACHE_DIR", "/var/lib/bigcloud/media")).expanduser()


def resolve_media_path(record: Any, directory: str | os.PathLike[str] | None = None) -> Path | None:
    """Yozuvga mos keladigan lokal faylni topish (bo'lmasa ``None``).

    Nomzodlar tartibi: ``<dir>/<file_name>``, ``<dir>/<uuid>``,
    ``<dir>/<uuid><ext>``, so'ng ``<uuid>*`` glob. Bu Telegram (bulut) bilan
    ishlaydigan yozuvlarda fayl diskda bo'lmasa ``None`` beradi — backfill
    ularni o'tkazib yuboradi.
    """
    base = media_cache_dir(directory)
    name = str(getattr(record, "file_name", "") or "").strip()
    candidates: list[Path] = []
    if name:
        candidates.append(base / name)
        candidates.append(base / Path(name).name)
    file_id = getattr(record, "id", None)
    if file_id is not None:
        stem = str(file_id)
        candidates.append(base / stem)
        candidates.extend(sorted(base.glob(f"{stem}.*")))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - g'alati fayl tizimi
            continue
    return None


@dataclass
class BackfillStats:
    """Backfill yakuniy hisoboti."""

    scanned: int = 0
    fixed: int = 0
    unchanged: int = 0
    skipped_missing: int = 0
    skipped_unknown: int = 0
    dry_run: bool = False
    changes: list[tuple[str, str | None, str, str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "fixed": self.fixed,
            "unchanged": self.unchanged,
            "skipped_missing": self.skipped_missing,
            "skipped_unknown": self.skipped_unknown,
            "dry_run": self.dry_run,
        }


async def run_backfill(
    *,
    dry_run: bool = False,
    directory: str | os.PathLike[str] | None = None,
    limit: int | None = None,
    session: Any | None = None,
    records: Sequence[Any] | None = None,
) -> BackfillStats:
    """``mime_type`` bo'sh/``octet-stream`` yozuvlar uchun turni yangilash.

    Faqat shu ikki maydon (``mime_type``, ``category``) o'zgaradi. Diskda
    topilmagan fayllar hisobga olinadi va o'tkazib yuboriladi. ``dry_run=True``
    da hech narsa yozilmaydi.
    """
    from sqlalchemy import func, or_, select

    stats = BackfillStats(dry_run=dry_run)

    async def _process(items: Iterable[Any]) -> None:
        for record in items:
            stats.scanned += 1
            path = resolve_media_path(record, directory)
            if path is None:
                stats.skipped_missing += 1
                continue
            mime, category = detect_file(path)
            if is_generic_mime(mime):
                stats.skipped_unknown += 1
                continue
            old_mime = record.mime_type
            old_category = record.category
            new_category_value = (
                category.value if isinstance(category, FileCategory) else str(category)
            )
            old_category_value = (
                old_category.value
                if isinstance(old_category, FileCategory)
                else str(old_category)
            )
            if mime == old_mime and new_category_value == old_category_value:
                stats.unchanged += 1
                continue
            stats.fixed += 1
            stats.changes.append(
                (str(record.file_name), old_mime, old_category_value, mime, new_category_value)
            )
            if not dry_run:
                record.mime_type = mime
                record.category = category

    if records is not None:
        await _process(records)
        return stats

    from db.database import session_scope

    async def _work(db_session: Any) -> None:
        statement = (
            select(File)
            .where(
                or_(
                    File.mime_type.is_(None),
                    File.mime_type == "",
                    func.lower(File.mime_type).in_(sorted(GENERIC_MIMES - {""})),
                )
            )
            .order_by(File.created_at.asc())
        )
        if limit:
            statement = statement.limit(int(limit))
        result = await db_session.scalars(statement)
        await _process(result.all())
        if not dry_run and stats.fixed:
            await db_session.commit()

    if session is not None:
        await _work(session)
        return stats
    async with session_scope() as owned:
        await _work(owned)
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.filetype",
        description="Kontent bo'yicha fayl turini aniqlash va backfill.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="mime_type bo'sh/octet-stream yozuvlar uchun turni yangilash",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="hech narsani yozmasdan nechta yozuv tuzatilishini ko'rsatish",
    )
    parser.add_argument(
        "--media-dir",
        default=None,
        help="lokal media kesh katalogi (sukut: MEDIA_CACHE_DIR yoki /var/lib/bigcloud/media)",
    )
    parser.add_argument("--limit", type=int, default=None, help="eng ko'pi bilan N ta yozuv")
    parser.add_argument("--file", default=None, help="bitta fayl uchun turni aniqlab ko'rsatish")
    parser.add_argument(
        "--remux",
        default=None,
        help="lokal faylni (mkv/avi) brauzer o'ynay oladigan mp4 ga remux qilish",
    )
    return parser


def _print_detection(path: str) -> int:
    mime, category = detect_file(path)
    print(f"{path}: {mime} / {category.value}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.file:
        return _print_detection(args.file)

    if args.remux:
        from services.remux import remux_to_mp4

        result = remux_to_mp4(args.remux)
        print(result.message)
        return 0 if result.ok else 1

    if not args.backfill:
        parser.print_help()
        return 2

    stats = asyncio.run(
        run_backfill(dry_run=args.dry_run, directory=args.media_dir, limit=args.limit)
    )
    mode = "DRY-RUN" if stats.dry_run else "APPLIED"
    print(
        f"[{mode}] scanned={stats.scanned} fixed={stats.fixed} "
        f"unchanged={stats.unchanged} missing={stats.skipped_missing} "
        f"unknown={stats.skipped_unknown}"
    )
    for name, old_mime, old_category, new_mime, new_category in stats.changes[:50]:
        print(f"  {name}: {old_mime or '-'}/{old_category} -> {new_mime}/{new_category}")
    if stats.dry_run:
        print("(dry-run: hech narsa yozilmadi)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
