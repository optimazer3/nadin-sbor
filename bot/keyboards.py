"""Клавиатуры и callback-данные."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.dates import format_date_short, next_weekday
from bot.models import ACTIVE_STATUSES, ARCHIVE_STATUSES, Status, Task
from bot.views import task_button_text

BTN_TASKS = "📋 Список задач"
BTN_DASHBOARD = "📊 Дашборд"
BTN_NEW = "➕ Новая задача"
BTN_CANCEL = "✖️ Отмена"

PAGE_SIZE = 8

# Фильтры списка: код -> (заголовок, статусы)
FILTERS: dict[str, tuple[str, tuple[Status, ...] | None]] = {
    "active": ("Активные задачи", ACTIVE_STATUSES),
    "archive": ("Архив (выполненные и отменённые)", ARCHIVE_STATUSES),
    "all": ("Все задачи", None),
}
FILTER_BUTTONS = {"active": "Активные", "archive": "Архив", "all": "Все"}


class DraftCb(CallbackData, prefix="d"):
    action: str  # save | cancel


class ListCb(CallbackData, prefix="l"):
    f: str = "active"
    p: int = 0


class TaskCb(CallbackData, prefix="t"):
    action: str
    id: int
    f: str = "active"
    p: int = 0
    v: str = ""


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_DASHBOARD)],
            [KeyboardButton(text=BTN_NEW)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Голосовое или текст задачи…",
    )


def draft_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, сохранить", callback_data=DraftCb(action="save").pack()),
                InlineKeyboardButton(text="❌ Нет", callback_data=DraftCb(action="cancel").pack()),
            ]
        ]
    )


def list_keyboard(tasks: Sequence[Task], today: date, f: str, page: int, pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for task in tasks:
        kb.row(
            InlineKeyboardButton(
                text=task_button_text(task, today),
                callback_data=TaskCb(action="open", id=task.id, f=f, p=page).pack(),
            )
        )
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=ListCb(f=f, p=page - 1).pack()))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=ListCb(f=f, p=page).pack()))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=ListCb(f=f, p=page + 1).pack()))
        kb.row(*nav)
    kb.row(
        *[
            InlineKeyboardButton(
                text=("• " + label + " •") if code == f else label,
                callback_data=ListCb(f=code, p=0).pack(),
            )
            for code, label in FILTER_BUTTONS.items()
        ]
    )
    return kb.as_markup()


def card_keyboard(task: Task, f: str, page: int) -> InlineKeyboardMarkup:
    def cb(action: str, v: str = "") -> str:
        return TaskCb(action=action, id=task.id, f=f, p=page, v=v).pack()

    kb = InlineKeyboardBuilder()
    # Быстрые кнопки для самых частых смен статуса; остальные — в «Сменить статус».
    if task.status in ARCHIVE_STATUSES:
        quick = [Status.IN_PROGRESS]  # вернуть в работу
    else:
        quick = [s for s in (Status.IN_PROGRESS, Status.DONE) if s != task.status]
    kb.row(*[InlineKeyboardButton(text=s.display, callback_data=cb("set_status", s.value)) for s in quick])
    kb.row(InlineKeyboardButton(text="📌 Сменить статус", callback_data=cb("status_menu")))
    kb.row(
        InlineKeyboardButton(text="✏️ Текст", callback_data=cb("edit_title")),
        InlineKeyboardButton(text="👤 Исполнитель", callback_data=cb("edit_assignee")),
        InlineKeyboardButton(text="📅 Срок", callback_data=cb("edit_deadline")),
    )
    kb.row(
        InlineKeyboardButton(text="🗑 Удалить", callback_data=cb("delete")),
        InlineKeyboardButton(text="⬅️ К списку", callback_data=ListCb(f=f, p=page).pack()),
    )
    return kb.as_markup()


def status_keyboard(task: Task, f: str, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for status in Status:
        text = f"· {status.display} ·" if status == task.status else status.display
        kb.row(
            InlineKeyboardButton(
                text=text,
                callback_data=TaskCb(action="set_status", id=task.id, f=f, p=page, v=status.value).pack(),
            )
        )
    kb.row(back_button(task.id, f, page))
    return kb.as_markup()


def assignee_keyboard(task: Task, team: Sequence[str], f: str, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, name in enumerate(team):
        kb.button(
            text=("✓ " + name) if name == task.assignee else name,
            callback_data=TaskCb(action="set_assignee", id=task.id, f=f, p=page, v=str(idx)).pack(),
        )
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(
            text="🚫 Без исполнителя",
            callback_data=TaskCb(action="set_assignee", id=task.id, f=f, p=page, v="-").pack(),
        )
    )
    kb.row(back_button(task.id, f, page))
    return kb.as_markup()


def deadline_options(today: date) -> dict[str, date | None]:
    """Быстрые варианты срока для кнопок."""
    return {
        "today": today,
        "tomorrow": today + timedelta(days=1),
        "friday": next_weekday(today, 4),
        "week": today + timedelta(days=7),
        "-": None,
    }


def deadline_keyboard(task: Task, today: date, f: str, page: int) -> InlineKeyboardMarkup:
    def cb(v: str) -> str:
        return TaskCb(action="set_deadline", id=task.id, f=f, p=page, v=v).pack()

    opts = deadline_options(today)

    def label(name: str, key: str) -> str:
        return f"{name} ({format_date_short(opts[key], today)})"

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=label("Сегодня", "today"), callback_data=cb("today")),
        InlineKeyboardButton(text=label("Завтра", "tomorrow"), callback_data=cb("tomorrow")),
    )
    kb.row(
        InlineKeyboardButton(text=label("Пятница", "friday"), callback_data=cb("friday")),
        InlineKeyboardButton(text=label("+1 неделя", "week"), callback_data=cb("week")),
    )
    kb.row(InlineKeyboardButton(text="🚫 Убрать срок", callback_data=cb("-")))
    kb.row(back_button(task.id, f, page))
    return kb.as_markup()


def delete_keyboard(task: Task, f: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить",
                    callback_data=TaskCb(action="delete_yes", id=task.id, f=f, p=page).pack(),
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=TaskCb(action="open", id=task.id, f=f, p=page).pack(),
                ),
            ]
        ]
    )


def back_button(task_id: int, f: str, page: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="⬅️ Назад", callback_data=TaskCb(action="open", id=task_id, f=f, p=page).pack()
    )


def cancel_keyboard(task_id: int, f: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[back_button(task_id, f, page)]])
