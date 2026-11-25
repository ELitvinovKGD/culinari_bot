"""
Точка входа: `python -m culinary_bot` запускает бота.

Что происходит при запуске:
1. Настраивается журнал (логи).
2. Подключаемся к базе, добавляем владельца из .env в админы.
3. Создаём Bot (связь с Telegram) и Dispatcher (распределитель событий).
4. Подключаем middleware и обработчики.
5. Запускаем long polling — бот сам регулярно спрашивает у Telegram
   «есть новые сообщения?». Публичный адрес и HTTPS для этого не нужны,
   поэтому бот одинаково работает на твоём компьютере и на сервере.

async/await: бот «асинхронный». Пока он ждёт ответа от Telegram или базы,
он не простаивает, а обрабатывает сообщения других людей. `await` —
«здесь можно подождать, переключись пока на другое».
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from culinary_bot.bot.handlers import build_router
from culinary_bot.bot.middlewares import ActorMiddleware, DbSessionMiddleware
from culinary_bot.config import settings
from culinary_bot.db.base import create_engine_and_sessionmaker
from culinary_bot.services import admins as admin_service

log = logging.getLogger(__name__)


async def main() -> None:
    """Собрать и запустить бота."""
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine, sessionmaker = create_engine_and_sessionmaker(settings.database_url)

    # Владелец из .env — всегда админ.
    async with sessionmaker() as session:
        await admin_service.ensure_owner(session, settings.owner_tg_id)
        await session.commit()

    # parse_mode=HTML по умолчанию для всех сообщений — см. texts.py.
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()

    # MemoryStorage хранит состояния диалогов (FSM) в памяти: при перезапуске
    # бота незаконченный ввод адреса сбросится, и клиент начнёт оформление
    # заново. Корзина при этом НЕ теряется — она в базе. Для нашего масштаба
    # это приемлемо; иначе понадобился бы Redis.
    dp = Dispatcher(storage=MemoryStorage(), bot_username=me.username)
    # Значения, переданные в Dispatcher(...) (bot_username), aiogram отдаёт
    # обработчикам по имени параметра — так обработчики узнают имя бота
    # для ссылок t.me/<имя>?start=...

    # outer_middleware на update — срабатывает до фильтров (см. middlewares.py).
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))
    dp.update.outer_middleware(ActorMiddleware())
    dp.include_router(build_router())

    # Меню команд в Telegram (кнопка «/» рядом с полем ввода).
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать"),
            BotCommand(command="menu", description="Меню текущего выпуска"),
            BotCommand(command="orders", description="Мои заказы"),
            BotCommand(command="delete_me", description="Удалить мои данные"),
        ]
    )

    log.info("Бот @%s запущен", me.username)
    try:
        # allowed_updates — получать только те типы событий, которые мы обрабатываем.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        # Аккуратно закрываем соединения при остановке (Ctrl+C или docker stop).
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
