"""Sharing handlers: ``/share``, the share manager and protected deep links.

Two flows live here:

1. **Owner side** - create links with an expiry, attach an auto-generated pass
   code, publish/unpublish a file, revoke links (:class:`ShareFactory` callbacks).
2. **Recipient side** - a deep link pointing at a *protected* share: the token is
   parked in FSM state (:class:`PassCodeState`) and the file is delivered only
   after :func:`~db.models.shared_link.verify_pass_code` succeeds.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.logic import or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.keyboards.inline import (
    FileActionFactory,
    ShareFactory,
    main_menu_keyboard,
    share_keyboard,
    share_links_keyboard,
    to_uuid,
    uid_of,
)
from bot.keyboards.reply import cancel_reply_keyboard
from bot.utils.emojis import Emoji
from bot.utils.formatters import escape_html, truncate_middle
from config import Settings
from db.models import File, User
from services.exceptions import (
    FileNotFoundError,
    InvalidPassCodeError,
    LinkExpiredError,
    LinkNotFoundError,
    LinkRevokedError,
    NotOwnerError,
)
from services.share import ShareService
from services.storage import StorageService
from services.telegram import TelegramStorage

logger = logging.getLogger(__name__)

router = Router(name="share")


class PassCodeState(StatesGroup):
    """Waiting for the access code of a protected share link."""

    waiting_code = State()


# ---------------------------------------------------------------------------
# Owner side
# ---------------------------------------------------------------------------
@router.message(or_f(Command("share"), F.text.in_({"🔗 Sharing", "Share", "Sharing"})))
async def on_share_command(
    message: Message, user: User, storage: StorageService, settings: Settings
) -> None:
    """``/share`` - list files that can be shared right now (newest first)."""
    records, total = await storage.list_files(
        user.telegram_id, folder_id=None, page=1, page_size=settings.storage.page_size
    )
    if not total:
        await message.answer(
            f"{Emoji.EMPTY} <b>Nothing to share yet.</b>\n"
            f"{Emoji.UPLOAD} Send me a file first, then open its menu and press "
            f"{Emoji.SHARE.plain()} <b>Share</b>.",
            reply_markup=main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
        )
        return

    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{Emoji.SHARE.plain()} {truncate_middle(record.file_name, 32)}",
                    callback_data=FileActionFactory(action="share", uid=uid_of(record.id)).pack(),
                )
            ]
            for record in records
        ]
    )
    await message.answer(
        f"{Emoji.SHARE} <b>Sharing</b>\nPick a file to manage its public links "
        f"({total} in the root folder):",
        reply_markup=keyboard,
    )


async def render_share_menu(
    *,
    callback: CallbackQuery | None = None,
    message: Message | None = None,
    user: User,
    record: File,
    share: ShareService,
    settings: Settings,
) -> None:
    """Render the share manager for one file (both entry points)."""
    links = await share.list_links(record.id, user.telegram_id, active_only=False)
    text = texts.share_menu(record, links)
    keyboard = share_keyboard(record, links, user=user)
    del settings

    if callback is not None:
        from bot.handlers.callbacks import _edit  # local: avoids an import cycle

        await _edit(callback, text, keyboard)
        await callback.answer()
        return
    if message is not None:
        await message.answer(text, reply_markup=keyboard)


@router.callback_query(ShareFactory.filter(F.action == "links"))
async def on_share_links(
    callback: CallbackQuery,
    callback_data: ShareFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
    settings: Settings,
) -> None:
    """Open the share manager of a file."""
    record = await _owned_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await callback.answer(texts.link_not_found(), show_alert=True)
        return
    await render_share_menu(callback=callback, user=user, record=record, share=share, settings=settings)


@router.callback_query(ShareFactory.filter(F.action == "ttl"))
async def on_share_create(
    callback: CallbackQuery,
    callback_data: ShareFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
    settings: Settings,
) -> None:
    """Create a link with the chosen lifetime (``0`` == never expires)."""
    record = await _owned_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    try:
        ttl = int(callback_data.value)
    except (TypeError, ValueError):
        ttl = settings.storage.default_link_ttl

    try:
        link = await share.create_link(record.id, user.telegram_id, ttl_seconds=ttl)
    except (NotOwnerError, FileNotFoundError) as exc:
        await callback.answer(exc.message, show_alert=True)
        return

    await _show_created_link(callback, share, record, link)


@router.callback_query(ShareFactory.filter(F.action == "pass"))
async def on_share_with_pass_code(
    callback: CallbackQuery,
    callback_data: ShareFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
    settings: Settings,
) -> None:
    """Create a protected link with a freshly generated numeric pass code."""
    record = await _owned_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    pass_code = share.new_pass_code(6)
    try:
        link = await share.create_link(
            record.id,
            user.telegram_id,
            ttl_seconds=settings.storage.default_link_ttl,
            pass_code=pass_code,
        )
    except (NotOwnerError, FileNotFoundError) as exc:
        await callback.answer(exc.message, show_alert=True)
        return

    await _show_created_link(callback, share, record, link, pass_code=pass_code)


@router.callback_query(ShareFactory.filter(F.action == "public"))
async def on_share_publish(
    callback: CallbackQuery,
    callback_data: ShareFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
    settings: Settings,
) -> None:
    """Publish/unpublish a file (public files are discoverable by token only)."""
    record = await _owned_file(storage, callback_data.uid, user.telegram_id)
    if record is None:
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    updated = await storage.set_visibility(record.id, user.telegram_id, not record.is_public)
    state_text = "Public" if updated.is_public else "Private"
    await callback.answer(f"{state_text} ✓")
    await render_share_menu(callback=callback, user=user, record=updated, share=share, settings=settings)


@router.callback_query(ShareFactory.filter(F.action == "revoke"))
async def on_share_revoke(
    callback: CallbackQuery,
    callback_data: ShareFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
) -> None:
    """Revoke one link and refresh the manager."""
    try:
        await share.revoke_link(callback_data.value, user.telegram_id)
    except (LinkNotFoundError, NotOwnerError) as exc:
        await callback.answer(exc.message, show_alert=True)
        return

    record = await _owned_file(storage, callback_data.uid, user.telegram_id)
    links = await share.list_links(record.id, user.telegram_id, active_only=False) if record else []
    from bot.handlers.callbacks import _edit

    await _edit(
        callback,
        texts.link_revoked(callback_data.value)
        + ("\n\n" + texts.link_list(record, links) if record else ""),
        share_links_keyboard(record, links, user=user) if record else None,
    )
    await callback.answer("Revoked")


@router.callback_query(ShareFactory.filter(F.action == "info"))
async def on_share_info(
    callback: CallbackQuery,
    callback_data: ShareFactory,
    user: User,
    storage: StorageService,
    share: ShareService,
) -> None:
    """Show details + both URLs of one link."""
    try:
        link = await share.get_link(callback_data.value)
    except LinkNotFoundError:
        await callback.answer(texts.link_not_found(), show_alert=True)
        return

    record = await _owned_file(storage, callback_data.uid, user.telegram_id)
    from bot.handlers.callbacks import _edit

    await _edit(
        callback,
        "\n".join(
            [
                f"{Emoji.LINK} <b>Link details</b>",
                f"{Emoji.FILE} {escape_html(truncate_middle(record.file_name if record else '—', 40))}",
                "",
                f"Token: <code>{escape_html(link.token)}</code>",
                f"State: {'active' if link.is_active_now else 'inactive'}"
                + (" · expired" if link.is_expired else ""),
                f"Expires: {texts.format_date(link.expires_at) if link.expires_at else 'never'}",
                f"Pass code: {'yes' if link.has_pass_code else 'no'}",
                f"Views: {link.views} · Downloads: {link.downloads}",
                "",
                f"{Emoji.ROBOT} <code>{escape_html(share.deep_link(link.token))}</code>",
                f"{Emoji.WEB_APP} <code>{escape_html(share.share_url(link.token))}</code>",
            ]
        ),
        share_keyboard(record, [link], user=user) if record else None,
    )
    await callback.answer()


async def _show_created_link(
    callback: CallbackQuery,
    share: ShareService,
    record: File,
    link: Any,
    *,
    pass_code: str | None = None,
) -> None:
    """Render the freshly created link with both entry points."""
    from bot.handlers.callbacks import _edit

    await _edit(
        callback,
        texts.link_created(
            link,
            record,
            deep_link=share.deep_link(link.token),
            web_url=share.share_url(link.token),
            pass_code=pass_code,
        ),
        share_links_keyboard(record, [link], user=record.owner),
    )
    await callback.answer("Link created")


async def _owned_file(storage: StorageService, uid: str, telegram_id: int) -> File | None:
    """Resolve a compact uid to an owned file (``None`` when missing)."""
    try:
        return await storage.get_file(to_uuid(uid), telegram_id)
    except (FileNotFoundError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Recipient side: protected deep links
# ---------------------------------------------------------------------------
async def start_pass_code_flow(message: Message, token: str, state: FSMContext) -> None:
    """Park the share token in FSM and ask the recipient for the access code.

    Called from the deep-link handler when :meth:`ShareService.resolve_token`
    raises :class:`InvalidPassCodeError`, i.e. the link is protected.
    """
    await state.set_state(PassCodeState.waiting_code)
    await state.update_data({"share_token": token})
    await message.answer(
        texts.pass_code_prompt(),
        reply_markup=cancel_reply_keyboard(),
    )


@router.message(PassCodeState.waiting_code, F.text)
async def on_pass_code(
    message: Message,
    user: User,
    storage: StorageService,
    share: ShareService,
    tg_storage: TelegramStorage,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Verify the typed access code and deliver the shared file."""
    data = await state.get_data()
    token = str(data.get("share_token") or "")
    code = (message.text or "").strip()
    await state.clear()

    if not token:
        await message.answer(texts.error_generic("Share session expired, open the link again."))
        return
    if code.lower().startswith("/") or not code:
        await message.answer(texts.operation_cancelled())
        return

    try:
        record, link = await share.resolve_token(token, pass_code=code)
    except InvalidPassCodeError:
        await message.answer(
            f"{Emoji.KEY} <b>Wrong access code.</b>\nOpen the link again and retry."
        )
        return
    except LinkExpiredError:
        await message.answer(texts.link_expired_message())
        return
    except LinkRevokedError:
        await message.answer(texts.link_revoked_message())
        return
    except (LinkNotFoundError, FileNotFoundError):
        await message.answer(texts.link_not_found())
        return

    await deliver_shared_file(
        message=message,
        user=user,
        record=record,
        link=link,
        storage=storage,
        tg_storage=tg_storage,
        settings=settings,
    )


async def deliver_shared_file(
    *,
    message: Message,
    user: User,
    record: File,
    link: Any,
    storage: StorageService,
    tg_storage: TelegramStorage,
    settings: Settings,
) -> None:
    """Send a resolved shared file and save a copy into the recipient's cloud.

    Shared by the deep-link handler and the pass-code flow so both behave
    identically (same copy, same quota handling, same counters).
    """
    owner_name = record.owner.first_name if record.owner is not None else None

    await tg_storage.deliver(
        message.chat.id,
        record,
        caption=(
            f"{Emoji.LINK} Shared file{f' from {owner_name}' if owner_name else ''}\n"
            f"Tap to download or play."
        ),
    )

    if record.user_id == user.telegram_id:
        await message.answer(texts.file_card(record))
        return

    from bot.handlers.start import record_to_kwargs

    try:
        stored = await storage.add_file(telegram_id=user.telegram_id, **record_to_kwargs(record))
        copy = stored.record
    except Exception as exc:
        logger.info("Shared file not copied for %s: %s", user.telegram_id, exc)
        await message.answer(
            f"{Emoji.CHECK} File delivered. It was not saved to your cloud "
            f"(already stored or quota exceeded)."
        )
        return

    link.register_access(is_download=True)
    storage.session.add(link)
    await storage.session.commit()

    await message.answer(
        texts.deep_link_file_saved(copy, owner_name=owner_name),
        reply_markup=main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
    )
