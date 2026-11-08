"""Список задач простым текстом. Изменения — голосом (см. handlers/create.py)."""

from __future__ import annotations

from functools import partial
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.keyboards import BTN_TASKS, main_menu
from bot.models import Task
from bot.numbering import Numbering
from bot.repository import TaskRepository
from bot.team import match_team, person_key
from bot.utils import today_in
from bot.views import order_tasks, task_card, task_list_messages

router = Router(name="tasks")

ARCHIVE_LIMIT = 30  # сколько последних выполненных/отменённых задач видно (и можно назвать по номеру)


async def load_tasks(repo: TaskRepository) -> list[Task]:
    """Активные задачи и последние закрытые — всё, что руководитель видит и может назвать."""
    return [*await repo.list_active(), *await repo.list_recent_archived(ARCHIVE_LIMIT)]


def renumber(numbering: Numbering, chat_id: int, tasks: list[Task], settings: Settings) -> None:
    """Номера по порядку, как задачи стоят в списке: по людям, внутри — по сроку."""
    numbering.reset(chat_id, [t.id for t in order_tasks(tasks, settings.team_members)])


def _belongs(task: Task, who: str, team: tuple[str, ...]) -> bool:
    wanted = person_key(who, team).split()
    have = person_key(task.assignee, team).split()
    return bool(wanted) and all(w in have for w in wanted)


async def list_messages(
    repo: TaskRepository,
    settings: Settings,
    numbering: Numbering,
    chat_id: int,
    f: str = "active",
    assignee: str | None = None,
) -> list[str]:
    """Тексты списка задач: f = active / archive / all, assignee — задачи одного человека."""
    all_tasks = await load_tasks(repo)
    renumber(numbering, chat_id, all_tasks, settings)
    team = settings.team_members
    if f == "archive":
        tasks = [t for t in all_tasks if not t.is_active]
    elif f == "all":
        tasks = all_tasks
    else:
        tasks = [t for t in all_tasks if t.is_active]
    who = ""
    if assignee:
        tasks = [t for t in tasks if _belongs(t, assignee, team)]
        who = f" — {escape(match_team(assignee, team) or assignee)}"
    title = {
        "archive": "✅ <b>Выполненные и отменённые</b>",
        "all": "📋 <b>Все задачи</b>",
    }.get(f, "📋 <b>Задачи в работе</b>")
    number = partial(numbering.number, chat_id)
    return task_list_messages(tasks, today_in(settings.timezone), title + who, number, team)


def card_text(task: Task, settings: Settings, number: int) -> str:
    return (
        task_card(task, today_in(settings.timezone), lambda _: number)
        + f"\n\nЧто сделать с задачей — скажите, например: <i>«задача {number} выполнена»</i>."
    )


@router.message(Command("tasks"))
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
    await callback.answer(
        "Эта кнопка больше не работает. Скажите голосом, что нужно сделать.", show_alert=True
    )
