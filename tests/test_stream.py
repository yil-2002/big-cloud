"""🎬 Media stream proxy + 🔗 ulashish havolalari.

Bu qatlam Mini App pleerini boqadi: `<video>`/`<audio>` teglari **Range**
so'rovlarini yuboradi (oldinga/orqaga o'tish uchun). Shuning uchun `206 Partial
Content` va `Content-Range` javoblari alohida tekshiriladi. Telegram tarmog'iga
chiqilmaymiz — `httpx.AsyncClient` o'rniga stub ishlatiladi.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

import httpx
import pytest

from services.share import ShareService
from tests.conftest import make_file

MEDIA_BYTES = b"".join(bytes([index % 256]) * 64 for index in range(32))  # 2 KB


# --------------------------------------------------------------------------- #
# Stub httpx klienti
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Telegram'dan qaytgan javobni taqlid qiladi (Range'ni tushunadi)."""

    def __init__(self, headers: dict[str, str], payload: bytes, status_code: int = 200) -> None:
        # Haqiqiy Telegram javobidagi kabi case-insensitive sarlavhalar.
        self.headers = httpx.Headers(headers)
        self.status_code = status_code
        self._payload = payload
        self.closed = False

    async def aiter_bytes(self, chunk_size: int = 65536):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]

    async def aclose(self) -> None:
        self.closed = True

    async def aread(self) -> bytes:
        return self._payload


class FakeClient:
    """`get`/`head`/`build_request`/`send` — route aynan shularni chaqiradi."""

    def __init__(self, *, payload: bytes = MEDIA_BYTES, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.sent_headers: list[dict[str, str]] = []
        self.urls: list[str] = []
        self.last_response: FakeResponse | None = None

    def _headers_for(self, headers: dict[str, str]) -> tuple[dict[str, str], bytes, int]:
        range_header = headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            start_text, _, end_text = range_header[len("bytes=") :].partition("-")
            start = int(start_text or 0)
            end = int(end_text) if end_text else len(self.payload) - 1
            end = min(end, len(self.payload) - 1)
            chunk = self.payload[start : end + 1]
            return (
                {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
                    "Accept-Ranges": "bytes",
                },
                chunk,
                206,
            )
        return (
            {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(self.payload)),
                "Accept-Ranges": "bytes",
            },
            self.payload,
            self.status_code,
        )

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.urls.append(url)
        headers, payload, status_code = self._headers_for(kwargs.get("headers") or {})
        return FakeResponse(headers, payload, status_code)

    async def head(self, url: str, **kwargs: Any) -> FakeResponse:
        self.urls.append(url)
        headers, _payload, _status = self._headers_for(kwargs.get("headers") or {})
        return FakeResponse(headers, b"", 200)

    def build_request(self, method: str, url: str, headers: dict[str, str] | None = None) -> Any:
        self.urls.append(url)
        self.sent_headers.append(dict(headers or {}))
        return {"method": method, "url": url, "headers": headers or {}}

    async def send(self, request: Any, **kwargs: Any) -> FakeResponse:
        headers, payload, status_code = self._headers_for(request.get("headers") or {})
        response = FakeResponse(headers, payload, status_code)
        self.last_response = response
        return response


@pytest.fixture()
def share(storage, settings) -> ShareService:
    return ShareService(storage.session, settings=settings)


@pytest.fixture()
def stream_client(session, settings, monkeypatch: pytest.MonkeyPatch):
    """Stream route'ni stub Telegram bilan ishlatadigan TestClient fabrikasi."""
    from fastapi.testclient import TestClient

    import api.routes.stream as stream_module
    from api.app import create_app

    fake = FakeClient()

    async def fake_resolve(file_id: str, _settings: Any, _client: Any) -> str:
        del file_id, _settings, _client
        return "documents/file_1.mp4"

    monkeypatch.setattr(stream_module, "get_telegram_file_path", fake_resolve)

    def factory(**overrides: Any) -> Any:
        for key, value in overrides.items():
            setattr(settings, key, value)
        app = create_app(settings)
        client = TestClient(app)
        client.__enter__()                      # lifespan: app.state.http yaratiladi
        app.state.http = fake                   # endi stub'ga almashtiramiz
        return client

    yield factory
    # TestClient'larni yopish (fixture teardown)
    import gc

    gc.collect()


