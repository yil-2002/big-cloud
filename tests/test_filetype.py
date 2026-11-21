"""``services.filetype`` — kontent bo'yicha tur aniqlash va backfill testlari.

Nega muhim: Telegram kengaytmasiz fayl (``tmp_3295``) uchun mime bermasa yoki
``application/octet-stream`` qaytarsa, Mini App pleeri ochilmasdi. Bu testlar
imzo (magic bytes) jadvalini, "faqat boshini o'qish" kafolatini va bir martalik
backfill xavfsizligini tekshiradi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from db.models import FileCategory
from services.filetype import (
    SNIFF_BYTES,
    BackfillStats,
    detect_bytes,
    detect_file,
    effective_type,
    is_generic_mime,
    read_head,
    resolve_media_path,
    run_backfill,
)

# --- sintetik sarlavhalar ---------------------------------------------------
MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41"
M4V = b"\x00\x00\x00\x20ftypM4V \x00\x00\x02\x00M4V M4A mp42isom"
M4A = b"\x00\x00\x00\x20ftypM4A \x00\x00\x02\x00M4A mp42isom"
MOV = b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00qt  "
WEBM = (
    b"\x1aE\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04"
    b"\x42\xf3\x81\x08\x42\x82\x84webm\x42\x87\x81\x04"
)
MKV = (
    b"\x1aE\xdf\xa3\xa0\x42\x86\x81\x01\x42\xf7\x81\x01\x42\xf2\x81\x04"
    b"\x42\xf3\x81\x08\x42\x82\x88matroska\x42\x87\x81\x04"
)
AVI = b"RIFF\x24\x00\x00\x00AVI LIST"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"
MP3_SYNC = b"\xff\xfb\x90\x64\x00\x00\x00\x00"
OGG = b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00"
FLAC = b"fLaC\x00\x00\x00\x22"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF"
GIF = b"GIF89a\x01\x00\x01\x00"
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3"
ZIP = b"PK\x03\x04\x14\x00\x00\x00"

#: (nom, sarlavha, kutilgan mime, kutilgan kategoriya)
CASES: tuple[tuple[str, bytes, str, FileCategory], ...] = (
    ("mp4", MP4, "video/mp4", FileCategory.VIDEO),
    ("m4v", M4V, "video/mp4", FileCategory.VIDEO),
    ("m4a", M4A, "audio/mp4", FileCategory.AUDIO),
    ("mov", MOV, "video/quicktime", FileCategory.VIDEO),
    ("webm", WEBM, "video/webm", FileCategory.VIDEO),
    ("mkv", MKV, "video/x-matroska", FileCategory.VIDEO),
    ("avi", AVI, "video/x-msvideo", FileCategory.VIDEO),
    ("wav", WAV, "audio/wav", FileCategory.AUDIO),
    ("webp", WEBP, "image/webp", FileCategory.PHOTO),
    ("mp3-id3", MP3_ID3, "audio/mpeg", FileCategory.AUDIO),
    ("mp3-sync", MP3_SYNC, "audio/mpeg", FileCategory.AUDIO),
    ("ogg/opus", OGG, "audio/ogg", FileCategory.AUDIO),
    ("flac", FLAC, "audio/flac", FileCategory.AUDIO),
    ("png", PNG, "image/png", FileCategory.PHOTO),
    ("jpeg", JPEG, "image/jpeg", FileCategory.PHOTO),
    ("gif", GIF, "image/gif", FileCategory.ANIMATION),
    ("pdf", PDF, "application/pdf", FileCategory.DOCUMENT),
    ("zip", ZIP, "application/zip", FileCategory.ARCHIVE),
)


@pytest.mark.parametrize("name,head,mime,category", CASES, ids=[case[0] for case in CASES])
def test_detect_bytes_magic_table(
    name: str, head: bytes, mime: str, category: FileCategory
) -> None:
    """Har bir format uchun imzo bo'yicha to'g'ri ``(mime, category)``."""
    assert detect_bytes(head) == (mime, category), name


@pytest.mark.parametrize(
    "head",
    [b"", b"a", b"ab", b"\x00\x01\x02\x03", b"\x00" * 16, b"not a real file at all"],
    ids=["empty", "one", "two", "garbage4", "zeros", "text"],
)
def test_detect_bytes_unknown_is_octet_stream(head: bytes) -> None:
    """Noto'g'ri/qisqa fayl — ``octet-stream``/``other`` (xato bermaydi)."""
    assert detect_bytes(head) == ("application/octet-stream", FileCategory.OTHER)


