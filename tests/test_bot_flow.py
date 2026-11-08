"""Сквозные сценарии: апдейты прогоняются через настоящий Dispatcher, Telegram подменён."""

from __future__ import annotations

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
from bot.keyboards import BTN_DASHBOARD, BTN_TASKS, DraftCb
from bot.main import build_dispatcher
from bot.models import Action, ActionType, Status, TaskDraft
from bot.repository import InMemoryTaskRepository
from bot.scheduler import ReminderService, send_weekly_reminder
from bot.services.task_parser import TaskParserError

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

    async def interpret(self, text: str, today: date, tasks=(), focus_id=None, schedule=None):
        self.texts.append(text)
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
    reminder.attach(scheduler, bot, repo)
    dp = build_dispatcher(
        settings, repo=repo, transcriber=transcriber, parser=parser, focus=focus, reminder=reminder
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


def inline_buttons(call) -> list[str]:
    markup = call.reply_markup
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


async def _create(env, title="Отчёт", assignee="Иван", deadline=date(2026, 11, 13)):
    [task] = await env.repo.create_many(
        [TaskDraft(title, assignee, deadline)], source="text", raw_text=title, created_by=ADMIN
    )
    return task


async def _two_tasks(env):
    report = await _create(env, "Подготовить отчёт", "Иван", date(2026, 11, 13))
    price = await _create(env, "Обновить прайс", "Мария", date(2026, 11, 11))
    return report, price


# ---------- вход и простота ----------


async def test_stranger_gets_id_and_no_access(env):
    await env.send("/start", uid=STRANGER)
    assert str(STRANGER) in env.session.last_text()
    assert "только для руководителя" in env.session.last_text()
    await env.send("Сделать что-то", uid=STRANGER)
    assert env.repo.tasks == {}


async def test_start_is_simple(env):
    await env.send("/start")
    call = env.session.sent()[-1]
    assert "Просто нажмите на микрофон" in call.text
    assert "каждый понедельник в 10:00" in call.text
    # Внизу ровно две большие кнопки, никаких кнопок в самом сообщении.
    assert [[b.text for b in row] for row in call.reply_markup.keyboard] == [[BTN_TASKS, BTN_DASHBOARD]]


async def test_task_list_is_plain_text_with_numbers(env):
    report, price = await _two_tasks(env)
    await env.send(BTN_TASKS)
    call = env.session.sent()[-1]
    assert "Задачи в работе</b> — 2" in call.text
    assert f"<b>№{price.id}</b>  Обновить прайс" in call.text  # раньше срок — выше в списке
    assert call.text.index(f"№{price.id}") < call.text.index(f"№{report.id}")
    assert "👤 Мария" in call.text
    assert f"«задача {price.id} выполнена»" in call.text  # подсказка, как сказать
    assert inline_buttons(call) == []


# ---------- создание ----------


async def test_voice_create_confirm_with_button(env):
    await env.send(voice=True)
    assert env.transcriber.audio == [b"OGG-AUDIO"]
    call = env.session.sent()[-1]
    assert "Вы сказали" in call.text and "Подготовить отчёт" in call.text
    assert "Ответьте «да» или «нет»" in call.text
    assert inline_buttons(call) == ["✅ Да", "❌ Нет"]
    plan_msg = last_bot_message_id(env.session)

    await env.press(DraftCb(action="save").pack(), plan_msg)
    [task] = env.repo.tasks.values()
    assert (task.title, task.assignee, task.deadline, task.source) == (
        "Подготовить отчёт",
        "Иван",
        date(2026, 11, 13),
        "voice",
    )
    assert "✅ Задача сохранена" in env.session.last_text()
    assert inline_buttons(env.session.sent()[-1]) == []

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
    assert "✅ Задача сохранена" in env.session.last_text()
    assert env.parser.texts == ["Ване подготовить отчёт до пятницы"]  # «да» нейросеть не спрашивали
    # У вопроса «Я правильно понял?» кнопки убраны.
    assert any(isinstance(c, EditMessageReplyMarkup) and c.message_id == plan_msg for c in env.session.calls)


async def test_cancel_by_voice_no(env):
    await env.send(voice=True)
    env.transcriber.text = "Нет"
    await env.send(voice=True)
    assert env.repo.tasks == {}
    assert "ничего не сохраняю" in env.session.last_text()
    # После «нет» бот снова в обычном режиме: «да» уже ничего не подтверждает.
    env.transcriber.text = "да"
    env.parser.drafts = []
    await env.send(voice=True)
    assert env.repo.tasks == {}


async def test_no_with_correction_is_a_new_command(env):
    await env.send("Ивану отчёт")
    env.parser.drafts = [TaskDraft("Подготовить отчёт", "Иван", date(2026, 11, 11))]
    await env.send("нет, срок до среды")
    assert env.parser.texts[-1] == "нет, срок до среды"  # ушло в нейросеть как новая команда
    assert "11.11.2026" in env.session.last_text()


async def test_cancel_by_button(env):
    await env.send("Купить воду")
    await env.press(DraftCb(action="cancel").pack(), last_bot_message_id(env.session))
    assert env.repo.tasks == {}
    assert "<code>Купить воду</code>" in env.session.last_text()


async def test_parser_failure_falls_back_to_raw_text(env):
    env.parser.fail = True
    await env.send("Купить воду в офис")
    assert "запишу его как новую задачу" in env.session.last_text()
    await env.send("да")
    assert env.repo.tasks[1].title == "Купить воду в офис"


async def test_nothing_understood(env):
    env.parser.drafts = []
    await env.send("привет")
    text = env.session.last_text()
    assert "Не понял, что нужно сделать" in text and "Задача 5 выполнена" in text


async def test_multiple_tasks_saved(env):
    env.parser.drafts = [TaskDraft("A", "Иван"), TaskDraft("B", "Мария")]
    await env.send("Ивану A, Марии B")
    assert "Задач: 2" in env.session.last_text()
    await env.send("да")
    assert [t.title for t in env.repo.tasks.values()] == ["A", "B"]
    assert "Сохранено задач: 2" in env.session.last_text()


# ---------- изменение и удаление голосом ----------


async def test_voice_status_change(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    env.transcriber.text = "задача один выполнена"
    await env.send(voice=True)
    assert sorted(env.parser.contexts[-1]) == [1, 2]
    text = env.session.last_text()
    assert f"Изменить задачу</b> №{report.id} «Подготовить отчёт»" in text
    assert "📌 Статус: 🆕 Новая → ✅ Выполнена" in text
    assert env.repo.tasks[report.id].status == Status.NEW  # пока не подтвердили — не меняем

    env.transcriber.text = "да"
    await env.send(voice=True)
    assert env.repo.tasks[report.id].status == Status.DONE
    assert env.session.last_text().startswith("🎧 Вы сказали: <i>«да»</i>")
    assert "статус → ✅ Выполнена" in env.session.last_text()
    assert env.focus.get(ADMIN) == report.id


async def test_voice_update_shows_old_and_new(env):
    _, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.UPDATE, task_ids=[price.id], deadline=date(2026, 11, 20), assignee="иван")
    ]
    await env.send("перенеси прайс на 20 ноября и отдай Ивану")
    text = env.session.last_text()
    assert "👤 Исполнитель: Мария → Иван" in text
    assert "📅 Срок: 11.11.2026 (ср) → 20.11.2026 (пт)" in text
    await env.send("да")
    task = env.repo.tasks[price.id]
    assert (task.assignee, task.deadline) == ("Иван", date(2026, 11, 20))


