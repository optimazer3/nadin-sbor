"""Точка входа: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx
import openai
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from postgrest.exceptions import APIError
from supabase import AsyncClientOptions, acreate_client

from bot.config import ConfigError, Settings, load_settings
from bot.focus import FocusStore
from bot.handlers import get_routers
from bot.middlewares import AdminOnlyMiddleware
from bot.repository import SupabaseTaskRepository
from bot.scheduler import ReminderService, setup_scheduler
from bot.services.task_parser import TaskParser
from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError
from bot.supabase_key import WHERE, inspect_key, project_ref

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


async def _check_telegram(bot: Bot, settings: Settings) -> None:
    proxy = settings.proxy_for("telegram")
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError as exc:
        raise StartupError("Telegram не принял BOT_TOKEN — проверьте токен от @BotFather") from exc
    except (
        TelegramNetworkError,
        ProxyError,
        ProxyConnectionError,
        ProxyTimeoutError,
        OSError,
        TimeoutError,
    ) as exc:
        if proxy:
            raise StartupError(
                f"Не удалось подключиться к Telegram через прокси {proxy}: {exc}\n"
                "Проверьте, что Happ запущен и подключён к серверу, и что порт в PROXY_URL "
                "совпадает с портом в настройках Happ (см. README, шаг «Прокси»)"
            ) from exc
        raise StartupError(
            f"Нет связи с Telegram: {exc}\n"
            "Из России Telegram API без прокси недоступен: включите Happ и укажите PROXY_URL "
            "в .env (см. README, шаг «Прокси»)"
        ) from exc
    log.info("Telegram: бот @%s", me.username)


async def _check_assemblyai(transcriber: AssemblyAITranscriber) -> None:
    # Без AssemblyAI бот всё равно работает с текстом, поэтому здесь только предупреждение.
    try:
        await transcriber.check()
    except TranscriptionError as exc:
        log.warning("AssemblyAI недоступен — голосовые распознаваться не будут: %s", exc)
    else:
        log.info("AssemblyAI: ключ принят")


async def _check_supabase(repo: SupabaseTaskRepository, settings: Settings) -> None:
    info = inspect_key(settings.supabase_key, settings.supabase_url)
    log.info("Supabase: ключ %s", info.describe())
    try:
        await repo.check()
    except APIError as exc:
        message = exc.message or str(exc)
        if "Legacy API keys are disabled" in message:
            raise StartupError(
                "Supabase: в проекте отключены старые ключи (service_role eyJ…). "
                "Возьмите Secret key: Project Settings → API Keys → вкладка «API Keys» → Secret keys "
                "(начинается с sb_secret_…)"
            ) from exc
        if "Invalid API key" in message or "JWT" in message:
            raise StartupError(
                f"Supabase не принял ключ ({message}).\n"
                f"Сейчас в SUPABASE_KEY: {info.describe()}.\n"
                f"Откройте в Supabase проект {project_ref(settings.supabase_url) or ''} → {WHERE}. "
                "Нажмите Copy и вставьте ключ в .env целиком, одной строкой, без кавычек. "
                "Сверьте начало и конец ключа с тем, что выше. Если не помогает — возьмите ключ "
                "с другой вкладки (sb_secret_… вместо eyJ… или наоборот)"
            ) from exc
        if exc.code in {"42P01", "PGRST205"} or "does not exist" in message or "schema cache" in message:
            raise StartupError(
                f"Supabase: таблицы tasks нет ({message}). Выполните файл supabase/schema.sql "
                "в SQL Editor (README, шаг 4.2)"
            ) from exc
        raise StartupError(f"Supabase вернул ошибку: {message}") from exc
    except Exception as exc:
        raise StartupError(
            f"Не удалось подключиться к Supabase: {exc!r}\nПроверьте SUPABASE_URL и прокси (PROXY_URL)"
        ) from exc
    log.info("Supabase: таблица tasks доступна")


async def main() -> None:
    settings = load_settings()
    if settings.proxy:
        log.info("Прокси %s для: %s", settings.proxy, ", ".join(sorted(settings.proxy_services)))
    else:
        log.info("Прокси не задан (PROXY_URL) — подключаюсь напрямую")

    tg_proxy = settings.proxy_for("telegram")
    bot = Bot(
        settings.bot_token,
        session=AiohttpSession(proxy=tg_proxy.aiohttp_url) if tg_proxy else None,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    aai_proxy = settings.proxy_for("assemblyai")
    transcriber = AssemblyAITranscriber(
        api_key=settings.assemblyai_api_key,
        base_url=settings.assemblyai_base_url,
        language_code=settings.assemblyai_language,
        speech_model=settings.assemblyai_speech_model,
        proxy=aai_proxy.aiohttp_url if aai_proxy else None,
    )
    sb_proxy = settings.proxy_for("supabase")
    sb_http = sb_proxy.httpx_client(httpx.AsyncClient, timeout=30.0) if sb_proxy else None
    llm_proxy = settings.proxy_for("llm")
    llm_http = llm_proxy.httpx_client(openai.DefaultAsyncHttpxClient) if llm_proxy else None

    scheduler = None
    try:
        await _check_telegram(bot, settings)

        supabase = await acreate_client(
            settings.supabase_url,
            settings.supabase_key,
            options=AsyncClientOptions(httpx_client=sb_http) if sb_http else None,
        )
        repo = SupabaseTaskRepository(supabase)
        await _check_supabase(repo, settings)
        await _check_assemblyai(transcriber)

        parser = TaskParser(
            openai.AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=60.0,
                http_client=llm_http,
            ),
            model=settings.llm_model,
            team_members=settings.team_members,
        )
        log.info("Нейросеть: модель %s через %s", settings.llm_model, settings.llm_base_url)

        reminder = ReminderService(settings)
        dp = build_dispatcher(
            settings, repo=repo, transcriber=transcriber, parser=parser, focus=FocusStore(), reminder=reminder
        )

        scheduler = setup_scheduler(bot, repo, settings, reminder)
        scheduler.start()
        log.info(
            "Еженедельная сводка: %s, следующая отправка %s",
            reminder.schedule.describe(),
            reminder.next_run(),
        )

        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Как пользоваться"),
                BotCommand(command="tasks", description="Задачи"),
                BotCommand(command="dashboard", description="Сводка"),
            ]
        )

        log.info("Бот запущен. Напишите ему /start в Telegram. Остановить — Ctrl+C")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await transcriber.close()
        await bot.session.close()
        for client in (sb_http, llm_http):
            if client is not None:
                await client.aclose()


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
