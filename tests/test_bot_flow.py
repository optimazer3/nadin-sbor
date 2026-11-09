"""Сквозные сценарии: апдейты прогоняются через настоящий Dispatcher, Telegram подменён."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from itertools import count
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetFile,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, File, InlineKeyboardMarkup, Message, Update, User, Voice
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import Settings
from bot.focus import FocusStore
from bot.handlers import get_routers
from bot.keyboards import BTN_TASKS, DraftCb
from bot.main import build_dispatcher
from bot.models import Action, ActionType, Status, TaskDraft
from bot.numbering import Numbering
from bot.repository import InMemoryTaskRepository
from bot.scheduler import ReminderService, send_weekly_reminder
from bot.services.task_parser import TaskParserError
from bot.services.transcriber import TranscriptionError

ADMIN = 111
STRANGER = 999


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._ids = count(1000)

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            message_id = next(self._ids) if isinstance(method, SendMessage) else method.message_id
            return Message(
                message_id=message_id,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
                # В ответе Telegram бывают только inline-кнопки сообщения.
                reply_markup=method.reply_markup
                if isinstance(method.reply_markup, InlineKeyboardMarkup)
                else None,
            ).as_(bot)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="voice/1.oga").as_(bot)
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b"OGG-AUDIO"

    # --- помощники для проверок ---
    def sent(self, kind=(SendMessage, EditMessageText)):
        return [c for c in self.calls if isinstance(c, kind)]

    def last_text(self) -> str:
        return self.sent()[-1].text

    def last_markup(self):
        return self.sent()[-1].reply_markup

    def buttons(self) -> list[tuple[str, str]]:
        markup = self.last_markup()
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]

    def toasts(self) -> list[str | None]:
        return [c.text for c in self.calls if isinstance(c, AnswerCallbackQuery)]


class FakeTranscriber:
    def __init__(self, text="Ване подготовить отчёт до пятницы"):
        self.text = text
        self.audio: list[bytes] = []

    async def transcribe(self, audio: bytes) -> str:
        self.audio.append(audio)
        return self.text


class FakeParser:
    """Вместо нейросети: по умолчанию «понимает» сообщение как создание задач из self.drafts.

    Чтобы проверить команды, задайте self.actions — тогда вернутся они.
    """

    def __init__(self):
        self.drafts = [TaskDraft("Подготовить отчёт", "Иван", date(2026, 11, 13))]
        self.actions: list[Action] | None = None
        self.fail = False
        self.date_answer: date | None = None
        self.texts: list[str] = []
        self.contexts: list[list[int]] = []
        self.focus_ids: list[int | None] = []
        self.schedules: list[str | None] = []
        self.numbers: list[dict[int, int]] = []

    async def interpret(self, text: str, today: date, tasks=(), focus_id=None, schedule=None, numbers=None):
        self.texts.append(text)
        self.numbers.append(dict(numbers or {}))
        self.schedules.append(schedule)
        self.contexts.append([t.id for t in tasks])
        self.focus_ids.append(focus_id)
        if self.fail:
            raise TaskParserError("нет связи")
        if self.actions is not None:
            return [Action.from_dict(a.to_dict()) for a in self.actions]
        return [Action.create(d) for d in self.drafts]

    async def parse_date(self, text: str, today: date):
        return self.date_answer


def make_settings(**overrides) -> Settings:
    values = dict(
        bot_token="42:TEST",
        admin_ids=frozenset({ADMIN}),
        supabase_url="",
        supabase_key="",
        assemblyai_api_key="",
        assemblyai_base_url="",
        assemblyai_language="ru",
        assemblyai_speech_model=None,
        llm_api_key="",
        llm_base_url="",
        llm_model="openai/gpt-4.1-mini",
        team_members=("Иван", "Мария"),
        timezone=ZoneInfo("Europe/Moscow"),
        reminder_day="mon",
        reminder_time=time(10, 0),
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def env(tmp_path):
    settings = make_settings()
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    repo = InMemoryTaskRepository()
    transcriber = FakeTranscriber()
    parser = FakeParser()
    focus = FocusStore()
    reminder = ReminderService(settings, path=tmp_path / "schedule.json")
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    numbering = Numbering()
    reminder.attach(scheduler, bot, repo, numbering)
    dp = build_dispatcher(
        settings,
        repo=repo,
        transcriber=transcriber,
        parser=parser,
        focus=focus,
        reminder=reminder,
        numbering=numbering,
    )

    class Env:
        pass

    e = Env()
    e.settings, e.session, e.bot, e.repo, e.transcriber, e.parser, e.dp = (
        settings,
        session,
        bot,
        repo,
        transcriber,
        parser,
        dp,
    )
    e.focus = focus
    e.numbering = numbering
    e.reminder, e.scheduler, e.schedule_file = reminder, scheduler, tmp_path / "schedule.json"
    e._uid = count(1)
    e._mid = count(1)

    def user(uid):
        return User(id=uid, is_bot=False, first_name="U")

    async def send(text=None, voice=False, uid=ADMIN):
        msg = Message(
            message_id=next(e._mid),
            date=datetime.now(),
            chat=Chat(id=uid, type="private"),
            from_user=user(uid),
            text=text,
            voice=Voice(file_id="v1", file_unique_id="v1u", duration=3) if voice else None,
        )
        await dp.feed_update(bot, Update(update_id=next(e._uid), message=msg))

    async def press(data: str, message_id: int, uid=ADMIN):
        """Нажатие inline-кнопки на сообщении бота с данным message_id."""
        msg = Message(message_id=message_id, date=datetime.now(), chat=Chat(id=uid, type="private"), text="x")
        cq = CallbackQuery(
            id=str(next(e._uid)), from_user=user(uid), chat_instance="ci", message=msg, data=data
        )
        await dp.feed_update(bot, Update(update_id=next(e._uid), callback_query=cq))

    e.send, e.press = send, press
    yield e
    # Роутеры объявлены на уровне модулей и могут быть подключены только к одному
    # Dispatcher — отцепляем их, чтобы следующий тест собрал диспетчер заново.
    for router in get_routers():
        router._parent_router = None


def last_bot_message_id(session: FakeSession) -> int:
    """id сообщения, которое бот последним отправил или отредактировал."""
    call = session.sent()[-1]
    if isinstance(call, EditMessageText):
        return call.message_id
    return 1000 + sum(isinstance(c, SendMessage) for c in session.calls) - 1


EMOJI = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u23e9-\u23fa]")


def inline_buttons(call) -> list[str]:
    markup = call.reply_markup
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def texts(env) -> list[str]:
    return [c.text for c in env.session.sent()]


async def _create(env, title="Отчёт", assignee="Иван", deadline=date(2026, 11, 13)):
    [task] = await env.repo.create_many(
        [TaskDraft(title, assignee, deadline)], source="text", raw_text=title, created_by=ADMIN
    )
    return task


async def _two_tasks(env):
    report = await _create(env, "Подготовить отчёт", "Иван", date(2026, 11, 13))
    price = await _create(env, "Обновить прайс", "Мария", date(2026, 11, 11))
    return report, price


@pytest.fixture(autouse=True)
def no_emoji_anywhere(env):
    """Во всех сценариях: ни одного смайлика ни в одном сообщении бота."""
    yield
    for text in texts(env):
        assert not EMOJI.search(text or ""), text


# ---------- вход ----------


async def test_stranger_gets_id_and_no_access(env):
    await env.send("/start", uid=STRANGER)
    assert str(STRANGER) in env.session.last_text()
    assert "только для руководителя" in env.session.last_text()
    await env.send("Сделать что-то", uid=STRANGER)
    assert env.repo.tasks == {}


async def test_start(env):
    await env.send("/start")
    call = env.session.sent()[-1]
    assert "Нажмите на микрофон" in call.text and "каждый понедельник в 10:00" in call.text
    assert [[b.text for b in row] for row in call.reply_markup.keyboard] == [[BTN_TASKS]]


async def test_list_button(env):
    report, price = await _two_tasks(env)
    await env.send(BTN_TASKS)
    call = env.session.sent()[-1]
    assert call.text == (
        "<b>Задачи на контроле</b>\n\n"
        "1. Подготовить отчёт\nИван, до 13 ноября\n\n"
        "2. Обновить прайс\nМария, до 11 ноября"
    )
    assert inline_buttons(call) == []


# ---------- добавление ----------


async def test_voice_create_confirm_with_button(env):
    await env.send(voice=True)
    assert env.transcriber.audio == [b"OGG-AUDIO"]
    call = env.session.sent()[-1]
    assert call.text == "Добавить задачу?\n\nПодготовить отчёт\nИван, до 13 ноября"
    assert inline_buttons(call) == ["Да", "Нет"]
    plan_msg = last_bot_message_id(env.session)

    await env.press(DraftCb(action="save").pack(), plan_msg)
    [task] = env.repo.tasks.values()
    assert (task.title, task.assignee, task.deadline, task.source) == (
        "Подготовить отчёт",
        "Иван",
        date(2026, 11, 13),
        "voice",
    )
    # «Готово.» — и сразу обновлённый список.
    assert texts(env)[-2] == "Готово."
    assert texts(env)[-1] == "<b>Задачи на контроле</b>\n\n1. Подготовить отчёт\nИван, до 13 ноября"

    await env.press(DraftCb(action="save").pack(), plan_msg)  # повторное нажатие — без дубля
    assert len(env.repo.tasks) == 1
    assert "неактуально" in env.session.toasts()[-1]


@pytest.mark.parametrize("answer", ["Да", "да, всё верно!", "Сохрани."])
async def test_confirm_by_voice_yes(env, answer):
    await env.send(voice=True)
    plan_msg = last_bot_message_id(env.session)
    env.transcriber.text = answer
    await env.send(voice=True)
    assert len(env.repo.tasks) == 1
    assert texts(env)[-2] == "Готово."
    assert env.parser.texts == ["Ване подготовить отчёт до пятницы"]  # «да» нейросеть не спрашивали
    assert any(isinstance(c, EditMessageReplyMarkup) and c.message_id == plan_msg for c in env.session.calls)


async def test_cancel_by_voice_no(env):
    await env.send(voice=True)
    env.transcriber.text = "Нет"
    await env.send(voice=True)
    assert env.repo.tasks == {}
    assert env.session.last_text() == "Хорошо, ничего не меняю."
    env.transcriber.text = "да"  # после «нет» «да» уже ничего не подтверждает
    env.parser.drafts = []
    await env.send(voice=True)
    assert env.repo.tasks == {}


async def test_no_with_correction_is_a_new_command(env):
    await env.send("Ивану отчёт")
    env.parser.drafts = [TaskDraft("Подготовить отчёт", "Иван", date(2026, 11, 11))]
    await env.send("нет, срок до среды")
    assert env.parser.texts[-1] == "нет, срок до среды"
    assert "до 11 ноября" in env.session.last_text()


async def test_cancel_by_button(env):
    await env.send("Купить воду")
    await env.press(DraftCb(action="cancel").pack(), last_bot_message_id(env.session))
    assert env.repo.tasks == {}
    assert env.session.last_text() == "Хорошо, ничего не меняю."


async def test_parser_failure_falls_back_to_raw_text(env):
    env.parser.fail = True
    await env.send("Купить воду в офис")
    assert env.session.last_text() == "Добавить задачу?\n\nКупить воду в офис"
    await env.send("да")
    assert env.repo.tasks[1].title == "Купить воду в офис"


async def test_nothing_understood(env):
    env.parser.drafts = []
    await env.send("привет")
    assert env.session.last_text() == "Не понял. Скажите ещё раз."


async def test_voice_not_heard(env, monkeypatch):
    async def broken(audio):
        raise TranscriptionError("timeout")

    monkeypatch.setattr(env.transcriber, "transcribe", broken)
    await env.send(voice=True)
    assert env.session.last_text() == "Не расслышал. Скажите ещё раз."


async def test_multiple_tasks_saved(env):
    env.parser.drafts = [TaskDraft("A", "Иван"), TaskDraft("B", "Мария")]
    await env.send("Ивану A, Марии B")
    assert env.session.last_text() == (
        "Я правильно понял?\n\n1. Добавить задачу\nA\nИван\n\n2. Добавить задачу\nB\nМария"
    )
    await env.send("да")
    assert [t.title for t in env.repo.tasks.values()] == ["A", "B"]


# ---------- изменение и удаление ----------


async def test_mark_done_leaves_the_list(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    env.transcriber.text = "задача один выполнена"
    await env.send(voice=True)
    assert sorted(env.parser.contexts[-1]) == [1, 2]
    assert env.session.last_text() == "Отметить задачу 1 выполненной?\n\nПодготовить отчёт"
    assert env.repo.tasks[report.id].status == Status.NEW  # пока не подтвердили — не меняем

    env.transcriber.text = "да"
    await env.send(voice=True)
    assert env.repo.tasks[report.id].status == Status.DONE
    assert texts(env)[-1] == "<b>Задачи на контроле</b>\n\n1. Обновить прайс\nМария, до 11 ноября"


async def test_update_shows_old_and_new(env):
    _, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.UPDATE, task_ids=[price.id], deadline=date(2026, 11, 20), assignee="иван")
    ]
    await env.send("перенеси прайс на 20 ноября и отдай Ивану")
    assert env.session.last_text() == (
        "Изменить задачу 2?\n\nОбновить прайс\nИсполнитель: Мария → Иван\nСрок: до 11 ноября → до 20 ноября"
    )
    await env.send("да")
    task = env.repo.tasks[price.id]
    assert (task.assignee, task.deadline) == ("Иван", date(2026, 11, 20))


async def test_delete(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.DELETE, task_ids=[price.id])]
    await env.send("удали задачу два")
    assert env.session.last_text() == "Удалить задачу 2?\n\nОбновить прайс"
    await env.send("нет")
    assert price.id in env.repo.tasks
    await env.send("удали задачу два")
    await env.send("да")
    assert price.id not in env.repo.tasks and report.id in env.repo.tasks


async def test_numbers_follow_the_shown_list(env):
    a = await _create(env, "Первая", "Иван")
    b = await _create(env, "Вторая", "Мария")
    c = await _create(env, "Третья", "Иван")
    await env.send(BTN_TASKS)
    assert "1. Первая" in env.session.last_text() and "3. Третья" in env.session.last_text()

    env.parser.actions = [Action(ActionType.DELETE, task_ids=[a.id])]
    await env.send("удали задачу 1")
    assert env.parser.numbers[-1] == {a.id: 1, b.id: 2, c.id: 3}
    await env.send("да")
    # После изменения показан новый список — и номера снова 1, 2.
    assert texts(env)[-1] == (
        "<b>Задачи на контроле</b>\n\n1. Вторая\nМария, до 13 ноября\n\n2. Третья\nИван, до 13 ноября"
    )
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[c.id], status=Status.DONE)]
    await env.send("задача 2 выполнена")
    assert env.parser.numbers[-1][c.id] == 2
    assert env.session.last_text() == "Отметить задачу 2 выполненной?\n\nТретья"


async def test_mixed_commands(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.CREATE, title="Позвонить клиенту", assignee="Мария"),
        Action(ActionType.UPDATE, task_ids=[report.id], status=Status.IN_PROGRESS),
        Action(ActionType.DELETE, task_ids=[price.id]),
    ]
    await env.send("Маше позвонить клиенту, отчёт в работу, прайс удали")
    assert env.session.last_text() == (
        "Я правильно понял?\n\n"
        "1. Добавить задачу\nПозвонить клиенту\nМария\n\n"
        "2. Взять задачу 1 в работу\nПодготовить отчёт\n\n"
        "3. Удалить задачу 2\nОбновить прайс"
    )
    await env.send("да")
    assert {t.title for t in env.repo.tasks.values()} == {"Подготовить отчёт", "Позвонить клиенту"}
    assert env.repo.tasks[report.id].status == Status.IN_PROGRESS


async def test_open_task_then_delete_it(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.OPEN_TASK, task_ids=[report.id])]
    await env.send("открой задачу один")
    assert env.session.last_text() == "<b>Задача 1</b>\n\nПодготовить отчёт\nИван, до 13 ноября"
    env.parser.actions = [Action(ActionType.DELETE, task_ids=[report.id])]
    await env.send("удали её")
    assert env.parser.focus_ids[-1] == report.id
    await env.send("да")
    assert report.id not in env.repo.tasks
    assert env.focus.get(ADMIN) is None


async def test_task_deleted_between_question_and_answer(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    await env.send("отчёт готов")
    await env.repo.delete(report.id)
    await env.send("да")
    assert "Задачи 1 уже нет." in env.session.last_text()


# ---------- показать ----------


async def test_show_commands(env):
    report, price = await _two_tasks(env)
    for kind in (ActionType.SHOW_DASHBOARD, ActionType.SHOW_TASKS):
        env.parser.actions = [Action(kind)]
        await env.send("покажи список")
        assert env.session.last_text().startswith("<b>Задачи на контроле</b>\n\n1. Подготовить отчёт")

    env.parser.actions = [Action(ActionType.SHOW_TASKS, assignee="Мария", filter="active")]
    await env.send("покажи задачи Марии")
    assert env.session.last_text() == "<b>Задачи на контроле</b>\n\n2. Обновить прайс\nМария, до 11 ноября"

    env.parser.actions = [Action(ActionType.SHOW_TASKS, filter="archive")]
    await env.send("покажи выполненные")
    assert env.session.last_text() == "<b>Выполненные и отменённые</b>\n\nЗадач нет."
    assert all(t.status == Status.NEW for t in env.repo.tasks.values())


async def test_unclear_lists_candidates(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.UNCLEAR, task_ids=[report.id, price.id], question="Какую задачу закрыть?")
    ]
    await env.send("закрой задачу")
    call = env.session.sent()[-1]
    assert call.text == "Какую задачу закрыть?\n\n1. Подготовить отчёт\n\n2. Обновить прайс"
    assert inline_buttons(call) == []


async def test_show_keeps_pending_question(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    await env.send("отчёт готов")
    env.parser.actions = [Action(ActionType.SHOW_TASKS)]
    await env.send("покажи список")
    await env.send("да")  # вопрос ещё в силе
    assert env.repo.tasks[report.id].status == Status.DONE


# ---------- еженедельный список ----------


async def test_change_schedule_by_voice(env):
    await env.send("Ивану отчёт")
    assert env.parser.schedules[-1] == "каждый понедельник в 10:00"
    await env.send("нет")

    env.parser.actions = [Action(ActionType.SET_SCHEDULE, weekday="fri", time="09:00")]
    await env.send("присылай список по пятницам в 9 утра")
    assert env.session.last_text() == "Присылать список каждую пятницу в 09:00?"
    assert env.reminder.schedule.day == "mon"

    await env.send("да")
    assert (env.reminder.schedule.day, env.reminder.schedule.at) == ("fri", time(9, 0))
    assert env.session.last_text() == "Готово. Список будет приходить каждую пятницу в 09:00."
    trigger = env.scheduler.get_job("weekly_dashboard").trigger
    assert "day_of_week='fri'" in str(trigger) and "hour='9'" in str(trigger)
    assert env.schedule_file.read_text(encoding="utf-8") == '{"day": "fri", "time": "09:00"}'
    assert (
        ReminderService(env.settings, path=env.schedule_file).schedule.describe() == "каждую пятницу в 09:00"
    )


async def test_weekly_list(env):
    await _create(env, "Отчёт", "Иван", date(2020, 1, 1))
    await send_weekly_reminder(env.bot, env.repo, env.settings, env.reminder, env.numbering)
    last = env.session.sent(SendMessage)[-1]
    assert last.chat_id == ADMIN
    assert re.fullmatch(
        r"<b>Задачи на контроле на \d+ \w+</b>\n\n1\. Отчёт\nИван, до 1 января 2020", last.text
    ), last.text


# ---------- сбои ----------


async def test_db_failure_keeps_question_for_retry(env, monkeypatch):
    original = env.repo.create_many

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.repo, "create_many", broken)
    await env.send("Купить воду")
    await env.send("да")
    assert env.session.last_text() == "Не получилось сохранить. Скажите «да» ещё раз."
    assert env.repo.tasks == {}

    monkeypatch.setattr(env.repo, "create_many", original)
    await env.send("да")
    assert len(env.repo.tasks) == 1


async def test_old_buttons(env):
    await env.press("t:open:5:active:0:", 5000)
    assert "больше не работает" in env.session.toasts()[-1]
