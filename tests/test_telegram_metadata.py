"""📤 Telegram media metadata: ``extract_file_info`` va kategoriya aniqlash.

Nega bu fayl kerak: ``services/telegram.py`` da ``File.sanitize_filename`` va
``File.detect_category`` **runtime** da chaqiriladi, ammo modul darajasida
``File`` importi yo'q edi — natijada har qanday hujjat/rasm yuklashda
``NameError: name 'File' is not defined`` yuzaga kelardi. Testlar bunday
xatoni birinchi chaqiruvdayoq ushlaydi.
"""

from __future__ import annotations

from typing import Any

from aiogram.types import (
    Audio,
    Chat,
    Document,
    Message,
    PhotoSize,
    Video,
    Voice,
)

from db.models import FILE_GROUPS, FileCategory
from services.telegram import (
    TelegramFileInfo,
    extract_file_info,
    is_supported_media,
)


# --------------------------------------------------------------------------- #
# Yordamchilar (Telegram media obyektlari o'rniga yengil stub'lar)
# --------------------------------------------------------------------------- #
def _message(**media: Any) -> Message:
    """Media maydonlari to'ldirilgan ``Message`` (aiogram validatsiyasidan o'tadi)."""
    return Message(
        message_id=1,
        date=1_789_000_000,
        chat=Chat(id=777, type="private"),
        **media,
    )


def _document(
    file_id: str = "AgAD-test",
    unique_id: str = "uniq-1",
    *,
    file_name: str | None = None,
    file_size: int = 4096,
    mime_type: str | None = None,
) -> Document:
    return Document(
        file_id=file_id,
        file_unique_id=unique_id,
        file_name=file_name,
        file_size=file_size,
        mime_type=mime_type,
    )


def _photo(
    file_id: str = "p-1",
    unique_id: str = "u-1",
    *,
    file_size: int = 1024,
    width: int = 90,
    height: int = 90,
) -> PhotoSize:
    return PhotoSize(
        file_id=file_id,
        file_unique_id=unique_id,
        file_size=file_size,
        width=width,
        height=height,
    )


def _video(**kwargs: Any) -> Video:
    payload = {"file_id": "v-1", "file_unique_id": "vu-1", "width": 640, "height": 360, "duration": 10}
    payload.update(kwargs)
    return Video(**payload)


def _audio(**kwargs: Any) -> Audio:
    payload = {"file_id": "a-1", "file_unique_id": "au-1", "duration": 10}
    payload.update(kwargs)
    return Audio(**payload)


def _voice(**kwargs: Any) -> Voice:
    payload = {"file_id": "vo-1", "file_unique_id": "vou-1", "duration": 5}
    payload.update(kwargs)
    return Voice(**payload)


#: Qisqa nom — testlarda ``_media`` o'rniga ishlatiladi (hujjat uchun).
_media = _document


# --------------------------------------------------------------------------- #
# Hujjat (document) — ``File`` runtime importini tekshiradigan asosiy yo'l
# --------------------------------------------------------------------------- #
def test_extract_document_metadata() -> None:
    message = _message(document=_media(file_name="hisobot 2026.pdf", mime_type="application/pdf"))
    info = extract_file_info(message)

    assert isinstance(info, TelegramFileInfo)
    assert info.file_id == "AgAD-test"
    assert info.file_unique_id == "uniq-1"
    assert info.file_size == 4096
    assert info.mime_type == "application/pdf"
    # Fayl nomi xavfsizlashtiriladi (``File.sanitize_filename`` chaqiriladi).
    assert info.file_name
    assert "/" not in info.file_name and "\\" not in info.file_name
    # Muhimi: PDF "📄 Hujjatlar" guruhiga tushadi (DOCUMENT yoki OTHER orqali).
    assert info.category in FILE_GROUPS["document"]


def test_extract_archive_document_category() -> None:
    """``.zip`` hujjat → 📦 ARXIV guruhiga tushadi."""
    message = _message(document=_media(file_name="loyiha.zip", mime_type="application/zip"))
    info = extract_file_info(message)
    assert info is not None
    assert info.category == FileCategory.ARCHIVE


def test_extract_apk_document_category() -> None:
    """APK → 🤖 apps guruhiga tushadi (Mini App'dagi APK/IPA tab'lari uchun)."""
    message = _message(document=_media(file_name="hugecloud.apk", mime_type="application/vnd.android.package-archive"))
    info = extract_file_info(message)
    assert info is not None
    # APK legacy bot'dagi kabi alohida "🤖 Ilovalar" guruhiga tegishli.
    assert info.category in FILE_GROUPS["apps"]


