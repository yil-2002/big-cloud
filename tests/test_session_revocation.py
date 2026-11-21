"""🔐 Chiqish (``/auth/logout``) sessiyani **haqiqatan** bekor qiladi.

Ilgari bu endpoint amalda hech narsa qilmasdi: JWT stateless bo'lgani uchun
server tokenni unutib yubora olmasdi va nusxalangan token amal muddati
tugagunicha (24 soat) ishlayverardi. Endi ``sid`` rad ro'yxatiga yoziladi.
"""

from __future__ import annotations

import pytest

from api.security import (
    active_session_key,
    create_session_token,
    decode_session_token,
    revocation_key,
)
from services.cache import get_cache


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
async def _fresh_session_state(user) -> None:
    """Har test toza holatdan boshlanadi.

    Testlar bitta jarayondagi keshni bo'lishadi, shuning uchun oldingi test
    yozgan "faol sessiya" keyingi testga o'tib ketmasligi kerak.
    """
    await get_cache().delete(active_session_key(user.telegram_id))
    yield
    await get_cache().delete(active_session_key(user.telegram_id))


def test_token_works_before_logout(client, settings, user) -> None:
    """Boshlang'ich holat: token haqiqatan ishlaydi (nazorat nuqtasi)."""
    token = create_session_token(user.telegram_id, settings=settings, session_id="sess-a")
    response = client.get("/api/v1/auth/me", headers=_auth(token))
    assert response.status_code == 200, response.text


def test_logout_revokes_the_session(client, settings, user) -> None:
    """Chiqishdan keyin **xuddi shu** token 401 bilan rad etiladi."""
    token = create_session_token(user.telegram_id, settings=settings, session_id="sess-b")
    headers = _auth(token)

    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    closed = client.post("/api/v1/auth/logout", headers=headers)
    assert closed.status_code == 200, closed.text

    after = client.get("/api/v1/auth/me", headers=headers)
    assert after.status_code == 401, after.text
    assert after.json()["detail"]["code"] == "session_revoked"


async def test_revocation_is_recorded_in_the_cache(client, settings, user) -> None:
    """``sid`` keshga amal muddati bilan yoziladi."""
    token = create_session_token(
        user.telegram_id, settings=settings, session_id="sess-c", ttl=600
    )
    assert client.post("/api/v1/auth/logout", headers=_auth(token)).status_code == 200

    claims = decode_session_token(token, settings=settings)
    cached = await get_cache().get(revocation_key(str(claims["sid"])))
    assert cached is True


def test_other_sessions_are_not_affected(client, settings, user) -> None:
    """Bekor qilish faqat shu sessiyaga tegishli — boshqasi ishlashda davom etadi."""
    first = create_session_token(user.telegram_id, settings=settings, session_id="sess-d")
    second = create_session_token(user.telegram_id, settings=settings, session_id="sess-e")

    assert client.post("/api/v1/auth/logout", headers=_auth(first)).status_code == 200

    assert client.get("/api/v1/auth/me", headers=_auth(first)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=_auth(second)).status_code == 200


def test_revocation_key_is_namespaced() -> None:
    """Kalit boshqa kesh yozuvlari bilan to'qnashmasligi kerak."""
    assert revocation_key("abc") == "revoked_session:abc"
