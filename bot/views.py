"""Тексты сообщений: черновик, карточка задачи, список, дашборд. Разметка — HTML."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from html import escape

from bot.dates import WEEKDAY_SHORT, format_date, format_date_short, relative_deadline
from bot.models import ACTIVE_STATUSES, Status, Task, TaskDraft

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