# --------------------------------------------------------------------------- #
# Stream ticket'lar (imzolangan, qisqa muddatli)
# --------------------------------------------------------------------------- #
async def test_stream_ticket_roundtrip(share, storage, user) -> None:
    """Ticket imzolanadi va tekshiriladi (DB'ga bormasdan)."""
    record = await make_file(storage, user.telegram_id, name="film.mp4", category="video")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)
    assert ticket.token and "." in ticket.token

    verified = share.verify_stream_ticket(ticket.token)
    assert verified.file_id == record.id
    assert verified.owner_id == user.telegram_id
    assert verified.purpose == "stream"


async def test_stream_ticket_tampering_rejected(share, storage, user) -> None:
    """Imzo buzilsa yoki fayl id almashtirilsa — ticket rad etiladi."""
    record = await make_file(storage, user.telegram_id, name="film.mp4")
    token = share.issue_stream_ticket(record.id, user.telegram_id).token
    payload, signature = token.split(".", 1)

    with pytest.raises(Exception):  # noqa: B017
        share.verify_stream_ticket(f"{payload}.{'0' * len(signature)}")
    with pytest.raises(Exception):  # noqa: B017
        share.verify_stream_ticket("buzilgan")


async def test_stream_ticket_expires(share, storage, user) -> None:
    """Muddati o'tgan ticket ishlamaydi (replay/uzoq muddatli havola yo'q)."""
    record = await make_file(storage, user.telegram_id, name="film.mp4")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id, ttl=-1)
    with pytest.raises(Exception):  # noqa: B017
        share.verify_stream_ticket(ticket.token)


async def test_stream_url_shape(share, storage, user, settings) -> None:
    """`stream_url` Mini App pleeriga beriladigan manzilni quradi."""
    record = await make_file(storage, user.telegram_id, name="film.mp4")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)
    url = share.stream_url(ticket)
    assert url.startswith(settings.app.base_url.rstrip("/"))
    assert "/api/v1/stream/" in url
    assert "download" not in url
    url = share.stream_url(ticket, download=True)
    assert "?download=1" in url, "yuklab olish parametri ? bilan boshlanishi kerak"
    assert "&download=1" not in url


# --------------------------------------------------------------------------- #
# Stream endpoint (Range support)
# --------------------------------------------------------------------------- #
async def test_stream_returns_full_body(stream_client, share, storage, user) -> None:
    """`GET /stream/{ticket}` — 200, to'liq baytlar, `Accept-Ranges: bytes`."""
    record = await make_file(storage, user.telegram_id, name="film.mp4", category="video")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 200, response.text
    assert response.content == MEDIA_BYTES
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"].startswith("inline")
    assert "film.mp4" in response.headers["content-disposition"]


async def test_stream_supports_range_requests(stream_client, share, storage, user) -> None:
    """`Range: bytes=0-99` → 206 + `Content-Range` (pleer seek qilishi uchun)."""
    record = await make_file(storage, user.telegram_id, name="film.mp4", category="video")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206, response.text
    assert response.content == MEDIA_BYTES[0:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(MEDIA_BYTES)}"
    assert response.headers["content-length"] == "100"


async def test_stream_middle_range(stream_client, share, storage, user) -> None:
    """O'rtadan boshlanadigan Range ham to'g'ri ishlaydi."""
    record = await make_file(storage, user.telegram_id, name="film.mp4", category="video")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}", headers={"Range": "bytes=1024-1535"})
    assert response.status_code == 206
    assert response.content == MEDIA_BYTES[1024:1536]


async def test_stream_head_probe(stream_client, share, storage, user) -> None:
    """`HEAD` — pleer avval shu bilan imkoniyatni tekshiradi (tanasi yo'q)."""
    record = await make_file(storage, user.telegram_id, name="film.mp4", category="video")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    response = client.head(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == b""


async def test_stream_download_flag_sets_attachment(stream_client, share, storage, user) -> None:
    """`?download=1` → `Content-Disposition: attachment`."""
    record = await make_file(storage, user.telegram_id, name="hisobot.pdf", category="document")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id, purpose="download")

    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}?download=1")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


async def test_stream_increments_view_counter(stream_client, share, storage, user) -> None:
    """Ko'rishlar hisoblagichi oshadi (statistika uchun)."""
    record = await make_file(storage, user.telegram_id, name="film.mp4", category="video")
    before = int(record.views or 0)
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    client = stream_client()
    assert client.get(f"/api/v1/stream/{ticket.token}").status_code == 200

    await storage.session.refresh(record)
    assert int(record.views or 0) == before + 1


