"""Форматирование дат (дедлайнов) и названия дней недели."""

from __future__ import annotations

from datetime import date, timedelta

WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAY_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def format_date(d: date) -> str:
    """15.11.2026 (вс)"""
    return f"{d:%d.%m.%Y} ({WEEKDAY_SHORT[d.weekday()]})"


def format_date_short(d: date, today: date) -> str:
    """15.11 — без года, если год текущий."""
    return f"{d:%d.%m}" if d.year == today.year else f"{d:%d.%m.%Y}"


def _days_word(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def relative_deadline(d: date, today: date) -> str:
    """«сегодня», «завтра», «через 3 дня», «просрочено на 2 дня»."""
    delta = (d - today).days
    if delta == 0:
        return "сегодня"
    if delta == 1:
        return "завтра"
    if delta > 1:
        return f"через {delta} {_days_word(delta)}"
    return f"просрочено на {-delta} {_days_word(delta)}"


def calendar_hint(today: date, days: int = 21) -> str:
    """Календарь на ближайшие недели — чтобы модель точно считала «до пятницы» и т.п."""
    lines = []
    for i in range(days):
        d = today + timedelta(days=i)
        mark = " (сегодня)" if i == 0 else ""
        lines.append(f"{d.isoformat()} {WEEKDAY_FULL[d.weekday()]}{mark}")
    return "\n".join(lines)


# Для расписания сводки: код дня недели (как в cron) -> «каждый понедельник».
WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_EVERY = {
    "mon": "каждый понедельник",
    "tue": "каждый вторник",
    "wed": "каждую среду",
    "thu": "каждый четверг",
    "fri": "каждую пятницу",
    "sat": "каждую субботу",
    "sun": "каждое воскресенье",
}
