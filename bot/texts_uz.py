"""O'zbekcha interfeys matnlari (eski bot tilini saqlab qolish uchun).

The legacy bot spoke Uzbek; this module provides the exact same wording for every
screen. :func:`install` replaces the functions in :mod:`bot.texts` when
``APP_LANGUAGE=uz`` (the default), so handlers keep calling ``texts.file_saved(...)``
and automatically emit Uzbek.

Rules followed here:

* HTML parse mode markup only (``<b>``, ``<code>``, ``<i>``);
* user-supplied text is escaped via :func:`bot.utils.formatters.escape_html`;
* emoji come from :class:`bot.utils.emojis.Emoji`, so custom emoji support and
  the legacy icon set (🎬 🖼️ 🤖 🍎 📌 📁) stay identical.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import bot.texts as en
from bot.utils.emojis import Emoji
from bot.utils.formatters import (
    escape_html,
    format_date,
    format_duration,
    format_number,
    format_relative_time,
    format_size,
    human_size_pair,
    key_value_line,
    mask_token,
    progress_bar,
    storage_status_emoji,
    truncate_middle,
)
from db.models import File, Folder, SharedLink, User

__all__ = ["install", "translations"]


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
#: Python 3.11 f-string'larida **backslash va tashqi tirnoqni takrorlash mumkin
#: emas** (bu imkoniyat 3.12'da qo'shilgan). Shu sababli apostrofli satr
#: oldindan konstantaga chiqariladi — obraz `python:3.11-slim` da ham ishlaydi.
_DEFAULT_NICKNAME = "do'stim"


def welcome(user: User, *, bot_username: str, web_app_url: str | None = None) -> str:
    """Birinchi marta kirganda ko'rsatiladigan xabar."""
    lines = [
        f"{Emoji.CLOUD} <b>Shaxsiy Bulut Xotirangiz</b>",
        "",
        f"Xush kelibsiz, <b>{escape_html(user.first_name or _DEFAULT_NICKNAME)}</b>!",
        "",
        f"{Emoji.UPLOAD} <b>Fayl yuboring — avtomatik saqlanadi!</b>",
        "Hech narsa server diskiga yozilmaydi: fayllar Telegram'da qoladi,",
        "bizda faqat shifrlangan manzil saqlanadi.",
        "",
        f"{Emoji.DOWNLOAD} Yuklab olish — bir zumda, trafik sarflanmaydi",
        f"{Emoji.SHARE} Ulashish — muddatli havola va parol bilan",
        f"{Emoji.INLINE} Inline — istalgan chatda <code>@{escape_html(bot_username)} nom</code>",
        f"{Emoji.PIN} Muhim fayllarni 📌 belgilang",
    ]
    if web_app_url:
        lines.append(f"\n{Emoji.WEB_APP} Web interfeys: <code>{escape_html(web_app_url)}</code>")
    lines.append("\nQuyidagi menyudan tanlang:")
    return "\n".join(lines)


def help_text(*, bot_username: str) -> str:
    """``/help`` — buyruqlar va maslahatlar."""
    rows = "\n".join(
        f"{Emoji.STAR} <b>/{name}</b> — {escape_html(description)}" for name, description in en.COMMANDS
    )
    return "\n".join(
        [
            f"{Emoji.ROBOT} <b>Yordam</b>",
            "",
            rows,
            "",
            f"{Emoji.INFO} <b>Maslahatlar</b>",
            "• Bir nechta faylni birdan yuboring (albom) — hammasi saqlanadi.",
            "• Faylga izoh (caption) yozsangiz, u ham saqlanadi.",
            f"• Istalgan chatda <code>@{escape_html(bot_username)}</code> deb yozing.",
            "• Fayllar Huge Cloud xavfsiz infratuzilmasida turadi — joyingiz cheksiz.",
        ]
    )


