import json
from datetime import date
from types import SimpleNamespace

import pytest

from bot.services.task_parser import TaskParser, TaskParserError

TODAY = date(2026, 11, 6)


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload = payload
        self.stop_reason = stop_reason
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        )


def make_parser(payload, stop_reason="end_turn", team=("Иван", "Мария")):
    messages = FakeMessages(payload, stop_reason)
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return TaskParser(client, model="claude-opus-5-5", team_members=team), messages


async def test_parse_tasks_maps_fields_and_request():
    parser, messages = make_parser(
        {
            "tasks": [
                {"title": "Подготовить отчёт", "assignee": "Иван", "deadline": "2026-11-13"},
                {"title": "  Позвонить клиенту ", "assignee": "  ", "deadline": "не дата"},
                {"title": "", "assignee": None, "deadline": None},
            ]
        }
    )
    drafts = await parser.parse_tasks("Ване отчёт до пятницы и позвонить клиенту", TODAY)

    assert [(d.title, d.assignee, d.deadline) for d in drafts] == [
        ("Подготовить отчёт", "Иван", date(2026, 11, 13)),
        ("Позвонить клиенту", None, None),
    ]
    [call] = messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "Иван, Мария" in call["system"]
    user = call["messages"][0]["content"]
    assert "2026-11-06, пятница" in user and "2026-11-13 пятница" in user
    assert "Ване отчёт до пятницы" in user


async def test_parse_tasks_empty_list():
    parser, _ = make_parser({"tasks": []})
    assert await parser.parse_tasks("привет", TODAY) == []


async def test_no_team_block_without_team():
    parser, messages = make_parser({"tasks": []}, team=())
    await parser.parse_tasks("x", TODAY)
    assert "Состав команды" not in messages.calls[0]["system"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_bad_stop_reason_raises(stop_reason):
    parser, _ = make_parser({"tasks": []}, stop_reason=stop_reason)
    with pytest.raises(TaskParserError):
        await parser.parse_tasks("x", TODAY)


async def test_invalid_json_raises():
    parser, _ = make_parser("not json")
    with pytest.raises(TaskParserError):
        await parser.parse_tasks("x", TODAY)


async def test_parse_date():
    parser, _ = make_parser({"deadline": "2026-11-30"})
    assert await parser.parse_date("к концу месяца", TODAY) == date(2026, 11, 30)
    parser, _ = make_parser({"deadline": None})
    assert await parser.parse_date("когда-нибудь", TODAY) is None
