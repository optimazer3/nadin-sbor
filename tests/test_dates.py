from datetime import date

from bot.dates import WEEKDAY_EVERY, calendar_hint

TODAY = date(2026, 11, 6)  # пятница


def test_calendar_hint():
    hint = calendar_hint(TODAY, days=3)
    assert hint.splitlines() == [
        "2026-11-06 пятница (сегодня)",
        "2026-11-07 суббота",
        "2026-11-08 воскресенье",
    ]


def test_weekday_every():
    assert WEEKDAY_EVERY["fri"] == "каждую пятницу"
    assert len(WEEKDAY_EVERY) == 7


def test_format_day():
    from bot.views import format_day

    assert format_day(date(2026, 11, 13), TODAY) == "13 ноября"
    assert format_day(date(2027, 1, 5), TODAY) == "5 января 2027"
