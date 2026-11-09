"""Тексты сообщений. Без смайликов и подсказок — только то, что нужно увидеть. Разметка — HTML."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from html import escape

from bot.dates import WEEKDAY_EVERY
from bot.models import Action, ActionType, Status, Task

TELEGRAM_LIMIT = 4000  # реальный предел 4096, оставляем запас

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip

Num = Callable[[int], int]  # номер задачи в базе -> номер, который видит руководитель

LIST_TITLE = "Задачи на контроле"


def _same(task_id: int) -> int:
    return task_id


def format_day(d: date, today: date) -> str:
    """«13 ноября», а для другого года — «13 ноября 2027»."""
    text = f"{d.day} {MONTHS[d.month - 1]}"
    return text if d.year == today.year else f"{text} {d.year}"


def details(assignee: str | None, deadline: date | None, today: date) -> str:
    """«Иван Петров, до 13 ноября». Чего нет — того не пишем."""
    parts = []
    if assignee:
        parts.append(escape(assignee))
    if deadline:
        parts.append(f"до {format_day(deadline, today)}")
    return ", ".join(parts)


def _block(n: int | None, title: str, assignee: str | None, deadline: date | None, today: date) -> str:
    head = f"{n}. {escape(title)}" if n is not None else escape(title)
    extra = details(assignee, deadline, today)
    return f"{head}\n{extra}" if extra else head


def order_tasks(tasks: Sequence[Task]) -> list[Task]:
    """Порядок списка и нумерации: задачи на контроле в порядке добавления, потом закрытые."""
    active = sorted((t for t in tasks if t.is_active), key=lambda t: t.id)
    closed = [t for t in tasks if not t.is_active]
    return [*active, *closed]


def task_list_messages(
    tasks: Sequence[Task], today: date, title: str = LIST_TITLE, number: Num = _same
) -> list[str]:
    """Список: номер, задача, под ней исполнитель и срок. Между задачами — пустая строка."""
    if not tasks:
        return [f"<b>{title}</b>\n\nЗадач нет."]
    blocks = [f"<b>{title}</b>"]
    blocks += [_block(number(t.id), t.title, t.assignee, t.deadline, today) for t in order_tasks(tasks)]
    return split_message(blocks)


def task_card(task: Task, today: date, number: Num = _same) -> str:
    lines = [
        f"<b>Задача {number(task.id)}</b>",
        "",
        _block(None, task.title, task.assignee, task.deadline, today),
    ]
    if not task.is_active:
        lines.append(task.status.label.lower())
    return "\n".join(lines)


def split_message(blocks: Sequence[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Склеивает блоки в сообщения не длиннее limit, не разрывая строки."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        for piece in _split_block(block, limit):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                messages.append(current)
                current = piece
    if current:
        messages.append(current)
    return messages


def _split_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    pieces: list[str] = []
    current = ""
    for line in block.split("\n"):
        line = line if len(line) <= limit else line[: limit - 1] + "…"
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


# ---------- «Я правильно понял?» ----------


def _change_lines(action: Action, task: Task | None, today: date) -> list[str]:
    """Что меняется: «Срок: до 5 ноября → до 20 ноября»."""
    lines = []
    if action.title:
        lines.append(f"Текст: {escape(action.title)}")
    if action.clear_assignee:
        lines.append("Исполнитель: убрать")
    elif action.assignee:
        old = f"{escape(task.assignee)} → " if task and task.assignee else ""
        lines.append(f"Исполнитель: {old}{escape(action.assignee)}")
    if action.clear_deadline:
        lines.append("Срок: убрать")
    elif action.deadline:
        old = f"до {format_day(task.deadline, today)} → " if task and task.deadline else ""
        lines.append(f"Срок: {old}до {format_day(action.deadline, today)}")
    if action.status:
        old = f"{task.status.label.lower()} → " if task else ""
        lines.append(f"Статус: {old}{action.status.label.lower()}")
    return lines


# Смена только статуса звучит по-человечески: «Отметить задачу 2 выполненной?»
_STATUS_QUESTIONS = {
    Status.DONE: "Отметить задачу {n} выполненной",
    Status.CANCELLED: "Отменить задачу {n}",
    Status.IN_PROGRESS: "Взять задачу {n} в работу",
    Status.ON_HOLD: "Поставить задачу {n} на паузу",
}


def _items(
    action: Action, tasks_by_id: Mapping[int, Task], today: date, number: Num
) -> list[tuple[str, str]]:
    """(что сделать, подробности) для каждого пункта плана."""
    if action.type == ActionType.CREATE:
        return [
            ("Добавить задачу", _block(None, action.title or "", action.assignee, action.deadline, today))
        ]
    if action.type == ActionType.SET_SCHEDULE:
        when = WEEKDAY_EVERY.get(action.weekday or "", action.weekday or "")
        return [(f"Присылать список {when} в {action.time}", "")]
    result = []
    for task_id in action.task_ids:
        task = tasks_by_id.get(task_id)
        title = escape(task.title) if task else ""
        only_status = action.status and set(action.changes()) == {"status"}
        if action.type == ActionType.DELETE:
            result.append((f"Удалить задачу {number(task_id)}", title))
        elif only_status and action.status in _STATUS_QUESTIONS:
            result.append((_STATUS_QUESTIONS[action.status].format(n=number(task_id)), title))
        else:
            body = "\n".join([title, *_change_lines(action, task, today)]).strip()
            result.append((f"Изменить задачу {number(task_id)}", body))
    return result


def plan_text(
    actions: Sequence[Action], tasks_by_id: Mapping[int, Task], today: date, number: Num = _same
) -> str:
    """Вопрос перед сохранением; под ним кнопки «Да» / «Нет»."""
    items = [item for a in actions for item in _items(a, tasks_by_id, today, number)]
    if len(items) == 1:
        head, body = items[0]
        return f"{head}?\n\n{body}" if body else f"{head}?"
    parts = ["Я правильно понял?"]
    for i, (head, body) in enumerate(items, 1):
        parts.append(f"{i}. {head}\n{body}" if body else f"{i}. {head}")
    return "\n\n".join(parts)


HELP_COMMANDS = (
    "«Ивану подготовить отчёт до пятницы»\n"
    "«Задача 5 выполнена»\n"
    "«Перенеси задачу 5 на понедельник»\n"
    "«Отдай задачу 5 Марии»\n"
    "«Удали задачу 5»\n"
    "«Покажи список»\n"
    "«Присылай список по пятницам в 9 утра»"
)
