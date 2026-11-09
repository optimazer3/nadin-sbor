"""Список задач на контроле: загрузка, нумерация 1, 2, 3… и текст сообщения."""

from __future__ import annotations

from functools import partial

from bot.config import Settings
from bot.models import Task
from bot.numbering import Numbering
from bot.repository import TaskRepository
from bot.team import person_key
from bot.utils import today_in
from bot.views import LIST_TITLE, format_day, order_tasks, task_card, task_list_messages

ARCHIVE_LIMIT = 30  # сколько последних выполненных/отменённых задач можно назвать по номеру


async def load_tasks(repo: TaskRepository) -> list[Task]:
    """Задачи на контроле и последние закрытые — всё, что можно назвать голосом."""
    return [*await repo.list_active(), *await repo.list_recent_archived(ARCHIVE_LIMIT)]


def renumber(numbering: Numbering, chat_id: int, tasks: list[Task]) -> None:
    """Номера по порядку — как задачи стоят в списке."""
    numbering.reset(chat_id, [t.id for t in order_tasks(tasks)])


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
    title: str | None = None,
) -> list[str]:
    """Тексты списка: f = active (на контроле) / archive (закрытые) / all; assignee — чьи задачи."""
    all_tasks = await load_tasks(repo)
    renumber(numbering, chat_id, all_tasks)
    if f == "archive":
        tasks = [t for t in all_tasks if not t.is_active]
        default_title = "Выполненные и отменённые"
    elif f == "all":
        tasks = all_tasks
        default_title = "Все задачи"
    else:
        tasks = [t for t in all_tasks if t.is_active]
        default_title = LIST_TITLE
    if assignee:
        tasks = [t for t in tasks if _belongs(t, assignee, settings.team_members)]
    number = partial(numbering.number, chat_id)
    return task_list_messages(tasks, today_in(settings.timezone), title or default_title, number)


def weekly_title(settings: Settings) -> str:
    today = today_in(settings.timezone)
    return f"{LIST_TITLE} на {format_day(today, today)}"


def card_text(task: Task, settings: Settings, number: int) -> str:
    return task_card(task, today_in(settings.timezone), lambda _: number)
