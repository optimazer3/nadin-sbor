from datetime import date

from bot.dates import WEEKDAY_EVERY, calendar_hint, relative_deadline

TODAY = date(2026, 11, 6)  # пятница


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


def test_weekday_every():
    assert WEEKDAY_EVERY["fri"] == "каждую пятницу"
    assert len(WEEKDAY_EVERY) == 7