def test_dangerous_file_name_is_sanitised() -> None:
    """Yo'l ajratuvchilari va boshqaruv belgilari fayl nomidan tozalanadi."""
    message = _message(document=_media(file_name="../../etc/passwd\x00.txt", mime_type="text/plain"))
    info = extract_file_info(message)
    assert info is not None
    assert "/" not in info.file_name and "\\" not in info.file_name
    assert "\x00" not in info.file_name
    assert info.file_name  # bo'sh qolmasligi kerak (fallback bor)
    # Nul bayt va yo'l ajratuvchilari yo'q = HTTP header/path traversal xavfsiz.
    assert all(char not in info.file_name for char in "\n\r\t")


def test_missing_file_name_falls_back() -> None:
    """``file_name`` bo'lmasa (sticker/voice) nom yasab beriladi."""
    message = _message(document=_media(file_name=None))
    info = extract_file_info(message)
    assert info is not None
    assert info.file_name
    assert "uniq-1" in info.file_name or info.file_name.endswith(".bin")


# --------------------------------------------------------------------------- #
# Rasm, video, audio, ovoz
# --------------------------------------------------------------------------- #
def test_extract_photo_picks_largest_size() -> None:
    small = _photo(file_id="p-small", unique_id="u-small", file_size=1024, width=90, height=90)
    large = _photo(file_id="p-large", unique_id="u-large", file_size=204800, width=1280, height=720)
    message = _message(photo=[small, large], caption="manzara.jpg")

    info = extract_file_info(message)
    assert info is not None
    assert info.file_id == "p-large"          # eng katta o'lcham tanlanadi
    assert info.category == FileCategory.PHOTO
    assert info.mime_type == "image/jpeg"
    assert info.width == 1280 and info.height == 720
    # Sarlavhadan fayl nomi olinadi.
    assert info.file_name == "manzara.jpg"


def test_photo_name_falls_back_when_caption_has_no_extension() -> None:
    message = _message(photo=[_photo(file_size=10)], caption="shunchaki izoh")
    info = extract_file_info(message)
    assert info is not None
    assert info.file_name.startswith("photo_")
    assert info.file_name.endswith(".jpg")


def test_extract_video_metadata_with_duration() -> None:
    message = _message(video=_video(file_name="film.mp4", mime_type="video/mp4", duration=125, width=1920, height=1080))
    info = extract_file_info(message)
    assert info is not None
    assert info.category == FileCategory.VIDEO
    assert info.duration == 125
    assert info.width == 1920


def test_extract_audio_metadata() -> None:
    message = _message(audio=_audio(file_name="qoshiq.mp3", mime_type="audio/mpeg", duration=200))
    info = extract_file_info(message)
    assert info is not None
    assert info.category == FileCategory.AUDIO
    assert info.duration == 200


def test_extract_voice_metadata() -> None:
    message = _message(voice=_voice(file_unique_id="voice-1", mime_type="audio/ogg", duration=12))
    info = extract_file_info(message)
    assert info is not None
    assert info.category == FileCategory.VOICE


def test_caption_truncated_to_1024() -> None:
    message = _message(document=_media(file_name="katta.txt"), caption="a" * 4000)
    info = extract_file_info(message)
    assert info is not None
    assert info.caption is not None
    assert len(info.caption) == 1024


def test_text_only_message_is_not_storable() -> None:
    message = _message(text="salom")
    assert extract_file_info(message) is None
    assert is_supported_media(message) is False


def test_is_supported_media_true_for_document() -> None:
    message = _message(document=_media(file_name="fayl.pdf"))
    assert is_supported_media(message) is True


def test_to_kwargs_shape_matches_storage_service() -> None:
    """``to_kwargs`` — ``StorageService.add_file`` imzosiga mos kalitlar."""
    message = _message(document=_media(file_name="fayl.pdf", mime_type="application/pdf"))
    info = extract_file_info(message)
    assert info is not None

    kwargs = info.to_kwargs()
    assert {"file_id", "file_unique_id", "file_name", "file_size"} <= set(kwargs)
    assert kwargs["file_name"] == info.file_name


def test_module_level_imports_cover_runtime_usage() -> None:
    """``services.telegram`` ichida ``File`` haqiqatan import qilingan (NameError yo'q)."""
    import services.telegram as telegram_module

    assert hasattr(telegram_module, "File"), (
        "services.telegram modulida File importi yo'q — sanitize_filename/detect_category "
        "chaqiruvi NameError beradi"
    )
    assert hasattr(telegram_module.File, "sanitize_filename")
    assert hasattr(telegram_module.File, "detect_category")
