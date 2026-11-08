"""Голосовые и текстовые сообщения руководителя — основной способ управления ботом.

Голосовое → распознавание → нейросеть понимает, что нужно: создать задачи, изменить или удалить
существующие, показать список / сводку / задачу, поменять время еженедельной сводки.
Всё, что меняет данные, бот сначала показывает и спрашивает «Я правильно понял?».
Ответить можно голосом («да» / «нет») или кнопкой. Показать — выполняется сразу.
"""

from __future__ import annotations

import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from bot.commands import apply_plan, normalize_team
from bot.config import Settings
from bot.focus import FocusStore
from bot.handlers.tasks import card_text, list_messages
from bot.keyboards import DraftCb, draft_keyboard, main_menu
from bot.models import Action, ActionType, Task, TaskDraft
from bot.repository import TaskRepository
from bot.scheduler import ReminderService, dashboard_messages
from bot.services.task_parser import TaskParser, TaskParserError
from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError
from bot.utils import drop_keyboard, safe_edit, today_in, transcribe_message
from bot.views import HELP_COMMANDS, apply_result_text, plan_text

log = logging.getLogger(__name__)
router = Router(name="create")

# Сколько недавно закрытых задач показывать нейросети, чтобы работало «верни задачу в работу».
RECENT_ARCHIVED = 20

# Короткие ответы на «Я правильно понял?». Сравниваются целиком, поэтому
# «нет, перенеси на среду» — это уже новая команда, а не просто «нет».
YES = {
    "да", "ага", "угу", "верно", "правильно", "все верно", "да верно", "да правильно", "да все верно",
    "сохрани", "сохраняй", "да сохрани", "сохранить", "подтверждаю", "ок", "окей", "хорошо", "давай",
    "да давай", "точно", "конечно", "да конечно", "выполняй", "делай", "да делай", "так", "все так",
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
    reminder: ReminderService,
) -> None:
    status = await message.answer("🎧 Слушаю…")
    try:
        text = await transcribe_message(message, bot, transcriber)
    except TranscriptionError as exc:
        log.warning("Ошибка распознавания: %s", exc)
        await status.edit_text(
            f"😕 Не расслышал. Попробуйте сказать ещё раз.\n\n<i>Техническая причина: {escape(str(exc))}</i>"
        )
        return
    await status.edit_text(f"🎧 Вы сказали: <i>«{escape(text)}»</i>\n\n🤔 Думаю…")
    await _handle(status, state, bot, parser, repo, settings, focus, reminder, text=text, source="voice")


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
) -> None:
    status = await message.answer("🤔 Думаю…")
    await _handle(
        status, state, bot, parser, repo, settings, focus, reminder, text=message.text or "", source="text"
    )


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
    reminder: ReminderService,
    *,
    text: str,
    source: str,
) -> None:
    today = today_in(settings.timezone)
    chat_id = status.chat.id
    recognized = text if source == "voice" else None
    heard = f"🎧 Вы сказали: <i>«{escape(text)}»</i>\n\n" if recognized else ""

    # Ответ «да» / «нет» на вопрос «Я правильно понял?»
    if await state.get_state() == CreateTask.confirm.state:
        answer = _short_answer(text)
        if answer in YES or answer in NO:
            data = await state.get_data()
            await state.clear()
            await drop_keyboard(bot, chat_id, data.get("draft_msg_id"))
            if answer in YES:
                ok = await _execute(status, state, data, repo, focus, reminder, user_id=chat_id, prefix=heard)
                if not ok:
                    await safe_edit(status, heard + "⚠️ Не получилось сохранить. Скажите «да» ещё раз.")
            else:
                await safe_edit(status, heard + _cancel_text(data.get("raw_text")))
            return

    context = await _context_tasks(repo)
    tasks_by_id = {t.id: t for t in context}
    note = ""
    try:
        actions = await parser.interpret(
            text, today, context, focus.get(chat_id), schedule=reminder.schedule.describe()
        )
    except TaskParserError as exc:
        log.warning("Не удалось разобрать сообщение через нейросеть: %s", exc)
        actions = [Action.create(TaskDraft(title=text.strip()))]
        note = "⚠️ Сейчас я не могу разобрать сообщение подробно — запишу его как новую задачу.\n\n"
    actions = normalize_team(actions, settings.team_members)

    if not actions:
        await safe_edit(
            status, f"{heard}🤷 Не понял, что нужно сделать. Скажите, например:\n\n{HELP_COMMANDS}"
        )
        return

    mutations = [a for a in actions if a.is_mutating]
    views: list[str] = []
    for action in actions:
        if not action.is_mutating:
            views.extend(await _render(action, repo, settings, focus, reminder, chat_id, tasks_by_id, heard))

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
        await safe_edit(status, note + plan_text(mutations, tasks_by_id, today, recognized), draft_keyboard())
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
    reminder: ReminderService,
    chat_id: int,
    tasks_by_id: dict[int, Task],
    heard: str,
) -> list[str]:
    """Ответ на команды «показать»: список, сводка, задача, уточняющий вопрос."""
    if action.type == ActionType.SHOW_DASHBOARD:
        return await dashboard_messages(repo, settings, reminder)

    if action.type == ActionType.SHOW_TASKS:
        return await list_messages(repo, settings, action.filter or "active", action.assignee)

    if action.type == ActionType.OPEN_TASK:
        task = await repo.get(action.task_ids[0])
        if task is None:
            return [f"Задачи №{action.task_ids[0]} нет — возможно, её удалили."]
        focus.set(chat_id, task.id)
        return [card_text(task, settings)]

    # unclear: переспрашиваем и перечисляем подходящие задачи
    question = action.question or "Не понял, о какой задаче речь."
    candidates = [tasks_by_id[i] for i in action.task_ids if i in tasks_by_id]
    lines = [f"{heard}🤔 {escape(question)}"]
    if candidates:
        lines.append("")
        lines += [
            f"<b>№{t.id}</b>  {escape(t.title)}" + (f" — {escape(t.assignee)}" if t.assignee else "")
            for t in candidates
        ]
        lines.append(f"\nСкажите с номером, например: <i>«задача {candidates[0].id} выполнена»</i>.")
    else:
        lines.append("\nСкажите с номером задачи. Номера видно по кнопке «📋 Задачи».")
    return ["\n".join(lines)]


