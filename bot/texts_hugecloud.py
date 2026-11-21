"""``hugecloud`` yangilanishi matnlari — autentifikatsiya, boshqaruv, saralash.

Nima uchun alohida modul?
-------------------------
:mod:`bot.texts` (inglizcha) va :mod:`bot.texts_uz` (o'zbekcha) juftligi eski
funksiyalarni monkey-patch qilish orqali ishlaydi. Yangi qo'shilgan matnlarni
ikkala faylga ham qo'shish o'rniga ular **bitta** modulda, ikki xil lug'at
ko'rinishida saqlanadi:

* :data:`EN` — inglizcha (sukut bo'yicha);
* :data:`UZ` — o'zbekcha (``APP_LANGUAGE=uz``);
* :func:`install` — ``bot.texts`` moduliga kerakli tilni o'rnatadi, shuning uchun
  handlerlar har doimgidek ``import bot.texts as texts`` deb ``texts.login_prompt()``
  chaqiraveradi.

Lug'atlar bir xil kalitlar to'plamiga ega ekanligi :func:`missing_keys` orqali
test qilinadi — yangi til qo'shilsa hech narsa "yo'qolib" qolmaydi.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from bot.utils.emojis import Emoji
from bot.utils.formatters import escape_html, format_date, format_size, truncate_middle

__all__ = ["EN", "UZ", "install", "install_language", "missing_keys", "translations"]


# ---------------------------------------------------------------------------
# Inglizcha (sukut bo'yicha)
# ---------------------------------------------------------------------------
def _unlimited() -> str:
    return "Unlimited"


def _quota_line(used: int, quota: int, label: str) -> str:
    """``💾 Used: 1.2 GB / Unlimited`` — quota is 0 when unlimited.

    Foiz ataylab ko'rsatilmaydi (eski "limit %" olib tashlandi).
    """
    if quota <= 0:
        return f"💾 Used: <b>{format_size(used)}</b> · Quota: <b>{label or _unlimited()}</b>"
    return f"💾 Used: <b>{format_size(used)}</b> / {format_size(quota)}"


#: Deep-link park notice, shared by the EN gate texts.
PARKED_EN = (
    "\u23f3 Your link is parked \u2014 it opens automatically as soon as you sign in."
)
UZ_PARKED_EN = PARKED_EN

#: O‘zbekcha park xabari (UZ darvoza matnlari uchun).
UZ_PARKED = (
    "\u23f3 Havolangiz saqlanib turibdi \u2014 tizimga kirishingiz bilan avtomatik ochiladi."
)


EN: dict[str, Callable[..., str]] = {
    # --- authentication -----------------------------------------------------
    "auth_gate_title": lambda: (
        f"{Emoji.LOCK.plain()} <b>Access restricted</b>\n\n"
        "This cloud is private. Sign in to use it:\n\n"
        "<code>/login &lt;login&gt; &lt;password&gt;</code>\n"
        "<code>/register &lt;login&gt; &lt;password&gt;</code>"
    ),
    "auth_required_action": lambda: (
        f"{Emoji.LOCK.plain()} Sign in first:\n<code>/login &lt;login&gt; &lt;password&gt;</code>"
    ),
    "blocked_message": lambda note=None: (
        f"{Emoji.ERROR.plain()} <b>Your account is blocked.</b>\n"
        + (f"Reason: {escape_html(note)}\n" if note else "")
        + "Contact the administrator."
    ),
    "register_prompt": lambda: (
        f"{Emoji.KEY.plain()} <b>Registration</b>\n\n"
        "Send: <code>/register login password</code>\n"
        "Login rules: 3-32 characters (letters, digits, _ and .)\n"
        "Password: at least 6 characters."
    ),
    "register_ok": lambda login: (
        f"{Emoji.CHECK.plain()} <b>Account created!</b>\n\n"
        f"Login: <code>{escape_html(login)}</code>\n"
        "You are signed in — send a file to store it."
    ),
    "register_taken": lambda login: (
        f"{Emoji.ERROR.plain()} Login <code>{escape_html(login)}</code> is already taken."
    ),
    "login_prompt": lambda: (
        f"{Emoji.KEY.plain()} <b>Sign in</b>\n\nSend: <code>/login login password</code>"
    ),
    "login_ok": lambda login, name: (
        f"{Emoji.CHECK.plain()} <b>Welcome, {escape_html(name)}!</b>\n"
        f"Login: <code>{escape_html(login)}</code>\n\nYou can now use the cloud."
    ),
    "login_unknown": lambda: (
        f"{Emoji.ERROR.plain()} No such login. Use <code>/register</code> to create an account."
    ),
    "login_wrong_password": lambda left: (
        f"{Emoji.ERROR.plain()} Wrong password. Attempts left: <b>{left}</b>"
    ),
    "login_locked": lambda seconds: (
        f"{Emoji.LOCK.plain()} Account temporarily locked. Try again in {_duration(seconds)}."
    ),
    "login_format": lambda: (
        f"{Emoji.ERROR.plain()} Format: <code>/login login password</code>"
    ),
    "register_format": lambda: (
        f"{Emoji.ERROR.plain()} Format: <code>/register login password</code>"
    ),
    "logout_ok": lambda: f"{Emoji.CHECK.plain()} Signed out. Use /login to come back.",
    "password_change_prompt": lambda: (
        "Send the new password: <code>/password old new</code>"
    ),
    "password_changed": lambda: f"{Emoji.CHECK.plain()} Password updated.",
    "password_wrong_current": lambda: f"{Emoji.ERROR.plain()} Current password is wrong.",
    "no_account_yet": lambda: (
        "You have no account yet. Create one: <code>/register login password</code>"
    ),
    "registration_closed": lambda: (
        f"{Emoji.LOCK.plain()} Registration is closed. Ask the administrator for an account."
    ),
    # --- files menu: groups + sorting ---------------------------------------
    "files_menu": lambda counts: (
        f"{Emoji.FILES.plain()} <b>Your files</b>\n\n"
        f"🎬 Media: <b>{counts.get('media', 0)}</b>\n"
        f"🎵 Audio: <b>{counts.get('audio', 0)}</b>\n"
        f"📄 Documents: <b>{counts.get('document', 0)}</b>\n"
        f"📦 Archives: <b>{counts.get('archive', 0)}</b>\n"
        f"🤖 APK/IPA: <b>{counts.get('apps', 0)}</b>\n"
        f"📌 Pinned: <b>{counts.get('pinned', 0)}</b>\n\n"
        "Pick a group or change the sorting:"
    ),
    "group_media": lambda: "🎬 Media",
    "group_audio": lambda: "🎵 Audio",
    "group_document": lambda: "📄 Documents",
    "group_archive": lambda: "📦 Archives",
"group_apps": "🤖 APK/IPA",
    "group_all": lambda: "📋 All files",
    "group_pinned": lambda: "📌 Pinned",
    "group_empty": lambda label: f"{Emoji.EMPTY.plain()} <b>{label}</b> — no files yet.",
    "sort_menu": lambda current: (
        f"{Emoji.SORT.plain()} <b>Sorting</b>\n\nCurrent: <b>{current}</b>\nChoose a new order:"
    ),
    "sort_newest": lambda: "📅 Newest first",
    "sort_oldest": lambda: "📅 Oldest first",
    "sort_largest": lambda: "📦 Largest first",
    "sort_smallest": lambda: "📦 Smallest first",
    "sort_name_asc": lambda: "🔤 Name A→Z",
    "sort_name_desc": lambda: "🔤 Name Z→A",
    "sort_applied": lambda label: f"{Emoji.CHECK.plain()} Sorting: <b>{label}</b>",
    # --- unlimited quota ----------------------------------------------------
    "quota_unlimited": lambda label=None: label or _unlimited(),
    "storage_line": lambda used, quota, label: _quota_line(used, quota, label),
    "unlimited_banner": lambda label=None: (
        f"{Emoji.CLOUD.plain()} Storage: <b>{label or _unlimited()}</b> — "
        "files live on Telegram's servers, no size cap."
    ),
    # --- dedupe / archive ---------------------------------------------------
    "dedupe_hit": lambda name: (
        f"{Emoji.CHECK.plain()} <b>{escape_html(truncate_middle(name, 42))}</b> is already "
        "in your cloud — opened instantly (no duplicate stored)."
    ),
    "archived_ok": lambda name: (
        f"{Emoji.SHIELD.plain()} Backup copy saved to the private archive: "
        f"<b>{escape_html(truncate_middle(name, 40))}</b>"
    ),
    "archived_fail": lambda: (
        f"{Emoji.WARNING.plain()} The archive copy failed; the file is still stored in your cloud."
    ),
    "archive_status_line": lambda line: f"{Emoji.SHIELD.plain()} Archive: {line}",
    # --- boshqaruv paneli ---------------------------------------------------
    "admin_only": lambda: (
        f"{Emoji.LOCK.plain()} This command is not available."
    ),
    "admin_password_needed": lambda: (
        f"{Emoji.KEY.plain()} Send the password to continue."
    ),
    "admin_password_wrong": lambda: f"{Emoji.ERROR.plain()} Wrong password.",
    "admin_panel": lambda version: (
        f"{Emoji.SHIELD.plain()} <b>Boshqaruv paneli</b> · v{escape_html(version)}\n\n"
        "📊 System statistics\n"
        "👥 Manage users\n"
        "📢 Broadcast\n"
        "⚙️ System settings\n"
        "📜 Audit log"
    ),
    "admin_stats": lambda stats: _admin_stats_en(stats),
    "admin_users_page": lambda rows, page, pages, total: (
        f"👥 <b>Users</b> — {total} total (page {page}/{pages})\n\n"
        + "\n".join(_admin_user_row_en(row) for row in rows)
    ),
    "admin_user_card": lambda user, files, size, quota_label: (
        f"👤 <b>{escape_html(user.display_name)}</b>\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Login: <code>{escape_html(user.login or '—')}</code>\n"
        f"Username: {('@' + user.username) if user.username else '—'}\n"
        f"Phone: 📱 {getattr(user, 'phone', None) or '—'}\n"
        f"Files: <b>{files}</b> · {format_size(size)}\n"
        f"Quota: <b>{quota_label}</b>\n"
        f"Signed in: {'✅' if user.is_authenticated else '❌'}\n"
        f"Blocked: {'🚫 yes' if user.is_blocked else 'no'}\n"
        f"Last login: {_stamp(user.last_login_at)}\n"
        f"Last seen: {_stamp(user.last_seen_at)}"
    ),
    "admin_blocked": lambda name: f"🚫 {escape_html(name)} blocked.",
    "admin_unblocked": lambda name: f"{Emoji.CHECK.plain()} {escape_html(name)} unblocked.",
    "admin_password_reset": lambda name, password: (
        f"{Emoji.KEY.plain()} New password for {escape_html(name)}:\n"
        f"<code>{escape_html(password)}</code>\n(show once — forward it now)"
    ),
    "admin_broadcast_prompt": lambda total: (
        f"📢 <b>Broadcast</b>\n\nSend the text/photo/video/document to deliver it to "
        f"<b>{total}</b> users.\n/cancel to abort."
    ),
    "admin_broadcast_progress": lambda sent, failed, total: (
        f"📢 Sending: {sent}/{total} delivered, {failed} failed…"
    ),
    "admin_broadcast_done": lambda sent, failed, total, seconds: (
        f"{Emoji.CHECK.plain()} <b>Broadcast finished</b>\n"
        f"Delivered: <b>{sent}</b> / {total}\n"
        f"Failed: <b>{failed}</b>\n"
        f"Time: {seconds:.1f}s"
    ),
    "admin_settings": lambda auth_required, allow_register, archive_line, quota_label: (
        f"⚙️ <b>System settings</b>\n\n"
        f"Authentication: <b>{'ON' if auth_required else 'OFF'}</b>\n"
        f"Registration: <b>{'open' if allow_register else 'closed'}</b>\n"
        f"Default quota: <b>{quota_label}</b>\n"
        f"{archive_line}"
    ),
    "admin_toggled": lambda label, value: (
        f"{Emoji.CHECK.plain()} {label}: <b>{'ON' if value else 'OFF'}</b>"
    ),
    "admin_audit_page": lambda rows, page, pages: (
        f"📜 <b>Audit log</b> (page {page}/{pages})\n\n" + "\n".join(_audit_row(row) for row in rows)
    ),
    "admin_user_not_found": lambda target: f"{Emoji.ERROR.plain()} User {target} not found.",
    "admin_no_users": lambda: "👥 No users yet.",
    # --- inline mode --------------------------------------------------------
    "inline_help": lambda bot_username: (
        f"Type <code>{bot_username} query</code> in any chat to search your cloud."
    ),
    # --- 🚪 session gate / Mini App pivot (bu bosqichda qo'shildi) ----------
"gate_button_login": "🔑 Sign in",
"gate_button_register": "✨ Create account",
"gate_button_help": "ℹ️ Help",
    "gate_new_session": lambda bot_username, web_url=None, parked=False: (
        "☁️ <b>Huge Cloud</b> — your personal cloud storage\n\n"
        "Your files live on <b>Telegram\u2019s servers</b>: we only keep their "
        "address (<code>manzil</code>). Unlimited space, duplicate files are "
        "never re-uploaded.\n\n"
        "<b>Step 1.</b> Create an account or sign in to continue:\n"
        "<code>/register login password</code>\n"
        "<code>/login login password</code>"
        + ("\n\n" + UZ_PARKED_EN if parked else "")
        + (
            "\n\nAfter signing in, everything moves into the <b>Mini App</b>: "
            f"<code>{web_url}</code>"
            if web_url
            else ""
        )
    ),
    "gate_returning_session": lambda login_hint=None, parked=False: (
        "🔒 <b>Session detected</b>\n\n"
        "For security, your account must be confirmed on every launch.\n\n"
        "<code>/login login password</code>"
        + (f"\n\n👤 Account: <b>{login_hint}</b>" if login_hint else "")
        + ("\n\n" + UZ_PARKED_EN if parked else "")
    ),
"gate_parked_notice": lambda: PARKED_EN,
"gate_callback_login": "🔑 Send <code>/login login password</code> to the bot.",
"gate_callback_register": "✨ Send <code>/register login password</code> to the bot.",
"home_open_mini_app": "Open the Mini App",
"home_all_files": "All files",
"home_pinned": "Important",
"home_sort": "Sort",
"home_share": "Share",
"home_stats": "Statistics",
    "home_welcome_authed": lambda name, web_url=None, file_count=0: (
        "✅ <b>Welcome back!</b>\n\n"
        f"Files in your cloud: <b>{file_count}</b>.\n\n"
        "Everything is managed from the <b>Mini App</b>: uploads, the player, "
        "sharing and the theme switcher."
        + (f"\n\n🌐 <code>{web_url}</code>" if web_url else "")
    ),
    "home_first_file_hint": lambda: (
        "📤 <b>Your first file</b>\n\n"
        "Tap <b>Upload</b> in the bottom-right corner of the Mini App, then send "
        "the file to this chat \u2014 it is stored on Telegram\u2019s servers and "
        "appears in your cloud instantly."
    ),
    "miniapp_upload_ack": lambda name, size_label: (
        "⬆️ <b>Upload started</b>\n\n"
        f"📄 {escape_html(str(name))}\n"
        f"📦 {size_label}\n\n"
        "File bytes travel <b>through Telegram</b>: send the file to this chat "
        "as a document and it lands in your cloud (duplicates are detected "
        "automatically)."
    ),
    "miniapp_upload_limit": lambda size_label: (
        f"⚠️ The file is too large: {size_label}. "
        "The maximum file size is 2 GiB."
    ),
    "miniapp_import_prompt": lambda: (
        "📥 <b>Import</b>\n\n"
        "Send a Huge Cloud share link or token \u2014 the file is copied into your "
        "cloud (no re-upload, duplicates are linked).\n\n"
        "Accepted formats:\n"
        "<code>t.me/&lt;bot&gt;?start=&lt;token&gt;</code>\n"
        "<code>https://.../shares/&lt;token&gt;</code>\n"
        "<code>&lt;token&gt;</code>"
    ),
    "miniapp_import_done": lambda name, size_label, deduplicated=False: (
        "✅ <b>Import finished</b>\n\n"
        f"📄 {escape_html(str(name))}\n"
        f"📦 {size_label}"
        + (
            "\n♻️ Duplicate file \u2014 nothing was re-uploaded, it is linked to "
            "the existing record."
            if deduplicated
            else ""
        )
    ),
    "miniapp_import_invalid": lambda: (
        "❌ The link or token was not found / has expired.\n\n"
        "Send a valid link, or <code>/cancel</code> to close the dialog."
    ),
    "miniapp_play_ack": lambda name: (
        "▶️ <b>Opening in the Telegram player</b>\n\n"
        f"📄 {escape_html(str(name))}\n\n"
        "Playback keeps going on the lock screen and in the background — "
        "you can close this chat and keep listening."
    ),
    "miniapp_play_invalid": lambda: (
        "❌ Could not open the file: it was not found in your cloud."
    ),
    "miniapp_unknown_action": lambda: (
        "ℹ️ The Mini App request was processed, but the action is unknown. "
        "Update the app and try again."
    ),
"session_resumed_deeplink": "🔗 Opening your saved link…",
    "gate_help_short": lambda bot_username=None: (
        "ℹ️ <b>Huge Cloud</b>\n\n"
        "1. <code>/register login password</code> \u2014 create an account\n"
        "2. <code>/login login password</code> \u2014 sign in\n"
        "3. After that everything lives in the Mini App: uploads, import, "
        "player, sharing, themes.\n\n"
        "Files stay on Telegram\u2019s servers; storage is unlimited."
    ),
}


UZ: dict[str, Callable[..., str]] = {
    # --- autentifikatsiya ---------------------------------------------------
    "auth_gate_title": lambda: (
        f"{Emoji.LOCK.plain()} <b>Ruxsat cheklangan</b>\n\n"
        "Bu bulut maxfiy. Ishlatish uchun tizimga kiring:\n\n"
        "<code>/login &lt;login&gt; &lt;parol&gt;</code>\n"
        "<code>/register &lt;login&gt; &lt;parol&gt;</code>"
    ),
    "auth_required_action": lambda: (
        f"{Emoji.LOCK.plain()} Avval tizimga kiring:\n"
        "<code>/login &lt;login&gt; &lt;parol&gt;</code>"
    ),
    "blocked_message": lambda note=None: (
        f"{Emoji.ERROR.plain()} <b>Hisobingiz bloklangan.</b>\n"
        + (f"Sabab: {escape_html(note)}\n" if note else "")
        + "Administrator bilan bog‘laning."
    ),
    "register_prompt": lambda: (
        f"{Emoji.KEY.plain()} <b>Ro‘yxatdan o‘tish</b>\n\n"
        "Yuboring: <code>/register login parol</code>\n"
        "Login qoidasi: 3-32 belgi (harf, raqam, _ va .)\n"
        "Parol: kamida 6 belgi."
    ),
    "register_ok": lambda login: (
        f"{Emoji.CHECK.plain()} <b>Hisob yaratildi!</b>\n\n"
        f"Login: <code>{escape_html(login)}</code>\n"
        "Siz tizimdasiz — fayl yuborsangiz saqlanadi."
    ),
    "register_taken": lambda login: (
        f"{Emoji.ERROR.plain()} <code>{escape_html(login)}</code> logini allaqachon band."
    ),
    "login_prompt": lambda: (
        f"{Emoji.KEY.plain()} <b>Tizimga kirish</b>\n\n"
        "Yuboring: <code>/login login parol</code>"
    ),
    "login_ok": lambda login, name: (
        f"{Emoji.CHECK.plain()} <b>Xush kelibsiz, {escape_html(name)}!</b>\n"
        f"Login: <code>{escape_html(login)}</code>\n\nBulutdan foydalanishingiz mumkin."
    ),
    "login_unknown": lambda: (
        f"{Emoji.ERROR.plain()} Bunday login topilmadi. "
        "<code>/register</code> bilan hisob oching."
    ),
    "login_wrong_password": lambda left: (
        f"{Emoji.ERROR.plain()} Parol noto‘g‘ri. Qolgan urinishlar: <b>{left}</b>"
    ),
    "login_locked": lambda seconds: (
        f"{Emoji.LOCK.plain()} Hisob vaqtincha qulflangan. "
        f"{_uz_duration(seconds)} dan keyin urinib ko‘ring."
    ),
    "login_format": lambda: (
        f"{Emoji.ERROR.plain()} Format: <code>/login login parol</code>"
    ),
    "register_format": lambda: (
        f"{Emoji.ERROR.plain()} Format: <code>/register login parol</code>"
    ),
    "logout_ok": lambda: (
        f"{Emoji.CHECK.plain()} Tizimdan chiqdingiz. Qaytish uchun /login."
    ),
    "password_change_prompt": lambda: (
        "Yangi parolni yuboring: <code>/password eski yangi</code>"
    ),
    "password_changed": lambda: f"{Emoji.CHECK.plain()} Parol yangilandi.",
    "password_wrong_current": lambda: f"{Emoji.ERROR.plain()} Joriy parol noto‘g‘ri.",
    "no_account_yet": lambda: (
        "Sizda hali hisob yo‘q. Ochish: <code>/register login parol</code>"
    ),
    "registration_closed": lambda: (
        f"{Emoji.LOCK.plain()} Ro‘yxatdan o‘tish yopilgan. "
        "Hisob uchun administratorga murojaat qiling."
    ),
    # --- fayllar: guruhlar + saralash ---------------------------------------
    "files_menu": lambda counts: (
        f"{Emoji.FILES.plain()} <b>Fayllaringiz</b>\n\n"
        f"🎬 Medialar: <b>{counts.get('media', 0)}</b>\n"
        f"🎵 Audiolar: <b>{counts.get('audio', 0)}</b>\n"
        f"📄 Hujjatlar: <b>{counts.get('document', 0)}</b>\n"
        f"📦 Arxivlar: <b>{counts.get('archive', 0)}</b>\n"
        f"🤖 APK/IPA: <b>{counts.get('apps', 0)}</b>\n"
        f"📌 Muhimlar: <b>{counts.get('pinned', 0)}</b>\n\n"
        "Guruhni tanlang yoki saralashni o‘zgartiring:"
    ),
    "group_media": lambda: "🎬 Medialar",
    "group_audio": lambda: "🎵 Audiolar",
    "group_document": lambda: "📄 Hujjatlar",
    "group_archive": lambda: "📦 Arxivlar",
    "group_apps": lambda: "🤖 APK/IPA",
    "group_all": lambda: "📋 Barcha fayllar",
    "group_pinned": lambda: "📌 Muhimlar",
    "group_empty": lambda label: f"{Emoji.EMPTY.plain()} <b>{label}</b> — hozircha fayl yo‘q.",
    "sort_menu": lambda current: (
        f"{Emoji.SORT.plain()} <b>Saralash</b>\n\nJoriy: <b>{current}</b>\nYangi tartibni tanlang:"
    ),
    "sort_newest": lambda: "📅 Vaqti bo‘yicha (Eng yangilar)",
    "sort_oldest": lambda: "📅 Vaqti bo‘yicha (Eng eskilar)",
    "sort_largest": lambda: "📦 Hajmi bo‘yicha (Eng kattalar)",
    "sort_smallest": lambda: "📦 Hajmi bo‘yicha (Eng kichiklar)",
    "sort_name_asc": lambda: "🔤 Nomi A→Z",
    "sort_name_desc": lambda: "🔤 Nomi Z→A",
    "sort_applied": lambda label: f"{Emoji.CHECK.plain()} Saralash: <b>{label}</b>",
    # --- cheksiz kvota ------------------------------------------------------
    "quota_unlimited": lambda label=None: label or _uz_unlimited(),
    "storage_line": lambda used, quota, label: _uz_quota_line(used, quota, label),
    "unlimited_banner": lambda label=None: (
        f"{Emoji.CLOUD.plain()} Xotira: <b>{label or _uz_unlimited()}</b> — "
        "fayllar Huge Cloud xavfsiz infratuzilmasida saqlanadi, hajm chegarasi yo‘q."
    ),
    # --- dedupe / arxiv -----------------------------------------------------
    "dedupe_hit": lambda name: (
        f"{Emoji.CHECK.plain()} <b>{escape_html(truncate_middle(name, 42))}</b> "
        "bullutingizda allaqachon bor — darhol ochildi (nusxa saqlanmadi)."
    ),
    "archived_ok": lambda name: (
        f"{Emoji.SHIELD.plain()} Zaxira nusxa maxfiy arxivga saqlandi: "
        f"<b>{escape_html(truncate_middle(name, 40))}</b>"
    ),
    "archived_fail": lambda: (
        f"{Emoji.WARNING.plain()} Arxiv nusxa olinmadi; fayl bullutingizda saqlangan."
    ),
    "archive_status_line": lambda line: f"{Emoji.SHIELD.plain()} Arxiv: {line}",
    # --- boshqaruv paneli ---------------------------------------------------
    "admin_only": lambda: (
        f"{Emoji.LOCK.plain()} Bu buyruq mavjud emas."
    ),
    "admin_password_needed": lambda: (
        f"{Emoji.KEY.plain()} Kirish uchun parolni yuboring."
    ),
    "admin_password_wrong": lambda: f"{Emoji.ERROR.plain()} Parol noto‘g‘ri.",
    "admin_panel": lambda version: (
        f"{Emoji.SHIELD.plain()} <b>Boshqaruv paneli</b> · v{escape_html(version)}\n\n"
        "📊 Tizim statistikasi\n"
        "👥 Foydalanuvchilarni boshqarish\n"
        "📢 E’lon yuborish\n"
        "⚙️ Tizim sozlamalari\n"
        "📜 Audit jurnal"
    ),
    "admin_stats": lambda stats: _uz_admin_stats(stats),
    "admin_users_page": lambda rows, page, pages, total: (
        f"👥 <b>Foydalanuvchilar</b> — jami {total} (sahifa {page}/{pages})\n\n"
        + "\n".join(_uz_user_row(row) for row in rows)
    ),
    "admin_user_card": lambda user, files, size, quota_label: (
        f"👤 <b>{escape_html(user.display_name)}</b>\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Login: <code>{escape_html(user.login or '—')}</code>\n"
        f"Username: {('@' + user.username) if user.username else '—'}\n"
        f"Telefon: 📱 {getattr(user, 'phone', None) or '—'}\n"
        f"Fayllar: <b>{files}</b> · {format_size(size)}\n"
        f"Kvota: <b>{quota_label}</b>\n"
        f"Tizimda: {'✅ ha' if user.is_authenticated else '❌ yo‘q'}\n"
        f"Bloklangan: {'🚫 ha' if user.is_blocked else 'yo‘q'}\n"
        f"Oxirgi kirish: {_uz_stamp(user.last_login_at)}\n"
        f"Oxirgi faollik: {_uz_stamp(user.last_seen_at)}"
    ),
    "admin_blocked": lambda name: f"🚫 {escape_html(name)} bloklandi.",
    "admin_unblocked": lambda name: f"{Emoji.CHECK.plain()} {escape_html(name)} blokdan chiqarildi.",
    "admin_password_reset": lambda name, password: (
        f"{Emoji.KEY.plain()} {escape_html(name)} uchun yangi parol:\n"
        f"<code>{escape_html(password)}</code>\n(bir marta ko‘rsatiladi — hozir yuboring)"
    ),
    "admin_broadcast_prompt": lambda total: (
        f"📢 <b>E’lon yuborish</b>\n\nMatn/rasm/video/hujjat yuboring — "
        f"<b>{total}</b> foydalanuvchiga yetkaziladi.\n/cancel — bekor qilish."
    ),
    "admin_broadcast_progress": lambda sent, failed, total: (
        f"📢 Yuborilmoqda: {sent}/{total} yetdi, {failed} xato…"
    ),
    "admin_broadcast_done": lambda sent, failed, total, seconds: (
        f"{Emoji.CHECK.plain()} <b>E’lon tugadi</b>\n"
        f"Yetkazildi: <b>{sent}</b> / {total}\n"
        f"Xato: <b>{failed}</b>\n"
        f"Vaqt: {seconds:.1f} soniya"
    ),
    "admin_settings": lambda auth_required, allow_register, archive_line, quota_label: (
        f"⚙️ <b>Tizim sozlamalari</b>\n\n"
        f"Autentifikatsiya: <b>{'YONIQ' if auth_required else 'O‘CHIQ'}</b>\n"
        f"Ro‘yxatdan o‘tish: <b>{'ochiq' if allow_register else 'yopiq'}</b>\n"
        f"Sukut kvota: <b>{quota_label}</b>\n"
        f"{archive_line}"
    ),
    "admin_toggled": lambda label, value: (
        f"{Emoji.CHECK.plain()} {label}: <b>{'YONIQ' if value else 'O‘CHIQ'}</b>"
    ),
    "admin_audit_page": lambda rows, page, pages: (
        f"📜 <b>Audit jurnal</b> (sahifa {page}/{pages})\n\n"
        + "\n".join(_audit_row(row) for row in rows)
    ),
    "admin_user_not_found": lambda target: f"{Emoji.ERROR.plain()} {target} foydalanuvchi topilmadi.",
    "admin_no_users": lambda: "👥 Hozircha foydalanuvchi yo‘q.",
    # --- inline rejim -------------------------------------------------------
    "inline_help": lambda bot_username: (
        f"Har qanday chatda <code>{bot_username} so‘rov</code> deb yozing — "
        "bulutdagi fayllaringiz chiqadi."
    ),
    # --- 🚪 session gate / Mini App pivot (bu bosqichda qo'shildi) ----------
    "gate_button_login": lambda: "🔑 Kirish",
    "gate_button_register": lambda: "✨ Ro‘yxatdan o‘tish",
    "gate_button_help": lambda: "ℹ️ Yordam",
    "gate_new_session": lambda bot_username, web_url=None, parked=False: (
        "☁️ <b>Huge Cloud</b> — shaxsiy bulut xotirangiz\n\n"
        "Fayllaringiz <b>Huge Cloud xavfsiz infratuzilmasida</b> saqlanadi: bizda faqat "
        "ularning manzili (<code>manzil</code>) qoladi. Cheksiz hajm, "
        "takroriy fayl yuklanmaydi.\n\n"
        "<b>1-qadam.</b> Davom etish uchun hisob yarating yoki tizimga kiring:\n"
        "<code>/register login parol</code>\n"
        "<code>/login login parol</code>"
        + (
            "\n\nKirishdan so‘ng boshqaruv <b>Mini App</b>ga o‘tadi: "
            f"<code>{web_url}</code>"
            if web_url
            else ""
        )
        + ("\n\n" + UZ_PARKED if parked else "")
    ),
    "gate_returning_session": lambda login_hint=None, parked=False: (
        "🔒 <b>Sessiya aniqlandi</b>\n\n"
        "Xavfsizlik uchun har ochilishda hisobni tasdiqlash kerak.\n\n"
        "<code>/login login parol</code>"
        + (f"\n\n👤 Hisob: <b>{login_hint}</b>" if login_hint else "")
        + (
            "\n\n⏳ Havolangiz saqlanib turibdi — kirishingiz bilan "
            "avtomatik ochiladi."
            if parked
            else ""
        )
    ),
    "gate_parked_notice": lambda: (
        "⏳ Havolangiz saqlanib turibdi — tizimga kirishingiz bilan "
        "avtomatik ochiladi."
    ),
    "gate_callback_login": lambda: (
        "🔑 Bot oynasiga <code>/login login parol</code> yuboring."
    ),
    "gate_callback_register": lambda: (
        "✨ Bot oynasiga <code>/register login parol</code> yuboring."
    ),
    "home_open_mini_app": lambda: "Mini Appni ochish",
    "home_all_files": lambda: "Barcha fayllar",
    "home_pinned": lambda: "Muhimlar",
    "home_sort": lambda: "Saralash",
    "home_share": lambda: "Ulashish",
    "home_stats": lambda: "Statistika",
    "home_welcome_authed": lambda name=None, web_url=None, file_count=0: (
        f"✅ <b>Xush kelibsiz{f', {name}' if name else ''}!</b>\n\n"
        f"Bulutdagi fayllar: <b>{file_count}</b> ta.\n\n"
        "Boshqaruv <b>Mini App</b>da: yuklash, import, pleer, ulashish va "
        "mavzu almashtirish — hammasi bitta oynada."
        + (f"\n\n🌐 <code>{web_url}</code>" if web_url else "")
    ),
    "home_first_file_hint": lambda: (
        "📤 <b>Birinchi fayl</b>\n\n"
        "Mini App'dagi pastki o‘ng burchakdagi <b>Yuklash</b> tugmasini bosing "
        "— faylni tanlang va uni shu chatga yuboring."
    ),
    "miniapp_upload_ack": lambda name, size_label: (
        f"⬆️ <b>Yuklash boshlandi</b>\n\n"
        f"📄 {name}\n📦 {size_label}\n\n"
        "Fayl baytlari <b>Telegram orqali</b> yuboriladi: uni shu chatga "
        "hujjat sifatida jo‘nating (yoki mavjud xabardan forward qiling). "
        "Bot uni darhol bulutga yozadi — takroriy fayl qayta yuklanmaydi."
    ),
    "miniapp_upload_limit": lambda size_label: (
        f"⚠️ Fayl juda katta: {size_label}. "
        "Bitta fayl uchun maksimal hajm — 2 GiB."
    ),
    "miniapp_import_prompt": lambda: (
        "📥 <b>Import</b>\n\n"
        "Huge Cloud ulashish havolasi yoki token'ini yuboring — fayl "
        "bulutingizga saqlanadi (takroriy bo‘lsa, bir zumda ulanadi).\n\n"
        "Bekor qilish: <code>/cancel</code>"
    ),
    "miniapp_import_done": lambda name, size_label, deduplicated=False: (
        f"✅ <b>Import bajarildi</b>\n\n"
        f"📄 {name}\n📦 {size_label}\n"
        + (
            "♻️ Takroriy fayl — qayta yuklanmadi, mavjud yozuvga ulandi."
            if deduplicated
            else "💾 Bulutga saqlandi."
        )
    ),
    "miniapp_import_invalid": lambda: (
        "❌ Havola yoki token topilmadi / muddati tugagan.\n\n"
        "To‘g‘ri havolani yuboring yoki <code>/cancel</code> bilan bekor qiling."
    ),
    "miniapp_play_ack": lambda name: (
        f"▶️ <b>Telegram pleerida ochilmoqda</b>\n\n"
        f"📄 {name}\n\n"
        "Ijro ekran qulfida va fon rejimida davom etadi — shu chatni "
        "yopsangiz ham tinglashda davom etasiz."
    ),
    "miniapp_play_invalid": lambda: (
        "❌ Faylni ochib bo‘lmadi: u bulutingizdan topilmadi."
    ),
    "miniapp_unknown_action": lambda: (
        "ℹ️ Mini App so‘rovi qayta ishlandi, lekin amal noma‘lum. "
        "Ilovani yangilab qayta urinib ko‘ring."
    ),
    "session_resumed_deeplink": lambda: (
        "🔗 Saqlangan havolangiz ochilmoqda…"
    ),
    "gate_help_short": lambda bot_username=None: (
        "\u2139\ufe0f <b>Huge Cloud</b>\n\n"
        "1. <code>/register login parol</code> \u2014 hisob ochish\n"
        "2. <code>/login login parol</code> \u2014 kirish\n"
        "3. Kirishdan so\u2018ng boshqaruv Mini App\u2018da: yuklash, import, "
        "pleer, ulashish, mavzu.\n\n"
        "Fayllar Huge Cloud xavfsiz infratuzilmasida saqlanadi; hajm cheksiz."
    ),
}


def _uz_stamp(value: datetime | None) -> str:
    return format_date(value, fmt="%Y-%m-%d %H:%M") if value else "—"


def _uz_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} soniya"
    if seconds < 3600:
        return f"{seconds // 60} daqiqa"
    return f"{seconds // 3600} soat"


# --------------------------------------------------------------------------- #
# Render yordamchilari (EN)
# --------------------------------------------------------------------------- #
# Bu funksiyalar **lug'atdan keyin** aniqlanadi: ``EN``/``UZ`` yozuvlari lambda
# bo'lgani uchun nomlar chaqiruv paytida (runtime) izlanadi. Aks holda
# ``admin_stats`` / ``login_locked`` kabi matnlar ``NameError`` beradi — bu
# ``tests/test_texts_integrity.py`` tomonidan tekshiriladi.
def _uz_unlimited() -> str:
    """UZ: cheksiz kvota yorlig'i (``_unlimited`` ning o'zbekcha jufti)."""
    return "Cheksiz"


def _uz_quota_line(used: int, quota: int, label: str) -> str:
    """``💾 Ishlatilgan: 1.2 GB · Kvota: Cheksiz`` — kvota 0 bo'lsa cheksiz.

    Foiz ataylab ko'rsatilmaydi (eski "limit %" olib tashlandi).
    """
    used = max(0, int(used or 0))
    quota = int(quota or 0)
    if quota <= 0:
        return (
            f"\U0001f4be Ishlatilgan: <b>{format_size(used)}</b> \u00b7 "
            f"Kvota: <b>{label or _uz_unlimited()}</b>"
        )
    return (
        f"\U0001f4be Ishlatilgan: <b>{format_size(used)}</b> / {format_size(quota)}"
    )


def _stamp(value: datetime | None) -> str:
    """EN sana belgisi (``_uz_stamp`` ning inglizcha jufti)."""
    return format_date(value, fmt="%Y-%m-%d %H:%M") if value else "\u2014"


def _duration(seconds: int) -> str:
    """EN kutilish vaqti: ``45 seconds`` / ``3 minutes`` / ``2 hours``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} seconds" if seconds != 1 else "1 second"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minutes" if minutes != 1 else "1 minute"
    hours = seconds // 3600
    return f"{hours} hours" if hours != 1 else "1 hour"


