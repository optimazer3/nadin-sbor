"""Старт, помощь, главное меню, дашборд."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings
from bot.keyboards import BTN_CANCEL, BTN_DASHBOARD, BTN_NEW, main_menu
from bot.repository import TaskRepository
from bot.scheduler import send_dashboard
from bot.views import HELP_COMMANDS

router = Router(name="common")

_WEEKDAY_ACC = {
    "mon": "каждый понедельник",
    "tue": "каждый вторник",
    "wed": "каждую среду",
    "thu": "каждый четверг",
    "fri": "каждую пятницу",
    "sat": "каждую субботу",
    "sun": "каждое воскресенье",
}


def help_text(settings: Settings) -> str:
    when = f"{_WEEKDAY_ACC[settings.reminder_day]} в {settings.reminder_time:%H:%M}"
    return (
        "👋 Я веду задачи вашей команды. Со мной можно говорить голосом.\n\n"
        "🎙 <b>Новая задача</b> — запишите голосовое или напишите, например:\n"
        "<i>«Ивану подготовить отчёт по продажам до пятницы»</i>.\n"
        "Я покажу, как понял, — останется нажать «Да, сохранить». "
        "В одном сообщении можно продиктовать сразу несколько задач.\n\n"
        f"🗣 <b>Голосовые команды.</b> {HELP_COMMANDS}\n"
        "Перед тем как изменить или удалить задачу, я переспрошу.\n\n"
        "📋 <b>Список задач</b> — открыть задачу, сменить статус, исправить текст, "
        "исполнителя или срок кнопками.\n\n"
        f"📊 <b>Дашборд</b> — сводка активных задач. Он же приходит автоматически {when}.\n\n"
        "Команды: /tasks — список, /dashboard — дашборд, /cancel — отменить действие."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.clear()
    await message.answer(help_text(settings), reply_markup=main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message, settings: Settings) -> None:
    await message.answer(help_text(settings), reply_markup=main_menu())


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=main_menu())


@router.message(F.text == BTN_NEW)
async def btn_new(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎙 Запишите голосовое или напишите задачу текстом.\n"
        "Скажите, <b>что</b> сделать, <b>кому</b> и <b>к какому сроку</b>.",
        reply_markup=main_menu(),
    )


@router.message(Command("dashboard"))
@router.message(F.text == BTN_DASHBOARD)
async def btn_dashboard(
    message: Message, state: FSMContext, bot: Bot, repo: TaskRepository, settings: Settings
) -> None:
    await state.clear()
    await send_dashboard(bot, message.chat.id, repo, settings)
