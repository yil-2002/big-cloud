"""🤖 Bot qatlami: inline rejim, autentifikatsiya darvozasi, o'zbekcha matnlar."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, InlineQuery, Message
from aiogram.types import User as TgUser

from bot import texts
from bot.handlers.inline import GROUP_PREFIXES, split_group
from bot.middlewares.auth_guard import (
    ALLOWED_COMMANDS,
    AuthGuardMiddleware,
    set_auth_override,
)
from bot.texts_hugecloud import UZ, install_language
from config import Settings
from db.models import File, FileCategory, User


# --------------------------------------------------------------------------- #
# Inline rejim: @bot <so'rov>
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("video hisob", ("media", "hisob")),
        ("media 2026", ("media", "2026")),
        ("rasm chelak", ("media", "chelak")),
        ("audio qoshiq", ("audio", "qoshiq")),
        ("musiqa", ("audio", "")),
        ("hujjat shartnoma", ("document", "shartnoma")),
        ("doc invoice", ("document", "invoice")),
        ("arxiv zip", ("archive", "zip")),
        ("apk telegram", ("apps", "telegram")),
        ("ipa app", ("apps", "app")),
        ("muhimlar", ("pinned", "")),
        ("hisobot", (None, "hisobot")),          # prefiks yo'q
        ("", (None, "")),
        ("   ", (None, "")),
        ("VIDEO katta", ("media", "katta")),      # registr muhim emas
    ],
)
def test_split_group(query: str, expected: tuple[str | None, str]) -> None:
    """Guruh prefiksi ajratiladi: ``media:so'rov`` -> (guruh, so'rov)."""
    assert split_group(query) == expected


def test_group_prefixes_cover_all_groups() -> None:
    """Har bir ``/files`` guruhi kamida bitta inline prefiksga ega."""
    covered = set(GROUP_PREFIXES.values())
    assert {"media", "audio", "document", "archive", "apps", "pinned"} <= covered


# --------------------------------------------------------------------------- #
# 🔐 Autentifikatsiya darvozasi (middleware)
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FakeAnswer:
    """``message.answer``/``callback.answer`` chaqiruvlarini yozib oladi."""

    calls: list[dict[str, Any]] = field(default_factory=list)


def _tg_user(telegram_id: int = 777) -> TgUser:
    return TgUser(id=telegram_id, is_bot=False, first_name="Tester")


def _chat(telegram_id: int = 777) -> Chat:
    return Chat(id=telegram_id, type="private")


def _message(text: str | None, telegram_id: int = 777) -> tuple[Message, FakeAnswer]:
    """Javob yuborishi kuzatiladigan soxta xabar."""
    record = FakeAnswer()

    async def answer(payload: str, **kwargs: Any) -> bool:
        record.calls.append({"text": payload, **kwargs})
        return True

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=_chat(telegram_id),
        from_user=_tg_user(telegram_id),
        text=text,
    )
    object.__setattr__(message, "answer", answer)
    return message, record


def _callback(data: str, telegram_id: int = 777) -> tuple[CallbackQuery, FakeAnswer]:
    record = FakeAnswer()

    async def answer(**kwargs: Any) -> bool:
        record.calls.append(kwargs)
        return True

    callback = CallbackQuery(
        id="cb-1",
        from_user=_tg_user(telegram_id),
        chat_instance="1",
        data=data,
        message=_message(None, telegram_id)[0],
    )
    object.__setattr__(callback, "answer", answer)
    return callback, record


def _inline(telegram_id: int = 777) -> tuple[InlineQuery, FakeAnswer]:
    record = FakeAnswer()

    async def answer(**kwargs: Any) -> bool:
        record.calls.append(kwargs)
        return True

    query = InlineQuery(id="iq-1", from_user=_tg_user(telegram_id), query="hisobot", offset="")
    object.__setattr__(query, "answer", answer)
    return query, record


def _row(
    *,
    telegram_id: int = 777,
    authenticated: bool = False,
    blocked: bool = False,
) -> User:
    """Baza qatorini eslatuvchi ``User`` (sessiyasiz — middleware uni talab qilmaydi)."""
    return User(
        telegram_id=telegram_id,
        username="tester",
        first_name="Tester",
        is_authenticated=authenticated,
        is_blocked=blocked,
    )


@pytest.fixture(autouse=True)
def _clear_override() -> Any:
    """Har testdan keyin runtime ustunni tozalaymiz."""
    set_auth_override(None)
    yield
    set_auth_override(None)
    AuthGuardMiddleware._last_touch.clear()


async def _run(middleware: AuthGuardMiddleware, event: Any, data: dict[str, Any]) -> Any:
    """Middleware'ni chaqirish; handler ``"CALLED"`` qaytaradi."""

    async def handler(_event: Any, _data: dict[str, Any]) -> str:
        return "CALLED"

    return await middleware(handler, event, data)


