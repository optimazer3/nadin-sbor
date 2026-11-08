"""Голосовые и текстовые сообщения руководителя.

Голосовое → распознавание → нейросеть понимает, что нужно: создать задачи, изменить или удалить
существующие, показать список / дашборд / задачу. Всё, что меняет данные, бот сначала показывает
и спрашивает «Я правильно понял?»; показать — выполняет сразу.
"""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.commands import apply_plan, normalize_team
from bot.config import Settings
from bot.focus import FocusStore
from bot.handlers.tasks import card_view, render_list, render_person_list
from bot.keyboards import DraftCb, ListCb, TaskCb, draft_keyboard
from bot.models import ARCHIVE_STATUSES, Action, ActionType, Task, TaskDraft
from bot.repository import TaskRepository
from bot.scheduler import dashboard_messages
from bot.services.task_parser import TaskParser, TaskParserError
from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError
from bot.utils import drop_keyboard, safe_edit, today_in, transcribe_message
from bot.views import HELP_COMMANDS, apply_result_text, plan_text, task_button_text

log = logging.getLogger(__name__)
router = Router(name="create")

# Сколько недавно закрытых задач показывать нейросети, чтобы работало «верни задачу в работу».
RECENT_ARCHIVED = 20

View = tuple[str, InlineKeyboardMarkup | None]


class CreateTask(StatesGroup):
    confirm = State()


# Новое сообщение можно прислать в обычном режиме или поверх ещё не подтверждённого плана.
_INPUT = StateFilter(None, CreateTask.confirm)


@router.message(_INPUT, F.voice | F.audio | F.video_note)
async def on_voice(
    message: Message,
    state: FSMContext,
    bot: Bot,
    transcriber: AssemblyAITranscriber,
    parser: TaskParser,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
) -> None:
    status = await message.answer("🎧 Распознаю голосовое…")
    try:
        text = await transcribe_message(message, bot, transcriber)
    except TranscriptionError as exc:
        log.warning("Ошибка распознавания: %s", exc)
        await status.edit_text(
            f"😕 Не получилось распознать голосовое: {escape(str(exc))}\n\n"
            "Попробуйте записать ещё раз или напишите текстом."
        )
        return
    await status.edit_text(f"🎧 Распознано: <i>«{escape(text)}»</i>\n\n🤔 Разбираю…")
    await _handle(status, state, bot, parser, repo, settings, focus, text=text, source="voice")


@router.message(_INPUT, F.text, ~F.text.startswith("/"))
async def on_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    parser: TaskParser,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
) -> None:
    status = await message.answer("🤔 Разбираю…")
    await _handle(status, state, bot, parser, repo, settings, focus, text=message.text or "", source="text")


async def _context_tasks(repo: TaskRepository) -> list[Task]:
    """Задачи, среди которых нейросеть ищет ту, о которой речь."""
    try:
        active = await repo.list_active()
        archived = await repo.list_recent_archived(RECENT_ARCHIVED)
    except Exception:
        log.exception("Не удалось загрузить задачи для разбора команды")
        return []
    return [*active, *archived]


async def _handle(
    status: Message,
    state: FSMContext,
    bot: Bot,
    parser: TaskParser,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
    *,
    text: str,
    source: str,
) -> None:
    today = today_in(settings.timezone)
    chat_id = status.chat.id
    recognized = text if source == "voice" else None
    heard = f"🎧 Распознано: <i>«{escape(text)}»</i>\n\n" if recognized else ""

    context = await _context_tasks(repo)
    tasks_by_id = {t.id: t for t in context}
    note = ""
    try:
        actions = await parser.interpret(text, today, context, focus.get(chat_id))
    except TaskParserError as exc:
        log.warning("Не удалось разобрать сообщение через нейросеть: %s", exc)
        actions = [Action.create(TaskDraft(title=text.strip()))]
        note = (
            "⚠️ Автоматический разбор сейчас недоступен — сохраню текст как новую задачу. "
            "Исполнителя и срок можно будет указать в карточке задачи.\n\n"
        )
    actions = normalize_team(actions, settings.team_members)

    if not actions:
        await status.edit_text(f"{heard}🤷 Не понял, что нужно сделать.\n\n{HELP_COMMANDS}")
        return

    mutations = [a for a in actions if a.is_mutating]
    views: list[View] = []
    for action in actions:
        if not action.is_mutating:
            views.extend(await _render(action, repo, settings, focus, chat_id, tasks_by_id, heard))

    if mutations:
        # Старый неподтверждённый план (если был) заменяется новым — убираем его кнопки.
        if await state.get_state() == CreateTask.confirm.state:
            previous = await state.get_data()
            await drop_keyboard(bot, chat_id, previous.get("draft_msg_id"))
        await state.set_state(CreateTask.confirm)
        await state.set_data(
            {
                "plan": [a.to_dict() for a in mutations],
                "source": source,
                "raw_text": text,
                "draft_msg_id": status.message_id,
            }
        )
        await status.edit_text(
            note + plan_text(mutations, tasks_by_id, today, recognized), reply_markup=draft_keyboard()
        )
        for view_text, kb in views:
            await status.answer(view_text, reply_markup=kb)
        return

    first, *rest = views
    await safe_edit(status, first[0], first[1])
    for view_text, kb in rest:
        await status.answer(view_text, reply_markup=kb)


