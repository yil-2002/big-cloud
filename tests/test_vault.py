"""📦 Shaxsiy arxiv kanali (vault): nusxalash, DB'ga yozish, tiklash, holat.

Telegram tarmog'iga chiqilmaymiz — ``Bot`` o'rniga soxta obyekt ishlatiladi,
shuning uchun testlar oflayn va deterministik.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import CopyMessage
from aiogram.types import MessageId

from db import database
from services.vault import ArchiveVault, get_vault, set_vault
from tests.conftest import make_file


# --------------------------------------------------------------------------- #
# Soxta Telegram obyektlari
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FakeMessage:
    """``archive_message`` faqat ``chat.id`` va ``message_id`` ni o'qiydi."""

    chat: Any
    message_id: int


@dataclass(slots=True)
class FakeChat:
    id: int = -1001234567890
    title: str = "HugeCloud Arxiv"
    username: str | None = None
    type: str = "channel"


@dataclass(slots=True)
class FakeMe:
    id: int = 4242


@dataclass(slots=True)
class FakeMember:
    status: str = "administrator"
    can_post_messages: bool | None = True


def _method() -> CopyMessage:
    """Istisnolar uchun haqiqiy ``TelegramMethod`` obyekti (aiogram talabi)."""
    return CopyMessage(chat_id=-1001234567890, from_chat_id=1, message_id=1)


