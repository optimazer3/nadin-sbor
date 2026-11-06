"""Разбор текста (или расшифровки голосового) в задачи через Claude API."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import anthropic

from bot.dates import WEEKDAY_FULL, calendar_hint
from bot.models import TaskDraft

log = logging.getLogger(__name__)

# При отказе модели по соображениям безопасности API сам повторит запрос
# на рекомендованной резервной модели (server-side fallback).
_BETAS = ["server-side-fallback-2026-07-01"]

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

TASKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Формулировка задачи без имени исполнителя и срока",
                    },
                    "assignee": {
                        **_NULLABLE_STRING,
                        "description": "Исполнитель в именительном падеже или null",
                    },
                    "deadline": {
                        **_NULLABLE_STRING,
                        "description": "Срок в формате YYYY-MM-DD или null",
                    },
                },
                "required": ["title", "assignee", "deadline"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

DATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "deadline": {
            **_NULLABLE_STRING,
            "description": "Дата в формате YYYY-MM-DD или null, если дату понять нельзя",
        }
    },
    "required": ["deadline"],
    "additionalProperties": False,
}

_SYSTEM_TASKS = """\
Ты помощник руководителя. Руководитель надиктовывает или пишет задачи для своей команды, \
а ты превращаешь сообщение в список задач.

Сообщение часто является автоматической расшифровкой голосового: в нём бывают ошибки \
распознавания, слова-паразиты, повторы и оговорки. Понимай смысл, а не букву.

Для каждой задачи заполни:
- title — короткая и ясная формулировка того, что нужно сделать, с заглавной буквы, \
без имени исполнителя и без срока (они идут в отдельные поля). Сохраняй важные детали: \
что, для кого, какой объём. Исправляй очевидные ошибки распознавания.
- assignee — кто должен выполнить задачу, в именительном падеже («Ивану» → «Иван»). \
Если исполнителей несколько — через запятую. Если исполнитель не назван — null.
- deadline — срок в формате YYYY-MM-DD или null, если срок не назван. Относительные сроки \
считай по календарю из сообщения: «до пятницы» / «в пятницу» — ближайшая пятница после \
сегодняшнего дня; «завтра» — следующий день; «к концу недели» — пятница текущей недели \
(если она уже прошла или сегодня — пятница следующей); «на следующей неделе» без дня — \
пятница следующей недели; «через N дней» — сегодня + N.

Если в сообщении несколько разных задач — верни их отдельными элементами. \
Если в сообщении нет ни одной задачи (приветствие, вопрос, бессмыслица) — верни пустой список.
{team_block}"""

_TEAM_BLOCK = """
Состав команды: {members}.
Если исполнитель похож на кого-то из команды (другой падеж, уменьшительное имя, \
ошибка распознавания: «Ване», «Машке», «Лёше») — запиши его имя ровно так, как оно указано \
в составе команды. Если человека нет в списке — запиши имя как услышал, в именительном падеже."""

_SYSTEM_DATE = """\
Пользователь пишет срок выполнения задачи на русском языке. Определи дату по календарю \
из сообщения и верни её в формате YYYY-MM-DD. «До пятницы» / «в пятницу» — ближайшая \
пятница после сегодняшнего дня; «к концу недели» — пятница текущей недели (если она уже \
прошла или сегодня — пятница следующей); «через неделю» — сегодня + 7 дней. \
Если дату понять нельзя — верни null."""


class TaskParserError(RuntimeError):
    pass


def _context(today: date) -> str:
    return (
        f"Сегодня {today.isoformat()}, {WEEKDAY_FULL[today.weekday()]}.\nКалендарь:\n{calendar_hint(today)}"
    )


def _parse_iso(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


class TaskParser:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        model: str = "claude-opus-5-5",
        team_members: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._model = model
        team_block = _TEAM_BLOCK.format(members=", ".join(team_members)) if team_members else ""
        self._system_tasks = _SYSTEM_TASKS.format(team_block=team_block)

    async def _ask(self, system: str, user_text: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                betas=_BETAS,
                fallbacks="default",
                # Простая задача извлечения — низкого усилия достаточно и это быстрее.
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": schema},
                },
                system=system,
                messages=[{"role": "user", "content": user_text}],
            )
        except anthropic.APIConnectionError as exc:
            raise TaskParserError(f"Нет связи с Claude API: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise TaskParserError("Claude API: превышен лимит запросов, попробуйте позже") from exc
        except anthropic.APIStatusError as exc:
            raise TaskParserError(f"Claude API {exc.status_code}: {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise TaskParserError("Claude отказался обрабатывать это сообщение")
        if response.stop_reason == "max_tokens":
            raise TaskParserError("Ответ Claude оборвался (max_tokens)")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise TaskParserError("Claude вернул пустой ответ")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TaskParserError("Claude вернул некорректный JSON") from exc
        if not isinstance(data, dict):
            raise TaskParserError("Claude вернул неожиданный формат ответа")
        return data

    async def parse_tasks(self, text: str, today: date) -> list[TaskDraft]:
        """Возвращает список задач из сообщения (может быть пустым)."""
        user = f"{_context(today)}\n\nСообщение руководителя:\n<message>\n{text}\n</message>"
        data = await self._ask(self._system_tasks, user, TASKS_SCHEMA)

        drafts: list[TaskDraft] = []
        for item in data.get("tasks") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            assignee = item.get("assignee")
            assignee = assignee.strip() if isinstance(assignee, str) and assignee.strip() else None
            drafts.append(
                TaskDraft(title=title, assignee=assignee, deadline=_parse_iso(item.get("deadline")))
            )
        return drafts

    async def parse_date(self, text: str, today: date) -> date | None:
        """Понимает срок на естественном языке («к концу месяца», «через две недели»)."""
        user = f"{_context(today)}\n\nСрок:\n<deadline>\n{text}\n</deadline>"
        data = await self._ask(_SYSTEM_DATE, user, DATE_SCHEMA)
        return _parse_iso(data.get("deadline"))
