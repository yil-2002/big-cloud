"""🗃️ ``/files`` — guruhlar, saralash va papkalar (hugecloud fayl menejeri).

Bu router spesifikatsiyadagi **Advanced File Management** bo'limini amalga
oshiradi:

1. **Guruhlar** (avto-kategorizatsiya): 🎬 Medialar, 🎵 Audiolar, 📄 Hujjatlar,
   📦 Arxivlar (+ 🤖 APK/IPA va 📌 Muhimlar);
2. **Saralash**: 📅 eng yangilar / eng eskilar, 📦 eng kattalar / eng kichiklar
   (+ 🔤 nomi bo'yicha);
3. **Papkalar**: yaratish va fayllarni ko'chirish (``bot/handlers/storage.py``
   dagi FSM oqimlari bilan birga ishlaydi);
4. **Media rejimi**: ``BROWSE_SEND_MEDIA=true`` bo'lsa fayllar **haqiqiy media**
   sifatida yuboriladi (Telegram serverida nusxalanadi — trafik va disk sarfi
   yo'q), aks holda ixcham tugmalar ro'yxati chiqadi.

Saralash tanlovi ``users.settings.sort`` da saqlanadi, shuning uchun
qayta ishga tushgandan keyin ham eslab qolinadi.

Callback grammatikasi (qisqa, 64 bayt chegarasiga mos):

=========================  =====================================================
``files``                  guruhlar menyusi
``grp:<key>:<page>``       guruh ro'yxati (``page`` 0 dan boshlanadi)
``sort::<ctx>``            saralash menyusi (``<ctx>`` — qaytish manzili)
``srt:<key>::<ctx>``       saralashni qo'llash va ``<ctx>`` ni qayta ko'rsatish
=========================  =====================================================
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from bot import texts
from bot.handlers.browsing import (
    _delete_quietly,
    _folder_name,
    _send_media_safe,
)
from bot.keyboards.hugecloud import (
    DEFAULT_SORT,
    SORTS,
    back_to_files,
    files_menu_kb,
    group_list_kb,
    sort_kb,
    sort_label,
)
from bot.keyboards.inline import FileActionFactory
from bot.keyboards.legacy import file_actions_kb
from bot.utils.emojis import Emoji
from bot.utils.formatters import escape_html, format_size, truncate_middle
from bot.utils.telegram_storage import TelegramStorage
from config import Settings
from db.models import File, User
from services.storage import StorageService

logger = logging.getLogger(__name__)

router = Router(name="files")

#: Guruh kaliti -> ``(list_files kwargs)``.
GROUP_FILTERS: dict[str, dict[str, Any]] = {
    "media": {"group": "media"},
    "audio": {"group": "audio"},
    "document": {"group": "document"},
    "archive": {"group": "archive"},
    "apps": {"group": "apps"},
    "pinned": {"pinned_only": True},
    "all": {},
}


def _user_sort(user: User) -> str:
    """Foydalanuvchining joriy saralash kaliti (sozlamalardan)."""
    value = str(user.setting("sort", DEFAULT_SORT) or DEFAULT_SORT)
    return value if value in SORTS else DEFAULT_SORT


def _sort_args(sort_key: str) -> dict[str, Any]:
    """Saralash kalitini ``list_files`` argumentlariga aylantirish."""
    sort_by, sort_desc, _ = SORTS.get(sort_key, SORTS[DEFAULT_SORT])
    return {"sort_by": sort_by, "sort_desc": sort_desc}


def _parse_group_context(context: str) -> tuple[str, int]:
    """``grp:media:2`` -> ``("media", 2)``; ``files`` -> ``("all", 0)``."""
    if context.startswith("grp:"):
        parts = context.split(":")
        key = parts[1] if len(parts) > 1 else "all"
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page = 0
        return (key if key in GROUP_FILTERS else "all"), max(0, page)
    return "all", 0


# ---------------------------------------------------------------------------
# /files — guruhlar menyusi
# ---------------------------------------------------------------------------
@router.message(Command("files"))
async def cmd_files(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
) -> None:
    """``/files`` — guruhlar + saralash menyusi (hisoblar bilan)."""
    await send_files_menu(message, user=user, storage=storage, settings=settings)


async def send_files_menu(
    message: Message | CallbackQuery,
    *,
    user: User,
    storage: StorageService,
    settings: Settings,
) -> None:
    """Guruhlar menyusini chizish (buyruq va callback uchun umumiy)."""
    counts = await storage.group_counts(user.telegram_id)
    payload = texts.files_menu(counts)
    markup = files_menu_kb(
        sort_key=_user_sort(user), web_app_url=settings.web.web_app_url
    )
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(payload, reply_markup=markup)  # type: ignore[union-attr]
        await message.answer()
    else:
        await message.answer(payload, reply_markup=markup)


@router.callback_query(F.data == "files")
async def cb_files(
    callback: CallbackQuery,
    user: User,
    storage: StorageService,
    settings: Settings,
) -> None:
    """Inline tugma: guruhlar menyusiga qaytish."""
    await send_files_menu(callback, user=user, storage=storage, settings=settings)


# ---------------------------------------------------------------------------
# Guruh ro'yxati
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("grp:"))
async def cb_group(
    callback: CallbackQuery,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
) -> None:
    """``grp:<key>:<page>`` — guruh fayllarini ko'rsatish."""
    group, page = _parse_group_context(callback.data)
    await render_group(
        callback.message,  # type: ignore[arg-type]
        group=group,
        page=page,
        user=user,
        storage=storage,
        settings=settings,
        tg_storage=tg_storage,
        callback=callback,
    )