def _audit_row(entry: Any) -> str:
    """Bitta audit yozuvi (EN va UZ bir xil tuzilishni ishlatadi)."""
    icon = Emoji.CHECK.plain() if entry.success else Emoji.ERROR.plain()
    when = format_date(entry.created_at, fmt="%m-%d %H:%M")
    detail = (entry.detail or "")[:60]
    actor = entry.actor_id or "\u2014"
    return f"{icon} <code>{when}</code> \u00b7 {entry.action} \u00b7 ID {actor} \u00b7 {escape_html(detail)}"


def _user_row_common(row: Any) -> str:
    """Foydalanuvchi qatori: belgilar + login (tilga bog'liq emas)."""
    marks = ("✅" if row.is_authenticated else "⚪") + (" 🚫" if row.is_blocked else "")
    login = escape_html(row.login) if row.login else "—"
    username = f"@{row.username}" if getattr(row, "username", None) else "—"
    phone = getattr(row, "phone", None) or "—"
    return f"• <code>{row.telegram_id}</code> · {escape_html(row.display_name)} · {login} · {username} · 📱 {phone} {marks}"


def _admin_user_row_en(row: Any) -> str:
    """``admin_users_page`` qatori (EN)."""
    return _user_row_common(row)


def _uz_user_row(row: Any) -> str:
    """``admin_users_page`` qatori (UZ)."""
    return _user_row_common(row)


