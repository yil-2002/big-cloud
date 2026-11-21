"""▶️ Mini App "Telegram'da ochish" oqimi (``hugecloud.play``).

Nega bu muhim: Mini App WebView'i fon rejimida media ijro eta olmaydi. Yagona
ishlaydigan yo'l — faylni chatga yuborish, shunda Telegram'ning **native
pleeri** qulf ekranida davom etadi. Shu sababli:

* tur ``hugecloud.play`` deb nomlangan va eksport qilingan;
* egasi bo'lmagan yoki mavjud bo'lmagan fayl **hech qachon** yuborilmaydi
  (egalik :meth:`StorageService.get_file` da tekshiriladi);
* xato foydalanuvchiga tushunarli matn bilan qaytadi.
"""

from __future__ import annotations

import pytest

from bot.handlers import miniapp
from bot.handlers.miniapp import DATA_PLAY, _handle_play_intent


class FakeChat:
    id = 555


class FakeMessage:
    def __init__(self) -> None:
        self.chat = FakeChat()
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


class FakeUser:
    telegram_id = 42


class FakeRecord:
    id = "11111111-2222-3333-4444-555555555555"
    file_name = "qoshiq.mp3"
    best_file_id = "BAACAgIAAxFILEID"
    category = "audio"


class FakeStorage:
    """``get_file`` ni imitatsiya qiladi (topilsa yozuv, aks holda xato)."""

    def __init__(self, record: FakeRecord | None = None, error: Exception | None = None) -> None:
        self._record = record
        self._error = error
        self.calls: list[tuple[str, int]] = []

    async def get_file(self, file_id: str, telegram_id: int) -> FakeRecord:
        self.calls.append((file_id, telegram_id))
        if self._error is not None:
            raise self._error
        assert self._record is not None
        return self._record


class FakeTelegramStorage:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict]] = []

    async def send_stored_file(self, chat_id: int, file_id: str, **kwargs) -> None:
        self.sent.append((chat_id, file_id, kwargs))


# ---------------------------------------------------------------------------
# Shartnoma
# ---------------------------------------------------------------------------
def test_play_type_is_exported() -> None:
    """Tur nomi ilova bilan kelishilgan va ``__all__`` da e'lon qilingan."""
    assert DATA_PLAY == "hugecloud.play"
    assert "DATA_PLAY" in miniapp.__all__


def test_play_handler_is_wired_into_dispatch() -> None:
    """Dispatch ``DATA_PLAY`` ni haqiqatan chaqirishini manba tasdiqlaydi."""
    import inspect

    source = inspect.getsource(miniapp.on_web_app_data)
    assert "DATA_PLAY" in source
    assert "_handle_play_intent" in source


# ---------------------------------------------------------------------------
# Xato holatlar
# ---------------------------------------------------------------------------
async def test_missing_file_id_is_rejected() -> None:
    message = FakeMessage()
    storage = FakeStorage(record=FakeRecord())

    await _handle_play_intent(message, FakeUser(), storage, FakeTelegramStorage(), {})

    assert storage.calls == [], "file_id bo'lmasa bazaga murojaat qilinmasligi kerak"
    assert len(message.answers) == 1


async def test_unknown_file_is_reported_and_not_sent() -> None:
    message = FakeMessage()
    storage = FakeStorage(error=FileNotFoundError("yo'q"))
    tg_storage = FakeTelegramStorage()

    await _handle_play_intent(
        message, FakeUser(), storage, tg_storage, {"file_id": "yoq-bunday-fayl"}
    )

    assert tg_storage.sent == [], "topilmagan fayl chatga yuborilmasligi kerak"
    assert len(message.answers) == 1


async def test_domain_error_is_not_leaked_to_user() -> None:
    """Servis qatlami xatosi foydalanuvchiga xom holda ko'rsatilmaydi."""
    from services.exceptions import NotOwnerError

    message = FakeMessage()
    storage = FakeStorage(error=NotOwnerError("boshqa foydalanuvchining fayli"))

    await _handle_play_intent(
        message, FakeUser(), storage, FakeTelegramStorage(), {"file_id": "begona"}
    )

    assert len(message.answers) == 1
    assert "boshqa foydalanuvchining fayli" not in message.answers[0]


# ---------------------------------------------------------------------------
# Muvaffaqiyatli oqim
# ---------------------------------------------------------------------------
async def test_file_is_sent_to_the_chat() -> None:
    message = FakeMessage()
    record = FakeRecord()
    storage = FakeStorage(record=record)
    tg_storage = FakeTelegramStorage()

    await _handle_play_intent(
        message, FakeUser(), storage, tg_storage, {"file_id": record.id}
    )

    assert storage.calls == [(record.id, FakeUser.telegram_id)]
    assert len(tg_storage.sent) == 1
    chat_id, file_id, kwargs = tg_storage.sent[0]
    assert chat_id == FakeChat.id
    assert file_id == record.best_file_id, "arxiv nusxasi bo'lsa u ishlatilishi kerak"
    assert kwargs.get("record") is record


async def test_send_failure_is_reported_to_user() -> None:
    """Yuborish yiqilsa — jim qolmaymiz, foydalanuvchi xabar oladi."""

    class BrokenTelegramStorage(FakeTelegramStorage):
        async def send_stored_file(self, chat_id: int, file_id: str, **kwargs) -> None:
            raise RuntimeError("Telegram API xatosi")

    message = FakeMessage()
    await _handle_play_intent(
        message, FakeUser(), FakeStorage(record=FakeRecord()), BrokenTelegramStorage(),
        {"file_id": FakeRecord.id},
    )

    assert len(message.answers) == 2, "avval tasdiq, keyin xato xabari"


# ---------------------------------------------------------------------------
# Matnlar ikkala tilda ham mavjud
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["miniapp_play_ack", "miniapp_play_invalid"])
def test_play_texts_exist(key: str) -> None:
    from bot import texts

    assert hasattr(texts, key), f"bot.texts.{key} o'rnatilmagan"


def test_play_ack_mentions_background_playback() -> None:
    """Tasdiq matni foydalanuvchiga asosiy foydani aytishi kerak."""
    from bot import texts

    body = texts.miniapp_play_ack("test.mp3")
    assert "test.mp3" in body


def test_legacy_brand_intent_still_accepted() -> None:
    """Keshlangan Mini App eski brend nomi bilan yuborsa ham oqim ishlaydi.

    Telegram WebView sahifani keshlab qolishi mumkin: o'sha nusxa hali ham
    ``bigcloud.*`` yuboradi. Prefiks normalizatsiya qilinmasa, yuklash/import
    foydalanuvchiga hech qanday xato ko'rsatmasdan jimgina ishlamay qolardi.
    """
    from bot.handlers.miniapp import (
        DATA_IMPORT,
        DATA_PLAY,
        DATA_UPLOAD,
        normalize_data_type,
    )

    assert normalize_data_type("bigcloud.upload") == DATA_UPLOAD
    assert normalize_data_type("bigcloud.import") == DATA_IMPORT
    assert normalize_data_type("bigcloud.play") == DATA_PLAY
    # Yangi nom o'zgarmaydi, notanish tur ham o'zgarmaydi.
    assert normalize_data_type(DATA_PLAY) == DATA_PLAY
    assert normalize_data_type("boshqa.narsa") == "boshqa.narsa"
    assert normalize_data_type("") == ""
