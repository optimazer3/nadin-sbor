"""Тексты сообщений: черновик, карточка задачи, список, дашборд. Разметка — HTML."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from html import escape

from bot.dates import WEEKDAY_EVERY, WEEKDAY_SHORT, format_date, format_date_short, relative_deadline
from bot.models import ACTIVE_STATUSES, Action, ActionType, Status, Task, TaskDraft
from bot.team import match_team, person_key

TELEGRAM_LIMIT = 4000  # реальный предел 4096, оставляем запас

NO_ASSIGNEE = "Без исполнителя"

_PLURAL = {Status.NEW: "Новые", Status.IN_PROGRESS: "В работе", Status.ON_HOLD: "На паузе"}


def _deadline_text(deadline: date | None, today: date) -> str:
    if deadline is None:
        return "не указан"
    return f"{format_date(deadline)} — {relative_deadline(deadline, today)}"


def draft_text(drafts: Sequence[TaskDraft], today: date, recognized: str | None) -> str:
    parts: list[str] = []
    if recognized:
        parts.append(f"🎧 Вы сказали: <i>«{escape(recognized)}»</i>\n")
    if len(drafts) == 1:
        parts.append("Я правильно понял задачу?\n")
    else:
        parts.append(f"Я правильно понял? Задач: {len(drafts)}\n")
    for i, d in enumerate(drafts, 1):
        prefix = f"<b>{i}.</b> " if len(drafts) > 1 else ""
        parts.append(
            f"{prefix}📝 {escape(d.title)}\n"
            f"👤 Исполнитель: {escape(d.assignee) if d.assignee else 'не указан'}\n"
            f"📅 Срок: {_deadline_text(d.deadline, today)}\n"
        )
    return "\n".join(parts).rstrip()


Num = Callable[[int], int]  # номер задачи из базы -> номер, который видит руководитель


def _same(task_id: int) -> int:
    return task_id


def task_card(task: Task, today: date, number: Num = _same) -> str:
    lines = [
        f"<b>Задача №{number(task.id)}</b>",
        "",
        f"📝 {escape(task.title)}",
        f"👤 Исполнитель: {escape(task.assignee) if task.assignee else 'не указан'}",
        f"📅 Срок: {_deadline_text(task.deadline, today)}",
        f"📌 Статус: {task.status.display}",
    ]
    if task.created_at:
        lines.append(f"🕓 Создана: {task.created_at:%d.%m.%Y}")
    return "\n".join(lines)


def _deadline_marker(deadline: date | None, today: date, active: bool) -> str:
    if deadline is None:
        return ""
    text = f"до {WEEKDAY_SHORT[deadline.weekday()]}, {format_date_short(deadline, today)}"
    if active:
        delta = (deadline - today).days
        if delta < 0:
            text += f"  ❗ {relative_deadline(deadline, today)}"
        elif delta == 0:
            text += "  ⏰ сегодня"
        elif delta == 1:
            text += "  ⏰ завтра"
    return text


def _sort_key(task: Task) -> tuple:
    return (task.deadline is None, task.deadline or date.max, task.id)


def group_by_person(tasks: Sequence[Task], team: Sequence[str] = ()) -> list[tuple[str, list[Task]]]:
    """Задачи по исполнителям: одно ФИО — одна группа. Внутри — по сроку, «Без исполнителя» в конце."""
    groups: dict[str, tuple[str, list[Task]]] = {}
    for task in tasks:
        key = person_key(task.assignee, team)
        name = (match_team(task.assignee, team) or NO_ASSIGNEE) if key else NO_ASSIGNEE
        groups.setdefault(key, (name, []))[1].append(task)
    ordered = sorted(groups.items(), key=lambda item: (item[0] == "", item[1][0].lower()))
    return [(name, sorted(items, key=_sort_key)) for _, (name, items) in ordered]


def order_tasks(tasks: Sequence[Task], team: Sequence[str] = ()) -> list[Task]:
    """Порядок для нумерации: сначала активные по людям, потом выполненные/отменённые по людям."""
    active = [t for t in tasks if t.is_active]
    closed = [t for t in tasks if not t.is_active]
    return [t for part in (active, closed) for _, items in group_by_person(part, team) for t in items]


def _tasks_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "задача"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "задачи"
    return "задач"


SEPARATOR = "➖➖➖➖➖➖➖➖"

LIST_HINT = (
    "💬 Чтобы изменить задачу, назовите её номер, например:\n"
    "<i>«задача {n} выполнена»</i>, <i>«перенеси задачу {n} на пятницу»</i>, <i>«удали задачу {n}»</i>."
)


def _list_item(task: Task, today: date, number: Num) -> str:
    details = []
    if task.deadline:
        details.append(f"📅 {_deadline_marker(task.deadline, today, task.is_active)}")
    else:
        details.append("📅 без срока")
    details.append(task.status.display)
    return f"<b>{number(task.id)}.</b> {escape(task.title)}\n      " + "  ·  ".join(details)


def task_list_messages(
    tasks: Sequence[Task], today: date, title: str, number: Num = _same, team: Sequence[str] = ()
) -> list[str]:
    """Список задач простым текстом: по исполнителям, номера по порядку, без кнопок."""
    if not tasks:
        return [f"{title}\n\nЗадач нет."]
    groups = group_by_person(tasks, team)
    blocks = [f"{title}\nВсего: {len(tasks)} {_tasks_word(len(tasks))}"]
    for name, items in groups:
        icon = "🙋" if name == NO_ASSIGNEE else "👤"
        head = f"{SEPARATOR}\n{icon} <b>{escape(name)}</b> — {len(items)} {_tasks_word(len(items))}"
        blocks.append(head + "\n\n" + "\n\n".join(_list_item(t, today, number) for t in items))
    first = min(number(t.id) for t in tasks)
    blocks.append(f"{SEPARATOR}\n" + LIST_HINT.format(n=first))
    return split_message(blocks)


def _dashboard_line(task: Task, today: date, number: Num) -> str:
    marker = _deadline_marker(task.deadline, today, active=True)
    tail = f"  ·  {marker}" if marker else ""
    return f"{task.status.emoji} <b>{number(task.id)}.</b> {escape(task.title)}{tail}"


def dashboard(
    tasks: Sequence[Task],
    today: date,
    title: str = "📊 <b>Сводка по задачам</b>",
    number: Num = _same,
    team: Sequence[str] = (),
) -> list[str]:
    """Сводка активных задач. Возвращает список сообщений (длинная сводка режется на части)."""
    active = [t for t in tasks if t.status in ACTIVE_STATUSES]
    header = f"{title} — {WEEKDAY_SHORT[today.weekday()]}, {today:%d.%m.%Y}"

    if not active:
        return [f"{header}\n\nАктивных задач нет 🎉"]

    counts = Counter(t.status for t in active)
    overdue = sum(1 for t in active if t.deadline and t.deadline < today)
    week_end = today.toordinal() + (6 - today.weekday())
    due_this_week = sum(
        1 for t in active if t.deadline and today.toordinal() <= t.deadline.toordinal() <= week_end
    )
    no_deadline = sum(1 for t in active if t.deadline is None)

    summary = [
        header,
        "",
        f"Активных задач: <b>{len(active)}</b>",
        " · ".join(f"{s.emoji} {_PLURAL[s]}: {counts[s]}" for s in ACTIVE_STATUSES if counts[s]),
    ]
    flags = []
    if overdue:
        flags.append(f"❗ Просрочено: <b>{overdue}</b>")
    if due_this_week:
        flags.append(f"⏰ Срок на этой неделе: {due_this_week}")
    if no_deadline:
        flags.append(f"Без срока: {no_deadline}")
    if flags:
        summary.append(" · ".join(flags))

    blocks: list[str] = ["\n".join(summary)]
    for name, items in group_by_person(active, team):
        lines = [SEPARATOR, f"👤 <b>{escape(name)}</b> — {len(items)} {_tasks_word(len(items))}"]
        lines += [_dashboard_line(t, today, number) for t in items]
        blocks.append("\n".join(lines))

    return split_message(blocks)


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


# ---------- голосовые команды: «Я правильно понял?» и итог ----------


ANSWER_HINT = "\n\nОтветьте «да» или «нет» — голосом или кнопкой."


def _short_deadline(deadline: date | None) -> str:
    return format_date(deadline) if deadline else "не указан"


def _task_ref(task_id: int, tasks_by_id: Mapping[int, Task], number: Num) -> str:
    task = tasks_by_id.get(task_id)
    n = number(task_id)
    return f"№{n} «{escape(task.title)}»" if task else f"№{n}"


def _change_lines(action: Action, task: Task | None) -> list[str]:
    """Строки «было → стало» для изменения задачи."""
    lines = []
    if action.title:
        lines.append(f"📝 Текст → «{escape(action.title)}»")
    if action.clear_assignee or action.assignee:
        old = escape(task.assignee) if task and task.assignee else "не указан"
        new = escape(action.assignee) if action.assignee and not action.clear_assignee else "убрать"
        lines.append(f"👤 Исполнитель: {old} → {new}")
    if action.clear_deadline or action.deadline:
        old = _short_deadline(task.deadline) if task else "?"
        new = format_date(action.deadline) if action.deadline and not action.clear_deadline else "убрать"
        lines.append(f"📅 Срок: {old} → {new}")
    if action.status:
        old = task.status.display if task else "?"
        lines.append(f"📌 Статус: {old} → {action.status.display}")
    return lines


def plan_text(
    actions: Sequence[Action],
    tasks_by_id: Mapping[int, Task],
    today: date,
    recognized: str | None,
    number: Num = _same,
) -> str:
    """Что бот собирается сделать — показывается с кнопками «Да, сохранить» / «Нет»."""
    if all(a.type == ActionType.CREATE for a in actions):
        return draft_text([a.draft() for a in actions], today, recognized) + ANSWER_HINT

    parts: list[str] = []
    if recognized:
        parts.append(f"🎧 Вы сказали: <i>«{escape(recognized)}»</i>\n")
    parts.append("Я правильно понял?\n")
    items: list[str] = []
    for action in actions:
        if action.type == ActionType.CREATE:
            items.append(
                "➕ <b>Новая задача</b>\n"
                f"📝 {escape(action.title or '')}\n"
                f"👤 Исполнитель: {escape(action.assignee) if action.assignee else 'не указан'}\n"
                f"📅 Срок: {_deadline_text(action.deadline, today)}"
            )
        elif action.type == ActionType.UPDATE:
            for task_id in action.task_ids:
                head = f"✏️ <b>Изменить задачу</b> {_task_ref(task_id, tasks_by_id, number)}"
                items.append("\n".join([head, *_change_lines(action, tasks_by_id.get(task_id))]))
        elif action.type == ActionType.SET_SCHEDULE:
            when = WEEKDAY_EVERY.get(action.weekday or "", action.weekday or "")
            items.append(f"⏰ <b>Присылать сводку</b> {when} в {action.time}")
        elif action.type == ActionType.DELETE:
            for task_id in action.task_ids:
                items.append(f"🗑 <b>Удалить задачу</b> {_task_ref(task_id, tasks_by_id, number)}")
    numbered = len(items) > 1
    for i, item in enumerate(items, 1):
        parts.append(f"<b>{i}.</b> {item}\n" if numbered else f"{item}\n")
    return "\n".join(parts).rstrip() + ANSWER_HINT


def apply_result_text(
    lines: Sequence[str], created: Sequence[Task], only_creates: bool, number: Num = _same
) -> str:
    if only_creates and created and len(lines) == len(created):
        head = "✅ Задача сохранена" if len(created) == 1 else f"✅ Сохранено задач: {len(created)}"
        return head + "\n\n" + "\n".join(f"№{number(t.id)} {escape(t.title)}" for t in created)
    return "✅ Готово\n\n" + "\n".join(lines)


HELP_COMMANDS = (
    "• <i>«Ивану подготовить отчёт до пятницы»</i> — новая задача\n"
    "• <i>«Задача 5 выполнена»</i> — сменить статус\n"
    "• <i>«Перенеси задачу 5 на понедельник»</i> — новый срок\n"
    "• <i>«Отдай задачу 5 Марии»</i> — другой исполнитель\n"
    "• <i>«Удали задачу 5»</i>\n"
    "• <i>«Покажи задачи Ивана»</i>, <i>«покажи сводку»</i>\n"
    "• <i>«Присылай сводку по пятницам в 9 утра»</i>"
)
