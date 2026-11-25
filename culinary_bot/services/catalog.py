"""
Каталог блюд — постоянная база карточек шефа.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import Dish
from culinary_bot.services import ServiceError

# Поля карточки, которые можно менять через бота. Если добавишь новое поле
# в models.Dish и захочешь редактировать его кнопкой — допиши сюда.
EDITABLE_FIELDS = {"name", "emoji", "description", "portion", "price_cents", "photo_file_id"}


async def create_dish(
    session: AsyncSession,
    *,
    name: str,
    price_cents: int,
    emoji: str | None = None,
    description: str | None = None,
    portion: str | None = None,
    photo_file_id: str | None = None,
) -> Dish:
    """Создать карточку блюда. Возвращает созданный объект с заполненным id."""
    if not name.strip():
        raise ServiceError("Название блюда не может быть пустым.")
    dish = Dish(
        name=name.strip(),
        price_cents=price_cents,
        emoji=emoji,
        description=description,
        portion=portion,
        photo_file_id=photo_file_id,
    )
    session.add(dish)
    await session.flush()
    return dish


async def get_dish(session: AsyncSession, dish_id: int) -> Dish:
    """Карточка по id; если её нет — ServiceError."""
    dish = await session.get(Dish, dish_id)
    if dish is None:
        raise ServiceError("Блюдо не найдено.")
    return dish


async def list_dishes(session: AsyncSession, *, archived: bool = False) -> list[Dish]:
    """Список карточек по алфавиту: обычные или архивные."""
    result = await session.scalars(select(Dish).where(Dish.is_archived.is_(archived)).order_by(Dish.name))
    return list(result)


def update_dish_field(dish: Dish, field: str, value: object) -> None:
    """
    Изменить одно поле карточки.

    Проверка `field in EDITABLE_FIELDS` — защита: значение field приходит
    из кнопки, и без проверки через подделанную кнопку можно было бы
    поменять, например, id. setattr(obj, "name", v) — то же, что obj.name = v,
    только имя поля передаётся строкой.
    """
    if field not in EDITABLE_FIELDS:
        raise ServiceError("Это поле нельзя изменить.")
    setattr(dish, field, value)


def set_archived(dish: Dish, archived: bool) -> None:
    """Убрать блюдо в архив (не предлагать для новых выпусков) или вернуть из архива."""
    dish.is_archived = archived