async def render_group(
    message: Message,
    *,
    group: str,
    page: int,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
    callback: CallbackQuery | None = None,
) -> None:
    """Bitta guruh sahifasini chizish.

    ``BROWSE_SEND_MEDIA=true`` bo'lsa eski bot kabi **haqiqiy media** yuboriladi
    (callback xabari o'chiriladi, sarlavha + har fayl alohida xabar, oxirgi
    faylga sahifalash tugmalari ilova qilinadi). Aks holda ixcham ro'yxat.
    """
    sort_key = _user_sort(user)
    page_size = settings.storage.page_size
    filters = GROUP_FILTERS.get(group, {})

    items, total = await storage.list_files(
        user.telegram_id,
        page=page + 1,               # servis sahifalari 1 dan boshlanadi
        page_size=page_size,
        **_sort_args(sort_key),
        **filters,
    )

    label = _group_label(group)
    if total == 0:
        await _delete_quietly(message)
        await message.answer(texts.group_empty(label), reply_markup=back_to_files())
        if callback is not None:
            await callback.answer()
        return

    pages = max(1, -(-total // page_size))

    if not settings.storage.browse_send_media:
        await _render_text_list(
            message,
            items=items,
            total=total,
            page=page,
            pages=pages,
            group=group,
            label=label,
            sort_key=sort_key,
            user=user,
            settings=settings,
        )
        if callback is not None:
            await callback.answer()
        return

    # --- media rejimi -------------------------------------------------------
    await _delete_quietly(message)
    start = page * page_size + 1
    end = min(total, (page + 1) * page_size)
    await message.answer(
        f"{Emoji.for_group(group)} <b>{label}</b> — {start}-{end} "
        f"ko'rsatilmoqda (jami {total})\n"
        f"{Emoji.SORT.plain()} {sort_label(sort_key)}"
    )

    last_index = len(items) - 1
    for index, record in enumerate(items):
        caption = _file_caption(record)
        keyboard = file_actions_kb(record.id, pinned=bool(record.is_pinned))
        if index == last_index:
            # Eski bot detalı: sahifalash va "Guruhlar/Menyu" oxirgi faylda.
            keyboard.inline_keyboard.append(
                _nav_row(group, page, pages, sort_key)
            )
        await _send_media_safe(tg_storage, message.chat.id, record, caption, keyboard)

    if callback is not None:
        await callback.answer()


def _nav_row(group: str, page: int, pages: int, sort_key: str) -> list[InlineKeyboardButton]:
    """Oxirgi fayl ostidagi navigatsiya qatori."""
    row: list[InlineKeyboardButton] = []
    if pages > 1:
        if page > 0:
            row.append(InlineKeyboardButton(text="⬅️", callback_data=f"grp:{group}:{page - 1}"))
        row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            row.append(InlineKeyboardButton(text="➡️", callback_data=f"grp:{group}:{page + 1}"))
    row.append(
        InlineKeyboardButton(
            text=f"{Emoji.SORT.plain()} {sort_label(sort_key)[:24]}",
            callback_data=f"sort::grp:{group}:{page}",
        )
    )
    row.append(InlineKeyboardButton(text=f"{Emoji.FILES.plain()}", callback_data="files"))
    row.append(InlineKeyboardButton(text=f"{Emoji.HOME.plain()}", callback_data="menu"))
    return row


async def _render_text_list(
    message: Message,
    *,
    items: Any,
    total: int,
    page: int,
    pages: int,
    group: str,
    label: str,
    sort_key: str,
    user: User,
    settings: Settings,
) -> None:
    """Ixcham ro'yxat rejimi (media yubormasdan)."""
    lines = [
        f"{Emoji.for_group(group)} <b>{label}</b> — jami {total}\n"
        f"{Emoji.SORT.plain()} {sort_label(sort_key)}\n",
    ]
    for index, record in enumerate(items, start=page * settings.storage.page_size + 1):
        pin = f"{Emoji.PIN.plain()} " if record.is_pinned else ""
        shield = f" {Emoji.SHIELD.plain()}" if record.is_archived else ""
        lines.append(
            f"{index}. {pin}{Emoji.for_category(record.category)} "
            f"<b>{escape_html(truncate_middle(record.file_name, 40))}</b> · "
            f"{format_size(record.file_size)}{shield}\n"
            f"    {Emoji.FOLDER.plain()} {escape_html(_folder_name(record))}"
        )

    keyboard = group_list_kb(group, page, pages, sort_key=sort_key)
    # Har fayl uchun 📌/🗑️ tugmalari (ixcham rejimda ham amallar ishlashi kerak).
    for record in items:
        keyboard.inline_keyboard.append(
            [
                InlineKeyboardButton(
                    text=(
                        f"{Emoji.PIN.plain()} {truncate_middle(record.file_name, 22)}"
                        if record.is_pinned
                        else f"{Emoji.UNLOCK.plain()} {truncate_middle(record.file_name, 22)}"
                    ),
                    callback_data=f"{'unpin' if record.is_pinned else 'pin'}:{record.id.hex}",
                ),
                InlineKeyboardButton(
                    text=f"{Emoji.DOWNLOAD.plain()} Olish",
                    # ``FileActionFactory`` — yagona handler shu formatni kutadi
                    # (``fa:dl:<hex>``). Ilgari bu yerda xom ``dl:<hex>`` yozilardi
                    # va tugma bosilganda **hech narsa** bo'lmasdi.
                    callback_data=FileActionFactory(action="dl", uid=record.id.hex).pack(),
                ),
                InlineKeyboardButton(
                    text=f"{Emoji.DELETE.plain()}", callback_data=f"delete:{record.id.hex}"
                ),
            ]
        )

    await _delete_quietly(message)
    await message.answer("\n".join(lines)[:4096], reply_markup=keyboard)
    del user  # sozlamalar orqali ishlatiladi


def _group_label(group: str) -> str:
    """Guruh nomining ko'rinadigan matni."""
    renderer = getattr(texts, f"group_{group}", None)
    return renderer() if callable(renderer) else group.title()


def _file_caption(record: File) -> str:
    """Media ostidagi izoh (eski bot formati + arxiv belgisi)."""
    caption = texts.file_caption(record, folder_name=_folder_name(record))
    if record.is_archived:
        caption += f"\n{Emoji.SHIELD.plain()} arxiv nusxasi mavjud"
    return caption


# ---------------------------------------------------------------------------
# Saralash
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("sort::"))
async def cb_sort_menu(
    callback: CallbackQuery,
    user: User,
    settings: Settings,
) -> None:
    """``sort::<ctx>`` — saralash menyusini ochish."""
    context = callback.data[len("sort::") :] or "files"
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.sort_menu(sort_label(_user_sort(user))),
        reply_markup=sort_kb(context=context, current=_user_sort(user)),
    )
    await callback.answer()