async def test_stream_rejects_bad_ticket(stream_client) -> None:
    """Noto'g'ri/imzasiz ticket → 4xx (media oqib ketmaydi)."""
    client = stream_client()
    assert client.get("/api/v1/stream/buzilgan-ticket").status_code in (400, 403, 404)


async def test_stream_rejects_ticket_of_missing_file(stream_client, share, user) -> None:
    """Ticket to'g'ri, lekin fayl bazada yo'q → 404."""
    ticket = share.issue_stream_ticket(uuid.uuid4(), user.telegram_id)
    client = stream_client()
    response = client.get(f"/api/v1/stream/{ticket.token}")
    assert response.status_code == 404


async def test_stream_disabled_returns_501(stream_client, share, storage, user, settings) -> None:
    """`STREAM_PROXY_ENABLED=false` → 501 (server-side proxy o'chirilgan)."""
    record = await make_file(storage, user.telegram_id, name="film.mp4")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    settings.storage.stream_proxy_enabled = False
    try:
        client = stream_client()
        assert client.get(f"/api/v1/stream/{ticket.token}").status_code == 501
    finally:
        settings.storage.stream_proxy_enabled = True


async def test_stream_upstream_error_is_502(stream_client, share, storage, user, monkeypatch) -> None:
    """Telegram 404/500 bersa → mijozga 502 (ichki xato oshkor bo'lmaydi)."""
    import api.routes.stream as stream_module

    async def boom(file_id: str, _settings: Any, _client: Any) -> str:
        raise httpx.HTTPError("upstream down")

    record = await make_file(storage, user.telegram_id, name="film.mp4")
    ticket = share.issue_stream_ticket(record.id, user.telegram_id)

    monkeypatch.setattr(stream_module, "get_telegram_file_path", boom)
    client = stream_client()
    assert client.get(f"/api/v1/stream/{ticket.token}").status_code in (500, 502)


# --------------------------------------------------------------------------- #
# Ulashish havolalari
# --------------------------------------------------------------------------- #
async def test_create_and_resolve_share_link(share, storage, user) -> None:
    """Havola yaratiladi va token bo'yicha topiladi."""
    record = await make_file(storage, user.telegram_id, name="umumiy.pdf")
    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=3600)
    assert link.token
    assert share.share_url(link.token).endswith(f"/s/{link.token}")

    resolved = await share.get_link(link.token)
    assert resolved.file_id == record.id
    assert resolved.is_active is True


# --------------------------------------------------------------------------- #
# Ko'rinarlilik va havola (Mini App "Ulashish"/"Import" oqimi shu qoidaga tayanadi)
# --------------------------------------------------------------------------- #
async def test_create_link_makes_private_file_resolvable(share, storage, user) -> None:
    """Havola yaratilganda PRIVATE fayl **UNLISTED** bo'ladi va token ishlaydi.

    Bu xatti-harakat oldin yo'q edi: havola yaratilar, lekin ``resolve_token``
    410 "This file is private" qaytarardi — ya'ni Mini App'dagi "Ulashish" va
    deep-link "Import" amalda ishlamasdi.
    """
    from db.models import Visibility

    record = await make_file(storage, user.telegram_id, name="maxfiy.mp4")
    assert record.visibility is Visibility.PRIVATE          # sukut bo'yicha shaxsiy

    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=3600)

    await storage.session.refresh(record)
    assert record.visibility is Visibility.UNLISTED          # token bilan ochiladi
    assert record.is_public is False                         # ro'yxatlarda ko'rinmaydi

    resolved, resolved_link = await share.resolve_token(link.token, register_view=False)
    assert resolved.id == record.id
    assert resolved_link.token == link.token


async def test_create_link_with_make_public_lists_the_file(share, storage, user) -> None:
    """``make_public=True`` → ``PUBLIC`` (ro'yxatlarda ham ko'rinadi)."""
    from db.models import Visibility

    record = await make_file(storage, user.telegram_id, name="ommaviy.mp4")
    link = await share.create_link(
        record.id, user.telegram_id, ttl_seconds=3600, make_public=True
    )

    await storage.session.refresh(record)
    assert record.visibility is Visibility.PUBLIC
    assert record.is_public is True
    resolved, _link = await share.resolve_token(link.token, register_view=False)
    assert resolved.id == record.id


