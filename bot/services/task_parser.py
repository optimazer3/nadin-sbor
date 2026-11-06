"""Разбор текста (или расшифровки голосового) в задачи через нейросеть.

Используется OpenAI-совместимый API (Chat Completions), поэтому подходит Polza AI,
OpenAI напрямую и любой другой сервис с таким же форматом: меняются только
LLM_BASE_URL, LLM_API_KEY и LLM_MODEL в .env.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

import openai

from bot.dates import WEEKDAY_FULL, calendar_hint
from bot.models import TaskDraft

log = logging.getLogger(__name__)

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
{team_block}
Ответь только JSON-объектом, без пояснений, строго в таком формате:
{{"tasks": [{{"title": "строка", "assignee": "строка или null", "deadline": "YYYY-MM-DD или null"}}]}}"""

_TEAM_BLOCK = """
Состав команды: {members}.
Если исполнитель похож на кого-то из команды (другой падеж, уменьшительное имя, \
ошибка распознавания: «Ване», «Машке», «Лёше») — запиши его имя ровно так, как оно указано \
в составе команды. Если человека нет в списке — запиши имя как услышал, в именительном падеже.
"""

_SYSTEM_DATE = """\
Пользователь пишет срок выполнения задачи на русском языке. Определи дату по календарю \
из сообщения. «До пятницы» / «в пятницу» — ближайшая пятница после сегодняшнего дня; \
«к концу недели» — пятница текущей недели (если она уже прошла или сегодня — пятница \
следующей); «через неделю» — сегодня + 7 дней; «к концу месяца» — последний день месяца.

Ответь только JSON-объектом, без пояснений: {"deadline": "YYYY-MM-DD"} \
или {"deadline": null}, если дату понять нельзя."""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


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


def _extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели (в том числе из блока ```json ... ```)."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise TaskParserError("Нейросеть вернула ответ не в формате JSON") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise TaskParserError("Нейросеть вернула некорректный JSON") from exc
    if not isinstance(data, dict):
        raise TaskParserError("Нейросеть вернула неожиданный формат ответа")
    return data


class TaskParser:
    def __init__(
        self,
        client: openai.AsyncOpenAI,
        model: str,
        team_members: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._model = model
        team_block = _TEAM_BLOCK.format(members=", ".join(team_members)) if team_members else ""
        self._system_tasks = _SYSTEM_TASKS.format(team_block=team_block)
        # Не все модели и сервисы поддерживают режим JSON; если запрос с ним отклонён,
        # дальше спрашиваем без него (формат всё равно задан в инструкции).
        self._json_mode = True

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.BadRequestError:
            if not self._json_mode:
                raise
            log.warning("Модель %s не приняла response_format=json_object — пробую без него", self._model)
            self._json_mode = False
            kwargs.pop("response_format")
            response = await self._client.chat.completions.create(**kwargs)

        if not response.choices:
            raise TaskParserError("Нейросеть вернула пустой ответ")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise TaskParserError("Ответ нейросети оборвался (слишком длинный)")
        if choice.finish_reason == "content_filter":
            raise TaskParserError("Нейросеть отказалась обрабатывать это сообщение")
        text = (choice.message.content or "").strip()
        if not text:
            raise TaskParserError("Нейросеть вернула пустой ответ")
        return text

    async def _ask(self, system: str, user_text: str) -> dict[str, Any]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]
        try:
            text = await self._complete(messages)
        except openai.AuthenticationError as exc:
            raise TaskParserError("Неверный ключ LLM_API_KEY") from exc
        except openai.NotFoundError as exc:
            raise TaskParserError(f"Модель «{self._model}» не найдена — проверьте LLM_MODEL") from exc
        except openai.RateLimitError as exc:
            raise TaskParserError("Превышен лимит запросов или закончился баланс, попробуйте позже") from exc
        except openai.APIConnectionError as exc:
            raise TaskParserError(f"Нет связи с сервисом нейросети: {exc}") from exc
        except openai.APIStatusError as exc:
            raise TaskParserError(f"Ошибка сервиса нейросети {exc.status_code}: {exc.message}") from exc
        return _extract_json(text)

    async def parse_tasks(self, text: str, today: date) -> list[TaskDraft]:
        """Возвращает список задач из сообщения (может быть пустым)."""
        user = f"{_context(today)}\n\nСообщение руководителя:\n<message>\n{text}\n</message>"
        data = await self._ask(self._system_tasks, user)

        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            raise TaskParserError("В ответе нейросети нет списка задач")

        drafts: list[TaskDraft] = []
        for item in tasks:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            assignee = item.get("assignee")
            if not isinstance(assignee, str) or assignee.strip().lower() in {"", "null", "none"}:
                assignee = None
            else:
                assignee = assignee.strip()
            drafts.append(
                TaskDraft(title=title, assignee=assignee, deadline=_parse_iso(item.get("deadline")))
            )
        return drafts

    async def parse_date(self, text: str, today: date) -> date | None:
        """Понимает срок на естественном языке («к концу месяца», «через две недели»)."""
        user = f"{_context(today)}\n\nСрок:\n<deadline>\n{text}\n</deadline>"
        data = await self._ask(_SYSTEM_DATE, user)
        return _parse_iso(data.get("deadline"))
