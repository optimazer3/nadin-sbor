"""Кнопки. Их нарочно мало: главное управление — голосом."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

BTN_TASKS = "📋 Задачи"
BTN_DASHBOARD = "📊 Сводка"


class DraftCb(CallbackData, prefix="d"):
    action: str  # save | cancel


def main_menu() -> ReplyKeyboardMarkup:
    """Две большие кнопки внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_DASHBOARD)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Нажмите на микрофон и говорите",
    )


def draft_keyboard() -> InlineKeyboardMarkup:
    """«Да» / «Нет» под вопросом «Я правильно понял?»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=DraftCb(action="save").pack()),
                InlineKeyboardButton(text="❌ Нет", callback_data=DraftCb(action="cancel").pack()),
            ]
        ]
    )