async def test_authenticated_user_passes(settings: Settings) -> None:
    middleware = AuthGuardMiddleware(settings)
    message, record = _message("/files")
    result = await _run(middleware, message, {"user": _row(authenticated=True)})
    assert result == "CALLED"
    assert record.calls == []                      # hech qanday rad javobi yo'q


async def test_unauthenticated_user_is_blocked_from_cloud(settings: Settings) -> None:
    """Hisobsiz foydalanuvchi bulut funksiyasiga o'ta olmaydi."""
    middleware = AuthGuardMiddleware(settings)
    message, record = _message("/files")
    result = await _run(middleware, message, {"user": _row(authenticated=False)})
    assert result is None                          # update yutildi
    assert len(record.calls) == 1                  # login taklifi yuborildi
    assert record.calls[0]["reply_markup"] is not None


@pytest.mark.parametrize("command", sorted(ALLOWED_COMMANDS))
async def test_allowed_commands_pass(settings: Settings, command: str) -> None:
    """/start, /login, /register, /help ... har doim ochiq."""
    middleware = AuthGuardMiddleware(settings)
    message, record = _message(command)
    result = await _run(middleware, message, {"user": _row(authenticated=False)})
    assert result == "CALLED", command
    assert record.calls == []


async def test_admin_always_passes(settings: Settings) -> None:
    """``ADMIN_ID`` dagi foydalanuvchi hisobsiz ham barcha funksiyalarni ochadi."""
    middleware = AuthGuardMiddleware(settings)
    message, _ = _message("/diyor", telegram_id=111)
    result = await _run(middleware, message, {"user": _row(telegram_id=111)})
    assert result == "CALLED"


async def test_blocked_user_gets_block_notice(settings: Settings) -> None:
    """🚫 Bloklangan hisob: boshqa hech narsa ishlamaydi (hatto /files)."""
    middleware = AuthGuardMiddleware(settings)
    message, record = _message("/files")
    result = await _run(middleware, message, {"user": _row(blocked=True)})
    assert result is None
    assert len(record.calls) == 1
    assert "blok" in record.calls[0]["text"].lower() or "🚫" in record.calls[0]["text"]


async def test_gate_disabled_lets_everyone_through(settings: Settings) -> None:
    """``AUTH_REQUIRED=false`` -> ochiq bot (legacy rejim)."""
    settings.auth.required = False
    middleware = AuthGuardMiddleware(settings)
    assert middleware.enabled is False
    message, record = _message("/files")
    result = await _run(middleware, message, {"user": _row(authenticated=False)})
    assert result == "CALLED"
    assert record.calls == []


async def test_runtime_override_beats_env(settings: Settings) -> None:
    """``/diyor`` -> ⚙️ Sozlamalar: darvoza ish vaqtida yoqiladi/o'chiriladi."""
    settings.auth.required = True
    middleware = AuthGuardMiddleware(settings)
    set_auth_override(False)                       # admin o'chirdi
    assert middleware.enabled is False
    message, _ = _message("/files")
    assert await _run(middleware, message, {"user": _row()}) == "CALLED"

    set_auth_override(True)                        # admin qayta yoqdi
    assert middleware.enabled is True
    message2, record2 = _message("/files")
    assert await _run(middleware, message2, {"user": _row()}) is None
    assert record2.calls


async def test_callback_is_rejected_with_alert(settings: Settings) -> None:
    """Tugma bosilsa — ``answer(show_alert=True)``, chat'ga xabar yozilmaydi."""
    middleware = AuthGuardMiddleware(settings)
    callback, record = _callback("files:grp:media:1")
    result = await _run(middleware, callback, {"user": _row()})
    assert result is None
    assert len(record.calls) == 1
    assert record.calls[0]["show_alert"] is True


async def test_inline_query_is_rejected_with_empty_result(settings: Settings) -> None:
    """Inline so'rov hisobsiz foydalanuvchiga bo'sh natija qaytaradi."""
    middleware = AuthGuardMiddleware(settings)
    query, record = _inline()
    result = await _run(middleware, query, {"user": _row()})
    assert result is None
    assert record.calls and record.calls[0]["results"] == []


async def test_event_without_user_is_ignored(settings: Settings) -> None:
    """``user`` bo'lmagan hodisa (kanal posti) tekshirilmaydi."""
    middleware = AuthGuardMiddleware(settings)
    message, _ = _message("/files")
    assert await _run(middleware, message, {}) == "CALLED"


