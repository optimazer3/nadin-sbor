"""Модели данных: задача, статусы, черновик задачи."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    DONE = "done"
    CANCELLED = "cancelled"

    @property
    def emoji(self) -> str:
        return _STATUS_EMOJI[self]

    @property
    def label(self) -> str:
        return _STATUS_LABEL[self]

    @property
    def display(self) -> str:  # «🔄 В работе»
        return f"{self.emoji} {self.label}"


_STATUS_EMOJI = {
    Status.NEW: "🆕",
    Status.IN_PROGRESS: "🔄",
    Status.ON_HOLD: "⏸",
    Status.DONE: "✅",
    Status.CANCELLED: "❌",
}

_STATUS_LABEL = {
    Status.NEW: "Новая",
    Status.IN_PROGRESS: "В работе",
    Status.ON_HOLD: "На паузе",
    Status.DONE: "Выполнена",
    Status.CANCELLED: "Отменена",
}

ACTIVE_STATUSES: tuple[Status, ...] = (Status.NEW, Status.IN_PROGRESS, Status.ON_HOLD)
ARCHIVE_STATUSES: tuple[Status, ...] = (Status.DONE, Status.CANCELLED)


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _to_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass
class Task:
    id: int
    title: str
    assignee: str | None
    deadline: date | None
    status: Status
    created_at: datetime | None = None
    updated_at: datetime | None = None
    source: str = "text"
    raw_text: str | None = None
    created_by: int | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Task:
        return cls(
            id=int(row["id"]),
            title=row["title"],
            assignee=row.get("assignee") or None,
            deadline=_to_date(row.get("deadline")),
            status=Status(row.get("status") or Status.NEW),
            created_at=_to_datetime(row.get("created_at")),
            updated_at=_to_datetime(row.get("updated_at")),
            source=row.get("source") or "text",
            raw_text=row.get("raw_text"),
            created_by=row.get("created_by"),
        )


@dataclass
class TaskDraft:
    """Задача, которую бот понял из сообщения, но ещё не сохранил."""

    title: str
    assignee: str | None = None
    deadline: date | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["deadline"] = self.deadline.isoformat() if self.deadline else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskDraft:
        return cls(
            title=data["title"],
            assignee=data.get("assignee") or None,
            deadline=_to_date(data.get("deadline")),
        )