async def _render(
    action: Action,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
    chat_id: int,
    tasks_by_id: dict[int, Task],
    heard: str,
) -> list[View]:
    """Ответ на команды «показать»: список, дашборд, карточка, уточняющий вопрос."""
    if action.type == ActionType.SHOW_DASHBOARD:
        return await dashboard_messages(repo, settings)

    if action.type == ActionType.SHOW_TASKS:
        if action.assignee:
            return [await render_person_list(repo, settings, action.assignee, action.filter or "active")]
        return [await render_list(repo, settings, action.filter or "active", 0)]

    if action.type == ActionType.OPEN_TASK:
        task = await repo.get(action.task_ids[0])
        if task is None:
            return [(f"Задача #{action.task_ids[0]} не найдена — возможно, её удалили.", None)]
        focus.set(chat_id, task.id)
        f = "archive" if task.status in ARCHIVE_STATUSES else "active"
        return [card_view(task, settings, f)]

    # unclear: переспрашиваем и предлагаем подходящие задачи кнопками
    question = action.question or "Не понял, о какой задаче речь."
    today = today_in(settings.timezone)
    rows = [
        [
            InlineKeyboardButton(
                text=task_button_text(tasks_by_id[i], today), callback_data=TaskCb(action="open", id=i).pack()
            )
        ]
        for i in action.task_ids
        if i in tasks_by_id
    ]
    rows.append([InlineKeyboardButton(text="📋 Список задач", callback_data=ListCb(f="active", p=0).pack())])
    example = "например с номером: <i>«задача 5 выполнена»</i>."
    hint = (
        f"Выберите задачу ниже или скажите точнее, {example}"
        if len(rows) > 1
        else f"Откройте список задач или скажите точнее, {example}"
    )
    return [(f"{heard}🤔 {escape(question)}\n\n{hint}", InlineKeyboardMarkup(inline_keyboard=rows))]


async def _take_plan(callback: CallbackQuery, state: FSMContext) -> dict | None:
    """Забирает план, если кнопка нажата на актуальном сообщении. Иначе None."""
    data = await state.get_data()
    is_current = (
        await state.get_state() == CreateTask.confirm.state
        and callback.message is not None
        and data.get("draft_msg_id") == callback.message.message_id
    )
    if not is_current:
        return None
    # Сразу очищаем состояние, чтобы двойное нажатие не выполнило команду дважды.
    await state.clear()
    return data


@router.callback_query(DraftCb.filter(F.action == "save"))
async def on_save(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    repo: TaskRepository,
    focus: FocusStore,
) -> None:
    data = await _take_plan(callback, state)
    if data is None:
        await callback.answer("Это сообщение уже неактуально", show_alert=True)
        if callback.message:
            await drop_keyboard(bot, callback.message.chat.id, callback.message.message_id)
        return

    plan = [Action.from_dict(d) for d in data["plan"]]
    result = await apply_plan(
        repo,
        plan,
        source=data.get("source", "text"),
        raw_text=data.get("raw_text"),
        created_by=callback.from_user.id,
    )
    if result.nothing_done and result.errors:
        await state.set_state(CreateTask.confirm)  # ничего не применилось — можно нажать ещё раз
        await state.set_data(data)
        await callback.answer("Не удалось сохранить в базу. Попробуйте ещё раз.", show_alert=True)
        return

    chat_id = callback.message.chat.id
    for task_id in result.deleted:
        focus.drop(chat_id, task_id)
    if len(result.touched) == 1:
        focus.set(chat_id, result.touched[0].id)

    only_creates = all(a.type == ActionType.CREATE for a in plan)
    buttons = [
        [InlineKeyboardButton(text=f"Открыть #{t.id}", callback_data=TaskCb(action="open", id=t.id).pack())]
        for t in result.touched[:8]
    ]
    await safe_edit(
        callback.message,
        apply_result_text(result.lines, result.created, only_creates),
        InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )
    await callback.answer("Сохранено" if only_creates else "Готово")


@router.callback_query(DraftCb.filter(F.action == "cancel"))
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await _take_plan(callback, state)
    raw = (data or {}).get("raw_text")
    text = (
        "❌ Не сохранил, ничего не изменил.\n\n"
        "Запишите голосовое ещё раз или напишите текстом — я разберу заново."
    )
    if raw:
        text += f"\n\nМожно скопировать текст ниже, поправить и отправить мне:\n<code>{escape(raw)}</code>"
    await safe_edit(callback.message, text)
    await callback.answer()


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Пришлите голосовое или текст: новую задачу или команду. Список задач — кнопка «📋 Список задач»."
    )
