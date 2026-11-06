"""Настройки бота из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    supabase_url: str
    supabase_key: str
    assemblyai_api_key: str
    assemblyai_base_url: str
    assemblyai_language: str
    assemblyai_speech_model: str | None
    anthropic_model: str
    team_members: tuple[str, ...]
    timezone: ZoneInfo
    reminder_day: str
    reminder_time: time


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


def _parse_admin_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS: «{part}» — не число") from exc
    if not ids:
        raise ConfigError("ADMIN_IDS: укажите хотя бы один Telegram ID")
    return frozenset(ids)


def _parse_team(raw: str) -> tuple[str, ...]:
    seen: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if name and name not in seen:
            seen.append(name)
    return tuple(seen)


def _parse_time(raw: str) -> time:
    try:
        hours, minutes = raw.strip().split(":")
        return time(int(hours), int(minutes))
    except ValueError as exc:
        raise ConfigError(f"REMINDER_TIME: «{raw}» — ожидается формат ЧЧ:ММ") from exc


def load_settings() -> Settings:
    load_dotenv()

    reminder_day = os.getenv("REMINDER_DAY", "mon").strip().lower()
    if reminder_day not in WEEKDAYS:
        raise ConfigError(f"REMINDER_DAY: ожидается одно из {', '.join(WEEKDAYS)}")

    tz_name = os.getenv("TIMEZONE", "Europe/Moscow").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError / ValueError
        raise ConfigError(f"TIMEZONE: неизвестный часовой пояс «{tz_name}»") from exc

    # Ключ Claude API SDK читает сам из ANTHROPIC_API_KEY; проверяем, что он есть.
    _required("ANTHROPIC_API_KEY")

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        admin_ids=_parse_admin_ids(_required("ADMIN_IDS")),
        supabase_url=_required("SUPABASE_URL"),
        supabase_key=_required("SUPABASE_KEY"),
        assemblyai_api_key=_required("ASSEMBLYAI_API_KEY"),
        assemblyai_base_url=os.getenv("ASSEMBLYAI_BASE_URL", "").strip() or "https://api.assemblyai.com",
        assemblyai_language=os.getenv("ASSEMBLYAI_LANGUAGE", "").strip() or "ru",
        assemblyai_speech_model=os.getenv("ASSEMBLYAI_SPEECH_MODEL", "").strip() or None,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "").strip() or "claude-opus-5-5",
        team_members=_parse_team(os.getenv("TEAM_MEMBERS", "")),
        timezone=tz,
        reminder_day=reminder_day,
        reminder_time=_parse_time(os.getenv("REMINDER_TIME", "10:00")),
    )
