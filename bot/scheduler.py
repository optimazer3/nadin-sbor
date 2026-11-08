"""Дашборд и еженедельное напоминание (по умолчанию — понедельник 10:00 по Москве)."""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.config import Settings
from bot.keyboards import ListCb
from bot.repository import TaskRepository
from bot.utils import today_in
from bot.views import dashboard

log = logging.getLogger(__name__)

WEEKLY_TITLE = "🔔 <b>Еженедельный дашборд задач</b>"


def _open_list_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Открыть список задач", callback_data=ListCb(f="active", p=0).pack()
                )
            ]
        ]
    )


DASHBOARD_TITLE = "📊 <b>Дашборд задач</b>"


async def dashboard_messages(
    repo: TaskRepository, settings: Settings, title: str = DASHBOARD_TITLE
) -> list[tuple[str, InlineKeyboardMarkup | None]]:
    """Сообщения дашборда; у последнего — кнопка «Открыть список задач»."""
    tasks = await repo.list_active()
    chunks = dashboard(tasks, today_in(settings.timezone), title=title)
    return [
        (chunk, _open_list_keyboard() if i == len(chunks) - 1 else None) for i, chunk in enumerate(chunks)
    ]


async def send_dashboard(
    bot: Bot,
    chat_id: int,
    repo: TaskRepository,
    settings: Settings,
    title: str = DASHBOARD_TITLE,
) -> None:
    for text, kb in await dashboard_messages(repo, settings, title):
        await bot.send_message(chat_id, text, reply_markup=kb)


async def send_weekly_reminder(bot: Bot, repo: TaskRepository, settings: Settings) -> None:
    log.info("Отправляю еженедельный дашборд: %s", sorted(settings.admin_ids))
    for admin_id in settings.admin_ids:
        try:
            await send_dashboard(bot, admin_id, repo, settings, title=WEEKLY_TITLE)
        except Exception:  # одному админу не дошло — остальным всё равно отправляем
            log.exception("Не удалось отправить дашборд пользователю %s", admin_id)


def setup_scheduler(bot: Bot, repo: TaskRepository, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    trigger = CronTrigger(
        day_of_week=settings.reminder_day,
        hour=settings.reminder_time.hour,
        minute=settings.reminder_time.minute,
        timezone=settings.timezone,
    )
    scheduler.add_job(
        send_weekly_reminder,
        trigger=trigger,
        kwargs={"bot": bot, "repo": repo, "settings": settings},
        id="weekly_dashboard",
        replace_existing=True,
        # Если компьютер «проснулся» позже 10:00 (но в течение 6 часов) — всё равно отправим.
        misfire_grace_time=6 * 3600,
        coalesce=True,
    )
    return scheduler
