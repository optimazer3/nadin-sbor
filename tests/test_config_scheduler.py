from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from bot.config import ConfigError, load_settings
from bot.scheduler import setup_scheduler

MSK = ZoneInfo("Europe/Moscow")

BASE_ENV = {
    "BOT_TOKEN": "42:TEST",
    "ADMIN_IDS": "111, 222",
    "SUPABASE_URL": "https://x.supabase.co",
    "SUPABASE_KEY": "sb_secret_test",
    "ASSEMBLYAI_API_KEY": "aai",
    "LLM_API_KEY": "polza-test",
    "TEAM_MEMBERS": "Иван, Мария, Иван, ",
}


@pytest.fixture
def env(monkeypatch):
    for key in [
        "TIMEZONE",
        "REMINDER_DAY",
        "REMINDER_TIME",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "ASSEMBLYAI_SPEECH_MODEL",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_load_settings_defaults(env):
    s = load_settings(env_file=None)
    assert s.admin_ids == {111, 222}
    assert s.team_members == ("Иван", "Мария")
    assert s.timezone == MSK
    assert (s.reminder_day, s.reminder_time) == ("mon", time(10, 0))
    assert s.llm_model == "openai/gpt-4.1-mini"
    assert s.llm_base_url == "https://api.polza.ai/api/v1"
    assert s.llm_api_key == "polza-test"
    assert s.assemblyai_speech_model is None


@pytest.mark.parametrize(
    ("key", "value"),
    [("ADMIN_IDS", "abc"), ("REMINDER_DAY", "monday"), ("REMINDER_TIME", "10"), ("TIMEZONE", "Mars/Base")],
)
def test_load_settings_errors(env, key, value):
    env.setenv(key, value)
    with pytest.raises(ConfigError):
        load_settings(env_file=None)


@pytest.mark.parametrize("key", ["BOT_TOKEN", "LLM_API_KEY", "SUPABASE_KEY"])
def test_missing_required(env, key):
    env.delenv(key)
    with pytest.raises(ConfigError, match=key):
        load_settings(env_file=None)


def test_weekly_job_fires_monday_10_msk(env):
    settings = load_settings(env_file=None)
    scheduler = setup_scheduler(bot=None, repo=None, settings=settings)
    job = scheduler.get_job("weekly_dashboard")
    now = datetime(2026, 11, 6, 15, 0, tzinfo=MSK)  # пятница
    fire = job.trigger.get_next_fire_time(None, now)
    assert fire == datetime(2026, 11, 9, 10, 0, tzinfo=MSK)
    assert fire.weekday() == 0
    # Ровно в понедельник 10:00 следующая отправка — через неделю.
    after = job.trigger.get_next_fire_time(fire, fire.replace(minute=1))
    assert after == datetime(2026, 11, 16, 10, 0, tzinfo=MSK)


def _jwt(role: str) -> str:
    import base64
    import json

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part({'iss': 'supabase', 'role': role})}.signature"


@pytest.mark.parametrize("key", [_jwt("service_role"), "sb_secret_abc123"])
def test_supabase_secret_keys_accepted(env, key):
    env.setenv("SUPABASE_KEY", key)
    assert load_settings(env_file=None).supabase_key == key


@pytest.mark.parametrize("key", [_jwt("anon"), "sb_publishable_abc123"])
def test_supabase_public_keys_rejected(env, key):
    env.setenv("SUPABASE_KEY", key)
    with pytest.raises(ConfigError, match="service_role"):
        load_settings(env_file=None)


ENV_TEXT = "\n".join(f"{k}={v}" for k, v in BASE_ENV.items()) + "\nTEAM_MEMBERS=Иван,Мария\n"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Пустое окружение и пустая папка запуска — настройки берутся только из файла."""
    for key in [*BASE_ENV, "TIMEZONE", "REMINDER_DAY", "REMINDER_TIME", "LLM_MODEL", "LLM_BASE_URL"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("filename", [".env", ".env.txt", "env.txt"])
def test_env_file_names(clean_env, filename):
    (clean_env / filename).write_text(ENV_TEXT, encoding="utf-8")
    s = load_settings()
    assert s.bot_token == "42:TEST"
    assert s.team_members == ("Иван", "Мария")


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp1251"])
def test_env_file_encodings(clean_env, encoding):
    (clean_env / ".env").write_text(ENV_TEXT, encoding=encoding)
    s = load_settings()
    assert s.bot_token == "42:TEST"
    assert s.team_members == ("Иван", "Мария")


def test_env_file_missing(clean_env):
    with pytest.raises(ConfigError, match="Не найден файл .env"):
        load_settings()
