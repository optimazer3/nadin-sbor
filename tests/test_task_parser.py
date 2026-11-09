"""Разбор задач: настоящий клиент openai против локального сервера в формате OpenAI API."""

import json
from datetime import date

import openai
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.models import ActionType, Status, Task
from bot.services.task_parser import TaskParser, TaskParserError, _extract_json

TODAY = date(2026, 11, 6)


class FakeLLM:
    """Отвечает как /v1/chat/completions. replies — очередь ответов (строка или (status, json))."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.auth: list[str | None] = []
        self.paths: list[str] = []

    async def handler(self, request: web.Request) -> web.Response:
        self.paths.append(request.path)
        self.auth.append(request.headers.get("Authorization"))
        self.requests.append(await request.json())
        reply = self.replies.pop(0)
        if isinstance(reply, tuple):
            status, body = reply
            return web.json_response(body, status=status)
        content, finish = reply if isinstance(reply, list) else (reply, "stop")
        return web.json_response(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "openai/gpt-4.1-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": finish,
                    }
                ],
            }
        )


@pytest.fixture
async def llm():
    servers = []

    async def make(*replies, team=("Иван", "Мария")):
        fake = FakeLLM(*replies)
        app = web.Application()
        app.router.add_post("/api/v1/chat/completions", fake.handler)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        client = openai.AsyncOpenAI(
            api_key="polza-key", base_url=str(server.make_url("/api/v1")), max_retries=0
        )
        return TaskParser(client, model="openai/gpt-4.1-mini", team_members=team), fake

    yield make
    for server in servers:
        await server.close()


def make_task(id, title, assignee=None, deadline=None, status=Status.NEW):
    return Task(id=id, title=title, assignee=assignee, deadline=deadline, status=status)


CONTEXT = [
    make_task(5, "Подготовить отчёт", "Иван", date(2026, 11, 13), Status.IN_PROGRESS),
    make_task(7, "Обновить прайс", "Мария"),
    make_task(9, "Заказать воду", None, None, Status.DONE),
]


def reply(*actions) -> str:
    return json.dumps({"actions": list(actions)}, ensure_ascii=False)


async def test_create_request_and_mapping(llm):
    parser, fake = await llm(
        reply(
            {"type": "create", "title": "Подготовить отчёт", "assignee": "Иван", "deadline": "2026-11-13"},
            {"type": "create", "title": "  Позвонить клиенту ", "assignee": "null", "deadline": "не дата"},
            {"type": "create", "title": "", "assignee": None, "deadline": None},
        )
    )
    actions = await parser.interpret("Ване отчёт до пятницы и позвонить клиенту", TODAY, CONTEXT, focus_id=7)

    assert [(a.type, a.title, a.assignee, a.deadline) for a in actions] == [
        (ActionType.CREATE, "Подготовить отчёт", "Иван", date(2026, 11, 13)),
        (ActionType.CREATE, "Позвонить клиенту", None, None),
    ]
    assert fake.paths == ["/api/v1/chat/completions"]
    assert fake.auth == ["Bearer polza-key"]
    [body] = fake.requests
    assert body["model"] == "openai/gpt-4.1-mini"
    assert body["response_format"] == {"type": "json_object"}
    system, user = body["messages"]
    assert system["role"] == "system" and "Иван, Мария" in system["content"] and "JSON" in system["content"]
    assert "2026-11-06, пятница" in user["content"] and "2026-11-13 пятница" in user["content"]
    assert "Ване отчёт до пятницы" in user["content"]
    # Нейросеть видит текущие задачи и знает, какая карточка открыта.
    assert "№5 | Подготовить отчёт | Иван | 2026-11-13 | in_progress" in user["content"]
    assert "№7 | Обновить прайс | Мария | - | new  <- открыта сейчас" in user["content"]


async def test_display_numbers_are_translated_to_ids(llm):
    # Руководитель видел: 1 — прайс (id 7), 2 — отчёт (id 5), 3 — вода (id 9).
    parser, fake = await llm(reply({"type": "update", "task_ids": [1, 2], "status": "done"}))
    [action] = await parser.interpret("задачи 1 и 2 выполнены", TODAY, CONTEXT, numbers={7: 1, 5: 2, 9: 3})
    assert action.task_ids == [7, 5]
    user = fake.requests[0]["messages"][1]["content"]
    assert user.index("№1 | Обновить прайс") < user.index("№2 | Подготовить отчёт") < user.index("№3 |")


async def test_update_delete_and_views(llm):
    parser, _ = await llm(
        reply(
            {"type": "update", "task_ids": [5], "status": "done", "deadline": None, "clear_deadline": False},
            {"type": "update", "task_ids": ["#7"], "deadline": "2026-11-20", "assignee": "Иван"},
            {"type": "update", "task_ids": [7], "clear_assignee": True, "clear_deadline": True},
            {"type": "delete", "task_ids": [9, 9]},
            {"type": "open_task", "task_ids": [5, 7]},
            {"type": "show_tasks", "assignee": "Мария", "filter": "archive"},
            {"type": "show_tasks", "filter": "что-то"},
            {"type": "show_dashboard"},
        )
    )
    actions = await parser.interpret("…", TODAY, CONTEXT)
    done, move, clear, delete, open_, person, plain, dash = actions
    assert (done.type, done.task_ids, done.status, done.changes()) == (
        ActionType.UPDATE,
        [5],
        Status.DONE,
        {"status": Status.DONE},
    )
    assert move.task_ids == [7] and move.changes() == {"assignee": "Иван", "deadline": date(2026, 11, 20)}
    assert clear.changes() == {"assignee": None, "deadline": None}
    assert (delete.type, delete.task_ids) == (ActionType.DELETE, [9])
    assert (open_.type, open_.task_ids) == (ActionType.OPEN_TASK, [5])
    assert (person.type, person.assignee, person.filter) == (ActionType.SHOW_TASKS, "Мария", "archive")
    assert plain.filter == "active"
    assert dash.type == ActionType.SHOW_DASHBOARD


async def test_unknown_ids_and_empty_updates_become_questions(llm):
    parser, _ = await llm(
        reply(
            {"type": "delete", "task_ids": [42]},  # такой задачи нет — нейросеть выдумала
            {"type": "update", "task_ids": [5]},  # не сказано, что менять
            {"type": "unclear", "task_ids": [5, 7, 100], "question": "Какой отчёт?"},
            {"type": "launch_rocket"},
            "мусор",
        )
    )
    missing, nothing, unclear = await parser.interpret("…", TODAY, CONTEXT)
    assert missing.type == ActionType.UNCLEAR and missing.task_ids == [] and "Не нашёл" in missing.question
    assert nothing.type == ActionType.UNCLEAR and nothing.task_ids == [5]
    assert unclear.task_ids == [5, 7] and unclear.question == "Какой отчёт?"


async def test_json_in_code_fence(llm):
    parser, _ = await llm('Вот ответ:\n```json\n{"actions": [{"type": "create", "title": "A"}]}\n```')
    [action] = await parser.interpret("x", TODAY)
    assert action.title == "A"


async def test_retries_without_json_mode_when_rejected(llm):
    parser, fake = await llm(
        (400, {"error": {"message": "response_format is not supported", "type": "invalid_request_error"}}),
        '{"actions": []}',
        '{"actions": []}',
    )
    assert await parser.interpret("привет", TODAY) == []
    assert "response_format" in fake.requests[0]
    assert "response_format" not in fake.requests[1]
    await parser.interpret("ещё раз", TODAY)  # дальше режим JSON уже не запрашиваем
    assert "response_format" not in fake.requests[2]


async def test_no_team_block_without_team(llm):
    parser, fake = await llm('{"actions": []}', team=())
    await parser.interpret("x", TODAY)
    assert "Состав команды" not in fake.requests[0]["messages"][0]["content"]
    assert "Текущих задач нет" in fake.requests[0]["messages"][1]["content"]


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ((401, {"error": {"message": "bad key"}}), "LLM_API_KEY"),
        ((404, {"error": {"message": "no model"}}), "LLM_MODEL"),
        ((429, {"error": {"message": "slow down"}}), "лимит"),
        ((500, {"error": {"message": "boom"}}), "500"),
        ("это не json", "JSON"),
        ('{"something": 1}', "списка действий"),
        (['{"actions": [', "length"], "оборвался"),
    ],
)
async def test_errors(llm, reply, message):
    parser, _ = await llm(reply)
    with pytest.raises(TaskParserError, match=message):
        await parser.interpret("x", TODAY)


async def test_set_schedule(llm):
    parser, fake = await llm(
        reply(
            {"type": "set_schedule", "weekday": "FRI", "time": "9:00"},
            {"type": "set_schedule", "weekday": "fri", "time": "25:00"},
            {"type": "set_schedule", "weekday": "пятница", "time": "09:00"},
        )
    )
    ok, bad_time, bad_day = await parser.interpret(
        "сводку по пятницам в 9", TODAY, schedule="каждый понедельник в 10:00"
    )
    assert (ok.type, ok.weekday, ok.time) == (ActionType.SET_SCHEDULE, "fri", "09:00")
    assert bad_time.type == bad_day.type == ActionType.UNCLEAR
    user = fake.requests[0]["messages"][1]["content"]
    assert "Еженедельный список сейчас приходит: каждый понедельник в 10:00" in user
    assert "set_schedule" in fake.requests[0]["messages"][0]["content"]


def test_extract_json_variants():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Конечно! {"a": {"b": 2}} Готово.') == {"a": {"b": 2}}
    with pytest.raises(TaskParserError):
        _extract_json("[1, 2]")
