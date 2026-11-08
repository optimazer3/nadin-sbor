"""Приветствие, помощь, сводка."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings
from bot.keyboards import BTN_DASHBOARD, main_menu
from bot.repository import TaskRepository
from bot.scheduler import ReminderService, send_dashboard
from bot.views import HELP_COMMANDS

router = Router(name="common")


def help_text(reminder: ReminderService) -> str:
    return (
        "Здравствуйте! 👋\n"
        "Я записываю задачи для вашей команды.\n\n"
        "🎙 <b>Просто нажмите на микрофон внизу и скажите, что нужно.</b>\n"
        "Например:\n"
        f"{HELP_COMMANDS}\n\n"
        "Прежде чем что-то сохранить, изменить или удалить, я переспрошу.\n\n"
        "📋 <b>Задачи</b> — список задач в работе\n"
        f"📊 <b>Сводка</b> — общая картина. Она приходит сама {reminder.schedule.describe()}."
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


@router.message(Command("dashboard"))
@router.message(F.text == BTN_DASHBOARD)
async def btn_dashboard(
    message: Message,
    state: FSMContext,
    bot: Bot,
    repo: TaskRepository,
    settings: Settings,
    reminder: ReminderService,
) -> None:
    await state.clear()
    await send_dashboard(bot, message.chat.id, repo, settings, reminder)