def test_detect_file_ignores_extension(tmp_path: Path) -> None:
    """Nom kengaytmasiz bo'lsa ham kontent aniqlanadi (``tmp_3295``)."""
    target = tmp_path / "tmp_3295"
    target.write_bytes(MP4 + b"\x00" * 100)
    assert detect_file(target) == ("video/mp4", FileCategory.VIDEO)


def test_detect_file_reads_only_head(tmp_path: Path) -> None:
    """Katta faylda faqat birinchi ~4 KB o'qiladi; 4 KB dan keyingi imzo ko'rilmaydi."""
    target = tmp_path / "big_tmp"
    # Boshida "noma'lum" baytlar, imzo esa 5 KB dan keyin — topilmasligi kerak.
    payload = b"\x00" * (SNIFF_BYTES + 1024) + MP4
    target.write_bytes(payload)
    assert detect_bytes(target.read_bytes()) != (
        "video/mp4",
        FileCategory.VIDEO,
    ), "nazorat: to'liq o'qilganda imzo topilardi"
    assert detect_file(target) == ("application/octet-stream", FileCategory.OTHER)


def test_read_head_limits_size(tmp_path: Path) -> None:
    """``read_head`` hech qachon ``size`` dan ko'p bayt qaytarmaydi."""
    target = tmp_path / "large.bin"
    target.write_bytes(b"x" * (SNIFF_BYTES * 3))
    assert len(read_head(target)) == SNIFF_BYTES
    assert len(read_head(target, 8)) == 8
    assert read_head(tmp_path / "yoq.bin") == b""


def test_is_generic_mime() -> None:
    assert is_generic_mime(None) is True
    assert is_generic_mime("") is True
    assert is_generic_mime("application/octet-stream") is True
    assert is_generic_mime("APPLICATION/OCTET-STREAM") is True
    assert is_generic_mime("video/mp4") is False


# ---------------------------------------------------------------------------
# ``effective_type`` — API uchun hisoblash
# ---------------------------------------------------------------------------
class _Record:
    """``File`` o'rnini bosuvchi minimal obyekt."""

    def __init__(self, name: str, mime: str | None, category: FileCategory | None) -> None:
        self.file_name = name
        self.mime_type = mime
        self.category = category


def test_effective_type_keeps_stored_value() -> None:
    record = _Record("clip.mp4", "video/mp4", FileCategory.VIDEO)
    assert effective_type(record) == ("video/mp4", FileCategory.VIDEO)


def test_effective_type_recomputes_category_from_mime() -> None:
    """Mime bor, kategoriya "other" — qayta hisoblanadi (kengaytmasiz nom)."""
    record = _Record("tmp_3295", "video/mp4", FileCategory.OTHER)
    assert effective_type(record) == ("video/mp4", FileCategory.VIDEO)


def test_effective_type_guesses_mime_from_extension() -> None:
    record = _Record("song.mp3", None, FileCategory.OTHER)
    mime, category = effective_type(record)
    assert mime == "audio/mpeg"
    assert category is FileCategory.AUDIO


def test_effective_type_unknown_stays_other() -> None:
    record = _Record("tmp_3295", "application/octet-stream", FileCategory.OTHER)
    mime, category = effective_type(record)
    assert mime is None
    assert category is FileCategory.OTHER


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------
def _write_media(directory: Path, name: str, payload: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(payload)
    return target


@pytest.fixture()
def media_dir(tmp_path: Path) -> Path:
    return tmp_path / "media"


async def test_resolve_media_path_by_name_and_id(tmp_path: Path, media_dir: Path) -> None:
    record = _Record("tmp_3295", None, FileCategory.OTHER)
    record.id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # type: ignore[attr-defined]
    assert resolve_media_path(record, media_dir) is None
    _write_media(media_dir, "tmp_3295", MP4)
    assert resolve_media_path(record, media_dir) == media_dir / "tmp_3295"


async def test_backfill_dry_run_changes_nothing(
    storage: Any, session: Any, user: Any, media_dir: Path
) -> None:
    """``--dry-run`` hech narsani o'zgartirmaydi, lekin tuzatishni hisoblaydi."""
    stored = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-dry",
        file_unique_id="uniq-dry",
        file_name="tmp_3295",
        file_size=1024,
        mime_type="application/octet-stream",
    )
    record = stored.record
    _write_media(media_dir, "tmp_3295", MP4)

    stats = await run_backfill(dry_run=True, directory=media_dir, session=session)

    assert isinstance(stats, BackfillStats)
    assert stats.dry_run is True
    assert stats.scanned == 1
    assert stats.fixed == 1
    assert (record.mime_type, record.category) == (
        "application/octet-stream",
        FileCategory.OTHER,
    ), "dry-run bazani o'zgartirmasligi shart"


