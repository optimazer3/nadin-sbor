"""Еженедельная автоматическая отправка списка задач.

День и время отправки можно поменять голосом («присылай список по пятницам в 9 утра»).
Новое расписание сохраняется в файл data/schedule.json и переживает перезапуск бота.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import WEEKDAYS, Settings
from bot.dates import WEEKDAY_EVERY
from bot.numbering import Numbering
from bot.repository import TaskRepository
from bot.tasklist import list_messages, weekly_title

log = logging.getLogger(__name__)

JOB_ID = "weekly_dashboard"
DEFAULT_SCHEDULE_FILE = Path("data") / "schedule.json"


@dataclass(frozen=True)
class Schedule:
    day: str  # mon … sun
    at: time

    def describe(self) -> str:
        """«каждый понедельник в 10:00»"""
        return f"{WEEKDAY_EVERY[self.day]} в {self.at:%H:%M}"


class ReminderService:
    """Хранит расписание еженедельной сводки и переставляет задачу в планировщике."""

    def __init__(self, settings: Settings, path: Path | None = DEFAULT_SCHEDULE_FILE) -> None:
        self._settings = settings
        self._path = path
        self._scheduler: AsyncIOScheduler | None = None
        self.schedule = self._load() or Schedule(settings.reminder_day, settings.reminder_time)

    def _load(self) -> Schedule | None:
        if not self._path or not self._path.is_file():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            hours, minutes = str(data["time"]).split(":")
            schedule = Schedule(str(data["day"]), time(int(hours), int(minutes)))
            if schedule.day not in WEEKDAYS:
                raise ValueError(schedule.day)
            return schedule
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Не удалось прочитать %s (%s) — беру расписание из .env", self._path, exc)
            return None

    def _trigger(self) -> CronTrigger:
        return CronTrigger(
            day_of_week=self.schedule.day,
            hour=self.schedule.at.hour,
            minute=self.schedule.at.minute,
            timezone=self._settings.timezone,
        )

    def attach(
        self, scheduler: AsyncIOScheduler, bot: Bot, repo: TaskRepository, numbering: Numbering | None = None
    ) -> None:
        self._scheduler = scheduler
        scheduler.add_job(
            send_weekly_reminder,
            trigger=self._trigger(),
            kwargs={
                "bot": bot,
                "repo": repo,
                "settings": self._settings,
                "reminder": self,
                "numbering": numbering,
            },
            id=JOB_ID,
            replace_existing=True,
            # Если компьютер «проснулся» позже (но в течение 6 часов) — всё равно отправим.
            misfire_grace_time=6 * 3600,
            coalesce=True,
        )

    def set(self, day: str, at: time) -> None:
        if day not in WEEKDAYS:
            raise ValueError(f"Неизвестный день недели: {day}")
        self.schedule = Schedule(day, at)
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"day": day, "time": f"{at:%H:%M}"}, ensure_ascii=False), encoding="utf-8"
            )
        if self._scheduler and self._scheduler.get_job(JOB_ID):
            self._scheduler.reschedule_job(JOB_ID, trigger=self._trigger())
        log.info("Еженедельный список теперь: %s", self.schedule.describe())

    def next_run(self) -> datetime | None:
        now = datetime.now(self._settings.timezone)
        return self._trigger().get_next_fire_time(None, now)


async def send_list(
    bot: Bot,
    chat_id: int,
    repo: TaskRepository,
    settings: Settings,
    numbering: Numbering | None = None,
    title: str | None = None,
) -> None:
    for text in await list_messages(repo, settings, numbering or Numbering(), chat_id, title=title):
        await bot.send_message(chat_id, text)


async def send_weekly_reminder(
    bot: Bot,
    repo: TaskRepository,
    settings: Settings,
    reminder: ReminderService | None = None,
    numbering: Numbering | None = None,
) -> None:
    log.info("Отправляю еженедельный список задач: %s", sorted(settings.admin_ids))
    for admin_id in settings.admin_ids:
        try:
            await send_list(bot, admin_id, repo, settings, numbering, title=weekly_title(settings))
        except Exception:  # одному не дошло — остальным всё равно отправляем
            log.exception("Не удалось отправить список пользователю %s", admin_id)


def setup_scheduler(
    bot: Bot,
    repo: TaskRepository,
    settings: Settings,
    reminder: ReminderService | None = None,
    numbering: Numbering | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    (reminder or ReminderService(settings, path=None)).attach(scheduler, bot, repo, numbering)
    return scheduler
