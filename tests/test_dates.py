from datetime import date

import pytest

from bot.dates import calendar_hint, next_weekday, parse_date_simple, relative_deadline

TODAY = date(2026, 11, 6)  # пятница


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("сегодня", date(2026, 11, 6)),
        ("Завтра", date(2026, 11, 7)),
        ("послезавтра", date(2026, 11, 8)),
        ("в понедельник", date(2026, 11, 9)),
        ("до среды", date(2026, 11, 11)),
        ("во вторник", date(2026, 11, 10)),
        ("пятница", date(2026, 11, 13)),  # сегодня пятница -> следующая
        ("пт", date(2026, 11, 13)),
        ("к воскресенью", date(2026, 11, 8)),
        ("15.11", date(2026, 11, 15)),
        ("15.11.2026", date(2026, 11, 15)),
        ("1/12/26", date(2026, 12, 1)),
        ("2027-01-20", date(2027, 1, 20)),
        ("10.01", date(2027, 1, 10)),  # уже прошло в этом году -> следующий
    ],
)
def test_parse_date_simple(text, expected):
    result = parse_date_simple(text, TODAY)
    assert result.ok
    assert result.value == expected


@pytest.mark.parametrize("text", ["-", "нет", "убрать", "без срока"])
def test_parse_date_clear(text):
    result = parse_date_simple(text, TODAY)
    assert result.ok and result.value is None


@pytest.mark.parametrize("text", ["к концу месяца", "через две недели", "31.02", "abc"])
def test_parse_date_unknown(text):
    assert not parse_date_simple(text, TODAY).ok


def test_next_weekday():
    assert next_weekday(TODAY, 0) == date(2026, 11, 9)
    assert next_weekday(TODAY, 4) == date(2026, 11, 13)


def test_relative_deadline():
    assert relative_deadline(TODAY, TODAY) == "сегодня"
    assert relative_deadline(date(2026, 11, 7), TODAY) == "завтра"
    assert relative_deadline(date(2026, 11, 9), TODAY) == "через 3 дня"
    assert relative_deadline(date(2026, 11, 11), TODAY) == "через 5 дней"
    assert relative_deadline(date(2026, 11, 5), TODAY) == "просрочено на 1 день"
    assert relative_deadline(date(2026, 10, 25), TODAY) == "просрочено на 12 дней"


def test_calendar_hint():
    hint = calendar_hint(TODAY, days=3)
    assert hint.splitlines() == [
        "2026-11-06 пятница (сегодня)",
        "2026-11-07 суббота",
        "2026-11-08 воскресенье",
    ]
