"""Разбор задач: настоящий клиент openai против локального сервера в формате OpenAI API."""

import json
from datetime import date

import openai
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

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


async def test_parse_tasks_request_and_mapping(llm):
    payload = {
        "tasks": [
            {"title": "Подготовить отчёт", "assignee": "Иван", "deadline": "2026-11-13"},
            {"title": "  Позвонить клиенту ", "assignee": "null", "deadline": "не дата"},
            {"title": "", "assignee": None, "deadline": None},
        ]
    }
    parser, fake = await llm(json.dumps(payload, ensure_ascii=False))
    drafts = await parser.parse_tasks("Ване отчёт до пятницы и позвонить клиенту", TODAY)

    assert [(d.title, d.assignee, d.deadline) for d in drafts] == [
        ("Подготовить отчёт", "Иван", date(2026, 11, 13)),
        ("Позвонить клиенту", None, None),
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


async def test_json_in_code_fence(llm):
    parser, _ = await llm(
        'Вот ответ:\n```json\n{"tasks": [{"title": "A", "assignee": null, "deadline": null}]}\n```'
    )
    [draft] = await parser.parse_tasks("x", TODAY)
    assert draft.title == "A"


async def test_retries_without_json_mode_when_rejected(llm):
    parser, fake = await llm(
        (400, {"error": {"message": "response_format is not supported", "type": "invalid_request_error"}}),
        '{"tasks": []}',
        '{"tasks": []}',
    )
    assert await parser.parse_tasks("привет", TODAY) == []
    assert "response_format" in fake.requests[0]
    assert "response_format" not in fake.requests[1]
    await parser.parse_tasks("ещё раз", TODAY)  # дальше режим JSON уже не запрашиваем
    assert "response_format" not in fake.requests[2]


async def test_no_team_block_without_team(llm):
    parser, fake = await llm('{"tasks": []}', team=())
    await parser.parse_tasks("x", TODAY)
    assert "Состав команды" not in fake.requests[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ((401, {"error": {"message": "bad key"}}), "LLM_API_KEY"),
        ((404, {"error": {"message": "no model"}}), "LLM_MODEL"),
        ((429, {"error": {"message": "slow down"}}), "лимит"),
        ((500, {"error": {"message": "boom"}}), "500"),
        ("это не json", "JSON"),
        ('{"something": 1}', "списка задач"),
        (['{"tasks": [', "length"], "оборвался"),
    ],
)
async def test_errors(llm, reply, message):
    parser, _ = await llm(reply)
    with pytest.raises(TaskParserError, match=message):
        await parser.parse_tasks("x", TODAY)


async def test_parse_date(llm):
    parser, fake = await llm('{"deadline": "2026-11-30"}', '{"deadline": null}')
    assert await parser.parse_date("к концу месяца", TODAY) == date(2026, 11, 30)
    assert await parser.parse_date("когда-нибудь", TODAY) is None
    assert "к концу месяца" in fake.requests[0]["messages"][1]["content"]


def test_extract_json_variants():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Конечно! {"a": {"b": 2}} Готово.') == {"a": {"b": 2}}
    with pytest.raises(TaskParserError):
        _extract_json("[1, 2]")
