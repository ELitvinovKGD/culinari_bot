"""
Каталог блюд — постоянная база карточек шефа.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import Dish, IssueItem
from culinary_bot.services import ServiceError

# Поля карточки, которые можно менять через бота. Если добавишь новое поле
# в models.Dish и захочешь редактировать его кнопкой — допиши сюда.
EDITABLE_FIELDS = {"name", "emoji", "description", "portion", "composition", "price_cents", "photo_file_id"}


async def create_dish(
    session: AsyncSession,
    *,
    name: str,
    price_cents: int,
    emoji: str | None = None,
    description: str | None = None,
    portion: str | None = None,
    composition: str | None = None,
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
        composition=composition,
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



async def list_dishes_recent_first(session: AsyncSession, recent_count: int) -> tuple[list[Dish], set[int]]:
    """
    Каталог для выбора: сначала НЕДАВНИЕ (блюда, которые последними ставили
    в лавки или «вне лавки» — их шеф повторяет чаще всего), потом остальные
    по алфавиту. Возвращает (список, id недавних — чтобы пометить их 🕘).
    """
    dishes = await list_dishes(session)
    # Для каждого блюда — номер последней позиции в выпусках (чем больше, тем позже).
    rows = await session.execute(select(IssueItem.dish_id, func.max(IssueItem.id)).group_by(IssueItem.dish_id))
    last_used = {dish_id: last_id for dish_id, last_id in rows}
    used = sorted((d for d in dishes if d.id in last_used), key=lambda d: last_used[d.id], reverse=True)
    recent = used[:recent_count]
    recent_ids = {d.id for d in recent}
    rest = [d for d in dishes if d.id not in recent_ids]  # list_dishes уже по алфавиту
    return recent + rest, recent_ids


def search_dishes(dishes: list[Dish], query: str) -> list[tuple[Dish, str | None]]:
    """
    Поиск по части слова в названии, составе и описании (решение от 26.11):
    «курица» найдёт и «Сациви», если курица в составе.

    Возвращает [(блюдо, причина)], где причина — None, если нашлось по названию,
    или подсказка «состав: …», «описание: …».

    Ищем в Python, а не в SQL: блюд у шефа сотни, не миллионы, а SQLite
    (в тестах) не умеет без учёта регистра сравнивать русские буквы.
    casefold() — «умный» lower(): «Курица» и «курица» совпадут.
    """
    needle = query.strip().casefold()
    if not needle:
        return []
    found: list[tuple[Dish, str | None]] = []
    for dish in dishes:
        if needle in dish.name.casefold():
            found.append((dish, None))
        elif dish.composition and needle in dish.composition.casefold():
            found.append((dish, f"состав: {_around(dish.composition, needle)}"))
        elif dish.description and needle in dish.description.casefold():
            found.append((dish, f"описание: {_around(dish.description, needle)}"))
    return found


def _around(text: str, needle: str, width: int = 18) -> str:
    """Кусочек текста вокруг найденного слова: «…копчёные рёбрышки, курица, лук…»."""
    at = text.casefold().find(needle)
    start, end = max(0, at - width // 2), min(len(text), at + len(needle) + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
