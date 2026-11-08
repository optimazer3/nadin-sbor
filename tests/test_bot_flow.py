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
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetFile,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, File, Message, Update, User, Voice

from bot.config import Settings
from bot.focus import FocusStore
from bot.handlers import get_routers
from bot.keyboards import BTN_TASKS, DraftCb, ListCb, TaskCb
from bot.main import build_dispatcher
from bot.models import Action, ActionType, Status, TaskDraft
from bot.repository import InMemoryTaskRepository
from bot.scheduler import send_weekly_reminder
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
                reply_markup=method.reply_markup,
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

    async def interpret(self, text: str, today: date, tasks=(), focus_id=None):
        self.texts.append(text)
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
def env():
    settings = make_settings()
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    repo = InMemoryTaskRepository()
    transcriber = FakeTranscriber()
    parser = FakeParser()
    focus = FocusStore()
    dp = build_dispatcher(settings, repo=repo, transcriber=transcriber, parser=parser, focus=focus)

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


async def test_stranger_gets_id_and_no_access(env):
    await env.send("/start", uid=STRANGER)
    assert str(STRANGER) in env.session.last_text()
    assert "только для руководителя" in env.session.last_text()
    await env.send("Сделать что-то", uid=STRANGER)
    assert env.repo.tasks == {}


async def test_voice_confirm_save_flow(env):
    await env.send(voice=True)
    assert env.transcriber.audio == [b"OGG-AUDIO"]
    assert env.parser.texts == ["Ване подготовить отчёт до пятницы"]
    text = env.session.last_text()
    assert "Распознано" in text and "Подготовить отчёт" in text and "Иван" in text
    draft_msg = last_bot_message_id(env.session)
    assert [b[1] for b in env.session.buttons()] == [
        DraftCb(action="save").pack(),
        DraftCb(action="cancel").pack(),
    ]

    await env.press(DraftCb(action="save").pack(), draft_msg)
    assert list(env.repo.tasks) == [1]
    task = env.repo.tasks[1]
    assert (task.title, task.assignee, task.deadline, task.source) == (
        "Подготовить отчёт",
        "Иван",
        date(2026, 11, 13),
        "voice",
    )
    assert task.raw_text == "Ване подготовить отчёт до пятницы"
    assert "✅ Задача сохранена" in env.session.last_text()

    # Повторное нажатие на ту же кнопку не создаёт дубль.
    await env.press(DraftCb(action="save").pack(), draft_msg)
    assert len(env.repo.tasks) == 1
    assert "неактуально" in env.session.toasts()[-1]


async def test_cancel_draft_and_stale_buttons(env):
    await env.send("Марии обновить прайс")
    first = last_bot_message_id(env.session)
    await env.send("Ивану позвонить клиенту")
    second = last_bot_message_id(env.session)
    assert first != second
    # Кнопки старого черновика убраны.
    assert any(isinstance(c, EditMessageReplyMarkup) and c.message_id == first for c in env.session.calls)

    await env.press(DraftCb(action="save").pack(), first)  # устаревший черновик
    assert env.repo.tasks == {}

    await env.press(DraftCb(action="cancel").pack(), second)
    assert "Не сохранил" in env.session.last_text()
    assert "Ивану позвонить клиенту" in env.session.last_text()
    await env.press(DraftCb(action="save").pack(), second)
    assert env.repo.tasks == {}


async def test_parser_failure_falls_back_to_raw_text(env):
    env.parser.fail = True
    await env.send("Купить воду в офис")
    assert "Автоматический разбор сейчас недоступен" in env.session.last_text()
    await env.press(DraftCb(action="save").pack(), last_bot_message_id(env.session))
    assert env.repo.tasks[1].title == "Купить воду в офис"


async def test_no_tasks_found(env):
    env.parser.drafts = []
    await env.send("привет")
    assert "Не понял, что нужно сделать" in env.session.last_text()
    assert "удалить" in env.session.last_text()  # подсказка с примерами команд


async def test_multiple_tasks_saved(env):
    env.parser.drafts = [TaskDraft("A", "Иван"), TaskDraft("B", "Мария")]
    await env.send("Ивану A, Марии B")
    assert "Задач: 2" in env.session.last_text()
    await env.press(DraftCb(action="save").pack(), last_bot_message_id(env.session))
    assert [t.title for t in env.repo.tasks.values()] == ["A", "B"]
    assert "Сохранено задач: 2" in env.session.last_text()


