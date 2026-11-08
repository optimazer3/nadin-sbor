"""Какую карточку задачи руководитель открыл последней — чтобы понимать «удали её», «эту задачу»."""

from __future__ import annotations

import time
from collections.abc import Callable


class FocusStore:
    def __init__(self, ttl: float = 30 * 60, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._items: dict[int, tuple[int, float]] = {}

    def set(self, chat_id: int, task_id: int) -> None:
        self._items[chat_id] = (task_id, self._clock())

    def get(self, chat_id: int) -> int | None:
        item = self._items.get(chat_id)
        if item is None:
            return None
        task_id, at = item
        if self._clock() - at > self._ttl:  # давно не открывали — «эта задача» уже не про неё
            del self._items[chat_id]
            return None
        return task_id

    def drop(self, chat_id: int, task_id: int | None = None) -> None:
        item = self._items.get(chat_id)
        if item and (task_id is None or item[0] == task_id):
            del self._items[chat_id]
