"""🚪 Darvoza (auth gate), deep-link "park & replay" va 📱 Mini App ko'prigi testlari.

Bu modul UI/UX qayta tuzilishining **bot qatlamini** tekshiradi:

1. ``bot.session`` — darvoza siyosati (qat'iy ustunlik) va deep-link parklash.
2. ``AuthGuardMiddleware`` — har qanday sessiyani (yangi/eski) darvozadan
   o'tkazadi: hisobsiz foydalanuvchi bulut funksiyasiga o'ta olmaydi, lekin
   ``/start <payload>`` **yo'qolmaydi**.
3. ``bot/handlers/start.py`` — login'dan keyin parklangan payload qayta ijro
   etiladi (bir martalik).
4. ``bot/handlers/miniapp.py`` — ``WebApp.sendData`` ko'prigi: Upload niyati
   (faylni chatga yuborish so'rovi + hajm limiti), Import oqimi
   (havola/token → fayl + dedup), noma'lum amal.
5. Darvoza tugmalari (``auth:login`` / ``auth:register`` / ``auth:help``) FSM'ni
   to'g'ri boshlaydi.
6. Matn jadvali: EN/UZ to'plami bir xil va yangi kalitlar mavjud.

Testlar **tarmoqqa chiqmaydi**: Telegram API chaqiruvi bo'lgan joylar
monkeypatch bilan almashtiriladi, xabarlar esa soxta ``answer`` orqali kuzatiladi.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, WebAppData

from bot import texts
from bot.handlers import miniapp
from bot.handlers import start as start_module
from bot.handlers.callbacks import on_gate_help, on_gate_login, on_gate_register
from bot.handlers.miniapp import DATA_IMPORT, DATA_UPLOAD, extract_share_token
from bot.keyboards.hugecloud import auth_gate_kb, mini_app_home_kb
from bot.middlewares.auth_guard import AuthGuardMiddleware, resolve_session_for, set_auth_override
from bot.session import (
    GUEST_COMMANDS,
    PARKED_PAYLOAD_KEY,
    SessionDecision,
    SessionKind,
    has_parked_payload,
    resolve_session,
    take_parked_payload,
)
from bot.states import AuthState, ImportState
from bot.texts_hugecloud import EN, UZ, install_language
from config import Settings
from db.models import User
from services.share import ShareService
from services.storage import StorageService
from services.telegram import TelegramStorage
from tests.conftest import make_file
from tests.test_bot import _callback, _message, _row, _run

pytestmark = pytest.mark.asyncio

WEB_URL = "https://t.me/Huge_cloud_bot/app"
#: aiogram token formatini qanoatlantiradigan **soxta** token (tarmoqqa chiqmaydi).
TEST_BOT_TOKEN = "123456789:AAFakeTokenForUnitTestsOnly000000000000"


@pytest.fixture(autouse=True)
def _uz_texts() -> Any:
    """Har testda o'zbekcha matnlar o'rnatilgan bo'lsin."""
    install_language("uz")
    yield


@pytest.fixture(autouse=True)
def _clear_override() -> Any:
    """Runtime ustun va throttle kechini tozalash (testlar bir-biriga ta'sir qilmasin)."""
    set_auth_override(None)
    yield
    set_auth_override(None)
    AuthGuardMiddleware._last_touch.clear()


@pytest.fixture()
def share_service(session: Any, settings: Settings) -> ShareService:
    """Ulashish servisi (haqiqiy, lekin tarmoqsiz)."""
    return ShareService(session, settings=settings)


@pytest.fixture()
def tg_storage(settings: Settings) -> TelegramStorage:
    """Telegram fayl wrapper'i (``Bot`` obyekti tarmoqqa chiqmaydi)."""
    return TelegramStorage(Bot(token=TEST_BOT_TOKEN), settings=settings)


def _state(chat_id: int = 777) -> FSMContext:
    """Haqiqiy (lekin xotiradagi) FSM konteksti."""
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=chat_id, user_id=chat_id)
    return FSMContext(storage=storage, key=key)


def _web_app_message(data_type: str, data: str = "", telegram_id: int = 777) -> tuple[Message, list]:
    """``WebApp.sendData`` orqali kelgan xabar (kuzatiladigan ``answer`` bilan)."""
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=_chat(telegram_id),
        from_user=_tg_user(telegram_id),
        web_app_data=WebAppData(
            data=data, data_type=data_type, button_text="⬆️ Yuklash"
        ),
    )
    calls: list[dict[str, Any]] = []

    async def answer(payload: str, **kwargs: Any) -> bool:
        calls.append({"text": payload, **kwargs})
        return True

    object.__setattr__(message, "answer", answer)
    return message, calls


