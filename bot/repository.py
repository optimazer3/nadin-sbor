"""Хранилище задач в Supabase (таблица public.tasks, см. supabase/schema.sql)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Protocol

from postgrest.types import CountMethod
from supabase import AsyncClient

from bot.models import ARCHIVE_STATUSES, Status, Task, TaskDraft

# Пометка «поле не меняем» для update(): None означает «очистить поле».
UNSET: Any = object()


def _escape_like(value: str) -> str:
    """Экранирует спецсимволы шаблона LIKE, чтобы имя искалось как есть."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class TaskRepository(Protocol):
    async def create_many(
        self, drafts: Sequence[TaskDraft], *, source: str, raw_text: str | None, created_by: int | None
    ) -> list[Task]: ...

    async def get(self, task_id: int) -> Task | None: ...

    async def list(
        self, statuses: Sequence[Status] | None, *, limit: int, offset: int, assignee: str | None = None
    ) -> tuple[list[Task], int]: ...

    async def list_active(self) -> list[Task]: ...

    async def list_recent_archived(self, limit: int) -> list[Task]: ...

    async def update(
        self,
        task_id: int,
        *,
        title: str = UNSET,
        assignee: str | None = UNSET,
        deadline: date | None = UNSET,
        status: Status = UNSET,
    ) -> Task | None: ...

    async def delete(self, task_id: int) -> bool: ...


def _changes(title: Any, assignee: Any, deadline: Any, status: Any) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if title is not UNSET:
        changes["title"] = title
    if assignee is not UNSET:
        changes["assignee"] = assignee
    if deadline is not UNSET:
        changes["deadline"] = deadline.isoformat() if deadline else None
    if status is not UNSET:
        changes["status"] = Status(status).value
    return changes


class SupabaseTaskRepository:
    def __init__(self, client: AsyncClient, table: str = "tasks") -> None:
        self._client = client
        self._table = table

    def _t(self):
        return self._client.table(self._table)

    async def create_many(
        self,
        drafts: Sequence[TaskDraft],
        *,
        source: str,
        raw_text: str | None,
        created_by: int | None,
    ) -> list[Task]:
        if not drafts:
            return []
        rows = [
            {
                "title": d.title,
                "assignee": d.assignee,
                "deadline": d.deadline.isoformat() if d.deadline else None,
                "status": Status.NEW.value,
                "source": source,
                "raw_text": raw_text,
                "created_by": created_by,
            }
            for d in drafts
        ]
        resp = await self._t().insert(rows).execute()
        return sorted((Task.from_row(r) for r in resp.data), key=lambda t: t.id)

    async def check(self) -> None:
        """Проверка при запуске: ключ подходит и таблица существует (иначе исключение)."""
        await self._t().select("id").limit(1).execute()

    async def get(self, task_id: int) -> Task | None:
        resp = await self._t().select("*").eq("id", task_id).limit(1).execute()
        return Task.from_row(resp.data[0]) if resp.data else None

    async def list(
        self, statuses: Sequence[Status] | None, *, limit: int, offset: int, assignee: str | None = None
    ) -> tuple[list[Task], int]:
        query = self._t().select("*", count=CountMethod.exact)
        if statuses:
            query = query.in_("status", [Status(s).value for s in statuses])
        if assignee:
            query = query.ilike("assignee", f"%{_escape_like(assignee)}%")
        query = (
            query.order("deadline", nullsfirst=False).order("id", desc=True).range(offset, offset + limit - 1)
        )
        resp = await query.execute()
        tasks = [Task.from_row(r) for r in resp.data]
        return tasks, resp.count if resp.count is not None else len(tasks)

    async def list_active(self) -> list[Task]:
        tasks: list[Task] = []
        page = 1000
        offset = 0
        while True:
            batch, total = await self.list(
                [Status.NEW, Status.IN_PROGRESS, Status.ON_HOLD], limit=page, offset=offset
            )
            tasks.extend(batch)
            offset += page
            if not batch or offset >= total:
                return tasks

    async def list_recent_archived(self, limit: int) -> list[Task]:
        """Недавно выполненные и отменённые задачи (чтобы можно было сказать «верни в работу»)."""
        resp = await (
            self._t()
            .select("*")
            .in_("status", [s.value for s in ARCHIVE_STATUSES])
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [Task.from_row(r) for r in resp.data]

    async def update(
        self,
        task_id: int,
        *,
        title: str = UNSET,
        assignee: str | None = UNSET,
        deadline: date | None = UNSET,
        status: Status = UNSET,
    ) -> Task | None:
        changes = _changes(title, assignee, deadline, status)
        if not changes:
            return await self.get(task_id)
        resp = await self._t().update(changes).eq("id", task_id).execute()
        return Task.from_row(resp.data[0]) if resp.data else None

    async def delete(self, task_id: int) -> bool:
        resp = await self._t().delete().eq("id", task_id).execute()
        return bool(resp.data)


class InMemoryTaskRepository:
    """Хранилище в памяти — для тестов. Ведёт себя как SupabaseTaskRepository."""

    def __init__(self) -> None:
        self.tasks: dict[int, Task] = {}
        self._next_id = 1

    async def create_many(
        self,
        drafts: Sequence[TaskDraft],
        *,
        source: str,
        raw_text: str | None,
        created_by: int | None,
    ) -> list[Task]:
        created = []
        for d in drafts:
            task = Task(
                id=self._next_id,
                title=d.title,
                assignee=d.assignee,
                deadline=d.deadline,
                status=Status.NEW,
                source=source,
                raw_text=raw_text,
                created_by=created_by,
            )
            self.tasks[task.id] = task
            self._next_id += 1
            created.append(task)
        return created

    async def get(self, task_id: int) -> Task | None:
        return self.tasks.get(task_id)

    async def list(
        self, statuses: Sequence[Status] | None, *, limit: int, offset: int, assignee: str | None = None
    ) -> tuple[list[Task], int]:
        items = [t for t in self.tasks.values() if not statuses or t.status in statuses]
        if assignee:
            items = [t for t in items if t.assignee and assignee.lower() in t.assignee.lower()]
        items.sort(key=lambda t: -t.id)
        items.sort(key=lambda t: (t.deadline is None, t.deadline or date.max))
        return items[offset : offset + limit], len(items)

    async def list_active(self) -> list[Task]:
        items, _ = await self.list([Status.NEW, Status.IN_PROGRESS, Status.ON_HOLD], limit=10**9, offset=0)
        return items

    async def list_recent_archived(self, limit: int) -> list[Task]:
        items = [t for t in self.tasks.values() if t.status in ARCHIVE_STATUSES]
        items.sort(key=lambda t: t.id, reverse=True)
        return items[:limit]

    async def update(
        self,
        task_id: int,
        *,
        title: str = UNSET,
        assignee: str | None = UNSET,
        deadline: date | None = UNSET,
        status: Status = UNSET,
    ) -> Task | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if title is not UNSET:
            task.title = title
        if assignee is not UNSET:
            task.assignee = assignee
        if deadline is not UNSET:
            task.deadline = deadline
        if status is not UNSET:
            task.status = Status(status)
        return task

    async def delete(self, task_id: int) -> bool:
        return self.tasks.pop(task_id, None) is not None