async def _create(env, title="Отчёт", assignee="Иван", deadline=date(2026, 11, 13)):
    [task] = await env.repo.create_many(
        [TaskDraft(title, assignee, deadline)], source="text", raw_text=title, created_by=ADMIN
    )
    return task


async def test_list_open_and_change_status(env):
    task = await _create(env)
    await env.send(BTN_TASKS)
    assert "Активные задачи" in env.session.last_text()
    open_cb = TaskCb(action="open", id=task.id).pack()
    assert open_cb in [b[1] for b in env.session.buttons()]

    await env.press(open_cb, 5000)
    assert f"Задача #{task.id}" in env.session.last_text()
    await env.press(TaskCb(action="set_status", id=task.id, v="in_progress").pack(), 5000)
    assert env.repo.tasks[task.id].status == Status.IN_PROGRESS
    await env.press(TaskCb(action="status_menu", id=task.id).pack(), 5000)
    assert "Выберите новый статус" in env.session.last_text()
    await env.press(TaskCb(action="set_status", id=task.id, v="done").pack(), 5000)
    assert env.repo.tasks[task.id].status == Status.DONE
    assert "Задача выполнена ✅" in env.session.toasts()

    # Выполненная задача ушла из активных и видна в архиве.
    await env.press(ListCb(f="active").pack(), 5000)
    assert "Задач нет" in env.session.last_text()
    await env.press(ListCb(f="archive").pack(), 5000)
    assert TaskCb(action="open", id=task.id, f="archive").pack() in [b[1] for b in env.session.buttons()]


async def test_edit_title_by_voice(env):
    task = await _create(env)
    await env.press(TaskCb(action="edit_title", id=task.id).pack(), 5000)
    assert "Пришлите новый текст" in env.session.last_text()
    env.transcriber.text = "Подготовить квартальный отчёт"
    await env.send(voice=True)
    assert env.repo.tasks[task.id].title == "Подготовить квартальный отчёт"
    assert "✅ Сохранено" in env.session.last_text()
    assert any(isinstance(c, DeleteMessage) for c in env.session.calls)  # убрали «Распознаю…»
    # После правки бот снова в обычном режиме: текст создаёт новую задачу.
    await env.send("Новая задача")
    assert "Я правильно понял" in env.session.last_text()


async def test_edit_assignee_button_and_text(env):
    task = await _create(env)
    await env.press(TaskCb(action="edit_assignee", id=task.id).pack(), 5000)
    labels = [b[0] for b in env.session.buttons()]
    assert "✓ Иван" in labels and "Мария" in labels
    await env.press(TaskCb(action="set_assignee", id=task.id, v="1").pack(), 5000)
    assert env.repo.tasks[task.id].assignee == "Мария"

    await env.press(TaskCb(action="edit_assignee", id=task.id).pack(), 5000)
    await env.send("иван")
    assert env.repo.tasks[task.id].assignee == "Иван"  # нормализовано по составу команды

    await env.press(TaskCb(action="edit_assignee", id=task.id).pack(), 5000)
    await env.press(TaskCb(action="set_assignee", id=task.id, v="-").pack(), 5000)
    assert env.repo.tasks[task.id].assignee is None


async def test_edit_deadline(env):
    task = await _create(env)
    await env.press(TaskCb(action="edit_deadline", id=task.id).pack(), 5000)
    await env.send("25.12.2026")
    assert env.repo.tasks[task.id].deadline == date(2026, 12, 25)

    await env.press(TaskCb(action="edit_deadline", id=task.id).pack(), 5000)
    env.parser.date_answer = None
    await env.send("когда-нибудь потом")
    assert "Не понял дату" in env.session.last_text()
    assert env.repo.tasks[task.id].deadline == date(2026, 12, 25)
    env.parser.date_answer = date(2026, 11, 30)
    await env.send("к концу месяца")
    assert env.repo.tasks[task.id].deadline == date(2026, 11, 30)

    await env.press(TaskCb(action="edit_deadline", id=task.id).pack(), 5000)
    await env.press(TaskCb(action="set_deadline", id=task.id, v="-").pack(), 5000)
    assert env.repo.tasks[task.id].deadline is None