async def test_private_file_is_never_reachable_by_token(share, storage, user) -> None:
    """Xavfsizlik qoidasi saqlanadi: PRIVATE fayl token bilan ochilmaydi."""
    import pytest as _pytest

    from db.models import Visibility
    from services.exceptions import LinkRevokedError

    record = await make_file(storage, user.telegram_id, name="yopiq.mp4")
    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=3600)

    # Egasi faylni qaytadan yopdi (havoladan voz kechdi)
    record.set_visibility(Visibility.PRIVATE)
    storage.session.add(record)
    await storage.session.commit()

    with _pytest.raises(LinkRevokedError):
        await share.resolve_token(link.token, register_view=False)


async def test_turning_public_off_keeps_live_links_working(share, storage, user) -> None:
    """"Ommaviy"ni o'chirish tarqatilgan havolalarni **buzmaydi**."""
    from db.models import Visibility

    record = await make_file(storage, user.telegram_id, name="hisobot.pdf")
    link = await share.create_link(
        record.id, user.telegram_id, ttl_seconds=3600, make_public=True
    )
    assert record.visibility is Visibility.PUBLIC

    updated = await storage.set_visibility(record.id, user.telegram_id, is_public=False)

    assert updated.visibility is Visibility.UNLISTED         # PRIVATE emas!
    assert updated.is_public is False                        # ro'yxatdan chiqdi
    resolved, _link = await share.resolve_token(link.token, register_view=False)
    assert resolved.id == record.id                          # havola ishlayapti


async def test_turning_public_off_without_links_goes_private(share, storage, user) -> None:
    """Faol havola bo'lmasa fayl to'liq yopiladi (``PRIVATE``)."""
    from db.models import Visibility

    record = await make_file(storage, user.telegram_id, name="yopiq2.pdf")
    await storage.set_visibility(record.id, user.telegram_id, is_public=True)

    updated = await storage.set_visibility(record.id, user.telegram_id, is_public=False)

    assert updated.visibility is Visibility.PRIVATE


async def test_expired_link_does_not_keep_file_unlisted(share, storage, user) -> None:
    """Muddati o'tgan havola faylni UNLISTED ushlab turmaydi."""
    import datetime as _dt

    from db.models import Visibility

    record = await make_file(storage, user.telegram_id, name="eski.mp4")
    link = await share.create_link(
        record.id, user.telegram_id, ttl_seconds=3600, make_public=True
    )
    link.expires_at = _dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=5)
    storage.session.add(link)
    await storage.session.commit()
    assert link.is_active_now is False

    updated = await storage.set_visibility(record.id, user.telegram_id, is_public=False)
    assert updated.visibility is Visibility.PRIVATE


async def test_revoked_link_does_not_keep_file_unlisted(share, storage, user) -> None:
    """Bekor qilingan havola ham faylni ochiq qoldirmaydi."""
    from db.models import Visibility

    record = await make_file(storage, user.telegram_id, name="bekor.mp4")
    link = await share.create_link(
        record.id, user.telegram_id, ttl_seconds=3600, make_public=True
    )
    await share.revoke_link(link.token, user.telegram_id)

    updated = await storage.set_visibility(record.id, user.telegram_id, is_public=False)
    assert updated.visibility is Visibility.PRIVATE


async def test_share_link_expiry(share, storage, user) -> None:
    """Muddati o'tgan havola faol emas."""
    import datetime as _dt

    record = await make_file(storage, user.telegram_id, name="vaqtinchalik.pdf")
    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=3600)
    assert link.is_expired is False
    assert link.is_active_now is True

    # Muddatni o'tmishga siljitamiz (ttl_seconds<=0 berilsa muddat UMUMAN
    # belgilanmaydi — ya'ni havola cheksiz, shuning uchun shunday tekshiramiz).
    link.expires_at = _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=10)
    storage.session.add(link)
    await storage.session.commit()

    assert link.is_expired is True
    assert link.is_active_now is False


async def test_link_without_ttl_never_expires(share, storage, user) -> None:
    """``ttl_seconds<=0`` -> muddatsiz havola (``expires_at=None``)."""
    record = await make_file(storage, user.telegram_id, name="muddatsiz.pdf")
    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=0)
    assert link.expires_at is None
    assert link.is_expired is False