async def test_voice_delete(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.DELETE, task_ids=[price.id])]
    await env.send("удали задачу два")
    assert f"🗑 <b>Удалить задачу</b> №{price.id} «Обновить прайс»" in env.session.last_text()
    await env.send("нет")
    assert price.id in env.repo.tasks
    await env.send("удали задачу два")
    await env.send("да")
    assert price.id not in env.repo.tasks and report.id in env.repo.tasks
    assert f"🗑 Удалена №{price.id} «Обновить прайс»" in env.session.last_text()


async def test_voice_mixed_commands(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.CREATE, title="Позвонить клиенту", assignee="Мария"),
        Action(ActionType.UPDATE, task_ids=[report.id], status=Status.IN_PROGRESS),
        Action(ActionType.DELETE, task_ids=[price.id]),
    ]
    await env.send("Маше позвонить клиенту, отчёт в работу, прайс удали")
    text = env.session.last_text()
    assert "<b>1.</b> ➕ <b>Новая задача</b>" in text and "<b>3.</b> 🗑" in text
    await env.send("да")
    assert {t.title for t in env.repo.tasks.values()} == {"Подготовить отчёт", "Позвонить клиенту"}
    assert env.repo.tasks[report.id].status == Status.IN_PROGRESS


async def test_open_task_then_delete_it(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.OPEN_TASK, task_ids=[report.id])]
    await env.send("открой задачу один")
    call = env.session.sent()[-1]
    assert f"Задача №{report.id}" in call.text and inline_buttons(call) == []
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
    assert "не найдена" in env.session.last_text()


# ---------- показать ----------