async def test_menu_button_cancels_edit(env):
    task = await _create(env)
    await env.press(TaskCb(action="edit_title", id=task.id).pack(), 5000)
    await env.send(BTN_TASKS)
    assert env.repo.tasks[task.id].title == "Отчёт"
    assert "Активные задачи" in env.session.last_text()


async def test_delete_task(env):
    task = await _create(env)
    await env.press(TaskCb(action="delete", id=task.id).pack(), 5000)
    assert "Удалить задачу?" in env.session.last_text()
    await env.press(TaskCb(action="delete_yes", id=task.id).pack(), 5000)
    assert env.repo.tasks == {}
    await env.press(TaskCb(action="open", id=task.id).pack(), 5000)
    assert "не найдена" in env.session.toasts()[-1]


async def test_dashboard_button_and_weekly_reminder(env):
    await _create(env, "Отчёт", "Иван", date(2020, 1, 1))
    await env.send("📊 Дашборд")
    assert "Дашборд задач" in env.session.last_text()
    assert "просрочено" in env.session.last_text()

    await send_weekly_reminder(env.bot, env.repo, env.settings)
    last = env.session.sent(SendMessage)[-1]
    assert last.chat_id == ADMIN
    assert "Еженедельный дашборд" in last.text
    assert last.reply_markup.inline_keyboard[0][0].callback_data == ListCb(f="active", p=0).pack()


# ---------- голосовые команды ----------


async def _two_tasks(env):
    report = await _create(env, "Подготовить отчёт", "Иван", date(2026, 11, 13))
    price = await _create(env, "Обновить прайс", "Мария", date(2026, 11, 11))
    return report, price


async def test_voice_status_change_with_confirmation(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    env.transcriber.text = "отчёт Ивана готов"
    await env.send(voice=True)

    # Нейросети передали текущие задачи; пока ничего не изменено — ждём подтверждения.
    assert sorted(env.parser.contexts[-1]) == [1, 2]
    text = env.session.last_text()
    assert "Распознано" in text and "Я правильно понял?" in text
    assert f"Изменить задачу</b> #{report.id} «Подготовить отчёт»" in text
    assert "📌 Статус: 🆕 Новая → ✅ Выполнена" in text
    assert env.repo.tasks[report.id].status == Status.NEW

    await env.press(DraftCb(action="save").pack(), last_bot_message_id(env.session))
    assert env.repo.tasks[report.id].status == Status.DONE
    result = env.session.last_text()
    assert result.startswith("✅ Готово") and "статус → ✅ Выполнена" in result
    assert env.focus.get(ADMIN) == report.id  # теперь «эта задача» — отчёт


async def test_voice_update_deadline_and_assignee_shows_old_and_new(env):
    _, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.UPDATE, task_ids=[price.id], deadline=date(2026, 11, 20), assignee="иван")
    ]
    await env.send("перенеси прайс на 20 ноября и отдай Ивану")
    text = env.session.last_text()
    assert "👤 Исполнитель: Мария → Иван" in text  # имя приведено к написанию из команды
    assert "📅 Срок: 11.11.2026 (ср) → 20.11.2026 (пт)" in text
    await env.press(DraftCb(action="save").pack(), last_bot_message_id(env.session))
    task = env.repo.tasks[price.id]
    assert (task.assignee, task.deadline) == ("Иван", date(2026, 11, 20))


async def test_voice_delete_needs_confirmation_and_cancel_keeps_task(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.DELETE, task_ids=[price.id])]
    await env.send("удали задачу про прайс")
    assert f"🗑 <b>Удалить задачу</b> #{price.id} «Обновить прайс»" in env.session.last_text()

    await env.press(DraftCb(action="cancel").pack(), last_bot_message_id(env.session))
    assert price.id in env.repo.tasks
    assert "ничего не изменил" in env.session.last_text()

    await env.send("удали задачу про прайс")
    await env.press(DraftCb(action="save").pack(), last_bot_message_id(env.session))
    assert price.id not in env.repo.tasks and report.id in env.repo.tasks
    assert f"🗑 Удалена #{price.id} «Обновить прайс»" in env.session.last_text()


