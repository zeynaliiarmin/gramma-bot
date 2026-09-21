"""Gramma — scheduling keyboard helpers (kept separate for clarity)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def time_picker() -> InlineKeyboardMarkup:
    """Quick preset buttons for scheduling."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏱ +1 ساعت", callback_data="schedule:quick:1h"),
                InlineKeyboardButton(text="⏱ +6 ساعت", callback_data="schedule:quick:6h"),
            ],
            [
                InlineKeyboardButton(text="🌅 فردا صبح", callback_data="schedule:quick:tomorrow"),
                InlineKeyboardButton(text="🗑 لغو", callback_data="schedule:cancel"),
            ],
        ]
    )