async def test_backfill_fixes_octet_stream_record(
    storage: Any, session: Any, user: Any, media_dir: Path
) -> None:
    """``octet-stream`` yozuv kontent bo'yicha tuzatiladi (faqat 2 maydon)."""
    stored = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-fix",
        file_unique_id="uniq-fix",
        file_name="tmp_3295",
        file_size=2048,
        mime_type="application/octet-stream",
    )
    record = stored.record
    before = (record.file_name, record.file_size, record.visibility, record.views)
    _write_media(media_dir, "tmp_3295", MKV)

    stats = await run_backfill(dry_run=False, directory=media_dir, session=session)
    assert stats.fixed == 1
    assert record.mime_type == "video/x-matroska"
    assert record.category is FileCategory.VIDEO
    # Boshqa maydonlar tegmagan.
    assert (record.file_name, record.file_size, record.visibility, record.views) == before


async def test_backfill_skips_missing_and_unknown(
    storage: Any, session: Any, user: Any, media_dir: Path
) -> None:
    """Diskda yo'q fayl o'tkazib yuboriladi; xato ko'tarilmaydi."""
    await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-missing",
        file_unique_id="uniq-missing",
        file_name="tmp_missing",
        file_size=10,
        mime_type="application/octet-stream",
    )
    _write_media(media_dir, "tmp_bad", b"not-a-known-signature")
    await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-bad",
        file_unique_id="uniq-bad",
        file_name="tmp_bad",
        file_size=20,
        mime_type="application/octet-stream",
    )

    stats = await run_backfill(dry_run=False, directory=media_dir, session=session)
    assert stats.scanned == 2
    assert stats.skipped_missing == 1
    assert stats.skipped_unknown == 1
    assert stats.fixed == 0


async def test_backfill_leaves_good_records_alone(
    storage: Any, session: Any, user: Any, media_dir: Path
) -> None:
    """Aniq turdagi yozuvlar backfill oqimiga umuman kirmaydi."""
    await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-good",
        file_unique_id="uniq-good",
        file_name="clip.mp4",
        file_size=10,
        mime_type="video/mp4",
    )
    stats = await run_backfill(dry_run=False, directory=media_dir, session=session)
    assert stats.scanned == 0


# ---------------------------------------------------------------------------
# ``add_file(content_path=...)`` — yuklash paytida kontent aniqlash
# ---------------------------------------------------------------------------
async def test_add_file_detects_type_from_content_when_mime_unknown(
    storage: Any, user: Any, tmp_path: Path
) -> None:
    """Telegram ``octet-stream`` bergan kengaytmasiz fayl kontentdan aniqlanadi."""
    uploaded = tmp_path / "tmp_3295"
    uploaded.write_bytes(MP4 + b"\x00" * 64)

    stored = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-content",
        file_unique_id="uniq-content",
        file_name="tmp_3295",
        file_size=uploaded.stat().st_size,
        mime_type="application/octet-stream",
        content_path=str(uploaded),
    )

    assert stored.record.mime_type == "video/mp4"
    assert stored.record.category is FileCategory.VIDEO


async def test_add_file_content_detection_keeps_voice_classification(
    storage: Any, user: Any, tmp_path: Path
) -> None:
    """Ovozli xabar (``voice``) aniq klassifikatsiyasi kontent bilan buzilmaydi."""
    uploaded = tmp_path / "voice.ogg"
    uploaded.write_bytes(OGG + b"\x00" * 32)

    stored = await storage.add_file(
        telegram_id=user.telegram_id,
        file_id="AgAD-voice",
        file_unique_id="uniq-voice",
        file_name="voice.ogg",
        file_size=uploaded.stat().st_size,
        mime_type="audio/ogg",
        category=FileCategory.VOICE,
        content_path=str(uploaded),
    )

    assert stored.record.category is FileCategory.VOICE



# ---------------------------------------------------------------------------
# CLI (bitta fayl aniqlash — DB'siz)
# ---------------------------------------------------------------------------
def test_cli_file_detection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from services.filetype import main

    target = tmp_path / "tmp_3295"
    target.write_bytes(MP4)
    assert main(["--file", str(target)]) == 0
    out = capsys.readouterr().out
    assert "video/mp4" in out and "video" in out


def test_cli_without_action_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    from services.filetype import main

    assert main([]) == 2
    assert "backfill" in capsys.readouterr().out
