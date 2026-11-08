"""Общие помощники для обработчиков."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InaccessibleMessage, InlineKeyboardMarkup, Message

from bot.services.transcriber import AssemblyAITranscriber, TranscriptionError

log = logging.getLogger(__name__)

# Bot API позволяет ботам скачивать файлы до 20 МБ.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def today_in(tz: ZoneInfo) -> date:
    return datetime.now(tz).date()


async def transcribe_message(message: Message, bot: Bot, transcriber: AssemblyAITranscriber) -> str:
    media = message.voice or message.audio or message.video_note
    if media is None:
        raise TranscriptionError("В сообщении нет аудио")
    if media.file_size and media.file_size > MAX_DOWNLOAD_BYTES:
        raise TranscriptionError("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие файлы")
    buffer = await bot.download(media)
    if buffer is None:
        raise TranscriptionError("Не удалось скачать аудио из Telegram")
    return await transcriber.transcribe(buffer.read())


async def safe_edit(
    message: Message | InaccessibleMessage, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """edit_text, который не падает на «message is not modified».

    Если исходное сообщение недоступно (удалено), отправляет новое.
    """
    if not isinstance(message, Message):
        await message.bot.send_message(message.chat.id, text, reply_markup=reply_markup)
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def drop_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Убирает inline-кнопки у старого сообщения (если оно ещё существует)."""
    if not message_id:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except TelegramBadRequest:
        pass