def dashboard(
    user: User, stats: dict[str, Any], *, bot_username: str, is_admin: bool = False
) -> str:
    """Bosh ekran: kvota, hisoblagichlar va tezkor amallar."""
    quota = int(stats.get("quota") or 0)
    used = int(stats.get("used") or 0)
    indicator = storage_status_emoji(used, quota)

    return "\n".join(
        [
            f"{Emoji.CLOUD} <b>Shaxsiy Bulut Xotirangiz</b>",
            "",
            f"{indicator} <code>{progress_bar(used, quota or 1, width=12)}</code>",
            f"{Emoji.STATS} {key_value_line('Band', human_size_pair(used, quota))}",
            f"{Emoji.FILE} {key_value_line('Fayllar', format_number(int(stats.get('files') or 0)))}",
            f"{Emoji.FOLDER} {key_value_line('Papkalar', format_number(int(stats.get('folders') or 0)))}",
            f"{Emoji.PIN} {key_value_line('Muhimlar', format_number(int(stats.get('pinned_files') or 0)))}",
            f"{Emoji.LINK} {key_value_line('Ommaviy', format_number(int(stats.get('public_files') or 0)))}",
            "",
            f"{Emoji.UPLOAD} Fayl yuboring — avtomatik saqlanadi!",
            "Quyidagi menyudan tanlang:",
        ]
    )


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------
def file_saved(record: File, *, user: User, quota: int, position: int = 1, total: int = 1) -> str:
    """Saqlangan fayl tasdiqnomasi (eski bot uslubida: ikonka + nom + hajm)."""
    counter = f" ({position}/{total})" if total > 1 else ""
    return (
        f"✅ <b>{escape_html(record.file_name)}</b>{counter} yuklandi\n"
        f"💾 {format_size(record.file_size)}"
    )



def album_saved(records: Sequence[File], *, user: User, quota: int) -> str:
    """Albom uchun umumiy tasdiqnoma."""
    names = "\n".join(
        f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 40))} ✅"
        for record in records[:10]
    )
    more = f"\n… va yana {len(records) - 10} ta ✅" if len(records) > 10 else ""
    return f"{names}{more}\n\n{len(records)} ta fayl yuklandi ✅"


def too_large(size: int, limit: int) -> str:
    return "\n".join(
        [
            f"{Emoji.ERROR} <b>Fayl juda katta</b>",
            "",
            f"{key_value_line('Yuborilgan', format_size(size))}",
            f"{key_value_line('Chegara', format_size(limit))}",
            "",
            f"{Emoji.INFO} Katta fayllar uchun kengaytirilgan rejim ishlating.",
        ]
    )


def quota_exceeded(*, required: int, free: int, quota: int, used: int) -> str:
    return "\n".join(
        [
            f"{Emoji.STORAGE_FULL} <b>Bo'sh joy yetarli emas</b>",
            "",
            f"{key_value_line('Fayl hajmi', format_size(required))}",
            f"{key_value_line('Hozir bo‘sh', format_size(free))}",
            f"{key_value_line('Kvota', human_size_pair(used, quota))}",
            "",
            f"{Emoji.DELETE} <b>/storage</b> orqali keraksiz faylni o'chiring.",
        ]
    )


def duplicate_file(name: str) -> str:
    return (
        f"{Emoji.INFO} <b>Bu fayl allaqachon saqlangan</b>\n"
        f"{Emoji.FILE} {escape_html(truncate_middle(name, 48))}\n\n"
        f"Bir xil fayllar ikki marta saqlanmaydi — {Emoji.FOLDER} <b>/storage</b> orqali oching."
    )


def unsupported_media() -> str:
    return (
        f"{Emoji.WARNING} <b>Buni saqlay olmayman.</b>\n"
        "Qo'llab-quvvatlanadi: hujjat, rasm, video, audio, ovozli xabar, "
        "video-xabar, animatsiya, stiker, APK va IPA.\n\n"
        f"{Emoji.INFO} To'liq ro'yxat: <b>/help</b>."
    )


