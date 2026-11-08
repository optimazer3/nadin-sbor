"""Выполнение подтверждённых команд: создать, изменить, удалить задачи."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from html import escape
from typing import TYPE_CHECKING

from bot.models import Action, ActionType, Task
from bot.repository import TaskRepository

if TYPE_CHECKING:
    from bot.scheduler import ReminderService

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    lines: list[str] = field(default_factory=list)  # что сделано — для сообщения руководителю
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


def match_team(name: str | None, team: Sequence[str]) -> str | None:
    """Приводит имя к написанию из TEAM_MEMBERS («иван» → «Иван»)."""
    if not name:
        return name
    for member in team:
        if member.lower() == name.strip().lower():
            return member
    return name.strip()


def normalize_team(actions: Sequence[Action], team: Sequence[str]) -> list[Action]:
    for action in actions:
        action.assignee = match_team(action.assignee, team)
    return list(actions)


def _change_summary(action: Action, task: Task) -> str:
    parts = []
    if action.title:
        parts.append("текст обновлён")
    if action.clear_assignee:
        parts.append("исполнитель убран")
    elif action.assignee:
        parts.append(f"исполнитель → {escape(task.assignee or '')}")
    if action.clear_deadline:
        parts.append("срок убран")
    elif action.deadline:
        parts.append(f"срок → {task.deadline:%d.%m.%Y}" if task.deadline else "срок обновлён")
    if action.status:
        parts.append(f"статус → {task.status.display}")
    return ", ".join(parts)


async def apply_plan(
    repo: TaskRepository,
    actions: Sequence[Action],
    *,
    source: str,
    raw_text: str | None,
    created_by: int | None,
    reminder: ReminderService | None = None,
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
            result.lines.append("⚠️ Не удалось создать новые задачи — ошибка базы данных")
        else:
            result.created.extend(created)
            result.lines.extend(f"➕ Создана №{t.id} «{escape(t.title)}»" for t in created)

    for action in actions:
        if action.type == ActionType.SET_SCHEDULE and reminder is not None:
            try:
                hours, minutes = (action.time or "").split(":")
                reminder.set(action.weekday or "", time(int(hours), int(minutes)))
            except (ValueError, OSError):
                log.exception("Не удалось поменять расписание сводки")
                result.errors += 1
                result.lines.append("⚠️ Не удалось поменять время сводки")
            else:
                result.schedule_changed = True
                result.lines.append(f"⏰ Сводка будет приходить {reminder.schedule.describe()}")
            continue
        if action.type not in (ActionType.UPDATE, ActionType.DELETE):
            continue
        for task_id in action.task_ids:
            try:
                if action.type == ActionType.UPDATE:
                    task = await repo.update(task_id, **action.changes())
                    if task is None:
                        result.lines.append(f"⚠️ Задача №{task_id} не найдена — возможно, её уже удалили")
                        continue
                    result.updated.append(task)
                    summary = _change_summary(action, task)
                    result.lines.append(f"✏️ №{task.id} «{escape(task.title)}»: {summary}")
                else:
                    task = await repo.get(task_id)
                    if task is None or not await repo.delete(task_id):
                        result.lines.append(f"⚠️ Задача №{task_id} не найдена — возможно, её уже удалили")
                        continue
                    result.deleted.append(task_id)
                    result.lines.append(f"🗑 Удалена №{task_id} «{escape(task.title)}»")
            except Exception:
                log.exception("Не удалось выполнить %s для задачи %s", action.type, task_id)
                result.errors += 1
                result.lines.append(f"⚠️ Задача №{task_id}: ошибка базы данных, попробуйте ещё раз")
    return result