def _chat(telegram_id: int) -> Any:
    """Shaxsiy chat obyekti (aiogram ``Message`` uni talab qiladi)."""
    from aiogram.types import Chat

    return Chat(id=telegram_id, type="private")


def _tg_user(telegram_id: int) -> Any:
    """Telegram foydalanuvchisi."""
    from aiogram.types import User as TgUser

    return TgUser(id=telegram_id, is_bot=False, first_name="Tester")


def _gate_kwargs(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> dict[str, Any]:
    """``_dispatch_start`` uchun umumiy DI lug'ati (payload chaqiruvda beriladi)."""
    return {
        "message": message,
        "user": user,
        "storage": storage,
        "share": share,
        "tg_storage": tg,
        "settings": settings,
        "state": state,
    }


# --------------------------------------------------------------------------- #
# 1. Sessiya siyosati (bot.session)
# --------------------------------------------------------------------------- #
def test_resolve_session_priority_order(settings: Settings) -> None:
    """Qat'iy ustunlik: blocked > admin > gate > authed > guest."""
    gate = settings.auth.required
    blocked = _row(authenticated=True, blocked=True)
    assert resolve_session(blocked, gate_enabled=gate).kind is SessionKind.BLOCKED

    admin = _row(authenticated=False)
    admin_resolution = resolve_session(admin, gate_enabled=gate, is_admin=True)
    assert admin_resolution.is_admin is True
    assert admin_resolution.decision is SessionDecision.ALLOW

    guest = _row(authenticated=False)
    assert (
        resolve_session(guest, gate_enabled=gate).kind is SessionKind.RETURNING_GUEST
    )

    authed = _row(authenticated=True)
    assert resolve_session(authed, gate_enabled=gate).kind is SessionKind.AUTHENTICATED


def test_gate_disabled_lets_everyone_through(settings: Settings) -> None:
    """``AUTH_REQUIRED=false`` → darvoza o'chadi, hamma o'tadi (eski xatti-harakat).

    Bu muhim: darvoza **yangi** qatlam, uni bir bayroq bilan o'chirib, loyihani
    oldingi holatiga qaytarish mumkin bo'lishi kerak.
    """
    resolution = resolve_session(_row(authenticated=False), gate_enabled=False)
    assert resolution.decision is SessionDecision.ALLOW
    assert resolution.allowed is True


def test_resolve_session_new_vs_returning_guest(settings: Settings) -> None:
    """Faoliyatsiz mehmon = yangi sessiya; faoliyatli mehmon = qaytib kelgan."""
    fresh = resolve_session(
        _row(authenticated=False), gate_enabled=settings.auth.required, has_any_activity=False
    )
    assert fresh.kind is SessionKind.NEW

    back = resolve_session(
        _row(authenticated=False), gate_enabled=settings.auth.required, has_any_activity=True
    )
    assert back.kind is SessionKind.RETURNING_GUEST


def test_guest_resolution_requires_auth(settings: Settings) -> None:
    """Mehmon uchun qaror: ``REQUIRE_AUTH`` (handler chaqirilmaydi)."""
    resolution = resolve_session(_row(authenticated=False), gate_enabled=settings.auth.required)
    assert resolution.allowed is False
    assert resolution.decision is SessionDecision.REQUIRE_AUTH


@pytest.mark.parametrize("command", sorted(GUEST_COMMANDS))
async def test_guest_commands_pass_the_middleware(settings: Settings, command: str) -> None:
    """Mehmon faqat auth/ma'lumot buyruqlarini ishlatadi (qolgani darvozada)."""
    middleware = AuthGuardMiddleware(settings)
    message, record = _message(command, telegram_id=800)

    result = await _run(middleware, message, {"user": _row(authenticated=False), "state": _state(800)})

    assert result == "CALLED"
    assert record.calls == []


@pytest.mark.parametrize("command", ["/files", "/upload", "/stats", "/pin"])
async def test_guest_cannot_use_cloud_commands(settings: Settings, command: str) -> None:
    """Bulut buyruqlari mehmon uchun yopiq (darvoza ekrani yuboriladi)."""
    middleware = AuthGuardMiddleware(settings)
    message, record = _message(command, telegram_id=801)

    result = await _run(middleware, message, {"user": _row(authenticated=False), "state": _state(801)})

    assert result is None
    assert len(record.calls) == 1


async def test_diyor_reaches_the_password_prompt_behind_the_gate(
    settings: Settings, monkeypatch
) -> None:
    """``/diyor`` darvozadan o'tadi — panel parol bilan ochiladi.

    ``ADMIN_ID`` bo'sh bo'lganda bu yagona kirish yo'li; darvoza bloklasa
    handler parol so'rashga ham yetib bormasdi.
    """
    from pydantic import SecretStr

    monkeypatch.setattr(settings.bot, "admin_password", SecretStr("test-parol"))
    monkeypatch.setattr(settings.bot, "admin_ids", ())
    monkeypatch.setattr(settings.bot, "admin_id", None)

    middleware = AuthGuardMiddleware(settings)
    message, record = _message("/diyor", telegram_id=803)

    result = await _run(middleware, message, {"user": _row(authenticated=False), "state": _state(803)})

    assert result == "CALLED", "admin panel eshigi darvozada to'xtamasligi kerak"
    assert record.calls == []


def test_resolve_session_for_new_vs_returning() -> None:
    """Yangi va qaytgan sessiya bazasiz aniqlanadi (hisob metama'lumotlari)."""
    fresh = _row(authenticated=False)
    assert resolve_session_for(fresh, gate_enabled=True, is_admin=False).kind is SessionKind.NEW

    returning = _row(authenticated=False)
    returning.last_login_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert (
        resolve_session_for(returning, gate_enabled=True, is_admin=False).kind
        is SessionKind.RETURNING_GUEST
    )

    storage_user = _row(authenticated=False)
    storage_user.storage_used = 1024
    assert (
        resolve_session_for(storage_user, gate_enabled=True, is_admin=False).kind
        is SessionKind.RETURNING_GUEST
    )

    seen_user = _row(authenticated=False)
    seen_user.last_seen_at = datetime(2026, 2, 2, tzinfo=UTC)
    assert (
        resolve_session_for(seen_user, gate_enabled=True, is_admin=False).kind
        is SessionKind.RETURNING_GUEST
    )


def test_resolve_session_for_prefers_blocked_and_authenticated() -> None:
    """Bloklangan/tasdiqlangan holat "yangi" deb hisoblanmaydi."""
    blocked = _row(authenticated=False, blocked=True)
    assert (
        resolve_session_for(blocked, gate_enabled=True, is_admin=False).kind is SessionKind.BLOCKED
    )

    authed = _row(authenticated=True)
    assert (
        resolve_session_for(authed, gate_enabled=True, is_admin=False).kind
        is SessionKind.AUTHENTICATED
    )


def test_resolve_session_for_without_user_row() -> None:
    """Foydalanuvchi qatori bo'lmasa (kanal posti) — qaror GUEST emas, ALLOW."""
    resolution = resolve_session_for(None, gate_enabled=True, is_admin=False)
    assert resolution.decision.value == "allow"


# --------------------------------------------------------------------------- #
# 2. Deep link park & replay
# --------------------------------------------------------------------------- #
async def test_park_and_take_is_single_use() -> None:
    """Payload bir marta olinadi: ikkinchi chaqiruvda yo'q."""
    state = _state(900)
    assert await has_parked_payload(state) is False
    assert await take_parked_payload(state) == ""

    await state.update_data(**{PARKED_PAYLOAD_KEY: "file_42"})
    assert await has_parked_payload(state) is True
    assert await take_parked_payload(state) == "file_42"
    assert await take_parked_payload(state) == ""
    assert await has_parked_payload(state) is False


async def test_park_without_state_is_noop() -> None:
    """FSM bo'lmasa (masalan kanal posti) park shunchaki o'tkazib yuboriladi."""
    from bot.session import park_payload

    assert await park_payload(None, "file_1") == ""
    assert await has_parked_payload(None) is False
    assert await take_parked_payload(None) == ""


async def test_park_truncates_oversized_payload() -> None:
    """Juda uzun payload FSM'ga axlat yozmasin (chegara bilan kesiladi)."""
    from bot.session import MAX_PAYLOAD_LENGTH, park_payload

    state = _state(901)
    await park_payload(state, "x" * (MAX_PAYLOAD_LENGTH + 100))
    parked = await take_parked_payload(state)
    assert parked and len(parked) <= MAX_PAYLOAD_LENGTH


async def test_gate_parks_deeplink_and_lets_start_through(settings: Settings) -> None:
    """``/start file_42``: middleware payload'ni **park qiladi** va ``/start`` ni o'tkazadi.

    Darvoza ekranini ``/start`` handler'ining o'zi yuboradi (chunki park qilish
    va ekranni bitta joyda bajarish payload'ni yo'qotmaslik kafolati).
    """
    middleware = AuthGuardMiddleware(settings)
    state = _state(778)
    message, record = _message("/start file_42", telegram_id=778)

    result = await _run(middleware, message, {"user": _row(authenticated=False), "state": state})

    assert result == "CALLED"  # ``/start`` ruxsat etilgan buyruq
    assert record.calls == []  # middleware o'zi xabar yubormadi
    # Payload FSM'da saqlanib qoldi (handler uni iste'mol qilmaydi)
    assert await has_parked_payload(state) is True
    assert await take_parked_payload(state) == "file_42"


async def test_gate_message_does_not_park_payload(settings: Settings) -> None:
    """Oddiy ``/start`` park qilmaydi (bo'sh payload FSM'ni ifloslantirmaydi)."""
    middleware = AuthGuardMiddleware(settings)
    state = _state(779)
    message, _record = _message("/start", telegram_id=779)

    await _run(middleware, message, {"user": _row(authenticated=False), "state": state})

    assert await has_parked_payload(state) is False


async def test_start_handler_shows_gate_with_park_notice(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """``/start`` handler'i mehmon uchun darvozani ko'rsatadi va payload'ni park qiladi."""
    state = _state(780)
    message, record = _message("/start share_ABC123", telegram_id=780)

    handled = await start_module._dispatch_start(
        message=message,
        payload="share_ABC123",
        user=_row(authenticated=False),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=state,
    )

    assert handled is False  # darvoza ko'rsatildi, payload ijro etilmadi
    assert len(record.calls) == 1
    assert "saqlanib" in record.calls[0]["text"]  # "havolangiz saqlanib turibdi"
    assert record.calls[0]["reply_markup"] is not None
    assert await take_parked_payload(state) == "share_ABC123"


async def test_gate_lets_active_auth_dialog_through(settings: Settings) -> None:
    """``/register`` dialogi davomida darvoza xalaqit bermaydi."""
    middleware = AuthGuardMiddleware(settings)
    state = _state(793)
    await state.set_state(AuthState.waiting_login)
    message, record = _message("mylogin", telegram_id=793)

    result = await _run(
        middleware,
        message,
        {"user": _row(authenticated=False), "state": state, "bot": None},
    )

    assert result == "CALLED"
    assert record.calls == []


async def test_gate_lets_import_dialog_through(settings: Settings) -> None:
    """Import dialogi (``ImportState``) ham ruxsat etilgan holatlar ro'yxatida."""
    middleware = AuthGuardMiddleware(settings)
    state = _state(794)
    await state.set_state(ImportState.waiting_token)
    message, record = _message("AbC123_-xY", telegram_id=794)

    result = await _run(middleware, message, {"user": _row(authenticated=True), "state": state})

    assert result == "CALLED"
    assert record.calls == []


async def test_gate_rejects_callback_with_alert_only(settings: Settings) -> None:
    """Callback rad etilganda faqat alert yuboriladi (xabar tahrirlanmaydi)."""
    middleware = AuthGuardMiddleware(settings)
    callback, answers = _callback("files:all", telegram_id=781)

    result = await _run(middleware, callback, {"user": _row(authenticated=False)})

    assert result is None
    assert len(answers.calls) == 1
    assert answers.calls[0].get("show_alert") is True


async def test_gate_swallows_telegram_errors(settings: Settings) -> None:
    """Telegram xatosi update'ni yo'qotmaydi: darvoza baribir ``None`` qaytaradi."""
    from aiogram.exceptions import TelegramForbiddenError

    middleware = AuthGuardMiddleware(settings)
    message, _record = _message("/files", telegram_id=782)

    async def boom(payload: str, **kwargs: Any) -> bool:
        raise TelegramForbiddenError(method=_dummy_method(), message="bot was blocked by the user")

    object.__setattr__(message, "answer", boom)

    result = await _run(middleware, message, {"user": _row(authenticated=False)})

    assert result is None


def _dummy_method() -> Any:
    """Telegram istisnosi uchun soxta ``method`` obyekti."""
    return State()


async def test_deeplink_replay_after_login(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
    user: User,
) -> None:
    """Login'dan keyin parklangan deep link aynan o'zi qayta ijro etiladi."""
    file_record = await make_file(storage, 777, name="hisobot.pdf")
    opened: list[int] = []

    async def fake_open(message: Message, file_id: int, user: User, *_a: Any, **_k: Any) -> bool:
        opened.append(file_id)
        return True

    monkeypatch.setattr(start_module, "_open_own_file", fake_open)
    monkeypatch.setattr(start_module, "mini_app_home_kb", lambda **_kw: "KB")

    state = _state(777)
    await state.update_data(**{PARKED_PAYLOAD_KEY: f"file_{file_record.id}"})
    message, record = _message("/start", telegram_id=777)
    authed_user = user
    authed_user.is_authenticated = True

    handled = await start_module._dispatch_start(
        payload=f"file_{file_record.id}",
        force=True,
        **_gate_kwargs(
            message, authed_user, storage, share_service, tg_storage, settings, state
        ),
    )

    assert handled is True
    assert opened == [str(file_record.id)]  # fayl ochildi (deep link ishladi)
    assert record.calls == []  # uy ekrani yuborilmadi


async def test_deeplink_payload_is_not_replayed_twice(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
    user: User,
) -> None:
    """Payload iste'mol qilingach ikkinchi ``/start`` uy ekranini ochadi."""
    file_record = await make_file(storage, 777, name="hisobot.pdf")
    opened: list[int] = []

    async def fake_open(message: Message, file_id: int, user: User, *_a: Any, **_k: Any) -> bool:
        opened.append(file_id)
        return True

    monkeypatch.setattr(start_module, "_open_own_file", fake_open)
    monkeypatch.setattr(start_module, "mini_app_home_kb", lambda **_kw: "KB")

    state = _state(777)
    message, record = _message("/start", telegram_id=777)
    authed_user = user
    authed_user.is_authenticated = True
    kwargs = _gate_kwargs(
        message, authed_user, storage, share_service, tg_storage, settings, state
    )

    await state.update_data(**{PARKED_PAYLOAD_KEY: f"file_{file_record.id}"})
    assert (
        await start_module._dispatch_start(payload=f"file_{file_record.id}", force=True, **kwargs)
        is True
    )

    record.calls.clear()
    assert await start_module._dispatch_start(payload="", force=True, **kwargs) is True
    assert opened == [str(file_record.id)]
    # Uy ekrani ikki xabardan iborat (legacy xatti-harakat): salom + dashboard
    assert len(record.calls) == 2
    assert record.calls[0]["reply_markup"] is not None
    assert record.calls[1]["reply_markup"] == "KB"  # Mini App inline klaviatura


async def test_gate_blocks_guest_before_deeplink_dispatch(settings: Settings) -> None:
    """Mehmon deep link yuborsa handler chaqirilmaydi (faqat darvoza)."""
    middleware = AuthGuardMiddleware(settings)
    state = _state(795)
    message, record = _message("/files", telegram_id=795)

    result = await _run(
        middleware, message, {"user": _row(authenticated=False), "state": state}
    )

    assert result is None
    assert len(record.calls) == 1
    assert "/register" in record.calls[0]["text"]


async def test_resume_after_auth_replays_parked_deeplink(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
    user: User,
) -> None:
    """Login tugagach: parklangan havola ochiladi **va** FSM'dan o'chiriladi."""
    file_record = await make_file(storage, 777, name="shartnoma.pdf")
    opened: list[str] = []

    async def fake_open(message: Message, file_id: str, user: User, *_a: Any, **_k: Any) -> bool:
        opened.append(file_id)
        return True

    monkeypatch.setattr(start_module, "_open_own_file", fake_open)

    state = _state(777)
    await state.update_data(**{PARKED_PAYLOAD_KEY: f"file_{file_record.id}"})
    message, record = _message("/start", telegram_id=777)
    user.is_authenticated = True

    await start_module.resume_after_auth(
        message, user, storage, share_service, tg_storage, settings, state
    )

    assert opened == [str(file_record.id)]
    assert await has_parked_payload(state) is False  # bir martalik: iste'mol qilindi
    # "Havolangiz ochilmoqda" xabari yuborildi (uzluksizlik hissi)
    assert any("ochilmoqda" in call["text"] for call in record.calls)


async def test_resume_after_auth_without_payload_shows_home(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
    user: User,
) -> None:
    """Parklangan havola bo'lmasa — Mini App bosh ekrani."""
    monkeypatch.setattr(start_module, "mini_app_home_kb", lambda **_kw: "KB")
    state = _state(777)
    message, record = _message("/start", telegram_id=777)
    user.is_authenticated = True

    await start_module.resume_after_auth(
        message, user, storage, share_service, tg_storage, settings, state
    )

    assert len(record.calls) >= 1
    assert any(call.get("reply_markup") == "KB" for call in record.calls)


# --------------------------------------------------------------------------- #
# 3. Darvoza tugmalari (inline callback)
# --------------------------------------------------------------------------- #
async def _attach_answer(callback: CallbackQuery, calls: list) -> None:
    """Callback xabariga kuzatiladigan ``answer`` o'rnatish."""

    async def answer(payload: str, **kwargs: Any) -> bool:
        calls.append({"text": payload, **kwargs})
        return True

    object.__setattr__(callback.message, "answer", answer)


async def test_gate_callback_login_starts_auth_fsm() -> None:
    """``auth:login`` → ``AuthState.waiting_login`` (flow=login) + yo'riqnoma."""
    state = _state(783)
    callback, answers = _callback("auth:login", telegram_id=783)
    sent: list[dict[str, Any]] = []
    await _attach_answer(callback, sent)

    await on_gate_login(callback, _row(authenticated=False), state)

    assert answers.calls  # callback.answer chaqirildi ("yuklanmoqda" qolmasligi uchun)
    assert await state.get_state() == AuthState.waiting_login
    assert (await state.get_data())["flow"] == "login"
    assert sent and sent[0]["reply_markup"] is not None


async def test_gate_callback_register_starts_auth_fsm() -> None:
    """``auth:register`` → ``AuthState.waiting_login`` (flow=register)."""
    state = _state(784)
    callback, answers = _callback("auth:register", telegram_id=784)
    sent: list[dict[str, Any]] = []
    await _attach_answer(callback, sent)

    await on_gate_register(callback, _row(authenticated=False), state)

    assert answers.calls
    assert await state.get_state() == AuthState.waiting_login
    assert (await state.get_data())["flow"] == "register"
    assert sent and sent[0]["reply_markup"] is not None


async def test_gate_callback_help_sends_guide() -> None:
    """``auth:help`` → qisqa yo'riqnoma xabari (alert emas: HTML + uzun matn)."""
    callback, answers = _callback("auth:help", telegram_id=785)
    sent: list[dict[str, Any]] = []
    await _attach_answer(callback, sent)

    await on_gate_help(callback)

    assert answers.calls
    assert sent and "/register" in sent[0]["text"]


async def test_gate_login_callback_replaces_previous_flow() -> None:
    """Avvalgi dialog qoldig'i yangi oqimni buzmaydi (state tozalanadi)."""
    state = _state(796)
    await state.set_state(ImportState.waiting_token)
    await state.update_data(**{PARKED_PAYLOAD_KEY: "file_9"})
    callback, _answers = _callback("auth:login", telegram_id=796)
    await _attach_answer(callback, [])

    await on_gate_login(callback, _row(authenticated=False), state)

    data = await state.get_data()
    assert data.get("flow") == "login"
    assert PARKED_PAYLOAD_KEY not in data


# --------------------------------------------------------------------------- #
# 4. Mini App ko'prigi: token ajratish
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AbC123_-xY", "AbC123_-xY"),
        ("t.me/Huge_cloud_bot?start=AbC123_-xY", "AbC123_-xY"),
        ("https://t.me/Huge_cloud_bot?start=AbC123_-xY", "AbC123_-xY"),
        ("https://cloud.example.com/api/v1/shares/AbC123_-xY", "AbC123_-xY"),
        ("havola: AbC123_-xY", "AbC123_-xY"),
        ("  AbC123_-xY  ", "AbC123_-xY"),
        ("", ""),
        (None, ""),
        ("salom dunyo", ""),  # token emas
        ("ab", ""),  # juda qisqa
        ("https://t.me/Huge_cloud_bot?start=", ""),
    ],
)
def test_extract_share_token(raw: str | None, expected: str) -> None:
    """Havola/token shakllarini to'g'ri ajratish (yoki bo'sh satr)."""
    assert extract_share_token(raw) == expected


