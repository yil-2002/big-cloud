"""Persistent (reply) keyboards.

The reply keyboard is the "always visible" navigation of the bot. It is sent once
after ``/start`` and re-sent whenever the user asks for the dashboard. A
``web_app`` button is included when ``WEB_APP_URL`` resolves, which turns the
Mini App into a one-tap feature.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from bot.utils.emojis import Emoji, custom_emoji_enabled
from db.models import User

__all__ = ["auth_reply_keyboard", "cancel_reply_keyboard", "main_reply_keyboard", "text_of"]

#: Button captions matched by :mod:`bot.handlers` (single source of truth).
BTN_STORAGE = "My files"
BTN_UPLOAD = "Upload"
BTN_SHARE = "Sharing"
BTN_STATS = "Stats"
BTN_SETTINGS = "Settings"
BTN_HELP = "Help"
BTN_WEBAPP = "Open app"
BTN_CANCEL = "Cancel"

#: hugecloud: autentifikatsiya klaviaturasi tugmalari.
BTN_LOGIN = "Login"
BTN_REGISTER = "Register"
BTN_FILES = "Fayllar"


def text_of(emoji: object, text: str, *, premium: bool = False) -> str:
    """Caption with optional custom emoji (same policy as inline buttons)."""
    icon = emoji.render(premium=premium) if custom_emoji_enabled() else emoji.plain()  # type: ignore[attr-defined]
    return f"{icon} {text}"


def main_reply_keyboard(user: User, *, web_app_url: str | None = None) -> ReplyKeyboardMarkup:
    """Three-row main keyboard: files, actions, system."""
    premium = bool(user.is_premium)
    rows: list[list[KeyboardButton]] = [
        [
            KeyboardButton(text=text_of(Emoji.FOLDER_OPEN, BTN_STORAGE, premium=premium)),
            KeyboardButton(text=text_of(Emoji.UPLOAD, BTN_UPLOAD, premium=premium)),
        ],
        [
            KeyboardButton(text=text_of(Emoji.SHARE, BTN_SHARE, premium=premium)),
            KeyboardButton(text=text_of(Emoji.STATS, BTN_STATS, premium=premium)),
        ],
        [
            KeyboardButton(text=text_of(Emoji.SETTINGS, BTN_SETTINGS, premium=premium)),
            KeyboardButton(text=text_of(Emoji.INFO, BTN_HELP, premium=premium)),
        ],
        [
            KeyboardButton(text="📱 Raqamni ulashish", request_contact=True),
        ],
    ]
    if web_app_url:
        rows.insert(
            0,
            [
                KeyboardButton(
                    text=text_of(Emoji.WEB_APP, BTN_WEBAPP, premium=premium),
                    web_app=WebAppInfo(url=web_app_url),
                )
            ],
        )
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Send a file or pick an action…",
        is_persistent=True,
    )


def auth_reply_keyboard(user: User | None = None) -> ReplyKeyboardMarkup:
    """🔐 Login/Register klaviaturasi — darvoza rad javobi bilan birga yuboriladi.

    Matnlar :mod:`bot.handlers.auth` dagi buyruq filtrlari bilan bog'langan:
    tugma bosilganda handler ``/login`` yoki ``/register`` ni chaqiradi.
    """
    premium = bool(user.is_premium) if user is not None else False
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=text_of(Emoji.KEY, BTN_LOGIN, premium=premium)),
                KeyboardButton(text=text_of(Emoji.SPARKLES, BTN_REGISTER, premium=premium)),
            ],
            [KeyboardButton(text=text_of(Emoji.INFO, BTN_HELP, premium=premium))],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="/login login parol",
    )


def cancel_reply_keyboard(user: User | None = None) -> ReplyKeyboardMarkup:
    """Single "Cancel" row shown while an FSM state waits for text input."""
    premium = bool(user.is_premium) if user is not None else False
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text_of(Emoji.CLOSE, BTN_CANCEL, premium=premium))]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Type the value or press Cancel…",
    )