def upload_hint() -> str:
    return "\n".join(
        [
            f"{Emoji.UPLOAD} <b>Qanday yuklanadi?</b>",
            "",
            "1. Faylni shu chatga yuboring (hujjat, rasm, video yoki audio).",
            "2. Xohlasangiz izoh (caption) yozing — u ham saqlanadi.",
            "3. Men sizga 📌 Pin, 📁 Ko'chirish, 🗑️ O'chirish tugmalari bilan javob beraman.",
            "",
            f"{Emoji.SPARKLES} Albom ham ishlaydi: 10 tagacha faylni birdan yuboring.",
            f"{Emoji.WARNING} Hajm chegarasi va kvota: <b>/stats</b>.",
        ]
    )


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------
def file_card(record: File, *, folder: Folder | None = None, links: Sequence[SharedLink] = ()) -> str:
    """Bitta fayl haqidagi to'liq ma'lumot."""
    active = [link for link in links if link.is_active_now]
    link_line = ""
    if active:
        link = active[0]
        expires = (
            f"{format_relative_time(link.expires_at).replace(' ago', '')} dan keyin tugaydi"
            if link.expires_at
            else "muddatsiz"
        )
        lock = f" {Emoji.KEY}" if link.has_pass_code else ""
        link_line = (
            f"\n{Emoji.LINK} {key_value_line('Havola', mask_token(link.token) + lock)} <i>({expires})</i>"
        )

    extra: list[str] = []
    if record.duration:
        extra.append(key_value_line("Davomiyligi", format_duration(record.duration)))
    if record.width and record.height:
        extra.append(key_value_line("O'lcham", f"{record.width}×{record.height}"))
    if record.mime_type:
        extra.append(key_value_line("Turi", record.mime_type))

    pin = f"{Emoji.PIN.plain()} " if record.is_pinned else ""
    return "\n".join(
        [
            f"{pin}{Emoji.for_category(record.category)} <b>{escape_html(truncate_middle(record.file_name, 60))}</b>",
            "",
            f"💾 {key_value_line('Hajm', format_size(record.file_size))}",
            f"{Emoji.CLOCK} {key_value_line('Qo‘shilgan', format_date(record.created_at))}",
            f"{Emoji.FOLDER} {key_value_line('Papka', folder.breadcrumb() if folder else 'umumiy')}",
            *extra,
            f"{Emoji.EYE} {key_value_line('Ko‘rishlar', format_number(record.views))}"
            f"  ·  {Emoji.DOWNLOAD.plain()} {format_number(record.downloads)}",
            f"{Emoji.LOCK if not record.is_public else Emoji.UNLOCK} "
            f"{key_value_line('Holat', 'Ommaviy' if record.is_public else 'Shaxsiy')}",
            link_line,
        ]
    )


def folder_list(
    folders: Sequence[Folder],
    files: Sequence[File],
    *,
    current: Folder | None,
    page: int,
    pages: int,
    total_files: int,
    query: str | None = None,
) -> str:
    """Brouzer ekrani sarlavhasi."""
    location = current.breadcrumb() if current else "umumiy"
    title = (
        f"{Emoji.SEARCH} Qidiruv: “{escape_html(query)}”"
        if query
        else f"{Emoji.FOLDER_OPEN} <b>{escape_html(location)}</b>"
    )
    if not files and not folders:
        return (
            f"{title}\n\n{Emoji.EMPTY} <b>Hozircha fayl yo'q.</b>\n"
            f"{Emoji.UPLOAD} Fayl yuboring — saqlanadi!"
        )
    return (
        f"{title}\n"
        f"{Emoji.INFO} Sahifa <b>{page}/{pages}</b> · {format_number(total_files)} ta fayl"
        f" · {len(folders)} ta papka\n\n"
        "Faylni bosing — menyusi ochiladi."
    )


def empty_folder() -> str:
    return (
        f"{Emoji.EMPTY} <b>Bu papka bo'sh.</b>\n"
        f"{Emoji.UPLOAD} Fayl yuboring va “Ko'chirish” orqali shu yerga joylang."
    )


