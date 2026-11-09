"""Голосовые и текстовые сообщения руководителя — основной способ управления ботом.

Голосовое → распознавание → нейросеть понимает, что нужно: добавить, изменить, удалить задачи,
показать список, поменять время еженедельного списка. Перед любым изменением бот спрашивает
и ждёт «да» (голосом или кнопкой). После изменения — «Готово.» и обновлённый список.
Сообщения короткие, без смайликов и подсказок.
"""

from __future__ import annotations

import logging
import re
from functools import partial
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.commands import ApplyResult, apply_plan, normalize_team
from bot.config import Settings
from bot.focus import FocusStore
from bot.keyboards import DraftCb, draft_keyboard, main_menu
from bot.models import Action, ActionType, Task, TaskDraft
from bot.numbering import Numbering
from bot.repository import TaskRepository
from bot.scheduler import ReminderService
from bot.services.task_parser import TaskParser, TaskParserError
from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError
from bot.tasklist import card_text, list_messages, load_tasks, renumber
from bot.utils import drop_keyboard, safe_edit, today_in, transcribe_message
from bot.views import plan_text

log = logging.getLogger(__name__)
router = Router(name="create")

NOT_UNDERSTOOD = "Не понял. Скажите ещё раз."

# Короткие ответы на вопрос перед сохранением. Сравниваются целиком, поэтому
# «нет, перенеси на среду» — это уже новая команда, а не просто «нет».
YES = {
    "да", "ага", "угу", "верно", "правильно", "все верно", "да верно", "да правильно", "да все верно",
    "сохрани", "сохраняй", "да сохрани", "сохранить", "подтверждаю", "ок", "окей", "хорошо", "давай",
    "да давай", "точно", "конечно", "да конечно", "выполняй", "делай", "да делай", "так", "все так",
    "добавь", "да добавь", "удали", "да удали", "измени", "да измени",
}  # fmt: skip
NO = {
    "нет", "не", "неверно", "неправильно", "не так", "нет не так", "нет неправильно", "отмена",
    "отмени", "отменить", "не надо", "не нужно", "не сохраняй", "нет не надо",
}  # fmt: skip


def _short_answer(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
    return " ".join(cleaned.split())


class CreateTask(StatesGroup):
    confirm = State()


# Новое сообщение можно прислать в обычном режиме или поверх ещё не подтверждённого вопроса.
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
    reminder: ReminderService,
    numbering: Numbering,
) -> None:
    status = await message.answer("Минутку…")
    try:
        text = await transcribe_message(message, bot, transcriber)
    except TranscriptionError as exc:
        log.warning("Ошибка распознавания: %s", exc)
        await status.edit_text("Не расслышал. Скажите ещё раз.")
        return
    log.info("Голосовое: %s", text)
    await _handle(
        status, state, bot, parser, repo, settings, focus, reminder, numbering, text=text, source="voice"
    )


@router.message(_INPUT, F.text, ~F.text.startswith("/"))
async def on_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    parser: TaskParser,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
    reminder: ReminderService,
    numbering: Numbering,
) -> None:
    status = await message.answer("Минутку…")
    await _handle(
        status,
        state,
        bot,
        parser,
        repo,
        settings,
        focus,
        reminder,
        numbering,
        text=message.text or "",
        source="text",
    )


async def _context_tasks(repo: TaskRepository) -> list[Task]:
    """Задачи, среди которых нейросеть ищет ту, о которой речь."""
    try:
        return await load_tasks(repo)
    except Exception:
        log.exception("Не удалось загрузить задачи для разбора команды")
        return []


async def _handle(
    status: Message,
    state: FSMContext,
    bot: Bot,
    parser: TaskParser,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
    reminder: ReminderService,
    numbering: Numbering,
    *,
    text: str,
    source: str,
) -> None:
    today = today_in(settings.timezone)
    chat_id = status.chat.id

    # Ответ «да» / «нет» на вопрос перед сохранением.
    if await state.get_state() == CreateTask.confirm.state:
        answer = _short_answer(text)
        if answer in YES or answer in NO:
            data = await state.get_data()
            await state.clear()
            await drop_keyboard(bot, chat_id, data.get("draft_msg_id"))
            if answer in YES:
                await _execute(
                    status, state, data, repo, settings, focus, reminder, numbering, user_id=chat_id
                )
            else:
                await safe_edit(status, "Хорошо, ничего не меняю.")
            return

    context = await _context_tasks(repo)
    tasks_by_id = {t.id: t for t in context}
    if not numbering.has(chat_id):  # список ещё не показывали (например, после перезапуска)
        renumber(numbering, chat_id, context)
    try:
        actions = await parser.interpret(
            text,
            today,
            context,
            focus.get(chat_id),
            schedule=reminder.schedule.describe(),
            numbers=numbering.numbers(chat_id, tasks_by_id),
        )
    except TaskParserError as exc:
        log.warning("Не удалось разобрать сообщение через нейросеть: %s", exc)
        actions = [Action.create(TaskDraft(title=text.strip()))]
    actions = normalize_team(actions, settings.team_members)

    if not actions:
        await safe_edit(status, NOT_UNDERSTOOD)
        return

    mutations = [a for a in actions if a.is_mutating]
    views: list[str] = []
    for action in actions:
        if not action.is_mutating:
            views.extend(await _render(action, repo, settings, focus, numbering, chat_id, tasks_by_id))

    if mutations:
        # Старый неподтверждённый вопрос (если был) заменяется новым — убираем его кнопки.
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
        number = partial(numbering.number, chat_id)
        await safe_edit(status, plan_text(mutations, tasks_by_id, today, number), draft_keyboard())
        for view in views:
            await status.answer(view)
        return

    first, *rest = views
    await safe_edit(status, first)
    for view in rest:
        await status.answer(view)


