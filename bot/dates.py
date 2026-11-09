"""Дни недели и календарь для нейросети."""

from __future__ import annotations

from datetime import date, timedelta

WEEKDAY_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


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