def folder_new_prompt() -> str:
    return (
        f"{Emoji.NEW_FOLDER} <b>Yangi papka</b>\n"
        "Papka nomini yozing (255 belgigacha), yoki "
        f"{Emoji.CLOSE} <b>Bekor qilish</b> tugmasini bosing."
    )


def folder_created(folder: Folder) -> str:
    return f"{Emoji.CHECK} <b>{escape_html(folder.name)}</b> papkasi yaratildi!"


def folder_deleted(name: str, *, files_removed: int) -> str:
    removed = f" ({files_removed} ta fayl o'chirildi)" if files_removed else ""
    return f"{Emoji.DELETE} <b>{escape_html(name)}</b> papkasi o'chirildi{removed}."


def folder_not_empty(*, files: int, folders: int) -> str:
    return (
        f"{Emoji.WARNING} <b>Papka bo'sh emas</b>\n"
        f"Ichida: {files} ta fayl va {folders} ta ichki papka.\n"
        "Hammasini o'chirish uchun “Ichidagilar bilan o'chirish” tugmasini bosing."
    )


def file_renamed_prompt(record: File) -> str:
    return (
        f"{Emoji.RENAME} <b>Fayl nomini o'zgartirish</b>\n"
        f"Hozirgi nomi: <code>{escape_html(record.file_name)}</code>\n"
        f"Yangi nomni yozing yoki {Emoji.CLOSE} <b>Bekor qilish</b>."
    )


def file_renamed(old: str, new: str) -> str:
    return (
        f"{Emoji.CHECK} Nom o'zgartirildi:\n"
        f"<code>{escape_html(truncate_middle(old, 40))}</code> → "
        f"<code>{escape_html(truncate_middle(new, 40))}</code>"
    )


def move_target_prompt(record: File, *, folder_count: int) -> str:
    return (
        f"{Emoji.MOVE} <b>“{escape_html(truncate_middle(record.file_name, 32))}” ni qaysi papkaga ko'chirish?</b>\n"
        f"Mavjud papkalar: {folder_count} ta. Yoki {Emoji.HOME} <b>umumiy</b>ga qaytaring."
    )


def file_moved(record: File, folder: Folder | None) -> str:
    destination = folder.breadcrumb() if folder else "umumiy"
    return (
        f"{Emoji.CHECK} <b>{escape_html(truncate_middle(record.file_name, 40))}</b> → "
        f"{Emoji.FOLDER} {escape_html(destination)}"
    )


def confirm_delete_file(record: File) -> str:
    return (
        f"{Emoji.WARNING} <b>{escape_html(truncate_middle(record.file_name, 48))}</b> "
        "ni o'chirishni tasdiqlaysizmi?\n\n"
        f"💾 {format_size(record.file_size)} bo'shaydi. "
        "Barcha ulashish havolalari ham bekor qilinadi."
    )


def confirm_delete_folder(folder: Folder, *, files: int, subfolders: int) -> str:
    return (
        f"{Emoji.WARNING} <b>“{escape_html(folder.name)}” papkasini o'chirish?</b>\n\n"
        f"Ichida: {files} ta fayl va {subfolders} ta ichki papka.\n"
        "Hammasi butunlay o'chiriladi."
    )


def file_deleted(record: File, *, freed: int) -> str:
    return (
        f"{Emoji.DELETE} <b>{escape_html(truncate_middle(record.file_name, 48))}</b> o'chirildi!\n"
        f"{Emoji.STORAGE_OK} {format_size(freed)} bo'shadi."
    )


def search_prompt() -> str:
    return (
        f"{Emoji.SEARCH} <b>Qidiruv</b>\n"
        "Fayl nomini (yoki uning bir qismini) yozing.\n"
        f"{Emoji.CLOSE} Bekor qilish uchun tugmani bosing."
    )


