"""Тексты сообщений: черновик, карточка задачи, список, дашборд. Разметка — HTML."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from html import escape

from bot.dates import WEEKDAY_SHORT, format_date, format_date_short, relative_deadline
from bot.models import ACTIVE_STATUSES, Action, ActionType, Status, Task, TaskDraft

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
        parts.append(f"🎧 <b>Распознано:</b>\n<i>«{escape(recognized)}»</i>\n")
    if len(drafts) == 1:
        parts.append("Я правильно понял задачу?\n")
    else:
        parts.append(f"Я правильно понял? Задач: {len(drafts)}\n")
    for i, d in enumerate(drafts, 1):
        prefix = f"<b>{i}.</b> " if len(drafts) > 1 else ""
        parts.append(
            f"{prefix}📝 {escape(d.title)}\n"
            f"👤 Исполнитель: {escape(d.assignee) if d.assignee else 'не указан'}\n"
            f"📅 Дедлайн: {_deadline_text(d.deadline, today)}\n"
        )
    return "\n".join(parts).rstrip()


def task_card(task: Task, today: date) -> str:
    lines = [
        f"<b>Задача #{task.id}</b>",
        "",
        f"📝 {escape(task.title)}",
        f"👤 Исполнитель: {escape(task.assignee) if task.assignee else 'не указан'}",
        f"📅 Дедлайн: {_deadline_text(task.deadline, today)}",
        f"📌 Статус: {task.status.display}",
    ]
    if task.created_at:
        lines.append(f"🕓 Создана: {task.created_at:%d.%m.%Y}")
    if task.source == "voice":
        lines.append("🎙 Создана голосом")
    return "\n".join(lines)


def _deadline_marker(deadline: date | None, today: date, active: bool) -> str:
    if deadline is None:
        return ""
    text = f"до {format_date_short(deadline, today)}"
    if active:
        delta = (deadline - today).days
        if delta < 0:
            text += f" ❗ {relative_deadline(deadline, today)}"
        elif delta == 0:
            text += " ⏰ сегодня"
        elif delta == 1:
            text += " ⏰ завтра"
    return text


def task_button_text(task: Task, today: date) -> str:
    """Короткая строка для кнопки в списке (Telegram обрезает длинные кнопки)."""
    title = task.title if len(task.title) <= 40 else task.title[:39] + "…"
    extra = []
    if task.assignee:
        extra.append(task.assignee.split(",")[0].strip())
    if task.deadline:
        extra.append(format_date_short(task.deadline, today))
    tail = f" · {' · '.join(extra)}" if extra else ""
    overdue = "❗" if task.is_active and task.deadline and task.deadline < today else ""
    return f"{task.status.emoji}{overdue} #{task.id} {title}{tail}"


def list_header(filter_title: str, total: int, page: int, pages: int) -> str:
    if total == 0:
        return f"📋 <b>{filter_title}</b>\n\nЗадач нет."
    page_info = f" · стр. {page + 1}/{pages}" if pages > 1 else ""
    return (
        f"📋 <b>{filter_title}</b> — {total}{page_info}\n\n"
        "Нажмите на задачу, чтобы открыть её и изменить статус, текст, исполнителя или срок."
    )


def _dashboard_line(task: Task, today: date) -> str:
    marker = _deadline_marker(task.deadline, today, active=True)
    tail = f" · {marker}" if marker else ""
    return f"{task.status.emoji} #{task.id} {escape(task.title)}{tail}"


def _sort_key(task: Task) -> tuple:
    return (task.deadline is None, task.deadline or date.max, task.id)


def dashboard(tasks: Sequence[Task], today: date, title: str = "📊 <b>Дашборд задач</b>") -> list[str]:
    """Сводка активных задач. Возвращает список сообщений (длинный дашборд режется на части)."""
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

    groups: dict[str, list[Task]] = {}
    for task in active:
        groups.setdefault(task.assignee or NO_ASSIGNEE, []).append(task)

    def group_order(name: str) -> tuple:
        return (name == NO_ASSIGNEE, name.lower())

    blocks: list[str] = ["\n".join(summary)]
    for name in sorted(groups, key=group_order):
        items = sorted(groups[name], key=_sort_key)
        lines = [f"👤 <b>{escape(name)}</b> ({len(items)})"]
        lines += [_dashboard_line(t, today) for t in items]
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


STATUS_CHANGED = {
    Status.NEW: "Статус: новая",
    Status.IN_PROGRESS: "Статус: в работе",
    Status.ON_HOLD: "Статус: на паузе",
    Status.DONE: "Задача выполнена ✅",
    Status.CANCELLED: "Задача отменена",
}


# ---------- голосовые команды: «Я правильно понял?» и итог ----------


def _short_deadline(deadline: date | None) -> str:
    return format_date(deadline) if deadline else "не указан"


def _task_ref(task_id: int, tasks_by_id: Mapping[int, Task]) -> str:
    task = tasks_by_id.get(task_id)
    return f"#{task_id} «{escape(task.title)}»" if task else f"#{task_id}"


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
    actions: Sequence[Action], tasks_by_id: Mapping[int, Task], today: date, recognized: str | None
) -> str:
    """Что бот собирается сделать — показывается с кнопками «Да, сохранить» / «Нет»."""
    if all(a.type == ActionType.CREATE for a in actions):
        return draft_text([a.draft() for a in actions], today, recognized)

    parts: list[str] = []
    if recognized:
        parts.append(f"🎧 <b>Распознано:</b>\n<i>«{escape(recognized)}»</i>\n")
    parts.append("Я правильно понял?\n")
    items: list[str] = []
    for action in actions:
        if action.type == ActionType.CREATE:
            items.append(
                "➕ <b>Новая задача</b>\n"
                f"📝 {escape(action.title or '')}\n"
                f"👤 Исполнитель: {escape(action.assignee) if action.assignee else 'не указан'}\n"
                f"📅 Дедлайн: {_deadline_text(action.deadline, today)}"
            )
        elif action.type == ActionType.UPDATE:
            for task_id in action.task_ids:
                head = f"✏️ <b>Изменить задачу</b> {_task_ref(task_id, tasks_by_id)}"
                items.append("\n".join([head, *_change_lines(action, tasks_by_id.get(task_id))]))
        elif action.type == ActionType.DELETE:
            for task_id in action.task_ids:
                items.append(f"🗑 <b>Удалить задачу</b> {_task_ref(task_id, tasks_by_id)}")
    numbered = len(items) > 1
    for i, item in enumerate(items, 1):
        parts.append(f"<b>{i}.</b> {item}\n" if numbered else f"{item}\n")
    return "\n".join(parts).rstrip()


def apply_result_text(lines: Sequence[str], created: Sequence[Task], only_creates: bool) -> str:
    if only_creates and created and len(lines) == len(created):
        head = "✅ Задача сохранена" if len(created) == 1 else f"✅ Сохранено задач: {len(created)}"
        return head + "\n\n" + "\n".join(f"#{t.id} {escape(t.title)}" for t in created)
    return "✅ Готово\n\n" + "\n".join(lines)


HELP_COMMANDS = (
    "Голосом или текстом можно:\n"
    "• создать задачу — <i>«Марии обновить прайс до среды»</i>\n"
    "• сменить статус — <i>«задача пять выполнена»</i>, <i>«отчёт Ивана в работу»</i>\n"
    "• перенести срок — <i>«перенеси прайс на пятницу»</i>\n"
    "• сменить исполнителя — <i>«отдай звонок клиенту Алексею»</i>\n"
    "• удалить — <i>«удали задачу про воду»</i>\n"
    "• посмотреть — <i>«покажи задачи Марии»</i>, <i>«покажи дашборд»</i>, <i>«открой задачу семь»</i>"
)
