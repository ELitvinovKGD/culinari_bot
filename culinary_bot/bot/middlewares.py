"""
Middleware — «прослойки», через которые проходит каждое событие от Telegram
перед тем, как попасть в обработчик.

Схема:  Telegram → DbSessionMiddleware → ActorMiddleware → фильтры → обработчик

Оба middleware регистрируются как «внешние» (outer) на уровне update —
они срабатывают ДО фильтров. Это важно: фильтр IsAdmin (filters.py)
читает is_admin, который кладёт сюда ActorMiddleware.

Middleware может добавить в словарь `data` значения, и aiogram передаст их
в обработчик по имени параметра. Например, обработчик
`async def show_menu(message, session, customer)` получит готовую сессию
базы и клиента, не создавая их сам.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from culinary_bot.services import admins as admin_service
from culinary_bot.services import customers as customer_service


class DbSessionMiddleware(BaseMiddleware):
    """
    Открывает сессию базы на время обработки одного события.

    Если обработчик завершился без ошибок — сохраняем изменения (commit).
    Если упал с исключением — откатываем (rollback): в базе не останется
    «половины заказа». Это и есть транзакция — всё или ничего.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # `async with` гарантирует, что сессия закроется даже при ошибке.
        async with self.sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


class ActorMiddleware(BaseMiddleware):
    """
    Определяет, кто прислал событие: клиент (создаёт запись при первом визите)
    и является ли он админом.

    В обработчик попадают:
        customer  — запись Customer этого человека;
        admin     — запись Admin или None;
        is_admin  — True/False (для фильтра IsAdmin).
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # aiogram сам кладёт в data["event_from_user"] автора события.
        user: User | None = data.get("event_from_user")
        session: AsyncSession = data["session"]
        if user is not None and not user.is_bot:
            data["customer"] = await customer_service.get_or_create(session, user.id, user.username)
            admin = await admin_service.get_admin(session, user.id)
            data["admin"] = admin
            data["is_admin"] = admin is not None
        else:
            data["customer"] = None
            data["admin"] = None
            data["is_admin"] = False
        return await handler(event, data)
