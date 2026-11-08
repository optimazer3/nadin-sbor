"""Список задач, карточка задачи, редактирование и смена статуса."""

from __future__ import annotations

import logging
import math
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.commands import match_team
from bot.config import Settings
from bot.dates import parse_date_simple
from bot.focus import FocusStore
from bot.keyboards import (
    BTN_TASKS,
    FILTERS,
    PAGE_SIZE,
    ListCb,
    TaskCb,
    assignee_keyboard,
    cancel_keyboard,
    card_keyboard,
    deadline_keyboard,
    deadline_options,
    delete_keyboard,
    list_keyboard,
    status_keyboard,
)
from bot.models import Status, Task
from bot.repository import TaskRepository
from bot.services.task_parser import TaskParser, TaskParserError
from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError
from bot.utils import drop_keyboard, has_audio, safe_edit, today_in, transcribe_message
from bot.views import STATUS_CHANGED, list_header, task_card

log = logging.getLogger(__name__)
router = Router(name="tasks")


class EditTask(StatesGroup):
    title = State()
    assignee = State()
    deadline = State()


# ---------- список ----------


async def render_list(repo: TaskRepository, settings: Settings, f: str, page: int):
    if f not in FILTERS:
        f = "active"
    title, statuses = FILTERS[f]
    page = max(page, 0)
    tasks, total = await repo.list(statuses, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    if page >= pages:  # страница опустела (например, после удаления)
        page = pages - 1
        tasks, total = await repo.list(statuses, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
    today = today_in(settings.timezone)
    return list_header(title, total, page, pages), list_keyboard(tasks, today, f, page, pages)


PERSON_LIMIT = 30


async def render_person_list(repo: TaskRepository, settings: Settings, assignee: str, f: str = "active"):
    """Задачи одного исполнителя (по голосовой команде «покажи задачи Марии»)."""
    if f not in FILTERS:
        f = "active"
    title, statuses = FILTERS[f]
    tasks, total = await repo.list(statuses, limit=PERSON_LIMIT, offset=0, assignee=assignee)
    today = today_in(settings.timezone)
    text = list_header(f"{title} — {escape(assignee)}", total, 0, 1)
    if total > len(tasks):
        text += f"\n\nПоказаны первые {len(tasks)}."
    return text, list_keyboard(tasks, today, f, 0, 1)


def card_view(task: Task, settings: Settings, f: str = "active", p: int = 0):
    return task_card(task, today_in(settings.timezone)), card_keyboard(task, f, p)


@router.message(Command("tasks"))
@router.message(F.text == BTN_TASKS)
async def show_tasks(message: Message, state: FSMContext, repo: TaskRepository, settings: Settings) -> None:
    await state.clear()
    text, kb = await render_list(repo, settings, "active", 0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(ListCb.filter())
async def on_list(
    callback: CallbackQuery,
    callback_data: ListCb,
    state: FSMContext,
    repo: TaskRepository,
    settings: Settings,
) -> None:
    await state.clear()
    text, kb = await render_list(repo, settings, callback_data.f, callback_data.p)
    await safe_edit(callback.message, text, kb)
    await callback.answer()


# ---------- карточка ----------


async def _show_card(
    callback: CallbackQuery,
    task: Task,
    settings: Settings,
    focus: FocusStore,
    f: str,
    p: int,
    toast: str | None = None,
) -> None:
    text, kb = card_view(task, settings, f, p)
    await safe_edit(callback.message, text, kb)
    focus.set(callback.from_user.id, task.id)
    await callback.answer(toast)


async def _load(
    callback: CallbackQuery, data: TaskCb, repo: TaskRepository, settings: Settings
) -> Task | None:
    task = await repo.get(data.id)
    if task is None:
        text, kb = await render_list(repo, settings, data.f, data.p)
        await safe_edit(callback.message, text, kb)
        await callback.answer("Задача не найдена — возможно, её удалили", show_alert=True)
    return task


@router.callback_query(TaskCb.filter(F.action == "open"))
async def on_open(
    callback: CallbackQuery,
    callback_data: TaskCb,
    state: FSMContext,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
) -> None:
    await state.clear()
    if task := await _load(callback, callback_data, repo, settings):
        await _show_card(callback, task, settings, focus, callback_data.f, callback_data.p)


@router.callback_query(TaskCb.filter(F.action == "status_menu"))
async def on_status_menu(
    callback: CallbackQuery, callback_data: TaskCb, repo: TaskRepository, settings: Settings
) -> None:
    if task := await _load(callback, callback_data, repo, settings):
        today = today_in(settings.timezone)
        await safe_edit(
            callback.message,
            task_card(task, today) + "\n\n📌 Выберите новый статус:",
            status_keyboard(task, callback_data.f, callback_data.p),
        )
        await callback.answer()


@router.callback_query(TaskCb.filter(F.action == "set_status"))
async def on_set_status(
    callback: CallbackQuery,
    callback_data: TaskCb,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
) -> None:
    try:
        status = Status(callback_data.v)
    except ValueError:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    task = await repo.update(callback_data.id, status=status)
    if task is None:
        await _load(callback, callback_data, repo, settings)
        return
    await _show_card(
        callback, task, settings, focus, callback_data.f, callback_data.p, STATUS_CHANGED[status]
    )


# ---------- удаление ----------


@router.callback_query(TaskCb.filter(F.action == "delete"))
async def on_delete(
    callback: CallbackQuery, callback_data: TaskCb, repo: TaskRepository, settings: Settings
) -> None:
    if task := await _load(callback, callback_data, repo, settings):
        today = today_in(settings.timezone)
        await safe_edit(
            callback.message,
            task_card(task, today) + "\n\n🗑 <b>Удалить задачу?</b> Это действие нельзя отменить.\n"
            "Если задача просто не нужна — можно поставить статус «Отменена».",
            delete_keyboard(task, callback_data.f, callback_data.p),
        )
        await callback.answer()


@router.callback_query(TaskCb.filter(F.action == "delete_yes"))
async def on_delete_yes(
    callback: CallbackQuery,
    callback_data: TaskCb,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
) -> None:
    deleted = await repo.delete(callback_data.id)
    focus.drop(callback.from_user.id, callback_data.id)
    text, kb = await render_list(repo, settings, callback_data.f, callback_data.p)
    await safe_edit(callback.message, text, kb)
    await callback.answer(f"Задача #{callback_data.id} удалена" if deleted else "Задача уже удалена")


# ---------- редактирование: вход в режим ----------

_PROMPTS = {
    "edit_title": (EditTask.title, "✏️ Пришлите новый текст задачи — сообщением или голосовым."),
    "edit_assignee": (
        EditTask.assignee,
        "👤 Выберите исполнителя кнопкой или пришлите имя сообщением / голосовым.",
    ),
    "edit_deadline": (
        EditTask.deadline,
        "📅 Выберите срок кнопкой или пришлите дату: <i>15.11</i>, <i>завтра</i>, "
        "<i>в пятницу</i>, <i>через две недели</i>… Можно голосом. «-» — убрать срок.",
    ),
}


@router.callback_query(TaskCb.filter(F.action.in_(tuple(_PROMPTS))))
async def on_edit(
    callback: CallbackQuery,
    callback_data: TaskCb,
    state: FSMContext,
    repo: TaskRepository,
    settings: Settings,
) -> None:
    task = await _load(callback, callback_data, repo, settings)
    if task is None:
        return
    new_state, prompt = _PROMPTS[callback_data.action]
    f, p = callback_data.f, callback_data.p
    if callback_data.action == "edit_assignee":
        kb = assignee_keyboard(task, settings.team_members, f, p)
    elif callback_data.action == "edit_deadline":
        kb = deadline_keyboard(task, today_in(settings.timezone), f, p)
    else:
        kb = cancel_keyboard(task.id, f, p)

    await state.set_state(new_state)
    await state.set_data({"task_id": task.id, "f": f, "p": p, "card_msg_id": callback.message.message_id})
    today = today_in(settings.timezone)
    await safe_edit(callback.message, task_card(task, today) + "\n\n" + prompt, kb)
    await callback.answer()


# ---------- редактирование: кнопки ----------


@router.callback_query(TaskCb.filter(F.action == "set_assignee"))
async def on_set_assignee(
    callback: CallbackQuery,
    callback_data: TaskCb,
    state: FSMContext,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
) -> None:
    assignee: str | None
    if callback_data.v == "-":
        assignee = None
    else:
        try:
            assignee = settings.team_members[int(callback_data.v)]
        except (ValueError, IndexError):
            await callback.answer("Состав команды изменился — откройте задачу заново", show_alert=True)
            return
    await state.clear()
    task = await repo.update(callback_data.id, assignee=assignee)
    if task is None:
        await _load(callback, callback_data, repo, settings)
        return
    await _show_card(
        callback, task, settings, focus, callback_data.f, callback_data.p, "Исполнитель обновлён"
    )


@router.callback_query(TaskCb.filter(F.action == "set_deadline"))
async def on_set_deadline(
    callback: CallbackQuery,
    callback_data: TaskCb,
    state: FSMContext,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
) -> None:
    options = deadline_options(today_in(settings.timezone))
    if callback_data.v not in options:
        await callback.answer("Неизвестный вариант", show_alert=True)
        return
    await state.clear()
    task = await repo.update(callback_data.id, deadline=options[callback_data.v])
    if task is None:
        await _load(callback, callback_data, repo, settings)
        return
    await _show_card(callback, task, settings, focus, callback_data.f, callback_data.p, "Срок обновлён")


# ---------- редактирование: ввод текстом или голосом ----------

_EDIT_INPUT = F.voice | F.audio | F.video_note | (F.text & ~F.text.startswith("/"))


async def _read_input(
    message: Message, bot: Bot, transcriber: AssemblyAITranscriber
) -> tuple[str | None, str]:
    """Возвращает (текст, приставка для ответа). Текст None — если голосовое не распозналось."""
    if not has_audio(message):
        return (message.text or "").strip(), ""
    status = await message.answer("🎧 Распознаю…")
    try:
        text = await transcribe_message(message, bot, transcriber)
    except TranscriptionError as exc:
        await status.edit_text(f"😕 Не получилось распознать: {escape(str(exc))}\nПопробуйте ещё раз.")
        return None, ""
    await status.delete()
    return text.strip(), f"🎧 Распознано: <i>«{escape(text)}»</i>\n\n"


async def _finish_edit(
    message: Message,
    bot: Bot,
    state: FSMContext,
    task: Task | None,
    settings: Settings,
    focus: FocusStore,
    prefix: str,
) -> None:
    data = await state.get_data()
    await state.clear()
    await drop_keyboard(bot, message.chat.id, data.get("card_msg_id"))
    if task is None:
        await message.answer("Задача не найдена — возможно, её удалили.")
        return
    focus.set(message.chat.id, task.id)
    today = today_in(settings.timezone)
    await message.answer(
        prefix + "✅ Сохранено\n\n" + task_card(task, today),
        reply_markup=card_keyboard(task, data.get("f", "active"), data.get("p", 0)),
    )


@router.message(EditTask.title, _EDIT_INPUT)
async def on_title_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    repo: TaskRepository,
    transcriber: AssemblyAITranscriber,
    settings: Settings,
    focus: FocusStore,
) -> None:
    text, prefix = await _read_input(message, bot, transcriber)
    if text is None:
        return
    if not text:
        await message.answer("Текст задачи не может быть пустым. Пришлите новый текст.")
        return
    data = await state.get_data()
    task = await repo.update(data["task_id"], title=text)
    await _finish_edit(message, bot, state, task, settings, focus, prefix)


@router.message(EditTask.assignee, _EDIT_INPUT)
async def on_assignee_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    repo: TaskRepository,
    transcriber: AssemblyAITranscriber,
    settings: Settings,
    focus: FocusStore,
) -> None:
    text, prefix = await _read_input(message, bot, transcriber)
    if text is None:
        return
    text = text.rstrip(".!")
    assignee = None if text.lower() in {"", "-", "—", "нет", "никто", "без исполнителя"} else text
    if assignee:
        assignee = match_team(assignee, settings.team_members)
    data = await state.get_data()
    task = await repo.update(data["task_id"], assignee=assignee)
    await _finish_edit(message, bot, state, task, settings, focus, prefix)


@router.message(EditTask.deadline, _EDIT_INPUT)
async def on_deadline_input(
    message: Message,
    state: FSMContext,
    bot: Bot,
    repo: TaskRepository,
    transcriber: AssemblyAITranscriber,
    parser: TaskParser,
    settings: Settings,
    focus: FocusStore,
) -> None:
    text, prefix = await _read_input(message, bot, transcriber)
    if text is None:
        return
    today = today_in(settings.timezone)
    parsed = parse_date_simple(text, today)
    if parsed.ok:
        deadline = parsed.value
    else:
        try:
            deadline = await parser.parse_date(text, today)
        except TaskParserError as exc:
            log.warning("Не удалось разобрать дату через нейросеть: %s", exc)
            deadline = None
        if deadline is None:
            await message.answer(
                prefix + "🤔 Не понял дату. Напишите, например: <i>15.11</i>, <i>завтра</i>, "
                "<i>в пятницу</i> — или «-», чтобы убрать срок."
            )
            return
    data = await state.get_data()
    task = await repo.update(data["task_id"], deadline=deadline)
    await _finish_edit(message, bot, state, task, settings, focus, prefix)