# --------------------------------------------------------------------------- #
# 5. Mini App ko'prigi: Upload / Import
# --------------------------------------------------------------------------- #
async def test_web_app_upload_intent_asks_for_the_file(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """``hugecloud.upload`` → "faylni chatga yuboring" tasdiq xabari."""
    message, calls = _web_app_message(DATA_UPLOAD, '{"name": "hisobot.pdf", "size": 2048}')

    await miniapp.on_web_app_data(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=_state(777),
    )

    assert len(calls) == 1
    assert "hisobot.pdf" in calls[0]["text"]
    assert calls[0]["reply_markup"] is not None


async def test_web_app_upload_intent_rejects_oversize(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Limitdan katta fayl darhol rad etiladi (foydalanuvchi vaqtini olmaslik uchun)."""
    limit = int(settings.storage.max_file_size)
    message, calls = _web_app_message(
        DATA_UPLOAD, f'{{"name": "katta.zip", "size": {limit + 1}}}'
    )

    await miniapp.on_web_app_data(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=_state(777),
    )

    assert len(calls) == 1
    assert calls[0]["text"] == texts.miniapp_upload_limit(texts_format_size(limit))


async def test_web_app_import_intent_starts_fsm(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """``hugecloud.import`` token'siz → ``ImportState.waiting_token`` + so'rov."""
    message, calls = _web_app_message(DATA_IMPORT, "{}")
    state = _state(786)

    await miniapp.on_web_app_data(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=state,
    )

    assert await state.get_state() == ImportState.waiting_token
    assert len(calls) == 1 and calls[0]["text"] == texts.miniapp_import_prompt()


async def test_web_app_import_with_token_opens_immediately(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Mini App token'ni payload'da yuborsa — so'rov takrorlanmaydi."""
    opened: list[str] = []

    async def fake_open(message: Message, token: str, user: User, *_a: Any, **_k: Any) -> None:
        opened.append(token)

    monkeypatch.setattr(start_module, "_open_share_token", fake_open)
    message, calls = _web_app_message(DATA_IMPORT, '{"token": "t.me/bot?start=Tok3n_abc"}')
    state = _state(787)

    await miniapp.on_web_app_data(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=state,
    )

    assert opened == ["Tok3n_abc"]
    assert calls == []  # qo'shimcha so'rov yuborilmadi
    assert await state.get_state() is None  # dialog yopildi


async def test_web_app_unknown_action_is_not_an_error(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Noma'lum ``data_type`` (eski ilova versiyasi) — xato emas, tushuntirish."""
    message, calls = _web_app_message("hugecloud.legacy", "{}")

    await miniapp.on_web_app_data(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=_state(788),
    )

    assert len(calls) == 1
    assert calls[0]["text"] == texts.miniapp_unknown_action()


async def test_web_app_broken_json_is_tolerated(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Buzilgan JSON payload → upload niyati baribir ishlaydi (istisnosiz)."""
    message, calls = _web_app_message(DATA_UPLOAD, "{not json")

    await miniapp.on_web_app_data(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=_state(789),
    )

    assert len(calls) == 1


async def test_web_app_non_dict_json_is_tolerated(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """JSON массив bo'lsa ham (obyekt emas) xato chiqmaydi."""
    message, calls = _web_app_message(DATA_UPLOAD, "[1, 2, 3]")

    await miniapp.on_web_app_data(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=_state(797),
    )

    assert len(calls) == 1


async def test_import_flow_accepts_link_then_closes(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Import dialogi: havola keldi → fayl ochildi → holat tozalandi."""
    opened: list[str] = []

    async def fake_open(message: Message, token: str, user: User, *_a: Any, **_k: Any) -> None:
        opened.append(token)

    monkeypatch.setattr(start_module, "_open_share_token", fake_open)
    state = _state(790)
    await state.set_state(ImportState.waiting_token)
    message, _calls = _message("https://t.me/Huge_cloud_bot?start=Tok3n_abc", telegram_id=790)

    await miniapp.on_import_token(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=state,
    )

    assert opened == ["Tok3n_abc"]
    assert await state.get_state() is None


async def test_import_flow_reports_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Deep-link xatosi foydalanuvchiga tushunarli xabar bo'lib qaytadi."""

    async def boom(message: Message, token: str, user: User, *_a: Any, **_k: Any) -> None:
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(start_module, "_open_share_token", boom)
    state = _state(798)
    await state.set_state(ImportState.waiting_token)
    message, calls = _web_app_message("", "", 798)
    object.__setattr__(message, "text", "AbC123_-xY")

    await miniapp.on_import_token(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=state,
    )

    assert len(calls) == 1
    assert calls[0]["text"] == texts.miniapp_import_invalid()


async def test_import_flow_rejects_invalid_link_and_stays(
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Noto'g'ri havola → xato xabari, lekin dialog **yopilmaydi**."""
    state = _state(791)
    await state.set_state(ImportState.waiting_token)
    message, calls = _web_app_message("", "", 791)
    object.__setattr__(message, "text", "bu havola emas")

    await miniapp.on_import_token(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=state,
    )

    assert await state.get_state() == ImportState.waiting_token
    assert len(calls) == 1
    assert calls[0]["text"] == texts.miniapp_import_invalid()


@pytest.mark.parametrize("cancel", ["/cancel", "Bekor qilish", "❌ Bekor qilish"])
async def test_import_flow_can_be_cancelled(
    cancel: str,
    settings: Settings,
    storage: StorageService,
    share_service: ShareService,
    tg_storage: TelegramStorage,
) -> None:
    """Bekor qilish dialog'ni yopadi (reply klaviatura va buyruq orqali)."""
    state = _state(792)
    await state.set_state(ImportState.waiting_token)
    message, calls = _web_app_message("", "", 792)
    object.__setattr__(message, "text", cancel)

    await miniapp.on_import_token(
        message,
        user=_row(authenticated=True),
        storage=storage,
        share=share_service,
        tg_storage=tg_storage,
        settings=settings,
        state=state,
    )

    assert await state.get_state() is None
    assert len(calls) == 1
    assert "bekor" in calls[0]["text"].lower()


# --------------------------------------------------------------------------- #
# 6. Klaviatura va matnlar
# --------------------------------------------------------------------------- #
def test_gate_keyboard_has_auth_actions() -> None:
    """Darvoza klaviaturasi: login/register/yordam callback'lari + Mini App."""
    kb = auth_gate_kb(web_app_url=WEB_URL)
    flat = [button.text for row in kb.inline_keyboard for button in row]
    data = [button.callback_data for row in kb.inline_keyboard for button in row]

    assert {"auth:login", "auth:register", "auth:help"} <= set(data)
    assert any("Mini App" in text for text in flat)
    web_buttons = [
        button for row in kb.inline_keyboard for button in row if button.web_app is not None
    ]
    assert web_buttons and web_buttons[0].web_app.url == WEB_URL


def test_gate_keyboard_works_without_web_app_url() -> None:
    """``WEB_APP_URL`` bo'lmasa ham klaviatura yaroqli (Mini App tugmasiz)."""
    kb = auth_gate_kb()
    data = [button.callback_data for row in kb.inline_keyboard for button in row]
    assert {"auth:login", "auth:register"} <= set(data)


def test_home_keyboard_opens_mini_app_first() -> None:
    """Uy klaviaturasi Mini App tugmasi bilan boshlanadi (Mini App Pivot)."""
    kb = mini_app_home_kb(web_app_url=WEB_URL)
    first = kb.inline_keyboard[0][0]

    assert first.text.endswith(texts.home_open_mini_app())
    assert first.web_app is not None and first.web_app.url == WEB_URL
    # Bot ichidagi muqobil yo'llar ham saqlanib qolgan (eski funksiyalar yo'qolmaydi)
    data = [button.callback_data for row in kb.inline_keyboard for button in row]
    assert "sort" in data and "share" in data
    assert any(item and item.startswith("grp:") for item in data)


def test_new_text_keys_exist_in_both_languages() -> None:
    """Yangi UI kalitlari EN va UZ jadvallarida mavjud (to'plam bir xil)."""
    assert set(EN) == set(UZ)
    expected = {
        "gate_button_login",
        "gate_button_register",
        "gate_button_help",
        "gate_new_session",
        "gate_returning_session",
        "gate_parked_notice",
        "gate_help_short",
        "home_open_mini_app",
        "home_welcome_authed",
        "miniapp_upload_ack",
        "miniapp_upload_limit",
        "miniapp_import_prompt",
        "miniapp_import_done",
        "miniapp_import_invalid",
        "miniapp_unknown_action",
    }
    assert expected <= set(UZ)


def test_gate_texts_render_in_uzbek() -> None:
    """Darvoza matnlari o'zbekcha render bo'ladi (HTML teglari bilan)."""
    new_text = texts.gate_new_session("Huge_cloud_bot", WEB_URL)
    assert "/register" in new_text and "Mini App" in new_text

    returning_text = texts.gate_returning_session("/login", parked=True)
    assert "/login" in returning_text and "saqlanib" in returning_text

    assert "saqlanib" in texts.gate_parked_notice()


def test_upload_ack_renders_name_and_size() -> None:
    """Upload tasdig'i fayl nomi va hajmini ko'rsatadi."""
    text = texts.miniapp_upload_ack("hisobot.pdf", "2 KB")
    assert "hisobot.pdf" in text and "2 KB" in text


def texts_format_size(size: int) -> str:
    """Handler ishlatadigan hajm formati (testda ham bir xil bo'lishi kerak)."""
    from bot.utils.formatters import format_size

    return format_size(size)
