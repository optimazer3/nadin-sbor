"""Доступ к боту только для руководителя (ADMIN_IDS) и только в личных сообщениях."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

log = logging.getLogger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")

        if isinstance(event, Message) and event.chat.type != "private":
            return None  # в группах бот молчит

        if user is not None and user.id in self.admin_ids:
            return await handler(event, data)

        log.info("Отказ в доступе: user_id=%s", user.id if user else None)
        if isinstance(event, Message) and user is not None:
            await event.answer(
                "Этот бот работает только для руководителя.\n\n"
                f"Ваш Telegram ID: <code>{user.id}</code>\n"
                "Если это вы — добавьте ID в ADMIN_IDS в файле .env и перезапустите бота."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None
