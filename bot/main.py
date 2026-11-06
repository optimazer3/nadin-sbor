"""Точка входа: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import sys

import openai
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from supabase import acreate_client

from bot.config import ConfigError, Settings, load_settings
from bot.handlers import get_routers
from bot.middlewares import AdminOnlyMiddleware
from bot.repository import SupabaseTaskRepository
from bot.scheduler import setup_scheduler
from bot.services.task_parser import TaskParser
from bot.services.transcriber import AssemblyAITranscriber

log = logging.getLogger("bot")


def build_dispatcher(settings: Settings, **deps) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), settings=settings, **deps)
    admin_only = AdminOnlyMiddleware(settings.admin_ids)
    dp.message.outer_middleware(admin_only)
    dp.callback_query.outer_middleware(admin_only)
    dp.include_routers(*get_routers())
    return dp


class StartupError(RuntimeError):
    pass


async def _check_telegram(bot: Bot) -> None:
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError as exc:
        raise StartupError("Telegram не принял BOT_TOKEN — проверьте токен от @BotFather") from exc
    except TelegramNetworkError as exc:
        raise StartupError(f"Нет связи с Telegram: {exc}") from exc
    log.info("Telegram: бот @%s", me.username)


async def _check_supabase(repo: SupabaseTaskRepository) -> None:
    try:
        await repo.check()
    except Exception as exc:
        raise StartupError(
            f"Не удалось прочитать таблицу tasks в Supabase: {exc}\n"
            "Проверьте SUPABASE_URL и SUPABASE_KEY (нужен ключ service_role / secret) "
            "и что в SQL Editor выполнен файл supabase/schema.sql"
        ) from exc
    log.info("Supabase: таблица tasks доступна")


async def main() -> None:
    settings = load_settings()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    transcriber = AssemblyAITranscriber(
        api_key=settings.assemblyai_api_key,
        base_url=settings.assemblyai_base_url,
        language_code=settings.assemblyai_language,
        speech_model=settings.assemblyai_speech_model,
    )
    scheduler = None
    try:
        await _check_telegram(bot)

        supabase = await acreate_client(settings.supabase_url, settings.supabase_key)
        repo = SupabaseTaskRepository(supabase)
        await _check_supabase(repo)

        parser = TaskParser(
            openai.AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=60.0),
            model=settings.llm_model,
            team_members=settings.team_members,
        )
        log.info("Нейросеть: модель %s через %s", settings.llm_model, settings.llm_base_url)

        dp = build_dispatcher(settings, repo=repo, transcriber=transcriber, parser=parser)

        scheduler = setup_scheduler(bot, repo, settings)
        scheduler.start()
        job = scheduler.get_job("weekly_dashboard")
        log.info("Еженедельный дашборд: следующая отправка %s", job.next_run_time if job else "—")

        await bot.set_my_commands(
            [
                BotCommand(command="tasks", description="Список задач"),
                BotCommand(command="dashboard", description="Дашборд активных задач"),
                BotCommand(command="cancel", description="Отменить текущее действие"),
                BotCommand(command="help", description="Как пользоваться"),
            ]
        )

        log.info("Бот запущен. Напишите ему /start в Telegram. Остановить — Ctrl+C")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await transcriber.close()
        await bot.session.close()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except ConfigError as exc:
        log.error("Ошибка в настройках (.env): %s", exc)
        sys.exit(1)
    except StartupError as exc:
        log.error("Бот не запустился: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
