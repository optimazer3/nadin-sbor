"""Номера задач по порядку (1, 2, 3…) вместо номеров из базы.

Руководитель видит в списке «1. Подготовить отчёт» и говорит «задача 1 выполнена». Чтобы номер
всегда означал ту задачу, которую он видел, бот запоминает последнюю показанную нумерацию.
Нумерация пересчитывается, когда показывается список или сводка; новые задачи между показами
получают следующие свободные номера, а номера удалённых не переиспользуются до следующего показа.
"""

from __future__ import annotations

from collections.abc import Iterable


class Numbering:
    def __init__(self) -> None:
        self._chats: dict[int, dict[int, int]] = {}  # chat_id -> {task_id: номер}

    def reset(self, chat_id: int, ordered_task_ids: Iterable[int]) -> None:
        """Новая нумерация по порядку, в котором задачи показаны."""
        mapping: dict[int, int] = {}
        for task_id in ordered_task_ids:
            mapping.setdefault(task_id, len(mapping) + 1)
        self._chats[chat_id] = mapping

    def has(self, chat_id: int) -> bool:
        return bool(self._chats.get(chat_id))

    def number(self, chat_id: int, task_id: int) -> int:
        """Номер задачи; если её ещё не показывали — следующий свободный."""
        mapping = self._chats.setdefault(chat_id, {})
        if task_id not in mapping:
            mapping[task_id] = max(mapping.values(), default=0) + 1
        return mapping[task_id]

    def numbers(self, chat_id: int, task_ids: Iterable[int]) -> dict[int, int]:
        return {task_id: self.number(chat_id, task_id) for task_id in task_ids}
