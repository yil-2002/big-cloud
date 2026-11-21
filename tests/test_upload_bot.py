"""Bot orqali fayl yuklashda arxivlash xatti-harakatlarini tekshirish."""

from __future__ import annotations

from unittest.mock import AsyncMock

from aiogram.types import Message

from bot.handlers.upload import _archive
from db.models import File, FileCategory
from services.storage import StoredFile
from services.vault import ArchiveResult


async def test_archive_success_deletes_message(monkeypatch, settings, user) -> None:
    """_archive() arxivlash muvaffaqiyatli bo'lsa xabarni o'chiradi."""
    import bot.handlers.upload as upload_module

    message = AsyncMock(spec=Message)
    message.delete = AsyncMock()

    record = File(
        id="00000000-0000-0000-0000-000000000000",
        user_id=user.telegram_id,
        file_id="FILE_ID",
        file_unique_id="UNIQ",
        file_name="test.txt",
        file_size=100,
        category=FileCategory.DOCUMENT,
    )
    stored = StoredFile(record=record, is_new=True)

    class FakeVault:
        enabled = True
        async def archive_message(self, *args, **kwargs):
            return ArchiveResult(ok=True, message_id=123)

    monkeypatch.setattr(upload_module, "get_vault", lambda settings: FakeVault())
    
    settings.archive.blocking = True

    await _archive(
        message,
        record=record,
        stored=stored,
        user=user,
        settings=settings,
        in_album=False,
    )

    message.delete.assert_called_once()


async def test_archive_fail_keeps_message(monkeypatch, settings, user) -> None:
    """_archive() arxivlash xato bo'lsa xabarni o'chirmaydi."""
    import bot.handlers.upload as upload_module

    message = AsyncMock(spec=Message)
    message.delete = AsyncMock()

    record = File(
        id="00000000-0000-0000-0000-000000000000",
        user_id=user.telegram_id,
        file_id="FILE_ID",
        file_unique_id="UNIQ",
        file_name="test.txt",
        file_size=100,
        category=FileCategory.DOCUMENT,
    )
    stored = StoredFile(record=record, is_new=True)

    class FakeVault:
        enabled = True
        async def archive_message(self, *args, **kwargs):
            return ArchiveResult(ok=False, error="archive disabled")

    monkeypatch.setattr(upload_module, "get_vault", lambda settings: FakeVault())
    
    settings.archive.blocking = True

    await _archive(
        message,
        record=record,
        stored=stored,
        user=user,
        settings=settings,
        in_album=False,
    )

    message.delete.assert_not_called()
