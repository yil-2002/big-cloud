"""Ko'p profilli sessiyalar (multi-profile).

Model
-----
* **Profil** — ``users`` qatori: login + parol + o'z fayllari/papkalari.
  Telegram foydalanuvchisining o'z qatori (``telegram_id`` = haqiqiy ID) ham
  profil; ``/register`` bilan ochilgan qo'shimcha profillarning ``telegram_id``
  si **manfiy** sintetik son (Telegram ID bilan to'qnashmaydi).
* **Aktiv profil** — Telegram foydalanuvchisining o'z qatoridagi
  ``active_profile_id`` (``None`` = o'z qatori). Bot va Mini App har so'rovda
  shu profil bilan ishlaydi: fayllar, papkalar, kvota — hammasi profilniki.
* **Bitta profil = bitta sessiya** — profil qatoridagi ``session_owner_id``
  (sessiyani ushlab turgan Telegram ID).

Qoidalar (``activate``)
-----------------------
Login/parol to'g'ri kiritilganda:

1. chaqiruvchining **oldingi** profil sessiyasi yakunlanadi (u profilga qaytish
   uchun yana login kerak);
2. profilning **oldingi egasi** (boshqa Telegram) sessiyasi yakunlanadi — u
   keyingi xabarida darvozaga tushadi;
3. profil chaqiruvchiga biriktiriladi.

Hech qanday fayl yoki papka ko'chirilmaydi va birlashtirilmaydi: profillar
bir-biridan to'liq ajratilgan. (Ilgari boshqa Telegram'dan kirish hisobni
"ko'chirib", joriy profil bilan birlashtirib yuborardi.)
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User

logger = logging.getLogger(__name__)

__all__ = ["ProfileService"]

#: Qo'shimcha profil ID diapazoni: -(1 .. 2**52). Telegram foydalanuvchi ID'lari
#: musbat, shuning uchun to'qnashuv imkonsiz; JS ``Number`` ga ham sig'adi.
_EXTRA_ID_BITS = 52


class ProfileService:
    """Aktiv profilni aniqlash va profillar o'rtasida o'tish."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # So'rovdagi aktiv profil
    # ------------------------------------------------------------------
    async def resolve(self, home: User) -> User:
        """Telegram foydalanuvchisining aktiv profilini qaytarish.

        Qaytarilgan qatorda ``_session_ok`` belgisi bo'ladi (``User.is_signed_in``
        shuni hisobga oladi): sessiya boshqa Telegram'ga o'tgan bo'lsa — ``False``.
        """
        caller = int(home.telegram_id)
        profile = home
        pid = home.active_profile_id
        if pid is not None and pid != caller:
            other = await self.session.get(User, pid)
            if other is not None and other.has_session_for(caller):
                profile = other
            else:
                # Profil sessiyasi boshqa joyda ochildi (yoki profil o'chirildi):
                # bu Telegram foydalanuvchisining sessiyasi yakunlangan.
                home.active_profile_id = None
                self.session.add(home)
                await self.session.commit()
        profile._session_ok = (not profile.is_authenticated) or profile.has_session_for(caller)  # type: ignore[attr-defined]
        return profile

    # ------------------------------------------------------------------
    # Profilga kirish (login/parol allaqachon tekshirilgan)
    # ------------------------------------------------------------------
    async def activate(self, caller_id: int, profile: User) -> User:
        """``profile`` ni ``caller_id`` ning aktiv profili qilish (commit qilmaydi).

        Chaqiruvchidan oldin parol tekshirilgan bo'lishi SHART.
        """
        caller_id = int(caller_id)
        target_id = int(profile.telegram_id)
        home = await self._home(caller_id, fallback=profile)

        # 1) Chaqiruvchining oldingi profili — sessiyasi yakunlanadi.
        previous_id = home.active_profile_id if home.active_profile_id is not None else caller_id
        if previous_id != target_id:
            previous = home if previous_id == caller_id else await self.session.get(User, previous_id)
            if previous is not None and previous.has_session_for(caller_id):
                previous.logout()
                self.session.add(previous)

        # 2) Profilning oldingi egasi (boshqa Telegram) — sessiyasi yakunlanadi.
        holder = profile.session_holder
        if holder is not None and holder != caller_id:
            holder_home = await self.session.get(User, holder)
            if holder_home is not None and holder_home.active_profile_id == target_id:
                holder_home.active_profile_id = None
                self.session.add(holder_home)
            logger.info("Profile %s session moved from %s to %s", target_id, holder, caller_id)

        # 3) Profil endi chaqiruvchiniki.
        profile.mark_authenticated()
        profile.session_owner_id = caller_id
        self.session.add(profile)
        if home is not profile:
            home.active_profile_id = None if target_id == caller_id else target_id
            self.session.add(home)
        profile._session_ok = True  # type: ignore[attr-defined]
        return profile

    async def _home(self, caller_id: int, *, fallback: User) -> User:
        """Chaqiruvchining o'z qatori (yo'q bo'lsa yaratiladi).

        Telegram'siz mijoz (brauzer) profilning o'z ID si bilan keladi — u holda
        uy qatori profilning o'zi.
        """
        if int(fallback.telegram_id) == caller_id:
            return fallback
        home = await self.session.get(User, caller_id)
        if home is None:
            home = User(telegram_id=caller_id)
            self.session.add(home)
            await self.session.flush()
        return home

    # ------------------------------------------------------------------
    # Qo'shimcha profil
    # ------------------------------------------------------------------
    async def create_extra_profile(self, caller_id: int, login: str, password: str) -> User:
        """Yangi (qo'shimcha) profil yaratish va unga o'tish (commit qilmaydi)."""
        home = await self.session.get(User, int(caller_id))
        profile = User(telegram_id=await self._new_extra_id(), profile_creator_id=int(caller_id))
        if home is not None:
            # Ko'rinish uchun ism; Telegram profil maydonlari o'zgarmaydi.
            profile.first_name = home.first_name
            profile.language_code = home.language_code
        profile.register_credentials(login, password)
        self.session.add(profile)
        await self.session.flush()
        return await self.activate(caller_id, profile)

    async def _new_extra_id(self) -> int:
        while True:
            candidate = -(secrets.randbits(_EXTRA_ID_BITS) + 1)
            if await self.session.get(User, candidate) is None:
                return candidate

    async def list_profiles(self, caller_id: int) -> list[User]:
        """Chaqiruvchiga tegishli profillar: o'z qatori (hisobi bo'lsa),
        u yaratgan qo'shimcha profillar va hozirgi aktiv profil."""
        caller_id = int(caller_id)
        rows = (
            await self.session.execute(
                select(User)
                .where(
                    or_(User.telegram_id == caller_id, User.profile_creator_id == caller_id),
                    User.login.is_not(None),
                )
                .order_by(User.created_at)
            )
        ).scalars().all()
        profiles = list(rows)
        home = await self.session.get(User, caller_id)
        if home is not None and home.active_profile_id is not None:
            active = await self.session.get(User, home.active_profile_id)
            if active is not None and active not in profiles:
                profiles.append(active)
        return profiles
