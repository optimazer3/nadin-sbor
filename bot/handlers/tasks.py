"""Кнопка «Список задач». Изменения — голосом (см. handlers/create.py)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.keyboards import BTN_TASKS, main_menu
from bot.numbering import Numbering
from bot.repository import TaskRepository
from bot.tasklist import list_messages

router = Router(name="tasks")


@router.message(Command("tasks"))
@router.message(Command("dashboard"))
@router.message(F.text == BTN_TASKS)
async def show_tasks(
    message: Message, state: FSMContext, repo: TaskRepository, settings: Settings, numbering: Numbering
) -> None:
    await state.clear()
    for text in await list_messages(repo, settings, numbering, message.chat.id):
        await message.answer(text, reply_markup=main_menu())


@router.callback_query(~F.data.startswith("d:"))
async def old_button(callback: CallbackQuery) -> None:
    """Кнопки из старых сообщений прошлой версии бота."""
    await callback.answer("Эта кнопка больше не работает.", show_alert=True)
