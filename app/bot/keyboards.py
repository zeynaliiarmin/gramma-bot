"""Gramma — unified pressable keyboard builders (inline keyboards).

Every reply the bot sends uses one of these builders, so the UI stays
consistent and users interact with buttons instead of typing commands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

if TYPE_CHECKING:
    from app.models import InstagramAccount, Post


MAIN_KB = [
    ("📤 انتشار محتوا", "publish:menu"),
    ("📅 تقویم محتوا", "calendar:menu"),
    ("🤝 پست کلبریشن", "collab:menu"),
    ("💬 کامیونیتی", "community:menu"),
    ("📥 دایرکت", "direct:menu"),
    ("🔍 جستجوی هوشمند", "search:menu"),
    ("📊 آمار و امنیت", "insights:menu"),
    ("🤖 دستیار هوش مصنوعی", "ai:menu"),
    ("🛠 تنظیمات", "settings:menu"),
]

# The complete menu (all capabilities) opened by the persistent "منوی کامل"
# button and the /start welcome screen. Every callback_data here is handled.
FULL_KB = [
    ("📤 انتشار", "publish:menu"),
    ("🛰 ریلز", "publish:new:reel"),
    ("📸 استوری", "publish:new:story"),
    ("💬 کامنت‌ها", "community:comments"),
    ("📥 دایرکت", "direct:menu"),
    ("📅 تقویم", "calendar:menu"),
    ("🤝 کلبریشن", "collab:menu"),
    ("📈 آمار پیج", "insights:menu"),
    ("🛠 تنظیمات", "settings:menu"),
    ("🖥 پنل مدیریت", "webapp:open"),
]


def main_menu(miniapp_ticket: str | None = None) -> InlineKeyboardMarkup:
    """The bot's home screen (with an optional Mini-App control-panel button)."""
    builder = InlineKeyboardBuilder()
    for text, cb in MAIN_KB:
        builder.button(text=text, callback_data=cb)
    if miniapp_ticket:
        builder.button(text="🖥 پنل مدیریت (Mini-App)", callback_data="webapp:open")
    builder.adjust(2)
    return builder.as_markup()


def full_menu() -> InlineKeyboardMarkup:
    """The complete inline menu (منوی کامل) with every capability."""
    builder = InlineKeyboardBuilder()
    for text, cb in FULL_KB:
        builder.button(text=text, callback_data=cb)
    builder.adjust(2)
    return builder.as_markup()


