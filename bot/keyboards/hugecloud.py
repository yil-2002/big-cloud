"""``hugecloud`` klaviaturalari: fayl guruhlari, saralash va boshqaruv paneli.

Callback grammatikasi (qisqa, chunki Telegram 64 bayt chegarasi bor):

=========================  =====================================================
``grp:<key>:<page>``       fayl guruhi: ``media``/``audio``/``document``/
                           ``archive``/``apps``/``all``/``pinned``
``sort::<ctx>``            saralash menyusini ochish (``<ctx>`` qaytish manzili)
``srt:<key>::<ctx>``       saralashni qo'llash va ``<ctx>`` ni qayta ko'rsatish
``adm``                    admin panel (faqat ``ADMIN_ID``)
``adm:stats``              📊 Tizim statistikasi
``adm:users:<page>``       👥 Foydalanuvchilar (sahifalangan)
``adm:u:<telegram_id>``    bitta foydalanuvchi kartasi
``adm:block:<id>``         bloklash / ``adm:unblock:<id>`` blokdan chiqarish
``adm:resetpw:<id>``       parolni tiklash
``adm:userfiles:<id>:<p>`` foydalanuvchi fayllari
``adm:bcast``              📢 E'lon yuborish
``adm:settings``           ⚙️ Tizim sozlamalari
``adm:toggle:auth``        autentifikatsiyani yoqish/o'chirish
``adm:toggle:register``    ro'yxatdan o'tishni ochish/yopish
``adm:archivestat``        arxiv kanalini tekshirish
``adm:audit:<page>``       📜 Audit jurnal
=========================  =====================================================

Saralash kalitlari (``srt:``) ``StorageService.list_files`` dagi
``sort_by``/``sort_desc`` juftligiga tarjima qilinadi — see :data:`SORTS`.
"""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot import texts
from bot.utils.emojis import Emoji
from db.models import User

__all__ = [
    "GROUP_KEYS",
    "SORTS",
    "admin_panel_kb",
    "admin_settings_kb",
    "admin_user_kb",
    "admin_users_page_kb",
    "audit_page_kb",
    "auth_gate_kb",
    "back_to_files",
    "files_menu_kb",
    "group_list_kb",
    "mini_app_home_kb",
    "noop_kb",
    "sort_kb",
    "sort_label",
]

#: ``/files`` guruh kalitlari (menyu tartibi).
GROUP_KEYS: tuple[str, ...] = ("media", "audio", "document", "archive", "apps", "pinned", "all")

#: Saralash variantlari: kalit -> (sort_by, sort_desc, matn funksiyasi nomi).
SORTS: dict[str, tuple[str, bool, str]] = {
    "new": ("created_at", True, "sort_newest"),
    "old": ("created_at", False, "sort_oldest"),
    "big": ("size", True, "sort_largest"),
    "small": ("size", False, "sort_smallest"),
    "az": ("name", True, "sort_name_asc"),
    "za": ("name", False, "sort_name_desc"),
}

#: Sukut bo'yicha saralash (yangi qo'shilgan fayl birinchi ko'rinadi).
DEFAULT_SORT = "new"


def sort_label(key: str) -> str:
    """Saralash kalitining foydalanuvchiga ko'rinadigan matni."""
    _, _, name = SORTS.get(key, SORTS[DEFAULT_SORT])
    renderer = getattr(texts, name, None)
    return renderer() if callable(renderer) else key


def _button(text: str, callback: str) -> InlineKeyboardButton:
    """Bitta inline tugma (qisqa yordamchi)."""
    return InlineKeyboardButton(text=text[:64], callback_data=callback[:64])


def _group_text(key: str) -> str:
    """Guruh tugmasi matni: emoji + o'zbekcha/inglizcha nom."""
    renderer = getattr(texts, f"group_{key}", None)
    label = renderer() if callable(renderer) else key.title()
    return label


