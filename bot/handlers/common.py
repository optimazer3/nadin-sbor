"""Приветствие: единственное место, где бот объясняет, как им пользоваться."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards import main_menu
from bot.scheduler import ReminderService
from bot.views import HELP_COMMANDS

router = Router(name="common")


def help_text(reminder: ReminderService) -> str:
    return (
        "Здравствуйте!\n\n"
        "Нажмите на микрофон внизу и скажите, что нужно сделать. Например:\n\n"
        f"{HELP_COMMANDS}\n\n"
        "Перед тем как что-то изменить, я переспрошу — ответьте «да» или «нет».\n\n"
        f"Список задач будет приходить {reminder.schedule.describe()}. "
        "Его можно открыть в любой момент кнопкой «Список задач»."
    )


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, state: FSMContext, reminder: ReminderService) -> None:
    await state.clear()
    await message.answer(help_text(reminder), reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Хорошо, отменил.", reply_markup=main_menu())
