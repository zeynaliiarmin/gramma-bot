"""Gramma — FSM states.

aiogram Finite State Machine definitions. Each multi-step dialog (media
upload, scheduling, free-text entry) is a separate state so concurrent
conversations from different users never bleed into each other.
"""

from aiogram.fsm.state import State, StatesGroup


class MediaWait(StatesGroup):
    """Waiting for the user to send photo/video content."""

    waiting = State()


class TimeWait(StatesGroup):
    """Waiting for a schedule datetime (YYYY-MM-DD HH:MM)."""

    waiting = State()


class TextWait(StatesGroup):
    """Generic free-text entry (caption topic, comment/DM replies)."""

    waiting = State()
