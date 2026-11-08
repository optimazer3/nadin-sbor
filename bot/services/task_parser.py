"""Понимание сообщений руководителя (текст или расшифровка голосового) через нейросеть:
новые задачи и команды — изменить, удалить, показать.

Используется OpenAI-совместимый API (Chat Completions), поэтому подходит Polza AI,
OpenAI напрямую и любой другой сервис с таким же форматом: меняются только
LLM_BASE_URL, LLM_API_KEY и LLM_MODEL в .env.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from datetime import date
from typing import Any

import openai

from bot.dates import WEEKDAY_CODES, WEEKDAY_FULL, calendar_hint
from bot.models import LIST_FILTERS, Action, ActionType, Status, Task

log = logging.getLogger(__name__)

_SYSTEM_INTERPRET = """\
Ты помощник руководителя в Telegram-боте задач команды. Руководитель надиктовывает голосом \
или пишет сообщение. Определи, что он хочет сделать, и верни список действий.

Сообщение часто является автоматической расшифровкой голосового: в нём бывают ошибки \
распознавания, слова-паразиты, повторы и оговорки. Понимай смысл, а не букву.

Типы действий (поле type):
- create — новая задача, поручение команде. Даже без слова «создай»: «Ивану подготовить отчёт \
до пятницы» — это create. Поля: title, assignee, deadline.
- update — изменить существующие задачи (task_ids). Заполни только то, что меняется, \
остальные поля — null: title (новая формулировка), assignee (новый исполнитель), \
deadline (новый срок), status. Чтобы убрать исполнителя или срок — clear_assignee / \
clear_deadline = true.
- delete — удалить задачи (task_ids). Только когда прямо просят удалить, убрать, стереть задачу.
- open_task — показать одну задачу («покажи задачу пять», «что там с отчётом»).
- show_tasks — показать список задач. filter: active (по умолчанию), archive (выполненные и \
отменённые), all. Если просят задачи конкретного человека — assignee.
- show_dashboard — показать дашборд, сводку, отчёт по задачам.
- set_schedule — поменять день и время, когда бот сам присылает еженедельную сводку \
(«присылай сводку по пятницам в 9 утра», «сводку в понедельник в 8:30», «отчёт на час позже»). \
Поля: weekday (mon, tue, wed, thu, fri, sat, sun) и time (ЧЧ:ММ, 24 часа). Если меняют только \
день или только время — второе поле возьми из текущего расписания. «Утра» — до 12, \
«вечера» — +12 часов, «в 9» без уточнения — 09:00.
- unclear — просят изменить, удалить или открыть задачу, но непонятно какую (подходит \
несколько задач или ни одной). В task_ids — подходящие задачи (если есть), в question — \
короткий уточняющий вопрос.

Статусы (status): new — новая; in_progress — в работе («взял в работу», «начали», \
«верни в работу»); on_hold — на паузе («отложи», «заморозь»); done — выполнена («сделано», \
«готово», «закрой задачу», «выполнил»); cancelled — отменена («отмени», «не актуально»). \
«Отмени задачу» — это update со статусом cancelled, а не delete.

Существующие задачи ищи в списке ниже: по номеру («задача пять», «пятая», «номер 5»), \
по смыслу формулировки, по исполнителю и сроку. «Эта задача», «её», «она» — открытая сейчас \
задача, если она отмечена в списке. Используй только номера из списка, не придумывай новые. \
Если одно действие касается нескольких задач («все задачи Ивана»), перечисли их в task_ids.

Поля title / assignee / deadline:
- title — короткая и ясная формулировка того, что нужно сделать, с заглавной буквы, без имени \
исполнителя и без срока. Сохраняй важные детали, исправляй очевидные ошибки распознавания.
- assignee — исполнитель в именительном падеже («Ивану» → «Иван»), несколько — через запятую.
- deadline — дата YYYY-MM-DD. Относительные сроки считай по календарю: «до пятницы» / \
«в пятницу» — ближайшая пятница после сегодняшнего дня; «завтра» — следующий день; \
«к концу недели» — пятница текущей недели (если она уже прошла или сегодня — пятница \
следующей); «на следующей неделе» без дня — пятница следующей недели; «через N дней» — \
сегодня + N; «перенеси на день/неделю» — от текущего срока задачи.

