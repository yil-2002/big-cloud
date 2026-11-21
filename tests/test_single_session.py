"""🔐 Bitta akkaunt = bitta sessiya (qurilmalar ro'yxati bilan).

Talab: login/parol to'g'ri kiritilsa, foydalanuvchi **eski qurilmasidan
chiqariladi**. Yangi sessiya akkauntning yagona faol sessiyasi bo'ladi, eski
token darhol ishlamaydi. Foydalanuvchi ``GET /auth/devices`` orqali qaysi
qurilmada faol ekanini ko'radi.
"""

from __future__ import annotations

import time

from api.security import (
    active_session_id,
    create_session_token,
    device_key,
    device_prefix,
    register_session,
    revocation_key,
)
from services.cache import get_cache


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(user, session_id: str, **meta):
    """Sessiyani akkauntning faol sessiyasi qilib ro'yxatga oladi."""
    return await register_session(
        get_cache(), user.telegram_id, session_id, ttl=600, **meta
    )


async def test_new_login_replaces_the_old_session(client, settings, user) -> None:
    """Yangi login — eski token shu zahoti ishlamay qoladi."""
    old = create_session_token(user.telegram_id, settings=settings, session_id="s-old")
    await _register(user, "s-old", user_agent="Telefon A")
    assert client.get("/api/v1/auth/me", headers=_auth(old)).status_code == 200

    # Boshqa qurilmadan login: yangi sessiya ro'yxatga olinadi.
    new = create_session_token(user.telegram_id, settings=settings, session_id="s-new")
    previous = await _register(user, "s-new", user_agent="Telefon B")

    assert previous == "s-old"
    revoked = await get_cache().get(revocation_key("s-old"))
    assert revoked is True

    after = client.get("/api/v1/auth/me", headers=_auth(old))
    assert after.status_code == 401, after.text
    assert after.json()["detail"]["code"] == "session_revoked"

    assert client.get("/api/v1/auth/me", headers=_auth(new)).status_code == 200


async def test_active_session_is_recorded(client, settings, user) -> None:
    """Faol sessiya id'si keshda saqlanadi (login paytida yoziladi)."""
    await _register(user, "s-active")
    assert await active_session_id(get_cache(), user.telegram_id) == "s-active"


async def test_session_without_registry_stays_valid(client, settings, user) -> None:
    """Keshda yozuv yo'q bo'lsa (masalan, eski token) autentifikatsiya ishlaydi."""
    token = create_session_token(user.telegram_id, settings=settings, session_id="s-unknown")
    response = client.get("/api/v1/auth/me", headers=_auth(token))
    assert response.status_code == 200, response.text


async def test_devices_lists_current_session(client, settings, user) -> None:
    """Qurilmalar ro'yxatida joriy sessiya belgilanadi."""
    await _register(user, "s-dev", user_agent="Mozilla/5.0 Test", ip_address="10.0.0.1")
    token = create_session_token(user.telegram_id, settings=settings, session_id="s-dev")

    response = client.get("/api/v1/auth/devices", headers=_auth(token))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["single_session"] is True
    assert len(payload["devices"]) == 1
    device = payload["devices"][0]
    assert device["sid"] == "s-dev"
    assert device["current"] is True
    assert device["user_agent"] == "Mozilla/5.0 Test"
    assert device["ip"] == "10.0.0.1"
    assert device["created_at"] > 0


async def test_register_session_clears_previous_device_records(settings, user) -> None:
    """Eski qurilma yozuvlari qolib ketmaydi (faqat bitta yozuv)."""
    cache = get_cache()
    await _register(user, "s-1")
    await get_cache().set(device_key(user.telegram_id, "s-1"), {"sid": "s-1"}, ttl=600)
    await _register(user, "s-2")

    keys = await cache.backend.keys(f"{device_prefix(user.telegram_id)}*")
    assert len(keys) == 1
    record = await cache.get(keys[0])
    assert record["sid"] == "s-2"


async def test_device_can_be_revoked(client, settings, user) -> None:
    """``DELETE /auth/devices/{sid}`` sessiyani yopadi."""
    await _register(user, "s-revoke")
    token = create_session_token(user.telegram_id, settings=settings, session_id="s-revoke")

    closed = client.delete("/api/v1/auth/devices/s-revoke", headers=_auth(token))
    assert closed.status_code == 200, closed.text

    assert await get_cache().get(revocation_key("s-revoke")) is True
    assert await active_session_id(get_cache(), user.telegram_id) is None
    assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 401


async def test_unknown_device_returns_404(client, settings, user) -> None:
    token = create_session_token(user.telegram_id, settings=settings, session_id="s-me")
    response = client.delete("/api/v1/auth/devices/yoq-sessiya", headers=_auth(token))
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "session_not_found"


async def test_devices_requires_authentication(client, settings, user) -> None:
    assert client.get("/api/v1/auth/devices").status_code in {401, 403}


async def test_register_session_returns_previous_and_records_meta(settings, user) -> None:
    cache = get_cache()
    await register_session(
        cache, user.telegram_id, "s-meta", ttl=600, user_agent="UA", ip_address="1.2.3.4"
    )
    record = await cache.get(device_key(user.telegram_id, "s-meta"))
    assert record["sid"] == "s-meta"
    assert record["user_agent"] == "UA"
    assert record["ip"] == "1.2.3.4"
    assert abs(int(record["created_at"]) - int(time.time())) < 60
