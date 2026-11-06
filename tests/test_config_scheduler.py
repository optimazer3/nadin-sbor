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
    "SUPABASE_KEY": "key",
    "ASSEMBLYAI_API_KEY": "aai",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "TEAM_MEMBERS": "Иван, Мария, Иван, ",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # чтобы не подхватился настоящий .env
    for key in ["TIMEZONE", "REMINDER_DAY", "REMINDER_TIME", "ANTHROPIC_MODEL", "ASSEMBLYAI_SPEECH_MODEL"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_load_settings_defaults(env):
    s = load_settings()
    assert s.admin_ids == {111, 222}
    assert s.team_members == ("Иван", "Мария")
    assert s.timezone == MSK
    assert (s.reminder_day, s.reminder_time) == ("mon", time(10, 0))
    assert s.anthropic_model == "claude-opus-5-5"
    assert s.assemblyai_speech_model is None


@pytest.mark.parametrize(
    ("key", "value"),
    [("ADMIN_IDS", "abc"), ("REMINDER_DAY", "monday"), ("REMINDER_TIME", "10"), ("TIMEZONE", "Mars/Base")],
)
def test_load_settings_errors(env, key, value):
    env.setenv(key, value)
    with pytest.raises(ConfigError):
        load_settings()


def test_missing_required(env):
    env.delenv("BOT_TOKEN")
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        load_settings()


def test_weekly_job_fires_monday_10_msk(env):
    settings = load_settings()
    scheduler = setup_scheduler(bot=None, repo=None, settings=settings)
    job = scheduler.get_job("weekly_dashboard")
    now = datetime(2026, 11, 6, 15, 0, tzinfo=MSK)  # пятница
    fire = job.trigger.get_next_fire_time(None, now)
    assert fire == datetime(2026, 11, 9, 10, 0, tzinfo=MSK)
    assert fire.weekday() == 0
    # Ровно в понедельник 10:00 следующая отправка — через неделю.
    after = job.trigger.get_next_fire_time(fire, fire.replace(minute=1))
    assert after == datetime(2026, 11, 16, 10, 0, tzinfo=MSK)
