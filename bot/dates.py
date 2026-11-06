"""Разбор и форматирование дат (дедлайнов)."""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAY_FULL = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

# Основы названий дней недели во всех падежах: «пятница», «в пятницу», «до пятницы».
_WEEKDAY_STEMS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")

_CLEAR_WORDS = {"-", "—", "нет", "убрать", "без срока", "без дедлайна", "удалить", "никакого"}

_DATE_RE = re.compile(r"^(\d{1,2})[./\-](\d{1,2})(?:[./\-](\d{2,4}))?$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


class DateParse:
    """Результат локального разбора: распознали дату / попросили убрать / не поняли."""

    __slots__ = ("ok", "value")

    def __init__(self, ok: bool, value: date | None = None) -> None:
        self.ok = ok
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - для отладки
        return f"DateParse(ok={self.ok}, value={self.value})"


def next_weekday(today: date, weekday: int) -> date:
    """Ближайший будущий день недели (если сегодня он же — через неделю)."""
    days = (weekday - today.weekday()) % 7
    return today + timedelta(days=days or 7)


def parse_date_simple(text: str, today: date) -> DateParse:
    """Понимает «-», «сегодня», «завтра», «послезавтра», «пятница», «15.11», «15.11.2026», «2026-11-15».

    Для всего остального возвращает DateParse(ok=False) — тогда можно спросить LLM.
    """
    s = text.strip().lower().rstrip(".!")
    s = re.sub(r"^(до|к|в|во|на|по)\s+", "", s)

    if s in _CLEAR_WORDS:
        return DateParse(True, None)
    if s == "сегодня":
        return DateParse(True, today)
    if s == "завтра":
        return DateParse(True, today + timedelta(days=1))
    if s == "послезавтра":
        return DateParse(True, today + timedelta(days=2))

    for idx, short in enumerate(WEEKDAY_SHORT):
        if s == short or s.startswith(_WEEKDAY_STEMS[idx]):
            return DateParse(True, next_weekday(today, idx))

    if m := _ISO_RE.match(s):
        try:
            return DateParse(True, date(int(m[1]), int(m[2]), int(m[3])))
        except ValueError:
            return DateParse(False)

    if m := _DATE_RE.match(s):
        day, month, year = int(m[1]), int(m[2]), m[3]
        try:
            if year:
                y = int(year)
                if y < 100:
                    y += 2000
                return DateParse(True, date(y, month, day))
            candidate = date(today.year, month, day)
            if candidate < today:  # «15.01» в ноябре — это следующий год
                candidate = date(today.year + 1, month, day)
            return DateParse(True, candidate)
        except ValueError:
            return DateParse(False)

    return DateParse(False)


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