def _admin_stats_block(stats: Mapping[str, Any]) -> str:
    """📊 Statistika bloklari (EN/UZ bir xil sonlar, faqat sarlavha tili farqli)."""
    users = stats.get("users") or {}
    files = stats.get("files") or {}
    window = int(stats.get("active_window_hours") or 24)
    lines = [
        f"\U0001f465 {int(users.get('total') or 0)} \u00b7 \u2705 {int(users.get('authenticated') or 0)} \u00b7 \U0001f511 {int(users.get('with_account') or 0)} \u00b7 \U0001f6ab {int(users.get('blocked') or 0)}",
        f"\U0001f4c1 {int(files.get('total') or 0)} \u00b7 \U0001f4ce {format_size(int(files.get('size') or 0))} \u00b7 \U0001f4cc {int(files.get('pinned') or 0)}",
        f"\U0001f5c2 {int(stats.get('folders') or 0)} \u00b7 \U0001f517 {int(stats.get('shared_links') or 0)} \u00b7 \U0001f7e2 {int(stats.get('active_sessions') or 0)}/{window}h",
    ]
    categories = stats.get("by_category") or {}
    if isinstance(categories, Mapping) and categories:
        parts = [
            f"{key}: {int((value or {}).get('count') or 0)}"
            for key, value in list(categories.items())[:6]
        ]
        lines.append("\U0001f4ca " + " \u00b7 ".join(parts))
    top = stats.get("top_users") or []
    if top:
        top_lines = [
            f"{index}. {escape_html(str(item.get('name') or item.get('telegram_id')))} \u2014 {format_size(int(item.get('size') or 0))}"
            for index, item in enumerate(list(top)[:5], start=1)
        ]
        lines.append("\U0001f3c6 " + "\n".join(top_lines))
    return "\n".join(lines)


