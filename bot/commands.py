"""Выполнение подтверждённых команд: создать, изменить, удалить задачи."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import time
from typing import TYPE_CHECKING

from bot.models import Action, ActionType, Task
from bot.repository import TaskRepository
from bot.team import match_team

if TYPE_CHECKING:
    from bot.scheduler import ReminderService

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    problems: list[str] = field(default_factory=list)  # что не получилось — показываем руководителю
    created: list[Task] = field(default_factory=list)
    updated: list[Task] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    schedule_changed: bool = False
    errors: int = 0

    @property
    def touched(self) -> list[Task]:
        """Задачи, которые остались и которые стоит предложить открыть."""
        seen: dict[int, Task] = {}
        for task in [*self.created, *self.updated]:
            seen[task.id] = task
        return list(seen.values())

    @property
    def nothing_done(self) -> bool:
        return not (self.created or self.updated or self.deleted or self.schedule_changed)


def normalize_team(actions: Sequence[Action], team: Sequence[str]) -> list[Action]:
    for action in actions:
        action.assignee = match_team(action.assignee, team)
    return list(actions)


async def apply_plan(
    repo: TaskRepository,
    actions: Sequence[Action],
    *,
    source: str,
    raw_text: str | None,
    created_by: int | None,
    reminder: ReminderService | None = None,
    number: Callable[[int], int] = lambda task_id: task_id,
) -> ApplyResult:
    """Выполняет действия по порядку. Ошибка в одном действии не мешает остальным."""
    result = ApplyResult()

    creates = [a.draft() for a in actions if a.type == ActionType.CREATE]
    if creates:
        try:
            created = await repo.create_many(creates, source=source, raw_text=raw_text, created_by=created_by)
        except Exception:
            log.exception("Не удалось создать задачи")
            result.errors += 1
            result.problems.append("Не получилось добавить задачу.")
        else:
            result.created.extend(created)

    for action in actions:
        if action.type == ActionType.SET_SCHEDULE and reminder is not None:
            try:
                hours, minutes = (action.time or "").split(":")
                reminder.set(action.weekday or "", time(int(hours), int(minutes)))
            except (ValueError, OSError):
                log.exception("Не удалось поменять расписание списка")
                result.errors += 1
                result.problems.append("Не получилось поменять время списка.")
            else:
                result.schedule_changed = True
            continue
        if action.type not in (ActionType.UPDATE, ActionType.DELETE):
            continue
        for task_id in action.task_ids:
            try:
                if action.type == ActionType.UPDATE:
                    task = await repo.update(task_id, **action.changes())
                    if task is None:
                        result.problems.append(f"Задачи {number(task_id)} уже нет.")
                        continue
                    result.updated.append(task)
                else:
                    task = await repo.get(task_id)
                    if task is None or not await repo.delete(task_id):
                        result.problems.append(f"Задачи {number(task_id)} уже нет.")
                        continue
                    result.deleted.append(task_id)
            except Exception:
                log.exception("Не удалось выполнить %s для задачи %s", action.type, task_id)
                result.errors += 1
                result.problems.append(f"Не получилось изменить задачу {number(task_id)}.")
    return result
