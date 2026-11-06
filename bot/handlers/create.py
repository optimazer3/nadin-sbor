"""Создание задач: голосовое/текст -> распознавание -> «Я правильно понял?» -> сохранение."""

from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import Settings
from bot.keyboards import DraftCb, TaskCb, draft_keyboard
from bot.models import TaskDraft
from bot.repository import TaskRepository
from bot.services.task_parser import TaskParser, TaskParserError
from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError
from bot.utils import drop_keyboard, safe_edit, today_in, transcribe_message
from bot.views import draft_text

log = logging.getLogger(__name__)
router = Router(name="create")


class CreateTask(StatesGroup):
    confirm = State()


# Новую задачу можно прислать в обычном режиме или поверх ещё не подтверждённого черновика.
_INPUT = StateFilter(None, CreateTask.confirm)


@router.message(_INPUT, F.voice | F.audio | F.video_note)
async def on_voice(
    message: Message,
    state: FSMContext,
    bot: Bot,
    transcriber: AssemblyAITranscriber,
    parser: TaskParser,
    settings: Settings,
) -> None:
    status = await message.answer("🎧 Распознаю голосовое…")
    try:
        text = await transcribe_message(message, bot, transcriber)
    except TranscriptionError as exc:
        log.warning("Ошибка распознавания: %s", exc)
        await status.edit_text(
            f"😕 Не получилось распознать голосовое: {escape(str(exc))}\n\n"
            "Попробуйте записать ещё раз или напишите задачу текстом."
        )
        return
    await status.edit_text(f"🎧 Распознано: <i>«{escape(text)}»</i>\n\n🤔 Разбираю задачу…")
    await _propose(status, state, bot, parser, settings, text=text, source="voice")


@router.message(_INPUT, F.text, ~F.text.startswith("/"))
async def on_text(
    message: Message, state: FSMContext, bot: Bot, parser: TaskParser, settings: Settings
) -> None:
    status = await message.answer("🤔 Разбираю задачу…")
    await _propose(status, state, bot, parser, settings, text=message.text or "", source="text")


async def _propose(
    status: Message,
    state: FSMContext,
    bot: Bot,
    parser: TaskParser,
    settings: Settings,
    *,
    text: str,
    source: str,
) -> None:
    today = today_in(settings.timezone)
    recognized = text if source == "voice" else None
    note = ""
    try:
        drafts = await parser.parse_tasks(text, today)
    except TaskParserError as exc:
        log.warning("Не удалось разобрать задачу через нейросеть: %s", exc)
        drafts = [TaskDraft(title=text.strip())]
        note = (
            "⚠️ Автоматический разбор сейчас недоступен — сохраню текст как есть. "
            "Исполнителя и срок можно будет указать в карточке задачи.\n\n"
        )

    # Старый черновик (если был) больше не подтверждается — убираем его кнопки.
    previous = await state.get_data()
    if await state.get_state() == CreateTask.confirm.state:
        await drop_keyboard(bot, status.chat.id, previous.get("draft_msg_id"))

    if not drafts:
        await state.clear()
        heard = f"🎧 Распознано: <i>«{escape(text)}»</i>\n\n" if recognized else ""
        await status.edit_text(
            f"{heard}🤷 Не нашёл в сообщении задачу.\n"
            "Скажите, что нужно сделать, кому и к какому сроку — например: "
            "<i>«Марии обновить прайс до среды»</i>."
        )
        return

    await state.set_state(CreateTask.confirm)
    await state.set_data(
        {
            "draft": [d.to_dict() for d in drafts],
            "source": source,
            "raw_text": text,
            "draft_msg_id": status.message_id,
        }
    )
    await status.edit_text(note + draft_text(drafts, today, recognized), reply_markup=draft_keyboard())


async def _take_draft(callback: CallbackQuery, state: FSMContext) -> dict | None:
    """Забирает черновик, если кнопка нажата на актуальном сообщении. Иначе None."""
    data = await state.get_data()
    is_current = (
        await state.get_state() == CreateTask.confirm.state
        and callback.message is not None
        and data.get("draft_msg_id") == callback.message.message_id
    )
    if not is_current:
        return None
    # Сразу очищаем состояние, чтобы двойное нажатие не создало задачу дважды.
    await state.clear()
    return data


@router.callback_query(DraftCb.filter(F.action == "save"))
async def on_save(
    callback: CallbackQuery, state: FSMContext, bot: Bot, repo: TaskRepository, settings: Settings
) -> None:
    data = await _take_draft(callback, state)
    if data is None:
        await callback.answer("Этот черновик уже неактуален", show_alert=True)
        if callback.message:
            await drop_keyboard(bot, callback.message.chat.id, callback.message.message_id)
        return

    drafts = [TaskDraft.from_dict(d) for d in data["draft"]]
    try:
        tasks = await repo.create_many(
            drafts,
            source=data.get("source", "text"),
            raw_text=data.get("raw_text"),
            created_by=callback.from_user.id,
        )
    except Exception:
        log.exception("Не удалось сохранить задачи")
        await state.set_state(CreateTask.confirm)  # вернём черновик, чтобы можно было повторить
        await state.set_data(data)
        await callback.answer("Не удалось сохранить в базу. Попробуйте ещё раз.", show_alert=True)
        return

    if len(tasks) == 1:
        head = "✅ Задача сохранена"
    else:
        head = f"✅ Сохранено задач: {len(tasks)}"
    lines = [f"#{t.id} {escape(t.title)}" for t in tasks]
    buttons = [
        [InlineKeyboardButton(text=f"Открыть #{t.id}", callback_data=TaskCb(action="open", id=t.id).pack())]
        for t in tasks[:8]
    ]
    await safe_edit(
        callback.message,
        head + "\n\n" + "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer("Сохранено")


@router.callback_query(DraftCb.filter(F.action == "cancel"))
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await _take_draft(callback, state)
    raw = (data or {}).get("raw_text")
    text = "❌ Не сохранил.\n\nЗапишите голосовое ещё раз или напишите задачу текстом — я разберу заново."
    if raw:
        text += f"\n\nМожно скопировать текст ниже, поправить и отправить мне:\n<code>{escape(raw)}</code>"
    await safe_edit(callback.message, text)
    await callback.answer()


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Пришлите голосовое или текст задачи. Список задач — кнопка «📋 Список задач» или /tasks."
    )