# ---------------------------------------------------------------------------
# Fayllar menyusi
# ---------------------------------------------------------------------------
#: Mini App tugmasi matni (reply klaviaturadagi ``BTN_WEBAPP`` bilan bog'langan).
MINI_APP_LABEL = "Mini App"


def auth_gate_kb(*, web_app_url: str | None = None) -> InlineKeyboardMarkup:
    """🔐 Darvoza ekrani: kirish / ro'yxatdan o'tish (+ ixtiyoriy Mini App).

    Nega inline: darvoza xabari **callback** (tugma bosilganda) holatida ham
    ko'rsatiladi, u yerda reply klaviaturani o'zgartirib bo'lmaydi. Shu sababli
    asosiy tugmalar inline, reply klaviatura esa :func:`bot.keyboards.reply.auth_reply_keyboard`
    orqali qo'shimcha yuboriladi.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=texts.gate_button_login(), callback_data="auth:login", style="success"),
        InlineKeyboardButton(text=texts.gate_button_register(), callback_data="auth:register"),
    )
    if web_app_url:
        builder.row(
            InlineKeyboardButton(
                text=f"{Emoji.WEB_APP.plain()} {MINI_APP_LABEL}",
                web_app=WebAppInfo(url=web_app_url),
                style="primary",
            )
        )
    builder.row(InlineKeyboardButton(text=texts.gate_button_help(), callback_data="auth:help"))
    return builder.as_markup()


def mini_app_home_kb(*, web_app_url: str | None = None) -> InlineKeyboardMarkup:
    """✅ Kirishdan keyingi "bosh ekran": Mini App birinchi, qolgani ikkinchi daraja.

    Mini App Pivot talabiga ko'ra asosiy UX ilovaga ko'chadi: birinchi qator
    har doim katta ``Mini App`` tugmasi (``WEB_APP_URL`` bo'lsa), keyin esa
    bot ichidagi muqobil yo'llar.
    """
    builder = InlineKeyboardBuilder()
    if web_app_url:
        builder.row(
            InlineKeyboardButton(
                text=f"{Emoji.WEB_APP.plain()} {texts.home_open_mini_app()}",
                web_app=WebAppInfo(url=web_app_url),
                style="primary",
            )
        )
    builder.row(_button(_group_text("media"), "grp:media:0"), _button(_group_text("audio"), "grp:audio:0"))
    builder.row(_button(_group_text("document"), "grp:document:0"), _button(_group_text("archive"), "grp:archive:0"))
    builder.row(
        _button(f"{Emoji.PIN.plain()} {texts.home_pinned()}", "grp:pinned:0"),
        _button(f"{Emoji.SORT.plain()} {texts.home_sort()}", "sort"),
    )
    builder.row(
        _button(f"{Emoji.FILES.plain()} {texts.home_all_files()}", "grp:all:0"),
        _button(f"{Emoji.SHARE.plain()} {texts.home_share()}", "share"),
    )
    return builder.as_markup()


def files_menu_kb(*, sort_key: str = DEFAULT_SORT, web_app_url: str | None = None) -> InlineKeyboardMarkup:
    """🎬 🎵 📄 📦 guruhlar + 🔃 saralash tugmalari."""
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(_group_text("media"), "grp:media:0"),
        _button(_group_text("audio"), "grp:audio:0"),
    )
    builder.row(
        _button(_group_text("document"), "grp:document:0"),
        _button(_group_text("archive"), "grp:archive:0"),
    )
    builder.row(
        _button(_group_text("apps"), "grp:apps:0"),
        _button(_group_text("pinned"), "grp:pinned:0"),
    )
    builder.row(_button(_group_text("all"), "grp:all:0"))
    builder.row(_button(f"{Emoji.SORT.plain()} {sort_label(sort_key)}", "sort"))
    if web_app_url:
        builder.row(
            InlineKeyboardButton(
                text=f"{Emoji.WEB_APP.plain()} Mini App",
                web_app=WebAppInfo(url=web_app_url),
            )
        )
    builder.row(_button(f"{Emoji.HOME.plain()} Menyu", "menu"))
    return builder.as_markup()


def group_list_kb(
    group: str,
    page: int,
    pages: int,
    *,
    sort_key: str = DEFAULT_SORT,
) -> InlineKeyboardMarkup:
    """Guruh ro'yxati ostidagi sahifalash + saralash + orqaga tugmalari."""
    builder = InlineKeyboardBuilder()
    if pages > 1:
        row: list[InlineKeyboardButton] = []
        if page > 0:
            row.append(_button("⬅️ Oldingi", f"grp:{group}:{page - 1}"))
        row.append(_button(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            row.append(_button("Keyingi ➡️", f"grp:{group}:{page + 1}"))
        builder.row(*row)
    builder.row(
        _button(f"{Emoji.SORT.plain()} {sort_label(sort_key)}", f"sort::grp:{group}:{page}"),
        _button(f"{Emoji.FILES.plain()} Guruhlar", "files"),
    )
    builder.row(_button(f"{Emoji.HOME.plain()} Menyu", "menu"))
    return builder.as_markup()


def sort_kb(*, context: str = "files", current: str = DEFAULT_SORT) -> InlineKeyboardMarkup:
    """🔃 Saralash menyusi: 4 ta asosiy variant (spesifikatsiya bo'yicha) + nom.

    ``context`` — saralash qo'llangandan keyin qayta ochiladigan callback
    (masalan ``grp:media:2`` yoki ``files``). U ``::`` bilan ajratiladi, chunki
    kontekstning o'zida ``:`` bo'lishi mumkin (``srt:new::grp:media:2``).
    """
    builder = InlineKeyboardBuilder()
    order = ("new", "old", "big", "small", "az", "za")
    for index in range(0, len(order), 2):
        pair = order[index : index + 2]
        builder.row(*[_sort_button(key, context, current) for key in pair])
    builder.row(_button(f"{Emoji.BACK.plain()} Orqaga", context))
    return builder.as_markup()


def _sort_button(key: str, context: str, current: str) -> InlineKeyboardButton:
    """Bitta saralash tugmasi (joriy variant ``•`` bilan belgilanadi)."""
    marker = "• " if key == current else ""
    return _button(f"{marker}{sort_label(key)}", f"srt:{key}::{context}")


def back_to_files() -> InlineKeyboardMarkup:
    """Fayllar menyusiga qaytish tugmasi."""
    builder = InlineKeyboardBuilder()
    builder.row(_button(f"{Emoji.FILES.plain()} Fayllar menyusi", "files"))
    builder.row(_button(f"{Emoji.HOME.plain()} Menyu", "menu"))
    return builder.as_markup()


def noop_kb() -> InlineKeyboardMarkup:
    """Bo'sh klaviatura (callback xabarini tahrirlashda kerak bo'ladi)."""
    return InlineKeyboardMarkup(inline_keyboard=[])


# ---------------------------------------------------------------------------
# Boshqaruv paneli
# ---------------------------------------------------------------------------
def admin_panel_kb() -> InlineKeyboardMarkup:
    """🛡️ ``/diyor`` bosh menyusi."""
    builder = InlineKeyboardBuilder()
    builder.row(_button(f"{Emoji.STATS.plain()} Tizim statistikasi", "adm:stats"))
    builder.row(_button(f"{Emoji.USERS.plain()} Foydalanuvchilar", "adm:users:0"))
    builder.row(_button(f"{Emoji.BROADCAST.plain()} E'lon yuborish", "adm:bcast"))
    builder.row(_button(f"{Emoji.SETTINGS.plain()} Tizim sozlamalari", "adm:settings"))
    builder.row(_button(f"{Emoji.AUDIT.plain()} Audit jurnal", "adm:audit:0"))
    builder.row(_button(f"{Emoji.SHIELD.plain()} Arxiv holati", "adm:archivestat"))
    builder.row(_button(f"{Emoji.CLOSE.plain()} Yopish", "adm:close"))
    return builder.as_markup()


def admin_users_page_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    """👥 Foydalanuvchilar ro'yxati navigatsiyasi."""
    builder = InlineKeyboardBuilder()
    if pages > 1:
        row: list[InlineKeyboardButton] = []
        if page > 0:
            row.append(_button("⬅️", f"adm:users:{page - 1}"))
        row.append(_button(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            row.append(_button("➡️", f"adm:users:{page + 1}"))
        builder.row(*row)
    builder.row(_button(f"{Emoji.SEARCH.plain()} Qidirish", "adm:usearch"))
    builder.row(_button(f"{Emoji.BACK.plain()} Panel", "adm"))
    return builder.as_markup()


def admin_user_kb(user: User) -> InlineKeyboardMarkup:
    """Bitta foydalanuvchi uchun amallar."""
    builder = InlineKeyboardBuilder()
    telegram_id = user.telegram_id
    builder.row(_button(f"{Emoji.FILES.plain()} Fayllari", f"adm:userfiles:{telegram_id}:0"))
    if user.is_blocked:
        builder.row(_button(f"{Emoji.CHECK.plain()} Blokdan chiqarish", f"adm:unblock:{telegram_id}"))
    else:
        builder.row(_button(f"{Emoji.ERROR.plain()} Bloklash", f"adm:block:{telegram_id}"))
    builder.row(_button(f"{Emoji.KEY.plain()} Parolni tiklash", f"adm:resetpw:{telegram_id}"))
    builder.row(_button(f"{Emoji.CLOCK.plain()} Sessiyani tugatish", f"adm:logout:{telegram_id}"))
    builder.row(_button(f"{Emoji.BACK.plain()} Ro'yxatga", "adm:users:0"))
    builder.row(_button(f"{Emoji.SHIELD.plain()} Panel", "adm"))
    return builder.as_markup()


def admin_settings_kb(*, auth_required: bool, allow_register: bool) -> InlineKeyboardMarkup:
    """⚙️ Jonli sozlamalar (qayta ishga tushirmasdan o'zgaradi)."""
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            f"{'✅' if auth_required else '⛔'} Autentifikatsiya",
            "adm:toggle:auth",
        )
    )
    builder.row(
        _button(
            f"{'✅' if allow_register else '⛔'} Ro'yxatdan o'tish",
            "adm:toggle:register",
        )
    )
    builder.row(_button(f"{Emoji.SHIELD.plain()} Arxivni tekshirish", "adm:archivestat"))
    builder.row(_button(f"{Emoji.SEND.plain()} Broadcast (quruq)", "adm:bcast:dry"))
    builder.row(_button(f"{Emoji.BACK.plain()} Panel", "adm"))
    return builder.as_markup()


def audit_page_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    """📜 Audit jurnal sahifalari."""
    builder = InlineKeyboardBuilder()
    if pages > 1:
        row: list[InlineKeyboardButton] = []
        if page > 0:
            row.append(_button("⬅️", f"adm:audit:{page - 1}"))
        row.append(_button(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            row.append(_button("➡️", f"adm:audit:{page + 1}"))
        builder.row(*row)
    builder.row(_button(f"{Emoji.ERROR.plain()} Faqat xatolar", f"adm:auditf:{page}"))
    builder.row(_button(f"{Emoji.BACK.plain()} Panel", "adm"))
    return builder.as_markup()


def user_row_buttons(users: Sequence[User], *, per_row: int = 2) -> list[list[InlineKeyboardButton]]:
    """Foydalanuvchilar ro'yxatini tugmalarga aylantirish (admin qidiruv natijasi)."""
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for user in users:
        icon = "🚫" if user.is_blocked else ("✅" if user.is_authenticated else "👤")
        current.append(_button(f"{icon} {user.display_name[:24]}", f"adm:u:{user.telegram_id}"))
        if len(current) == per_row:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return rows
