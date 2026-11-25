"""
Корзина клиента.

Корзина живёт в базе (таблица cart_items): клиент может набрать блюда,
уйти и вернуться через день — всё на месте. Очищается после оформления
заказа и при закрытии приёма.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import CartItem, Customer, Issue, IssueItem, IssueStatus
from culinary_bot.services import ServiceError

# Ограничение на количество одной позиции — защита от случайного "+" 500 раз.
MAX_QUANTITY = 50


async def get_cart(session: AsyncSession, customer: Customer, issue: Issue) -> list[CartItem]:
    """
    Содержимое корзины клиента по выпуску, в порядке номеров блюд.

    Позиции, снятые с продажи, сюда не попадают (они удаляются из корзин
    при снятии, но фильтр — дополнительная страховка).
    """
    result = await session.scalars(
        select(CartItem)
        .join(IssueItem)
        .where(
            CartItem.customer_id == customer.id,
            IssueItem.issue_id == issue.id,
            IssueItem.is_active.is_(True),
        )
        .order_by(IssueItem.position)
    )
    return list(result)


def quantities(cart: list[CartItem]) -> dict[int, int]:
    """Корзина в виде словаря {id позиции: количество} — удобно для кнопок."""
    return {cart_item.issue_item_id: cart_item.quantity for cart_item in cart}


def cart_total_cents(cart: list[CartItem]) -> int:
    """Сумма корзины в центах."""
    return sum(ci.issue_item.price_cents * ci.quantity for ci in cart)


def cart_count(cart: list[CartItem]) -> int:
    """Сколько всего порций в корзине."""
    return sum(ci.quantity for ci in cart)


async def change_quantity(session: AsyncSession, customer: Customer, item: IssueItem, delta: int) -> int:
    """
    Изменить количество позиции в корзине на delta (+1 или -1).

    Возвращает новое количество. При 0 строка корзины удаляется.
    Ошибка, если выпуск уже не принимает заказы или блюдо снято с продажи.
    """
    if item.issue.status != IssueStatus.OPEN:
        raise ServiceError("Приём заказов уже закрыт.")
    if not item.is_active:
        raise ServiceError("Это блюдо уже закончилось.")

    cart_item = await session.scalar(
        select(CartItem).where(CartItem.customer_id == customer.id, CartItem.issue_item_id == item.id)
    )
    current = cart_item.quantity if cart_item else 0
    new = max(0, min(MAX_QUANTITY, current + delta))

    if new == 0:
        if cart_item is not None:
            await session.delete(cart_item)
    elif cart_item is None:
        session.add(CartItem(customer_id=customer.id, issue_item_id=item.id, quantity=new))
    else:
        cart_item.quantity = new
    await session.flush()
    return new


async def clear_cart(session: AsyncSession, customer: Customer, issue: Issue) -> None:
    """Очистить корзину клиента по выпуску (после оформления заказа)."""
    for cart_item in await get_cart(session, customer, issue):
        await session.delete(cart_item)