async def test_revoke_share_link(share, storage, user) -> None:
    """Havolani bekor qilish (Mini App "Bekor qilish" tugmasi)."""
    record = await make_file(storage, user.telegram_id, name="maxfiy.pdf")
    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=3600)
    revoked = await share.revoke_link(link.token, user.telegram_id)
    assert revoked.is_active is False


async def test_share_link_owner_isolation(share, storage, user) -> None:
    """Boshqa foydalanuvchi havolani bekor qila olmaydi."""
    other, _ = await storage.get_or_create_user(telegram_id=888, first_name="Other")
    record = await make_file(storage, user.telegram_id, name="shaxsiy.pdf")
    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=3600)

    with pytest.raises(Exception):  # noqa: B017
        await share.revoke_link(link.token, other.telegram_id)


async def test_pass_code_protected_link(share, storage, user) -> None:
    """Parolli havola: to'g'ri kod bilan ochiladi, xato kod bilan yo'q."""
    record = await make_file(storage, user.telegram_id, name="parolli.zip")
    # Ommaviy havola: shaxsiy fayl havolasi tashqariga ochilmaydi (pastdagi test).
    link = await share.create_link(
        record.id, user.telegram_id, ttl_seconds=3600, make_public=True
    )
    updated = await share.set_pass_code(link.token, user.telegram_id, "123456")
    assert updated.pass_code
    assert updated.has_pass_code is True

    resolved, resolved_link = await share.resolve_token(link.token, pass_code="123456")
    assert resolved.id == record.id
    assert resolved_link.token == link.token

    with pytest.raises(Exception):  # noqa: B017
        await share.resolve_token(link.token, pass_code="000000")


async def test_private_file_link_is_not_public(share, storage, user) -> None:
    """Havola faylni **ommaviy** qilmaydi: u faqat token bilan ochiladi.

    Shartnoma (UI/UX qayta tuzilishidan keyin aniqlashtirildi):

    1. Egasi havola yaratganda fayl ``PRIVATE`` → ``UNLISTED`` ga o'tadi:
       token bilan ochiladi, lekin ``is_public`` False qoladi, ya'ni ommaviy
       ro'yxatlarda ko'rinmaydi. (Avval fayl PRIVATE qolar va havola ochilganda
       410 qaytarardi — "Ulashish" tugmasi amalda ishlamasdi.)
    2. Egasi faylni qayta yopsa (``PRIVATE``), tarqatilgan token darhol o'ladi:
       ma'lumot sizmaydi.
    3. Egasi uchun fayl har doim ochiq (``resolve_for_owner``).
    """
    from db.models import Visibility
    from services.exceptions import LinkRevokedError

    record = await make_file(storage, user.telegram_id, name="maxfiy.pdf")
    link = await share.create_link(record.id, user.telegram_id, ttl_seconds=3600)
    assert record.visibility is Visibility.UNLISTED
    assert record.is_public is False

    # 1) Havola ishlaydi (egasi uni ataylab yaratdi)
    resolved, _link = await share.resolve_token(link.token, register_view=False)
    assert resolved.id == record.id

    # 2) Egasi faylni qayta yopdi → token o'ladi
    record.set_visibility(Visibility.PRIVATE)
    storage.session.add(record)
    await storage.session.commit()
    with pytest.raises(LinkRevokedError):
        await share.resolve_token(link.token, register_view=False)

    # 3) Egasi uchun esa fayl ochiladi.
    own = await share.resolve_for_owner(record.id, user.telegram_id)
    assert own.id == record.id


# --------------------------------------------------------------------------- #
# 🔗 Deep-link → FSM (himoyalangan havola) oqimi
# --------------------------------------------------------------------------- #
class _FakeFSMState:
    """``FSMContext`` o'rnini bosadigan stub (faqat kerakli metodlar)."""

    def __init__(self) -> None:
        self.state: Any = None
        self.data: dict[str, Any] = {}

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def update_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.data.update(payload)
        return dict(self.data)

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)


class _FakeMessage:
    """``message.answer`` chaqiruvlarini yozib oladigan stub."""

    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.answers.append({"text": text, **kwargs})