В одном сообщении может быть несколько действий — верни их все по порядку. \
Если сообщение не похоже ни на задачу, ни на команду (приветствие, бессмыслица) — верни \
пустой список.
{team_block}
Ответь только JSON-объектом, без пояснений, строго в таком формате:
{{"actions": [{{"type": "create|update|delete|open_task|show_tasks|show_dashboard|set_schedule|unclear", \
"task_ids": [номера], "title": "строка или null", "assignee": "строка или null", \
"deadline": "YYYY-MM-DD или null", "status": "new|in_progress|on_hold|done|cancelled или null", \
"clear_assignee": false, "clear_deadline": false, "filter": "active|archive|all или null", \
"question": "строка или null", "weekday": "mon…sun или null", "time": "ЧЧ:ММ или null"}}]}}"""

_TEAM_BLOCK = """
Состав команды: {members}.
Если исполнитель похож на кого-то из команды (другой падеж, уменьшительное имя, \
ошибка распознавания: «Ване», «Машке», «Лёше») — запиши его имя ровно так, как оно указано \
в составе команды. Если человека нет в списке — запиши имя как услышал, в именительном падеже.
"""

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


_MAX_IDS = 30
_NULL_STRINGS = {"", "null", "none", "нет"}


def _tasks_block(tasks: Sequence[Task], focus_id: int | None) -> str:
    if not tasks:
        return "Текущих задач нет."
    lines = ["Текущие задачи (номер | формулировка | исполнитель | срок | статус):"]
    for t in tasks:
        mark = "  <- открыта сейчас" if t.id == focus_id else ""
        deadline = t.deadline.isoformat() if t.deadline else "-"
        lines.append(f"#{t.id} | {t.title} | {t.assignee or '-'} | {deadline} | {t.status.value}{mark}")
    return "\n".join(lines)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip().lower() in _NULL_STRINGS:
        return None
    return value.strip()


def _parse_time(value: Any) -> str | None:
    """«9:00», «09.30», «9» → «09:00» / «09:30»; иначе None."""
    text = _clean(value)
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", text)
    if not match:
        return None
    hours, minutes = int(match[1]), int(match[2] or 0)
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def _ids(value: Any) -> list[int]:
    result: list[int] = []
    for item in value if isinstance(value, list) else []:
        text = str(item).strip().lstrip("#")
        if text.isdigit() and int(text) not in result:
            result.append(int(text))
    return result


def _to_actions(items: list[Any], known_ids: set[int]) -> list[Action]:
    """Проверяет ответ нейросети: выкидывает мусор и несуществующие номера задач."""
    actions: list[Action] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            kind = ActionType(str(item.get("type") or "").strip().lower())
        except ValueError:
            continue
        ids = [i for i in _ids(item.get("task_ids")) if i in known_ids][:_MAX_IDS]
        status_raw = _clean(item.get("status"))
        status = Status(status_raw) if status_raw in {s.value for s in Status} else None
        filter_raw = _clean(item.get("filter"))
        action = Action(
            kind,
            task_ids=ids,
            title=_clean(item.get("title")),
            assignee=_clean(item.get("assignee")),
            deadline=_parse_iso(item.get("deadline")),
            status=status,
            clear_assignee=item.get("clear_assignee") is True,
            clear_deadline=item.get("clear_deadline") is True,
            filter=filter_raw if filter_raw in LIST_FILTERS else None,
            question=_clean(item.get("question")),
        )

        if kind == ActionType.CREATE:
            if action.title:
                actions.append(Action.create(action.draft()))
        elif kind in (ActionType.UPDATE, ActionType.DELETE, ActionType.OPEN_TASK):
            if not ids:
                actions.append(
                    Action(
                        ActionType.UNCLEAR,
                        question=action.question or "Не нашёл задачу, о которой идёт речь.",
                    )
                )
            elif kind == ActionType.UPDATE and not action.changes():
                actions.append(
                    Action(ActionType.UNCLEAR, task_ids=ids, question="Не понял, что изменить в задаче.")
                )
            elif kind == ActionType.OPEN_TASK:
                actions.append(Action(kind, task_ids=ids[:1]))
            else:
                actions.append(action)
        elif kind == ActionType.SHOW_TASKS:
            actions.append(Action(kind, assignee=action.assignee, filter=action.filter or "active"))
        elif kind == ActionType.SHOW_DASHBOARD:
            actions.append(Action(kind))
        elif kind == ActionType.SET_SCHEDULE:
            weekday = (_clean(item.get("weekday")) or "").lower()
            at = _parse_time(item.get("time"))
            if weekday in WEEKDAY_CODES and at:
                actions.append(Action(kind, weekday=weekday, time=at))
            else:
                actions.append(
                    Action(
                        ActionType.UNCLEAR,
                        question="Не понял, в какой день и во сколько присылать сводку. "
                        "Скажите, например: «по пятницам в 9 утра».",
                    )
                )
        else:  # unclear
            actions.append(Action(kind, task_ids=ids[:8], question=action.question))
    return actions


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
        self._system_interpret = _SYSTEM_INTERPRET.format(team_block=team_block)
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

    async def interpret(
        self,
        text: str,
        today: date,
        tasks: Sequence[Task] = (),
        focus_id: int | None = None,
        schedule: str | None = None,
    ) -> list[Action]:
        """Понимает, что хочет руководитель: создать, изменить, удалить, показать задачи.

        tasks — текущие задачи (чтобы найти, о какой речь), focus_id — открытая сейчас карточка.
        """
        user = (
            f"{_context(today)}\n\n{_tasks_block(tasks, focus_id)}\n\n"
            f"Еженедельная сводка сейчас приходит: {schedule or 'не настроено'}.\n\n"
            f"Сообщение руководителя:\n<message>\n{text}\n</message>"
        )
        data = await self._ask(self._system_interpret, user)
        actions = data.get("actions")
        if not isinstance(actions, list):
            raise TaskParserError("В ответе нейросети нет списка действий")
        return _to_actions(actions, known_ids={t.id for t in tasks})