async def _render(
    action: Action,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
    numbering: Numbering,
    chat_id: int,
    tasks_by_id: dict[int, Task],
) -> list[str]:
    """Ответ на «покажи»: список, задача или уточняющий вопрос."""
    if action.type in (ActionType.SHOW_TASKS, ActionType.SHOW_DASHBOARD):
        return await list_messages(
            repo, settings, numbering, chat_id, action.filter or "active", action.assignee
        )

    if action.type == ActionType.OPEN_TASK:
        task = await repo.get(action.task_ids[0])
        if task is None:
            return [f"Задачи {numbering.number(chat_id, action.task_ids[0])} уже нет."]
        focus.set(chat_id, task.id)
        return [card_text(task, settings, numbering.number(chat_id, task.id))]

    # unclear: переспрашиваем и перечисляем подходящие задачи
    question = escape(action.question or "Не понял, какая задача. Назовите номер.")
    candidates = [tasks_by_id[i] for i in action.task_ids if i in tasks_by_id]
    lines = [question] + [f"{numbering.number(chat_id, t.id)}. {escape(t.title)}" for t in candidates]
    return ["\n\n".join(lines)]


def _result_text(result: ApplyResult, reminder: ReminderService) -> str:
    parts = ["Готово."] if not result.nothing_done else []
    if result.schedule_changed:
        parts = [f"Готово. Список будет приходить {reminder.schedule.describe()}."]
    parts += result.problems
    return "\n\n".join(parts)


async def _execute(
    message: Message,
    state: FSMContext,
    data: dict,
    repo: TaskRepository,
    settings: Settings,
    focus: FocusStore,
    reminder: ReminderService,
    numbering: Numbering,
    *,
    user_id: int,
) -> bool:
    """Выполняет подтверждённый план: «Готово.» и обновлённый список. False — если ничего не вышло."""
    chat_id = message.chat.id
    plan = [Action.from_dict(d) for d in data["plan"]]
    result = await apply_plan(
        repo,
        plan,
        source=data.get("source", "text"),
        raw_text=data.get("raw_text"),
        created_by=user_id,
        reminder=reminder,
        number=partial(numbering.number, chat_id),
    )
    if result.nothing_done and result.errors:
        await state.set_state(CreateTask.confirm)  # ничего не применилось — можно ответить «да» ещё раз
        await state.set_data(data)
        await safe_edit(message, "Не получилось сохранить. Скажите «да» ещё раз.")
        return False

    for task_id in result.deleted:
        focus.drop(chat_id, task_id)
    if len(result.touched) == 1:
        focus.set(chat_id, result.touched[0].id)
    await safe_edit(message, _result_text(result, reminder))

    if result.created or result.updated or result.deleted:
        for text in await list_messages(repo, settings, numbering, chat_id):
            await message.answer(text)
    return True


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
    settings: Settings,
    focus: FocusStore,
    reminder: ReminderService,
    numbering: Numbering,
) -> None:
    data = await _take_plan(callback, state)
    if data is None:
        await callback.answer("Это уже неактуально", show_alert=True)
        if callback.message:
            await drop_keyboard(bot, callback.message.chat.id, callback.message.message_id)
        return
    await _execute(
        callback.message,
        state,
        data,
        repo,
        settings,
        focus,
        reminder,
        numbering,
        user_id=callback.from_user.id,
    )
    await callback.answer()


@router.callback_query(DraftCb.filter(F.action == "cancel"))
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await _take_plan(callback, state)
    await safe_edit(callback.message, "Хорошо, ничего не меняю.")
    await callback.answer()


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Скажите голосом, что нужно сделать.", reply_markup=main_menu())