def _cancel_text(raw: str | None) -> str:
    text = "❌ Хорошо, ничего не сохраняю.\n\nСкажите ещё раз, как нужно."
    if raw:
        text += f"\n\nВаш текст, если нужно скопировать и поправить:\n<code>{escape(raw)}</code>"
    return text


async def _execute(
    message: Message,
    state: FSMContext,
    data: dict,
    repo: TaskRepository,
    focus: FocusStore,
    reminder: ReminderService,
    *,
    user_id: int,
    prefix: str = "",
) -> bool:
    """Выполняет подтверждённый план и пишет итог в message. False — если ничего не вышло."""
    plan = [Action.from_dict(d) for d in data["plan"]]
    result = await apply_plan(
        repo,
        plan,
        source=data.get("source", "text"),
        raw_text=data.get("raw_text"),
        created_by=user_id,
        reminder=reminder,
    )
    if result.nothing_done and result.errors:
        await state.set_state(CreateTask.confirm)  # ничего не применилось — можно подтвердить ещё раз
        await state.set_data(data)
        return False

    chat_id = message.chat.id
    for task_id in result.deleted:
        focus.drop(chat_id, task_id)
    if len(result.touched) == 1:
        focus.set(chat_id, result.touched[0].id)
    only_creates = all(a.type == ActionType.CREATE for a in plan)
    await safe_edit(message, prefix + apply_result_text(result.lines, result.created, only_creates))
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
    focus: FocusStore,
    reminder: ReminderService,
) -> None:
    data = await _take_plan(callback, state)
    if data is None:
        await callback.answer("Это уже неактуально", show_alert=True)
        if callback.message:
            await drop_keyboard(bot, callback.message.chat.id, callback.message.message_id)
        return
    if await _execute(callback.message, state, data, repo, focus, reminder, user_id=callback.from_user.id):
        await callback.answer("Готово")
    else:
        await callback.answer("Не получилось сохранить. Нажмите «Да» ещё раз.", show_alert=True)


@router.callback_query(DraftCb.filter(F.action == "cancel"))
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await _take_plan(callback, state)
    await safe_edit(callback.message, _cancel_text((data or {}).get("raw_text")))
    await callback.answer()


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Нажмите на микрофон внизу и скажите, что нужно сделать.", reply_markup=main_menu())