async def test_last_seen_is_throttled(settings: Settings, session) -> None:
    """``last_seen_at`` har update'da emas, minutiga bir marta yoziladi."""
    from db.models import User as DbUser

    row = DbUser(telegram_id=777, first_name="Tester")
    session.add(row)
    await session.commit()

    middleware = AuthGuardMiddleware(settings)
    message, _ = _message("/start")
    await _run(middleware, message, {"user": row, "session": session})
    first_stamp = row.last_seen_at
    assert first_stamp is not None

    row.last_seen_at = None
    await _run(middleware, message, {"user": row, "session": session})
    assert row.last_seen_at is None                # oyna ichida yozilmadi


# --------------------------------------------------------------------------- #
# 🇺🇿 Matnlar (o'zbekcha qatlam)
# --------------------------------------------------------------------------- #
def test_install_language_publishes_all_uz_texts() -> None:
    """Barcha hugecloud matnlari ``bot.texts`` ga o'rnatiladi.

    Son qat'iy emas (``>= 62``): yangi UI qatlamlari (darvoza, Mini App ko'prigi)
    matn qo'shganda bu test qayta yozilishi shart emas — muhimi **hammasi**
    o'rnatilishi va ikki til jadvali bir xil to'plamda bo'lishi.
    """
    report = install_language("uz")
    assert report["hugecloud"] == len(UZ) >= 62
    assert report["legacy"] >= 0
    for name in UZ:
        assert hasattr(texts, name), name


def test_uz_sort_labels() -> None:
    """Saralash nomlari o'zbekcha: "Vaqti bo'yicha" va "Hajmi bo'yicha"."""
    install_language("uz")
    assert "Vaqti bo" in texts.sort_newest() and "yangi" in texts.sort_newest().lower()
    assert "Vaqti bo" in texts.sort_oldest() and "eski" in texts.sort_oldest().lower()
    assert "Hajmi bo" in texts.sort_largest() and "katta" in texts.sort_largest().lower()
    assert "Hajmi bo" in texts.sort_smallest() and "kichik" in texts.sort_smallest().lower()
    # Nom bo'yicha saralash ham bor
    assert texts.sort_name_asc() and texts.sort_name_desc()


def test_uz_group_labels() -> None:
    """Guruh nomlari: 🎬 Medialar, 🎵 Audiolar, 📄 Hujjatlar, 📦 Arxivlar, 🤖 APK/IPA."""
    install_language("uz")
    assert texts.group_media() == "🎬 Medialar"
    assert texts.group_audio() == "🎵 Audiolar"
    assert texts.group_document() == "📄 Hujjatlar"
    assert texts.group_archive() == "📦 Arxivlar"
    assert texts.group_apps() == "🤖 APK/IPA"
    assert texts.group_pinned() == "📌 Muhimlar"
    assert texts.group_all() == "📋 Barcha fayllar"


def test_uz_unlimited_quota() -> None:
    """Kvota 0 bo'lsa 'Cheksiz' deb ko'rsatiladi."""
    install_language("uz")
    assert texts.quota_unlimited("Cheksiz (Unlimited)") == "Cheksiz (Unlimited)"
    assert "Cheksiz" in texts.unlimited_banner("Cheksiz (Unlimited)")
    assert "Cheksiz" in texts.storage_line(1024, 0, "Cheksiz (Unlimited)")


def test_auth_gate_texts_are_uzbek() -> None:
    install_language("uz")
    gate = texts.auth_gate_title()
    assert "/login" in gate and "/register" in gate
    assert texts.auth_required_action()
    assert texts.blocked_message(None)


def test_english_mode_keeps_english() -> None:
    """``APP_LANGUAGE=en`` — o'zbekcha qatlam o'rnatilmaydi."""
    report = install_language("en")
    assert report["legacy"] == 0
    assert report["hugecloud"] == len(UZ)


# --------------------------------------------------------------------------- #
# Model yordamchilari (bot matnlari shu maydonlarni o'qiydi)
# --------------------------------------------------------------------------- #
def test_file_best_file_id_prefers_archive() -> None:
    """Arxiv nusxasi bo'lsa shu ``file_id`` ishlatiladi (persistence)."""
    record = File(
        id=uuid.uuid4(),
        user_id=777,
        file_id="AgAD-ORIGINAL",
        file_unique_id="uniq-1",
        file_name="film.mp4",
        file_size=1024,
        category=FileCategory.VIDEO,
    )
    assert record.best_file_id == "AgAD-ORIGINAL"
    record.mark_archived(chat_id="-1001234567890", message_id=42, file_id="CHANNEL-ID")
    assert record.best_file_id == "CHANNEL-ID"
    assert record.archived_at is not None
    assert record.is_archived is True


def test_user_display_name_and_quota() -> None:
    user = User(telegram_id=777, first_name="Sobir", last_name="Karimov", username="sobir")
    assert "Sobir" in user.display_name
    assert user.quota(0) == 0
    assert user.quota_is_unlimited(0) is True
    assert user.free_space(0) > 0