def _admin_stats_en(stats: Mapping[str, Any]) -> str:
    """📊 ``admin_stats`` (EN sarlavha)."""
    return f"\U0001f4ca <b>System statistics</b>\n\n{_admin_stats_block(stats)}"


def _uz_admin_stats(stats: Mapping[str, Any]) -> str:
    """📊 ``admin_stats`` (UZ sarlavha)."""
    return f"\U0001f4ca <b>Tizim statistikasi</b>\n\n{_admin_stats_block(stats)}"


#: Active language table (replaced by :func:`install`).
translations: dict[str, Callable[..., str]] = EN


def missing_keys(language: dict[str, Any] | None = None) -> list[str]:
    """Keys present in :data:`EN` but absent from ``language`` (default :data:`UZ`)."""
    target = language if language is not None else UZ
    return sorted(set(EN) - set(target))


def install_language(language: str = "uz") -> dict[str, int]:
    """Bitta chaqiruvda **barcha** matnlarni o'rnatish (startup uchun).

    Tartib muhim: avval :func:`bot.texts_uz.install` (eski bot matnlari), keyin
    :func:`install` (hugecloud matnlari) — ikkinchisi birinchi ustidan yozmasin.

    ``APP_LANGUAGE=en`` bo'lsa o'zbekcha qatlam o'rnatilmaydi.
    """
    report: dict[str, int] = {"legacy": 0, "hugecloud": 0}
    if str(language).lower().startswith("uz"):
        import bot.texts_uz as uz

        uz.install()
        report["legacy"] = len(uz.translations)
    report["hugecloud"] = len(install(language))
    return report


def install(language: str = "uz") -> list[str]:
    """Publish the chosen language into :mod:`bot.texts`.

    Returns the list of names installed, so startup logs can show it and tests
    can assert nothing was skipped.
    """
    import bot.texts as en

    table = UZ if str(language).lower().startswith("uz") else EN
    globals()["translations"] = table

    installed: list[str] = []
    for name, function in table.items():
        setattr(en, name, function)
        installed.append(name)

    # ``__all__`` ni yangilaymiz: ``from bot.texts import *`` ham to'liq ishlaydi.
    exported = set(getattr(en, "__all__", []))
    exported.update(installed)
    en.__all__ = sorted(exported)  # type: ignore[attr-defined]
    return installed
