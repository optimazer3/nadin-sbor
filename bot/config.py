"""Настройки бота из переменных окружения (.env)."""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from bot.proxy import Proxy, ProxyError, parse_services

log = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Polza AI — OpenAI-совместимый API. Точный адрес и ID модели сверьте в личном кабинете Polza.
DEFAULT_LLM_BASE_URL = "https://api.polza.ai/api/v1"
DEFAULT_LLM_MODEL = "openai/gpt-4.1-mini"


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
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    team_members: tuple[str, ...]
    timezone: ZoneInfo
    reminder_day: str
    reminder_time: time
    proxy: Proxy | None = None
    proxy_services: frozenset[str] = frozenset()

    def proxy_for(self, service: str) -> Proxy | None:
        """Прокси для сервиса (telegram / assemblyai / supabase / llm) или None — напрямую."""
        return self.proxy if self.proxy and service in self.proxy_services else None


def _parse_proxy() -> tuple[Proxy | None, frozenset[str]]:
    raw = os.getenv("PROXY_URL", "").strip()
    if not raw:
        return None, frozenset()
    try:
        return Proxy.parse(raw), parse_services(os.getenv("PROXY_SERVICES"))
    except ProxyError as exc:
        raise ConfigError(str(exc)) from exc


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


def _check_supabase_key(key: str) -> str:
    """Не даём по ошибке подставить публичный ключ (anon / publishable) вместо service_role."""
    wrong = (
        "SUPABASE_KEY: это публичный ключ ({kind}). Боту нужен секретный ключ: "
        "service_role (вкладка Legacy API Keys) или Secret key sb_secret_… (вкладка API Keys)"
    )
    if key.startswith("sb_publishable_"):
        raise ConfigError(wrong.format(kind="publishable"))
    if key.startswith("eyJ") and key.count(".") == 2:  # старый формат ключа — JWT
        try:
            payload = key.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, json.JSONDecodeError):
            return key
        if claims.get("role") == "anon":
            raise ConfigError(wrong.format(kind="anon"))
    return key


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


def find_env_file(name: str = ".env") -> Path | None:
    """Ищет .env в папке запуска.

    Блокнот в Windows часто сохраняет файл как «.env.txt», а Проводник прячет расширение,
    поэтому такой файл тоже принимаем (и предупреждаем в логе).
    """
    path = Path(name)
    if path.is_file():
        return path
    for alt in (path.with_name(path.name + ".txt"), path.with_name(path.name.lstrip(".") + ".txt")):
        if alt.is_file():
            log.warning(
                "Файл %s не найден, читаю настройки из %s. Лучше переименуйте его в %s",
                path.name,
                alt.name,
                path.name,
            )
            return alt
    return None


def _load_env_file(path: Path) -> None:
    # utf-8-sig понимает и обычный UTF-8, и UTF-8 с BOM (так сохраняют некоторые редакторы);
    # cp1251 — кодировка «ANSI» старого Блокнота в русской Windows.
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        load_dotenv(path, encoding=encoding)
        return
    raise ConfigError(f"Не удалось прочитать {path.name}: сохраните файл в кодировке UTF-8")


def load_settings(env_file: str | None = ".env") -> Settings:
    """Читает настройки. env_file — имя .env в папке запуска (None — не читать файл)."""
    if env_file:
        found = find_env_file(env_file)
        if found is not None:
            _load_env_file(found)
        elif not os.getenv("BOT_TOKEN"):
            raise ConfigError(
                f"Не найден файл {env_file} в папке {Path.cwd()}. Создайте его из .env.example "
                "(см. README, шаг 8) и запускайте бота из папки проекта"
            )

    reminder_day = os.getenv("REMINDER_DAY", "mon").strip().lower()
    if reminder_day not in WEEKDAYS:
        raise ConfigError(f"REMINDER_DAY: ожидается одно из {', '.join(WEEKDAYS)}")

    tz_name = os.getenv("TIMEZONE", "Europe/Moscow").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError / ValueError
        raise ConfigError(f"TIMEZONE: неизвестный часовой пояс «{tz_name}»") from exc

    proxy, proxy_services = _parse_proxy()

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        admin_ids=_parse_admin_ids(_required("ADMIN_IDS")),
        supabase_url=_required("SUPABASE_URL"),
        supabase_key=_check_supabase_key(_required("SUPABASE_KEY")),
        assemblyai_api_key=_required("ASSEMBLYAI_API_KEY"),
        assemblyai_base_url=os.getenv("ASSEMBLYAI_BASE_URL", "").strip() or "https://api.assemblyai.com",
        assemblyai_language=os.getenv("ASSEMBLYAI_LANGUAGE", "").strip() or "ru",
        assemblyai_speech_model=os.getenv("ASSEMBLYAI_SPEECH_MODEL", "").strip() or None,
        llm_api_key=_required("LLM_API_KEY"),
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip() or DEFAULT_LLM_BASE_URL,
        llm_model=os.getenv("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL,
        team_members=_parse_team(os.getenv("TEAM_MEMBERS", "")),
        timezone=tz,
        reminder_day=reminder_day,
        reminder_time=_parse_time(os.getenv("REMINDER_TIME", "10:00")),
        proxy=proxy,
        proxy_services=proxy_services,
    )