async def test_show_commands_run_immediately(env):
    report, price = await _two_tasks(env)

    env.parser.actions = [Action(ActionType.SHOW_DASHBOARD)]
    await env.send("покажи сводку")
    assert "Сводка по задачам" in env.session.last_text()
    assert "каждый понедельник в 10:00" in env.session.last_text()

    env.parser.actions = [Action(ActionType.SHOW_TASKS, assignee="Мария", filter="active")]
    await env.send("покажи задачи Марии")
    text = env.session.last_text()
    assert "Задачи в работе</b> — Мария — 1" in text
    assert "Обновить прайс" in text and "Подготовить отчёт" not in text

    env.parser.actions = [Action(ActionType.SHOW_TASKS, filter="archive")]
    await env.send("покажи выполненные")
    assert "Выполненные и отменённые" in env.session.last_text()
    assert all(t.status == Status.NEW for t in env.repo.tasks.values())


async def test_unclear_lists_candidates_as_text(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.UNCLEAR, task_ids=[report.id, price.id], question="Какую задачу закрыть?")
    ]
    await env.send("закрой задачу")
    call = env.session.sent()[-1]
    assert "Какую задачу закрыть?" in call.text
    assert f"<b>№{report.id}</b>  Подготовить отчёт — Иван" in call.text
    assert inline_buttons(call) == []


async def test_show_keeps_pending_question(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    await env.send("отчёт готов")
    env.parser.actions = [Action(ActionType.SHOW_DASHBOARD)]
    await env.send("покажи сводку")
    await env.send("да")  # вопрос «Я правильно понял?» ещё в силе
    assert env.repo.tasks[report.id].status == Status.DONE


async def test_dashboard_button(env):
    await _create(env, "Отчёт", "Иван", date(2020, 1, 1))
    await env.send(BTN_DASHBOARD)
    text = env.session.last_text()
    assert "Сводка по задачам" in text and "просрочено" in text
    assert inline_buttons(env.session.sent()[-1]) == []


# ---------- расписание сводки ----------


async def test_change_dashboard_schedule_by_voice(env):
    await env.send("Ивану отчёт")  # parser видит текущее расписание
    assert env.parser.schedules[-1] == "каждый понедельник в 10:00"
    await env.send("нет")

    env.parser.actions = [Action(ActionType.SET_SCHEDULE, weekday="fri", time="09:00")]
    await env.send("присылай сводку по пятницам в 9 утра")
    assert "⏰ <b>Присылать сводку</b> каждую пятницу в 09:00" in env.session.last_text()
    assert env.reminder.schedule.day == "mon"  # до подтверждения ничего не меняем

    await env.send("да")
    assert (env.reminder.schedule.day, env.reminder.schedule.at) == ("fri", time(9, 0))
    assert "Сводка будет приходить каждую пятницу в 09:00" in env.session.last_text()
    # Планировщик переставлен, расписание сохранено в файл.
    trigger = env.scheduler.get_job("weekly_dashboard").trigger
    assert "day_of_week='fri'" in str(trigger) and "hour='9'" in str(trigger)
    assert env.schedule_file.read_text(encoding="utf-8") == '{"day": "fri", "time": "09:00"}'

    # После перезапуска бот берёт расписание из файла, а не из .env.
    again = ReminderService(env.settings, path=env.schedule_file)
    assert again.schedule.describe() == "каждую пятницу в 09:00"

    await env.send("/start")
    assert "каждую пятницу в 09:00" in env.session.last_text()


async def test_weekly_reminder(env):
    await _create(env, "Отчёт", "Иван", date(2020, 1, 1))
    await send_weekly_reminder(env.bot, env.repo, env.settings, env.reminder)
    last = env.session.sent(SendMessage)[-1]
    assert last.chat_id == ADMIN
    assert "Еженедельная сводка" in last.text and "Чтобы изменить, скажите" in last.text


# ---------- сбои ----------


async def test_db_failure_keeps_question_for_retry(env, monkeypatch):
    original = env.repo.create_many

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.repo, "create_many", broken)
    await env.send("Купить воду")
    plan_msg = last_bot_message_id(env.session)
    await env.press(DraftCb(action="save").pack(), plan_msg)
    assert "Не получилось сохранить" in env.session.toasts()[-1]
    await env.send("да")
    assert "Не получилось сохранить" in env.session.last_text()
    assert env.repo.tasks == {}

    monkeypatch.setattr(env.repo, "create_many", original)
    await env.send("да")  # можно подтвердить ещё раз
    assert len(env.repo.tasks) == 1


async def test_old_buttons_explain_themselves(env):
    await env.press("t:open:5:active:0:", 5000)
    assert "больше не работает" in env.session.toasts()[-1]
