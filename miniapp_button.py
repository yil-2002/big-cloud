"""
Botingizning /start handler iga qo'shing.
Mini App URLni GitHub Pages yoki Render static files orqali host qiling.
"""

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

MINI_APP_URL = "https://YOUR_USERNAME.github.io/cloud-mini-app/"  # <-- o'zgartiring

@dp.message_handler(commands=["start"])
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="☁️ Cloud Drive ochish",
            web_app=WebAppInfo(url=MINI_APP_URL)
        )
    ]])
    await message.answer(
        "☁️ <b>Cloud Drive</b>\n\n"
        "Fayllaringizni quyidagi tugma orqali ko'ring va boshqaring:",
        reply_markup=kb,
        parse_mode="HTML"
    )