def search_results(records: Sequence[File], query: str) -> str:
    if not records:
        return (
            f"{Emoji.SEARCH} “{escape_html(query)}” bo'yicha hech narsa topilmadi.\n"
            f"{Emoji.INFO} Qisqaroq so'z bilan sinab ko'ring yoki <b>/storage</b> orqali ko'ring."
        )
    lines = "".join(
        f"\n{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 42))} "
        f"<i>({format_size(record.file_size)})</i>"
        for record in records[:15]
    )
    return f"{Emoji.SEARCH} <b>{len(records)} ta natija:</b> “{escape_html(query)}”{lines}"


def search_empty() -> str:
    return f"{Emoji.SEARCH} Hech narsa topilmadi."


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------
def share_menu(record: File, links: Sequence[SharedLink]) -> str:
    active = [link for link in links if link.is_active_now]
    state = (
        f"{Emoji.LOCK} <b>Shaxsiy</b> — faol havola yo'q."
        if not active
        else f"{Emoji.UNLOCK} <b>{len(active)} ta faol havola.</b>"
    )
    return "\n".join(
        [
            f"{Emoji.SHARE} <b>Ulashish</b>",
            f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 48))}",
            "",
            state,
            "",
            "Muddatli havola yarating, uni parol bilan himoya qiling",
            "yoki faylni ommaviy qiling.",
        ]
    )


def link_created(
    link: SharedLink,
    record: File,
    *,
    deep_link: str,
    web_url: str,
    pass_code: str | None = None,
) -> str:
    expires = (
        f"{format_relative_time(link.expires_at).replace(' ago', '')} dan keyin tugaydi"
        if link.expires_at
        else "muddatsiz"
    )
    lines = [
        f"{Emoji.LINK} <b>Havola yaratildi</b>",
        f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 48))}",
        "",
        f"{Emoji.ROBOT} <b>Telegram:</b>\n<code>{escape_html(deep_link)}</code>",
        f"{Emoji.WEB_APP} <b>Web:</b>\n<code>{escape_html(web_url)}</code>",
        "",
        f"{Emoji.CLOCK} {escape_html(expires)}",
    ]
    if pass_code:
        lines.append(f"{Emoji.KEY} <b>Parol:</b> <code>{escape_html(pass_code)}</code>")
    lines.append(f"\n{Emoji.INFO} Istalgan havolani ulashing — parol bir marta so'raladi.")
    return "\n".join(lines)


def link_list(record: File, links: Sequence[SharedLink]) -> str:
    if not links:
        return (
            f"{Emoji.EMPTY} <b>{escape_html(truncate_middle(record.file_name, 40))}</b> "
            "uchun havola yo'q."
        )
    rows: list[str] = []
    for link in links[:10]:
        if not link.is_active:
            state = f"{Emoji.CROSS} bekor qilingan"
        elif link.is_expired:
            state = f"{Emoji.CLOCK} muddati tugagan"
        else:
            state = f"{Emoji.CHECK} faol"
        lock = f" {Emoji.KEY}" if link.has_pass_code else ""
        rows.append(
            f"\n<code>{escape_html(mask_token(link.token, visible=6))}</code>{lock} — {state}"
            f"\n   {Emoji.EYE.plain()} {link.views} · {Emoji.DOWNLOAD.plain()} {link.downloads}"
            f" · {format_relative_time(link.created_at)}"
        )
    return (
        f"{Emoji.SHARE} <b>{escape_html(truncate_middle(record.file_name, 36))} havolalari</b>"
        + "".join(rows)
    )


def link_revoked(token: str) -> str:
    return f"{Emoji.CROSS} <code>{escape_html(mask_token(token))}</code> havolasi bekor qilindi."


def link_expired_message() -> str:
    return f"{Emoji.CLOCK} <b>Bu havolaning muddati tugagan.</b>\nEgasidan yangisini so'rang."


def link_revoked_message() -> str:
    return f"{Emoji.CROSS} <b>Bu havola egasi tomonidan bekor qilingan.</b>"


def link_not_found() -> str:
    return f"{Emoji.ERROR} <b>Havola topilmadi.</b>\nU o'chirilgan bo'lishi mumkin."