class FakeBot:
    """``copy_message``/``forward_message``/``get_chat`` uchun minimal stub."""

    def __init__(self, *, fail_times: int = 0, fail_with: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.copy_kwargs: list[dict[str, Any]] = []
        self._fail_times = fail_times
        self._fail_with = fail_with or TelegramBadRequest(_method(), "channel not found")
        self.next_message_id = 900

    async def copy_message(self, **kwargs: Any) -> MessageId:
        self.calls.append("copy_message")
        self.copy_kwargs.append(kwargs)
        self._maybe_fail()
        self.next_message_id += 1
        return MessageId(message_id=self.next_message_id)

    async def forward_message(self, **kwargs: Any) -> FakeMessage:
        self.calls.append("forward_message")
        self._maybe_fail()
        return FakeMessage(chat=FakeChat(id=kwargs.get("chat_id", 1)), message_id=555)

    async def delete_message(self, chat_id: Any, message_id: int) -> bool:
        self.calls.append("delete_message")
        return True

    async def get_chat(self, chat_id: Any) -> FakeChat:
        self.calls.append("get_chat")
        self._maybe_fail()
        return FakeChat(id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else 1)

    async def me(self) -> FakeMe:
        self.calls.append("me")
        return FakeMe()

    async def get_chat_member(self, chat_id: Any, user_id: int) -> FakeMember:
        self.calls.append("get_chat_member")
        return FakeMember()

    def _maybe_fail(self) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._fail_with


def extract_file_id_stub(message: Any) -> str | None:
    """``services.telegram.extract_file_id`` o'rniga ishlatiladigan stub."""
    return "CHANNEL-FILE-ID"


@pytest.fixture()
async def vault_env(session, settings):
    """Vault uchun tayyor muhit: (storage, settings, session_factory)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    factory_engine = create_async_engine(settings.database_url, future=True)
    factory = async_sessionmaker(factory_engine, expire_on_commit=False)
    # ``get_session_factory()`` chaqiruvlari test bazasiga yo'naltiriladi.
    database._engine = factory_engine
    database._session_factory = factory
    yield factory
    database._engine = None
    database._session_factory = None
    await factory_engine.dispose()


@pytest.fixture(autouse=True)
def _patch_extract_file_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``recover_file`` ichidagi import qilingan yordamchini stub'laymiz."""
    import services.vault as vault_module

    monkeypatch.setattr(vault_module, "extract_file_id", extract_file_id_stub, raising=False)


# --------------------------------------------------------------------------- #
# Sozlash / o'chirish
# --------------------------------------------------------------------------- #
def test_enabled_follows_configuration(settings) -> None:
    """``ARCHIVE_CHANNEL_ID`` berilgan -> vault yoqilgan."""
    vault = ArchiveVault(FakeBot(), settings=settings)  # type: ignore[arg-type]
    assert vault.enabled is True
    assert vault.channel_id == settings.archive.channel_id


async def test_archive_disabled_returns_without_network(settings, storage, user) -> None:
    """Kanal sozlanmagan bo'lsa hech qanday Telegram so'rovi ketmaydi."""
    settings.archive.channel_id = ""
    bot = FakeBot()
    vault = ArchiveVault(bot, settings=settings)  # type: ignore[arg-type]
    assert vault.enabled is False

    record = await make_file(storage, user.telegram_id, name="a.txt")
    result = await vault.archive_message(
        FakeMessage(chat=FakeChat(id=user.telegram_id), message_id=11),
        file_uuid=record.id,
        telegram_id=user.telegram_id,
        file_name=record.file_name,
    )
    assert result.ok is False
    assert result.error == "archive disabled"
    assert bot.calls == []                        # tarmoqqa chiqmadi


# --------------------------------------------------------------------------- #
# Arxivlash
# --------------------------------------------------------------------------- #
async def test_archive_message_persists_channel_ids(settings, storage, user, vault_env) -> None:
    """Nusxa kanalga ketadi va ``archive_*`` ustunlari bazaga yoziladi."""
    bot = FakeBot()
    vault = ArchiveVault(bot, settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    record = await make_file(storage, user.telegram_id, name="hisobot.pdf", size=2048)

    result = await vault.archive_message(
        FakeMessage(chat=FakeChat(id=user.telegram_id), message_id=77),
        file_uuid=record.id,
        telegram_id=user.telegram_id,
        file_name=record.file_name,
        caption="muhim hujjat",
    )

    assert result.ok is True
    assert result.message_id == bot.next_message_id
    assert "copy_message" in bot.calls

    # Maxfiylik: kanal izohida foydalanuvchi ID'si, fayl nomi va izoh YO'Q.
    caption = bot.copy_kwargs[0]["caption"]
    assert str(user.telegram_id) not in caption
    assert "hisobot.pdf" not in caption
    assert "muhim hujjat" not in caption
    assert caption.startswith(settings.archive.caption_prefix)
    assert bot.copy_kwargs[0]["chat_id"] == settings.archive.channel_id

    # Baza qatori yangilangan (yangi sessiyadan o'qiymiz).
    async with vault_env() as check:
        from db.models import File

        row = await check.get(File, record.id)
        assert row is not None
        assert row.archive_message_id == result.message_id
        assert row.archive_chat_id == str(settings.archive.channel_id)
        assert row.archived_at is not None

    # ``best_file_id`` arxiv nusxasi bo'lmaguncha asl file_id ni qaytaradi.
    assert record.best_file_id == record.file_id


async def test_archive_failure_does_not_break_upload(settings, storage, user, vault_env) -> None:
    """Kanal javob bermasa — fayl baribir saqlangan, natija ``ok=False``."""
    bot = FakeBot(fail_times=99, fail_with=TelegramBadRequest(_method(), "chat not found"))
    settings.archive.max_attempts = 2
    vault = ArchiveVault(bot, settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    record = await make_file(storage, user.telegram_id, name="x.mp4")

    result = await vault.archive_message(
        FakeMessage(chat=FakeChat(id=user.telegram_id), message_id=5),
        file_uuid=record.id,
        telegram_id=user.telegram_id,
        file_name="x.mp4",
    )
    assert result.ok is False
    assert result.error
    assert result.attempts == 2                   # max_attempts marta urindi

    # Fayl bazada qoldi (upload arxivga bog'liq emas).
    _items, total = await storage.list_files(user.telegram_id)
    assert total == 1


async def test_archive_retries_after_rate_limit(settings, storage, user, vault_env) -> None:
    """``TelegramRetryAfter`` bo'lsa kutib qayta urinadi va muvaffaqiyat qayd etadi."""
    bot = FakeBot(fail_times=1, fail_with=TelegramRetryAfter(_method(), "slow down", retry_after=1))
    settings.archive.max_attempts = 3
    settings.archive.retry_delay = 0.0            # testda kutmaslik uchun
    vault = ArchiveVault(bot, settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    record = await make_file(storage, user.telegram_id, name="retry.bin")

    result = await vault.archive_message(
        FakeMessage(chat=FakeChat(id=user.telegram_id), message_id=9),
        file_uuid=record.id,
        telegram_id=user.telegram_id,
        file_name="retry.bin",
    )
    assert result.ok is True
    assert result.attempts == 2


async def test_schedule_runs_in_background(settings, storage, user, vault_env) -> None:
    """``ARCHIVE_BLOCKING=false`` — vazifa fon rejimida bajariladi."""
    bot = FakeBot()
    vault = ArchiveVault(bot, settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    record = await make_file(storage, user.telegram_id, name="bg.txt")

    async def job():
        return await vault.archive_message(
            FakeMessage(chat=FakeChat(id=user.telegram_id), message_id=3),
            file_uuid=record.id,
            telegram_id=user.telegram_id,
            file_name="bg.txt",
        )

    task = vault.schedule(job)
    assert task is not None
    result = await asyncio.wait_for(task, timeout=5)
    assert result.ok is True


def test_schedule_returns_none_when_disabled(settings) -> None:
    settings.archive.channel_id = ""
    vault = ArchiveVault(FakeBot(), settings=settings)  # type: ignore[arg-type]
    assert vault.schedule(lambda: asyncio.sleep(0)) is None  # type: ignore[arg-type,return-value]


# --------------------------------------------------------------------------- #
# Tiklash (recover)
# --------------------------------------------------------------------------- #
async def test_recover_file_stores_channel_file_id(settings, storage, user, vault_env) -> None:
    """Asl ``file_id`` eskirganda kanal nusxasidan yangi id olinadi."""
    bot = FakeBot()
    vault = ArchiveVault(bot, settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    record = await make_file(storage, user.telegram_id, name="eski.mp4")
    record.mark_archived(chat_id=settings.archive.channel_id, message_id=1234)
    storage.session.add(record)
    await storage.session.commit()

    recovered = await vault.recover_file(record, to_chat_id=user.telegram_id)
    assert recovered == "CHANNEL-FILE-ID"

    async with vault_env() as check:
        from db.models import File

        row = await check.get(File, record.id)
        assert row is not None
        assert row.archive_file_id == "CHANNEL-FILE-ID"
        # ``best_file_id`` endi arxiv nusxasini afzal ko'radi.
        assert row.best_file_id == "CHANNEL-FILE-ID"

    # Vaqtinchalik xabarlar o'chirilgan (probe + forward).
    assert bot.calls.count("delete_message") == 2


async def test_recover_without_archive_copy_is_noop(settings, storage, user, vault_env) -> None:
    """Arxiv nusxasi yo'q fayl uchun tiklash hech narsa qilmaydi."""
    bot = FakeBot()
    vault = ArchiveVault(bot, settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    record = await make_file(storage, user.telegram_id, name="arxivsiz.txt")

    assert await vault.recover_file(record, to_chat_id=user.telegram_id) is None
    assert bot.calls == []


# --------------------------------------------------------------------------- #
# Holat (admin panel uchun)
# --------------------------------------------------------------------------- #
async def test_status_reports_healthy_channel(settings, vault_env) -> None:
    """⚙️ Tizim sozlamalari: kanal topildi, bot admin, xabar yubora oladi."""
    vault = ArchiveVault(FakeBot(), settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    state = await vault.status()
    assert state.configured is True
    assert state.chat_exists is True
    assert state.bot_is_admin is True
    assert state.bot_can_post is True
    assert state.healthy is True
    assert "arxiv faol" in state.as_line()
    assert state.to_dict()["enabled"] is True


async def test_status_reports_missing_channel(settings, vault_env) -> None:
    """Kanal topilmasa holat ``healthy=False`` va xato matni bilan."""
    bot = FakeBot(fail_times=99, fail_with=TelegramBadRequest(_method(), "chat not found"))
    vault = ArchiveVault(bot, settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    state = await vault.status()
    assert state.chat_exists is False
    assert state.healthy is False
    assert "Kanal topilmadi" in state.as_line()


async def test_status_when_not_configured(settings, vault_env) -> None:
    settings.archive.channel_id = ""
    vault = ArchiveVault(FakeBot(), settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    state = await vault.status()
    assert state.configured is False
    assert state.channel_id == "<unset>"
    assert "sozlanmagan" in state.as_line()


async def test_counters_split_archived_and_pending(settings, storage, user, vault_env) -> None:
    """``(arxivlangan, kutilayotgan)`` hisoblagichi."""
    vault = ArchiveVault(FakeBot(), settings=settings, session_factory=vault_env)  # type: ignore[arg-type]
    first = await make_file(storage, user.telegram_id, name="a.txt")
    await make_file(storage, user.telegram_id, name="b.txt")
    first.mark_archived(chat_id=settings.archive.channel_id, message_id=7)
    storage.session.add(first)
    await storage.session.commit()

    archived, pending = await vault.counters()
    assert archived == 1
    assert pending == 1


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
def test_vault_singleton(settings) -> None:
    """``set_vault``/``get_vault`` — bot bir marta yaratiladi."""
    set_vault(None)
    bot = FakeBot()
    created = get_vault(bot, settings=settings)  # type: ignore[arg-type]
    assert isinstance(created, ArchiveVault)
    assert get_vault() is created               # ikkinchi chaqiriq yangi nusxa emas
    set_vault(None)
    assert get_vault() is None
