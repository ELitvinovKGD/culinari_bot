"""
Общие заготовки (фикстуры) для тестов.

Фикстура в pytest — функция, которая готовит что-то для теста. Тест просто
пишет её имя в параметрах (`async def test_x(session): ...`), и pytest сам
вызывает фикстуру и передаёт результат.

Тесты работают с базой SQLite в памяти: она создаётся заново для каждого
теста за миллисекунды и не требует Docker. Структура таблиц та же,
что и в PostgreSQL, — её строит SQLAlchemy по models.py.
"""

import os

# Настройки должны существовать ДО импорта culinary_bot.config — иначе
# Settings() упадёт без BOT_TOKEN. setdefault не перезапишет настоящие значения.
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_TG_ID", "1")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from culinary_bot.db.base import Base  # noqa: E402
from culinary_bot.db.models import DeliveryType  # noqa: E402
from culinary_bot.services import catalog, customers, issues  # noqa: E402


@pytest.fixture
async def session():
    """
    Чистая база для одного теста.

    StaticPool — одно общее соединение: база в памяти SQLite живёт, пока
    живо соединение, и без этого каждое новое соединение видело бы пустую базу.
    """
    # Если задан TEST_DATABASE_URL — тесты идут на настоящем PostgreSQL
    # (полезно перед релизом: проверить, что всё работает и там).
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        engine = create_async_engine(url)
    else:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
async def open_issue(session):
    """
    Готовый открытый выпуск с двумя блюдами: суп 10 € и биточки 12.50 €.
    """
    soup = await catalog.create_dish(session, name="Гороховый суп", price_cents=1000, portion="700 г")
    patties = await catalog.create_dish(session, name="Биточки", price_cents=1250, portion="5 шт.")
    issue = await issues.create_draft(session, "GREAT HITS")
    await issues.add_dish_to_issue(session, issue, soup)
    await issues.add_dish_to_issue(session, issue, patties)
    await issues.open_issue(session, issue)
    await session.flush()
    return issue


async def make_customer(session, tg_id: int = 100, name: str = "Ева"):
    """Клиент с заполненными контактами (вспомогательная функция для тестов)."""
    customer = await customers.get_or_create(session, tg_id, f"user{tg_id}")
    customers.save_contact(
        customer, name=name, phone="+995 555 123 456", delivery_type=DeliveryType.DELIVERY, address="ул. Руставели 1"
    )
    return customer
