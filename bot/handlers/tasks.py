"""Список задач простым текстом. Изменения — голосом (см. handlers/create.py)."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.keyboards import BTN_TASKS, main_menu
from bot.models import ACTIVE_STATUSES, ARCHIVE_STATUSES, Task
from bot.repository import TaskRepository
from bot.utils import today_in
from bot.views import task_card, task_list_messages

router = Router(name="tasks")

ARCHIVE_LIMIT = 30  # сколько последних выполненных/отменённых показывать
PERSON_LIMIT = 100


async def list_messages(
    repo: TaskRepository, settings: Settings, f: str = "active", assignee: str | None = None
) -> list[str]:
    """Тексты списка задач: f = active / archive / all, assignee — задачи одного человека."""
    today = today_in(settings.timezone)
    who = f" — {escape(assignee)}" if assignee else ""
    if assignee:
        statuses = {"active": ACTIVE_STATUSES, "archive": ARCHIVE_STATUSES}.get(f)
        tasks, _ = await repo.list(statuses, limit=PERSON_LIMIT, offset=0, assignee=assignee)
    elif f == "archive":
        tasks = await repo.list_recent_archived(ARCHIVE_LIMIT)
    elif f == "all":
        tasks = [*await repo.list_active(), *await repo.list_recent_archived(ARCHIVE_LIMIT)]
    else:
        tasks = await repo.list_active()
    title = {
        "archive": "✅ <b>Выполненные и отменённые</b>",
        "all": "📋 <b>Все задачи</b>",
    }.get(f, "📋 <b>Задачи в работе</b>")
    return task_list_messages(tasks, today, title + who)


def card_text(task: Task, settings: Settings) -> str:
    return (
        task_card(task, today_in(settings.timezone))
        + f"\n\nЧто сделать с задачей — скажите, например: <i>«задача {task.id} выполнена»</i>."
    )


@router.message(Command("tasks"))
@router.message(F.text == BTN_TASKS)
async def show_tasks(message: Message, state: FSMContext, repo: TaskRepository, settings: Settings) -> None:
    await state.clear()
    for text in await list_messages(repo, settings):
        await message.answer(text, reply_markup=main_menu())


@router.callback_query(~F.data.startswith("d:"))
async def old_button(callback: CallbackQuery) -> None:
    """Кнопки из старых сообщений прошлой версии бота."""
    await callback.answer(
        "Эта кнопка больше не работает. Скажите голосом, что нужно сделать.", show_alert=True
    )