def pass_code_prompt() -> str:
    return (
        f"{Emoji.KEY} <b>Kirish kodi kerak</b>\n"
        "Fayl egasi bergan raqamli kodni yuboring."
    )


def pass_code_wrong() -> str:
    return f"{Emoji.KEY} <b>Kod noto'g'ri.</b>\nHavolani qayta ochib, urinib ko'ring."


# ---------------------------------------------------------------------------
# Deep links
# ---------------------------------------------------------------------------
def deep_link_file_saved(record: File, *, owner_name: str | None = None) -> str:
    source = f" ({escape_html(owner_name)} dan)" if owner_name else ""
    return "\n".join(
        [
            f"{Emoji.CHECK} <b>Bulutingizga saqlandi{source}</b>",
            "",
            f"{Emoji.for_category(record.category)} {escape_html(truncate_middle(record.file_name, 48))}",
            f"💾 {key_value_line('Hajm', format_size(record.file_size))}",
            "",
            f"{Emoji.DOWNLOAD} Fayl yuqorida — bosing va yuklab oling.",
            f"{Emoji.FOLDER} Boshqarish: <b>/storage</b>.",
        ]
    )


def deep_link_not_found(payload: str) -> str:
    return (
        f"{Emoji.ERROR} <b>Bu havola bo'yicha hech narsa topilmadi.</b>\n"
        f"Kod: <code>{escape_html(mask_token(payload, visible=8))}</code>\n\n"
        "Fayl o'chirilgan yoki havola muddati tugagan bo'lishi mumkin."
    )


# ---------------------------------------------------------------------------
# Inline
# ---------------------------------------------------------------------------
def inline_hint(*, bot_username: str) -> str:
    return "\n".join(
        [
            f"{Emoji.INLINE} <b>Inline rejim</b>",
            "",
            f"<b>Istalgan</b> chatda <code>@{escape_html(bot_username)} fayl nomi</code> deb yozing —",
            "guruh, kanal izohlari yoki boshqa odam bilan yozishmalarda.",
            "Natijada o'z bulutingizdagi fayllar chiqadi va bir zumda yuboriladi.",
            "",
            f"{Emoji.LOCK} Natijalarni <b>faqat siz</b> ko'rasiz.",
            f"{Emoji.SETTINGS} <b>/settings</b> orqali o'chirib qo'yish mumkin.",
        ]
    )


def inline_empty(query: str) -> str:
    return f"{Emoji.EMPTY} “{escape_html(query)}” bo'yicha hech narsa topilmadi."


def inline_disabled() -> str:
    return (
        f"{Emoji.LOCK} <b>Inline qidiruv o'chirilgan.</b>\n"
        f"{Emoji.SETTINGS} <b>/settings</b> orqali yoqing."
    )


# ---------------------------------------------------------------------------
# Settings, stats, auth
# ---------------------------------------------------------------------------
def settings_text(user: User, settings_map: dict[str, Any]) -> str:
    rows = "".join(
        f"\n{(Emoji.CHECK if value else Emoji.CROSS).plain()} {escape_html(label)}"
        for label, value in settings_map.items()
    )
    return (
        f"{Emoji.SETTINGS} <b>Sozlamalar</b>{rows}\n\n"
        f"{Emoji.INFO} Qatorni bosib o'zgartiring — darhol kuchga kiradi."
    )


def setting_toggled(label: str, value: bool) -> str:
    icon = Emoji.CHECK if value else Emoji.CROSS
    state = "yoqilgan" if value else "o‘chirilgan"
    return f"{icon} <b>{escape_html(label)}</b>: {state}"