def persistent_menu() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with the «📋 منوی کامل» button.

    The button stays visible in the chat at all times; when pressed it sends
    the text «📋 منوی کامل» which the bot answers with the full inline menu
    (full_menu). Uses input_field_placeholder to hint typing in RTL.
    """
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 منوی کامل")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="روی «📋 منوی کامل» بزنید",
    )


def back(target: str = "home") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="◀️ بازگشت", callback_data=f"nav:{target}")]


def with_back(markup: InlineKeyboardMarkup, target: str = "home") -> InlineKeyboardMarkup:
    """Append a back button to any existing keyboard."""
    return InlineKeyboardMarkup(inline_keyboard=list(markup.inline_keyboard) + [back(target)])


def publish_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in [
        ("🖼 عکس", "publish:new:photo"),
        ("🎬 ویدیو / ریلز", "publish:new:video"),
        ("🌀 کاروسل", "publish:new:carousel"),
        ("📸 استوری", "publish:new:story"),
        ("🕰 پست‌های زمان‌بندی‌شده", "schedule:list"),
    ]:
        b.button(text=text, callback_data=cb)
    b.adjust(2)
    b.row(*back())
    return b.as_markup()


def kind_select() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in [
        ("🖼 عکس", "publish:new:photo"),
        ("🎬 ویدیو", "publish:new:video"),
        ("🌀 کاروسل", "publish:new:carousel"),
        ("📸 استوری", "publish:new:story"),
        ("🛰 ریلز", "publish:new:reel"),
    ]:
        b.button(text=text, callback_data=cb)
    b.adjust(3)
    b.row(*back("publish"))
    return b.as_markup()


def schedule_kind_pick() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🕰 زمان‌بندی برای بعد", callback_data="schedule:time")
    b.button(text="🚀 انتشار فوری (همین الان)", callback_data="schedule:now")
    b.button(text="🗑 لغو", callback_data="schedule:cancel")
    b.adjust(1)
    return b.as_markup()


def community_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in [
        ("💬 کامنت‌ها", "community:comments"),
        ("🔇 پنهان/حذف", "community:moderate"),
        ("✉️ پاسخ خصوصی", "community:private"),
    ]:
        b.button(text=text, callback_data=cb)
    b.adjust(1)
    b.row(*back())
    return b.as_markup()


def direct_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in [
        ("📥 صندوق پیام", "direct:inbox"),
        ("🗂 دسته‌بندی خودکار", "direct:categorize"),
        ("✍️ پاسخ سریع", "direct:quick"),
    ]:
        b.button(text=text, callback_data=cb)
    b.adjust(1)
    b.row(*back())
    return b.as_markup()


def insights_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in [
        ("📈 داشبورد", "insights:dashboard"),
        ("🧠 تحلیل هوشمند پیج (AI)", "insights:ai"),
        ("🩺 گزارش سلامت پیج", "insights:health"),
        ("🛡 گزارش امنیتی", "insights:security"),
    ]:
        b.button(text=text, callback_data=cb)
    b.adjust(1)
    b.row(*back())
    return b.as_markup()


def settings_menu(account: "InstagramAccount | None") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if account is None:
        b.button(text="🔗 اتصال پیج اینستاگرام", callback_data="connect:start")
    else:
        b.button(text="🔌 قطع اتصال پیج", callback_data="connect:disconnect")
        b.button(text="🔐 بررسی امنیت", callback_data="security:check")
    b.button(text="🌐 زبان", callback_data="settings:lang")
    b.row(*back())
    return b.as_markup()


def caption_actions(account_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in [
        ("✨ " + ("کپشن جذاب"), f"ai:caption:{account_id}"),
        ("🌍 " + ("کپشن انگلیسی"), f"ai:caption_en:{account_id}"),
        ("🤖 " + ("دسته‌بندی دایرکت"), f"ai:classify:{account_id}"),
    ]:
        b.button(text=text, callback_data=cb)
    b.adjust(1)
    b.row(*back())
    return b.as_markup()


def ai_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✨ تولید کپشن", callback_data="ai:pick")
    b.button(text="💡 ایده پست از موضوع", callback_data="ai:ideas")
    b.button(text="🌐 جستجوی وب (AI)", callback_data="ai:websearch")
    b.button(text="🤖 دسته‌بندی هوشمند دایرکت", callback_data="direct:categorize")
    b.adjust(1)
    b.row(*back())
    return b.as_markup()


def confirm_connect(flow_token: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 اتصال پیج اینستاگرام", callback_data=f"connect:open:{flow_token}")
    b.row(*back("settings"))
    return b.as_markup()


KEY_PARTS_SEP = ":"


def paginated(items: list[tuple[str, str]], prefix: str, page: int = 0, per_page: int = 6) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    start = page * per_page
    chunk = items[start : start + per_page]
    for text, cb in chunk:
        builder.button(text=text, callback_data=cb)
    if not chunk:
        builder.button(text="— موردی نیست —", callback_data="nav:home")
    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"{prefix}:page:{page - 1}")
        )
    if start + per_page < len(items):
        nav_buttons.append(
            InlineKeyboardButton(text="بعدی ➡️", callback_data=f"{prefix}:page:{page + 1}")
        )
    if nav_buttons:
        builder.row(*nav_buttons)
    builder.row(*back())
    return builder.as_markup()
