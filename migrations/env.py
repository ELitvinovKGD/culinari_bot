"""
Окружение Alembic: как подключиться к базе и где взять описание таблиц.

Этот файл Alembic запускает сам при командах `alembic upgrade head`
и `alembic revision --autogenerate`. Менять его приходится редко.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# Импорт models нужен, чтобы все классы-таблицы "зарегистрировались" в Base.metadata.
# Без него autogenerate решил бы, что таблиц нет, и предложил бы их удалить.
from culinary_bot.db import models  # noqa: F401
from culinary_bot.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Описание того, какими таблицы ДОЛЖНЫ быть. Autogenerate сравнивает его
# с реальной базой и пишет миграцию с разницей.
target_metadata = Base.metadata


def get_database_url() -> str:
    """
    Адрес базы для миграций.

    Берём DATABASE_URL напрямую из окружения, а не из culinary_bot.config:
    для миграций не нужен BOT_TOKEN, и так `alembic` можно запускать
    без полного .env.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Не задан DATABASE_URL — адрес базы данных для миграций.")
    return url


def run_migrations_offline() -> None:
    """Режим "без подключения": Alembic только печатает SQL (alembic upgrade head --sql)."""
    context.configure(url=get_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Применить миграции через уже открытое соединение."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Обычный режим: подключиться к базе и применить миграции.

    Наш драйвер (asyncpg) асинхронный, а Alembic — обычный (синхронный).
    run_sync запускает синхронную функцию Alembic внутри асинхронного соединения.
    """
    engine = create_async_engine(get_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