async def test_protected_share_link_parks_token_in_fsm(share, storage, user, monkeypatch) -> None:
    """Parol bilan himoyalangan havola: token FSM'ga yoziladi va kod so'raladi.

    Bu yo'l avval ``NameError: name 'state' is not defined`` bilan yiqilardi —
    ``_open_share_token`` handler'dan ``state`` ni parametrsiz chaqirardi.
    """
    from bot.handlers.start import _open_share_token
    from config import get_settings
    from services.exceptions import InvalidPassCodeError

    record = await make_file(
        storage,
        user.telegram_id,
        name=f"maxfiy-{uuid.uuid4().hex[:6]}.pdf",
        size=2048,
    )
    link = await share.create_link(
        record.id, user.telegram_id, ttl_seconds=3600, make_public=True
    )
    await share.set_pass_code(link.token, user.telegram_id, "1234")

    async def _raise_invalid(*args: Any, **kwargs: Any) -> Any:
        raise InvalidPassCodeError("pass code required")

    monkeypatch.setattr(share, "resolve_token", _raise_invalid)

    message = _FakeMessage()
    state = _FakeFSMState()
    await _open_share_token(
        message,            # type: ignore[arg-type]
        link.token,
        user,
        storage,
        share,
        None,               # tg_storage — bu yo'lda ishlatilmaydi (kod darhol so'raladi)
        get_settings(),
        state,              # type: ignore[arg-type]
    )

    # Token FSM'ga park qilindi va foydalanuvchidan kod so'raldi.
    assert state.data.get("share_token") == link.token
    assert state.state is not None
    assert message.answers, "kod so'ralmadi (javob yuborilmagan)"


def test_open_share_token_signature_includes_state() -> None:
    """Statik kafolat: ``_open_share_token`` imzosida ``state`` bo'lishi shart."""
    import inspect

    from bot.handlers.start import _open_share_token

    parameters = inspect.signature(_open_share_token).parameters
    assert "state" in parameters, (
        "state parametri yo'q — himoyalangan havola oqimi NameError beradi"
    )
    # FSM turi annotatsiyada ham ko'rinadi (kelajakda tekshiruvlar uchun).
    assert "FSMContext" in str(parameters["state"].annotation)


# ---------------------------------------------------------------------------
# Content-Type: Telegram ``octet-stream`` + ``nosniff`` rasmni bloklamasin
# ---------------------------------------------------------------------------
def test_thumbnail_content_type_is_always_jpeg() -> None:
    """Thumbnail javobi ``image/jpeg`` — aks holda brauzer rasmni chizmaydi."""
    from api.routes.stream import _guess_media_type

    class _OctetUpstream:
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/octet-stream"}

    class _Record:
        mime_type = "video/mp4"      # asosiy fayl turi thumb'ga o'tmasligi kerak
        file_name = "clip.mp4"

    assert _guess_media_type(_Record(), is_thumb=True, upstream=_OctetUpstream()) == "image/jpeg"


def test_octet_stream_is_replaced_by_a_real_media_type() -> None:
    """``application/octet-stream`` o'rniga mime/kengaytmadan tur olinadi."""
    from api.routes.stream import _guess_media_type

    class _OctetUpstream:
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/octet-stream"}

    class _Photo:
        mime_type = None
        file_name = "IMG_0001.JPG"

    class _PhotoWithMime:
        mime_type = "image/png"
        file_name = "blob"

    class _VideoUpstream:
        headers: ClassVar[dict[str, str]] = {"Content-Type": "video/mp4"}

    class _Video:
        mime_type = None
        file_name = "clip.mp4"

    assert _guess_media_type(_Photo(), is_thumb=False, upstream=_OctetUpstream()) == "image/jpeg"
    assert _guess_media_type(_PhotoWithMime(), is_thumb=False, upstream=_OctetUpstream()) == "image/png"
    # Haqiqiy upstream turi ustun turadi
    assert _guess_media_type(_Video(), is_thumb=False, upstream=_VideoUpstream()) == "video/mp4"


def test_response_headers_set_an_explicit_content_type() -> None:
    """``_response_headers`` aniq tur yozadi va ``nosniff`` ni saqlaydi."""
    from api.routes.stream import _response_headers

    class _Upstream:
        headers: ClassVar[dict[str, str]] = {"Content-Type": "application/octet-stream", "Content-Length": "10"}

    headers = _response_headers(
        _Upstream(), download=False, name="photo.jpg", content_type="image/jpeg"
    )
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Disposition"].startswith("inline")
