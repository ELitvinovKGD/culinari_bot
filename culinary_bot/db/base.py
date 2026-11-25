"""
Подключение к базе данных.

Здесь создаются:
- `Base` — родительский класс для всех таблиц (см. models.py);
- `create_engine_and_sessionmaker()` — подключение к базе и «фабрика сессий».

Сессия (session) — это одно «рабочее окно» в базу: через неё читаем и
изменяем данные, а в конце либо сохраняем всё разом (commit), либо
откатываем (rollback). На каждое сообщение от Telegram открывается своя
сессия — это делает middleware в bot/middlewares.py.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Общий предок всех таблиц.

    SQLAlchemy собирает в `Base.metadata` описание всех классов-наследников.
    По этому описанию Alembic понимает, какие таблицы должны быть в базе.
    """


def create_engine_and_sessionmaker(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """
    Создать подключение к базе и фабрику сессий.

    Engine — пул соединений с базой: держит несколько открытых соединений
    и раздаёт их по запросу, чтобы не подключаться заново на каждое сообщение.

    Принимает адрес базы (например, из settings.database_url).
    Возвращает пару (engine, sessionmaker): engine нужен, чтобы закрыть
    соединения при остановке бота, sessionmaker — чтобы открывать сессии.
    """
    # pool_pre_ping=True: перед использованием соединения проверить, живо ли оно.
    # Без этого после перезапуска базы бот получал бы ошибку на первом запросе.
    engine = create_async_engine(database_url, pool_pre_ping=True)

    # expire_on_commit=False: после commit объекты остаются читаемыми.
    # По умолчанию SQLAlchemy "забывает" значения после commit и при
    # следующем обращении идёт в базу снова — в асинхронном коде это
    # приводит к ошибкам, если сессия уже закрыта.
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker
