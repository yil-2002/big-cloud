"""Command router + reply-keyboard button routing.

Two kinds of input land here:

1. **Slash commands** — ``/storage``, ``/search``, ``/settings``, … (``/start``
   lives in :mod:`bot.handlers.start`, which this router includes *after* itself
   so button texts never shadow deep links).
2. **Reply-keyboard buttons** — pressing a persistent button sends its *visible
   text* as a plain message, so each label is matched explicitly. Labels come
   from :mod:`bot.keyboards.reply`, which makes them emoji-proof: the same
   constant builds the button and matches the incoming text.

This router is registered **before** :mod:`bot.handlers.upload`, so free-form
text is answered here instead of by the "send me a file" hint handler.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.logic import or_f
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import texts
from bot.handlers import storage as storage_handlers
from bot.handlers.callbacks import render_settings
from bot.keyboards.inline import main_menu_keyboard
from bot.keyboards.reply import (
    BTN_HELP,
    BTN_SETTINGS,
    BTN_SHARE,
    BTN_STATS,
    BTN_STORAGE,
    BTN_UPLOAD,
    BTN_WEBAPP,
    main_reply_keyboard,
    text_of,
)
from bot.utils.emojis import Emoji
from config import Settings
from db.models import User
from services.storage import StorageService

logger = logging.getLogger(__name__)

router = Router(name="commands")


def _button_texts(emoji: object, label: str) -> tuple[str, ...]:
    """Every textual variant a reply button can send back.

    Covers the plain label (custom emoji disabled) and the rendered label with
    an icon prefix, so toggling ``TG_EMOJI_*`` never breaks routing.
    """
    return (label, text_of(emoji, label))


#: All spellings of "cancel" accepted while an FSM flow waits for input.
_CANCEL_TEXTS: tuple[str, ...] = ("Cancel", "/cancel", f"{Emoji.CLOSE.plain()} Cancel", "bekor qilish")


# ---------------------------------------------------------------------------
# /storage - file browser
# ---------------------------------------------------------------------------
@router.message(or_f(Command("storage"), F.text.in_(_button_texts(Emoji.FOLDER_OPEN, BTN_STORAGE))))
async def cmd_storage(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Open the cloud browser at the root folder."""
    await storage_handlers.open_root(
        message, user=user, storage=storage, settings=settings, state=state
    )


# ---------------------------------------------------------------------------
# /search - name search
# ---------------------------------------------------------------------------
@router.message(or_f(Command("search"), F.text.in_(_button_texts(Emoji.SEARCH, "Search"))))
async def cmd_search(message: Message, state: FSMContext) -> None:
    """Start the search flow (the next text message becomes the query)."""
    await storage_handlers.start_search(message, state=state)


# ---------------------------------------------------------------------------
# /upload - how to store a file
# ---------------------------------------------------------------------------
@router.message(or_f(Command("upload"), F.text.in_(_button_texts(Emoji.UPLOAD, BTN_UPLOAD))))
async def cmd_upload(message: Message) -> None:
    """Explain the upload flow."""
    await message.answer(texts.upload_hint())


# ---------------------------------------------------------------------------
# /settings - preferences
# ---------------------------------------------------------------------------
@router.message(or_f(Command("settings"), F.text.in_(_button_texts(Emoji.SETTINGS, BTN_SETTINGS))))
async def cmd_settings(message: Message, user: User, settings: Settings) -> None:
    """Render the preferences screen."""
    await render_settings(message=message, user=user, settings=settings, edit=False)


# ---------------------------------------------------------------------------
# /stats - quota & breakdown
# ---------------------------------------------------------------------------
@router.message(or_f(Command("stats"), F.text.in_(_button_texts(Emoji.STATS, BTN_STATS))))
async def cmd_stats(
    message: Message, user: User, storage: StorageService, settings: Settings
) -> None:
    """Quota bar plus a per-category breakdown."""
    stats = await storage.stats(user.telegram_id)
    await message.answer(
        texts.stats_text(user, stats, max_file_size=settings.storage.max_file_size)
    )


# ---------------------------------------------------------------------------
# /share - sharing entry point
# ---------------------------------------------------------------------------
@router.message(or_f(Command("share"), F.text.in_(_button_texts(Emoji.SHARE, BTN_SHARE))))
async def cmd_share(
    message: Message, user: User, storage: StorageService, settings: Settings
) -> None:
    """Delegate to the share router's implementation (single source of copy)."""
    from bot.handlers.share import on_share_command

    await on_share_command(message=message, user=user, storage=storage, settings=settings)


# ---------------------------------------------------------------------------
# /webapp - reopen the Mini App button
# ---------------------------------------------------------------------------
@router.message(or_f(Command("webapp"), F.text.in_(_button_texts(Emoji.WEB_APP, BTN_WEBAPP))))
async def cmd_webapp(message: Message, user: User, settings: Settings) -> None:
    """Show the Mini App button again (handy after the keyboard was hidden)."""
    if not settings.web.web_app_url:
        await message.answer(
            f"{Emoji.WARNING} The Mini App URL is not configured "
            f"(set <code>WEB_APP_URL</code> or <code>BASE_URL</code>)."
        )
        return
    await message.answer(
        f"{Emoji.WEB_APP} <b>Mini App</b>\n"
        f"Use the button below to open the full file manager.\n"
        f"<code>{settings.web.web_app_url}</code>",
        reply_markup=main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
@router.message(or_f(Command("help"), F.text.in_(_button_texts(Emoji.INFO, BTN_HELP))))
async def cmd_help(message: Message, settings: Settings) -> None:
    """Command list and usage tips."""
    await message.answer(texts.help_text(bot_username=settings.bot.username))


# ---------------------------------------------------------------------------
# Cancel while an FSM flow waits for text
# ---------------------------------------------------------------------------
@router.message(F.text.in_(_CANCEL_TEXTS))
async def cmd_cancel(message: Message, user: User, settings: Settings, state: FSMContext) -> None:
    """Drop any pending FSM state and restore the main keyboard."""
    await state.clear()
    await message.answer(
        texts.operation_cancelled(),
        reply_markup=main_reply_keyboard(user, web_app_url=settings.web.web_app_url),
    )


# ---------------------------------------------------------------------------
# Unknown text -> dashboard
# ---------------------------------------------------------------------------
@router.message(F.text)
async def cmd_fallback(
    message: Message,
    user: User,
    storage: StorageService,
    settings: Settings,
    state: FSMContext,
) -> None:
    """Any other text message: show the dashboard and the main keyboard.

    Media never reaches this handler because :mod:`bot.handlers.upload` is
    registered earlier in the dispatcher.
    """
    await state.clear()
    stats = await storage.stats(user.telegram_id)
    await message.answer(
        texts.dashboard(
            user,
            stats,
            bot_username=settings.bot.username,
            is_admin=user.telegram_id in settings.bot.admin_ids,
        ),
        reply_markup=main_menu_keyboard(user, web_app_url=settings.web.web_app_url),
    )


def register_start_router(parent: Router) -> None:
    """Append :mod:`bot.handlers.start` so ``/start`` deep links still work."""
    from bot.handlers.start import router as start_router

    parent.include_router(start_router)


__all__ = ["register_start_router", "router"]