@router.callback_query(F.data == "sort")
async def cb_sort_menu_root(
    callback: CallbackQuery, user: User, settings: Settings
) -> None:
    """``sort`` — saralash menyusi (guruhlar menyusidan)."""
    del settings
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.sort_menu(sort_label(_user_sort(user))),
        reply_markup=sort_kb(context="files", current=_user_sort(user)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("srt:"))
async def cb_sort_apply(
    callback: CallbackQuery,
    user: User,
    storage: StorageService,
    settings: Settings,
    tg_storage: TelegramStorage,
) -> None:
    """``srt:<key>::<ctx>`` — saralashni saqlash va ro'yxatni yangilash."""
    payload = callback.data[len("srt:") :]
    key, _, context = payload.partition("::")
    if key not in SORTS:
        await callback.answer("Noma'lum saralash", show_alert=True)
        return

    # Tanlovni foydalanuvchi sozlamalariga yozamiz (restart'dan keyin ham qoladi).
    await storage.update_settings(user.telegram_id, {"sort": key})
    user.settings = {**(user.settings or {}), "sort": key}
    await callback.answer(texts.sort_applied(sort_label(key)))

    if not context or context == "files":
        await send_files_menu(callback, user=user, storage=storage, settings=settings)
        return

    group, page = _parse_group_context(context)
    await render_group(
        callback.message,  # type: ignore[arg-type]
        group=group,
        page=page,
        user=user,
        storage=storage,
        settings=settings,
        tg_storage=tg_storage,
        callback=callback,
    )


# ---------------------------------------------------------------------------
# Qo'shimcha buyruqlar
# ---------------------------------------------------------------------------
@router.message(Command("sort"))
async def cmd_sort(message: Message, user: User) -> None:
    """``/sort`` — saralash menyusini xabar ko'rinishida ochish."""
    await message.answer(
        texts.sort_menu(sort_label(_user_sort(user))),
        reply_markup=sort_kb(context="files", current=_user_sort(user)),
    )


@router.message(Command("groups"))
async def cmd_groups(
    message: Message, user: User, storage: StorageService, settings: Settings
) -> None:
    """``/groups`` — guruhlar menyususi (``/files`` sinonimi)."""
    await send_files_menu(message, user=user, storage=storage, settings=settings)
