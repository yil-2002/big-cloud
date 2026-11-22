"""👥 Ko'p profilli sessiyalar — Mini App API darajasida (uchdan-uchga).

Talab: boshqa profilga o'tish ishlasin; profilga muvaffaqiyatli kirilgandan
keyin eski sessiya yakunlansin. Fayllar profillar o'rtasida birlashmasin.
"""

from __future__ import annotations

from api.security import create_session_token, register_session
from services.auth import AuthService
from services.cache import get_cache
from tests.conftest import make_file


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _token(settings, telegram_id: int, session_id: str) -> str:
    """Token + uni Telegram foydalanuvchisining faol sessiyasi qilib ro'yxatga olish."""
    token = create_session_token(telegram_id, settings=settings, session_id=session_id)
    await register_session(get_cache(), telegram_id, session_id, ttl=600)
    return token


async def test_switch_profile_ends_old_session_over_api(client, settings, storage, user) -> None:
    auth = AuthService(storage.session, storage=storage, settings=settings)
    a_id, b_id = user.telegram_id, 888
    assert (await auth.register(a_id, "alisher", "parol123")).ok
    await make_file(storage, a_id, name="alisher.pdf")
    await storage.get_or_create_user(telegram_id=b_id, first_name="Bobur")
    assert (await auth.register(b_id, "bobur", "parol456")).ok
    await make_file(storage, b_id, name="bobur.pdf")

    token_a = await _token(settings, a_id, "mp-a")
    token_b = await _token(settings, b_id, "mp-b")

    response = client.get("/api/v1/auth/me", headers=_auth(token_a))
    assert response.status_code == 200, response.text
    me_a = response.json()
    assert me_a["login"] == "alisher" and me_a["is_authenticated"] is True

    # B "alisher" profiliga kiradi (bot yoki Mini App — bir xil servis).
    assert (await auth.login("alisher", "parol123", telegram_id=b_id)).ok

    # A ning sessiyasi yakunlandi: login ekrani, bulut yopiq.
    me_a = client.get("/api/v1/auth/me", headers=_auth(token_a)).json()
    assert me_a["is_authenticated"] is False
    assert client.get("/api/v1/files", headers=_auth(token_a)).status_code == 403

    # B endi "alisher" profilida: faqat shu profil fayllari, birlashma yo'q.
    me_b = client.get("/api/v1/auth/me", headers=_auth(token_b)).json()
    assert me_b["login"] == "alisher" and me_b["is_authenticated"] is True
    files = client.get("/api/v1/files", headers=_auth(token_b))
    assert files.status_code == 200
    names = {item["name"] for item in files.json()["items"]}
    assert names == {"alisher.pdf"}


async def test_extra_profile_over_api_is_isolated(client, settings, storage, user) -> None:
    auth = AuthService(storage.session, storage=storage, settings=settings)
    a_id = user.telegram_id
    assert (await auth.register(a_id, "alisher", "parol123")).ok
    await make_file(storage, a_id, name="asosiy.pdf")
    extra = (await auth.register(a_id, "ish_profil", "parol789")).user
    await make_file(storage, extra.telegram_id, name="ish.pdf")

    token = await _token(settings, a_id, "mp-x")
    me = client.get("/api/v1/auth/me", headers=_auth(token)).json()
    assert me["login"] == "ish_profil" and me["is_authenticated"] is True
    names = {i["name"] for i in client.get("/api/v1/files", headers=_auth(token)).json()["items"]}
    assert names == {"ish.pdf"}

    # Asosiy profilga qaytish.
    assert (await auth.login("alisher", "parol123", telegram_id=a_id)).ok
    names = {i["name"] for i in client.get("/api/v1/files", headers=_auth(token)).json()["items"]}
    assert names == {"asosiy.pdf"}