def stats_text(user: User, stats: dict[str, Any], *, max_file_size: int) -> str:
    """Eski botning 📊 Statistika ekrani (MB/GB ko'rinishi bilan)."""
    quota = int(stats.get("quota") or 0)
    used = int(stats.get("used") or 0)
    mb = round(used / 1024 / 1024, 2)
    gb = round(mb / 1024, 3)
    by_cat = stats.get("by_category") or {}

    def count(name: str) -> int:
        return int((by_cat.get(name) or {}).get("count", 0))

    rows = "".join(
        f"\n  {Emoji.for_category(name)} {escape_html(str(name).upper() if name in ('apk', 'ipa') else name.title())}: "
        f"<b>{format_number(item['count'])}</b> · {format_size(item['size'])}"
        for name, item in sorted(by_cat.items(), key=lambda kv: -kv[1]["size"])
    )
    return "\n".join(
        [
            f"{Emoji.STATS} <b>Statistika</b>",
            "",
            f"{storage_status_emoji(used, quota)} <code>{progress_bar(used, quota or 1, width=14)}</code>",
            f"{Emoji.FILE} {key_value_line('Jami fayllar', format_number(int(stats.get('files') or 0)))}",
            f"💾 {key_value_line('Umumiy hajm', f'{mb} MB ({gb} GB)')}",
            f"{key_value_line('Kvota', human_size_pair(used, quota))}",
            f"{key_value_line('Bo‘sh', format_size(user.free_space(quota)))}",
            f"{Emoji.PIN} {key_value_line('Muhim fayllar', format_number(int(stats.get('pinned_files') or 0)))}",
            f"{Emoji.FOLDER} {key_value_line('Papkalar', format_number(int(stats.get('folders') or 0)))}",
            f"{Emoji.LINK} {key_value_line('Ommaviy', format_number(int(stats.get('public_files') or 0)))}",
            f"{key_value_line('Maks. fayl', format_size(max_file_size))}",
            "",
            f"<b>Turlar bo'yicha</b>{rows or '  —'}",
        ]
    )


def password_prompt() -> str:
    """🔐 Parol darvozasi (eski botdagi ``AuthState``)."""
    return f"{Emoji.LOCK} <b>Parolni kiriting:</b>"


def password_wrong(attempts_left: int) -> str:
    return (
        f"{Emoji.CROSS} <b>Noto'g'ri parol!</b> Qayta kiriting."
        + (f"\n{Emoji.INFO} Qolgan urinishlar: {attempts_left}" if attempts_left >= 0 else "")
    )


def password_locked(retry_after: int) -> str:
    return (
        f"{Emoji.HOURGLASS} <b>Juda ko'p urinish.</b>\n"
        f"{retry_after} soniyadan so'ng qayta urinib ko'ring."
    )


def password_ok() -> str:
    return "\n".join(
        [
            f"{Emoji.CHECK} <b>Xush kelibsiz!</b>",
            "",
            "Menyudan tanlang:",
        ]
    )


def not_owner() -> str:
    return f"{Emoji.LOCK} <b>Ruxsat yo'q.</b>\nBu fayl boshqa foydalanuvchiniki."


def error_generic(message: str | None = None) -> str:
    detail = escape_html(message) if message else "Bizning tomonda xatolik yuz berdi."
    return f"{Emoji.ERROR} <b>{detail}</b>\n{Emoji.REFRESH} Birozdan so'ng qayta urinib ko'ring."


def operation_cancelled() -> str:
    return f"{Emoji.CLOSE} Amal bekor qilindi."


def admin_only() -> str:
    return f"{Emoji.LOCK} Amalni bajarib bo'lmadi."


def auth_required() -> str:
    """Eski botdagi “🔐 Avval /start orqali kiring!” xabari."""
    return f"{Emoji.LOCK} Avval <b>/start</b> orqali kiring!"


def pin_toggled(record: File) -> str:
    """📌 Pin holati o'zgarganda."""
    state = "muhim belgilandi" if record.is_pinned else "dan pin olindi"
    return (
        f"{Emoji.PIN} <b>{escape_html(truncate_middle(record.file_name, 42))}</b> {state}!"
    )