async def test_voice_mixed_create_update_delete(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.CREATE, title="Позвонить клиенту", assignee="Мария"),
        Action(ActionType.UPDATE, task_ids=[report.id], status=Status.IN_PROGRESS),
        Action(ActionType.DELETE, task_ids=[price.id]),
    ]
    await env.send("Маше позвонить клиенту, отчёт в работу, прайс удали")
    text = env.session.last_text()
    assert "<b>1.</b> ➕ <b>Новая задача</b>" in text and "<b>3.</b> 🗑" in text
    await env.press(DraftCb(action="save").pack(), last_bot_message_id(env.session))
    titles = {t.title for t in env.repo.tasks.values()}
    assert titles == {"Подготовить отчёт", "Позвонить клиенту"}
    assert env.repo.tasks[report.id].status == Status.IN_PROGRESS
    result = env.session.last_text()
    assert "➕ Создана #3 «Позвонить клиенту»" in result and "🗑 Удалена #2" in result


async def test_focus_from_opened_card_is_passed_to_parser(env):
    report, _ = await _two_tasks(env)
    await env.press(TaskCb(action="open", id=report.id).pack(), 5000)
    env.parser.actions = [Action(ActionType.DELETE, task_ids=[report.id])]
    await env.send("удали её")
    assert env.parser.focus_ids[-1] == report.id
    await env.press(DraftCb(action="save").pack(), last_bot_message_id(env.session))
    assert env.focus.get(ADMIN) is None  # удалённая задача больше не «открыта»


async def test_task_deleted_between_question_and_confirmation(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    await env.send("отчёт готов")
    plan_msg = last_bot_message_id(env.session)
    await env.repo.delete(report.id)
    await env.press(DraftCb(action="save").pack(), plan_msg)
    assert "не найдена" in env.session.last_text()


async def test_voice_show_commands_run_immediately(env):
    report, price = await _two_tasks(env)

    env.parser.actions = [Action(ActionType.SHOW_DASHBOARD)]
    await env.send("покажи дашборд")
    assert "Дашборд задач" in env.session.last_text()

    env.parser.actions = [Action(ActionType.SHOW_TASKS, assignee="Мария", filter="active")]
    await env.send("покажи задачи Марии")
    text = env.session.last_text()
    assert "Активные задачи — Мария" in text
    opened = [b[1] for b in env.session.buttons()]
    assert TaskCb(action="open", id=price.id).pack() in opened
    assert TaskCb(action="open", id=report.id).pack() not in opened

    env.parser.actions = [Action(ActionType.OPEN_TASK, task_ids=[report.id])]
    await env.send("открой задачу один")
    assert f"Задача #{report.id}" in env.session.last_text()
    assert env.focus.get(ADMIN) == report.id
    assert all(r.status == Status.NEW for r in env.repo.tasks.values())  # ничего не изменилось


async def test_unclear_offers_candidates(env):
    report, price = await _two_tasks(env)
    env.parser.actions = [
        Action(ActionType.UNCLEAR, task_ids=[report.id, price.id], question="Какую задачу закрыть?")
    ]
    await env.send("закрой задачу")
    text = env.session.last_text()
    assert "Какую задачу закрыть?" in text
    buttons = [b[1] for b in env.session.buttons()]
    assert TaskCb(action="open", id=report.id).pack() in buttons
    assert ListCb(f="active", p=0).pack() in buttons
    assert all(t.status == Status.NEW for t in env.repo.tasks.values())


async def test_show_command_keeps_pending_plan(env):
    report, _ = await _two_tasks(env)
    env.parser.actions = [Action(ActionType.UPDATE, task_ids=[report.id], status=Status.DONE)]
    await env.send("отчёт готов")
    plan_msg = last_bot_message_id(env.session)
    env.parser.actions = [Action(ActionType.SHOW_DASHBOARD)]
    await env.send("покажи дашборд")
    await env.press(DraftCb(action="save").pack(), plan_msg)  # план всё ещё можно подтвердить
    assert env.repo.tasks[report.id].status == Status.DONE


async def test_db_failure_keeps_plan_for_retry(env, monkeypatch):
    original = env.repo.create_many

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(env.repo, "create_many", broken)
    await env.send("Купить воду")
    plan_msg = last_bot_message_id(env.session)
    await env.press(DraftCb(action="save").pack(), plan_msg)
    assert "Не удалось сохранить" in env.session.toasts()[-1]
    assert env.repo.tasks == {}

    monkeypatch.setattr(env.repo, "create_many", original)
    await env.press(DraftCb(action="save").pack(), plan_msg)  # та же кнопка ещё работает
    assert len(env.repo.tasks) == 1