# ---------------------------------------------------------------------------
# Legacy category menu titles (🎬 Videolar / 🖼️ Rasmlar / 🤖 APK/IPA …)
# ---------------------------------------------------------------------------
CATEGORY_TITLES: dict[str, str] = {
    "video": f"{Emoji.VIDEO.plain()} Videolar",
    "photo": f"{Emoji.PHOTO.plain()} Rasmlar",
    "apps": f"{Emoji.ROBOT.plain()} APK/IPA",
    "audio": f"{Emoji.AUDIO.plain()} Musiqalar",
    "document": f"{Emoji.DOCUMENT.plain()} Hujjatlar",
    "archive": f"{Emoji.ARCHIVE.plain()} Arxivlar",
    "other": f"{Emoji.FILE.plain()} Boshqalar",
    "all": f"{Emoji.STAR.plain()} Barchasi",
    "pinned": f"{Emoji.PIN.plain()} Muhimlar",
}


def category_title(key: str) -> str:
    """Lokalize qilingan kategoriya sarlavhasi."""
    return CATEGORY_TITLES.get(key, CATEGORY_TITLES["all"])


def category_header(key: str, page: int, page_size: int, total: int) -> str:
    """Eski bot uslubidagi sarlavha: ``<b>🎬 Videolar</b> — 1-5 ko'rsatilmoqda:``"""
    if total == 0:
        return f"<b>{category_title(key)}</b> — hozircha fayl yo'q."
    start = page * page_size + 1
    end = min(total, (page + 1) * page_size)
    return f"<b>{category_title(key)}</b> — {start}-{end} ko'rsatilmoqda (jami {total}):"


def file_caption(record: File, *, folder_name: str = "umumiy") -> str:
    """Fayl ostidagi izoh — eski bot formatini takrorlaydi.

    ``📌 🎬 <b>name</b>\\n💾 12.5 MB  |  📅 2026-09-13 12:00\\n📁 umumiy``
    """
    pin_icon = f"{Emoji.PIN.plain()} " if record.is_pinned else ""
    icon = Emoji.for_category(record.category)
    mb = round(record.file_size / 1024 / 1024, 2)
    date = format_date(record.created_at, fmt="%Y-%m-%d %H:%M")
    return (
        f"{pin_icon}{icon} <b>{escape_html(record.file_name)}</b>\n"
        f"💾 {mb} MB  |  {Emoji.CLOCK.plain()} {date}\n"
        f"{Emoji.FOLDER.plain()} {escape_html(folder_name)}"
    )


def no_files_yet() -> str:
    return (
        f"{Emoji.EMPTY} Hozircha fayl yo'q.\n"
        f"{Emoji.UPLOAD} Fayl yuboring — saqlanadi!"
    )


#: name -> function mapping applied by :func:`install`.
translations: dict[str, Any] = {
    name: value
    for name, value in list(globals().items())
    if callable(value) and not name.startswith("_") and name not in {"install"}
}


def install() -> None:
    """Monkey-patch :mod:`bot.texts` with the Uzbek strings.

    Called once at startup from :mod:`main` when ``APP_LANGUAGE=uz``. Handlers
    keep importing ``bot.texts`` as usual, so no call site changes.
    """
    for name, function in translations.items():
        if hasattr(en, name):
            setattr(en, name, function)
    # Legacy-only helpers that have no English counterpart.
    en.CATEGORY_TITLES = CATEGORY_TITLES  # type: ignore[attr-defined]
    en.category_title = category_title  # type: ignore[attr-defined]
    en.category_header = category_header  # type: ignore[attr-defined]
    en.file_caption = file_caption  # type: ignore[attr-defined]
    en.no_files_yet = no_files_yet  # type: ignore[attr-defined]
    en.password_prompt = password_prompt  # type: ignore[attr-defined]
    en.password_wrong = password_wrong  # type: ignore[attr-defined]
    en.password_locked = password_locked  # type: ignore[attr-defined]
    en.password_ok = password_ok  # type: ignore[attr-defined]
    en.pin_toggled = pin_toggled  # type: ignore[attr-defined]
    en.auth_required = auth_required  # type: ignore[attr-defined]
    en.search_empty = search_empty  # type: ignore[attr-defined]
    en.pass_code_wrong = pass_code_wrong  # type: ignore[attr-defined]
