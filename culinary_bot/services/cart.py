"""
Корзина клиента.

Корзина живёт в базе (таблица cart_items): клиент может набрать блюда,
уйти и вернуться через день — всё на месте. Очищается после оформления
заказа и при закрытии приёма.
"""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import CartItem, Customer, Issue, IssueItem, IssueStatus, utcnow
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
    if new == current:
        return current

    if item.is_stock:
        await reserve(session, item, new - current)

    if new == 0:
        if cart_item is not None:
            await session.delete(cart_item)
    elif cart_item is None:
        session.add(CartItem(customer_id=customer.id, issue_item_id=item.id, quantity=new))
    else:
        cart_item.quantity = new
        cart_item.updated_at = utcnow()  # продлеваем бронь
    await session.flush()
    return new


async def reserve(session: AsyncSession, item: IssueItem, delta: int) -> None:
    """
    Забронировать (delta > 0) или вернуть (delta < 0) порции блюда вне лавки.

    ВАЖНО — защита от «перебора». Если осталась одна банка и двое нажали «+»
    одновременно, наивный код («прочитать остаток → если > 0, уменьшить»)
    продал бы её обоим: оба прочитали 1 до того, как кто-то уменьшил.
    Поэтому проверка и уменьшение делаются ОДНИМ запросом к базе:

        UPDATE issue_items SET stock_left = stock_left - 1
        WHERE id = 15 AND stock_left >= 1

    База выполняет его атомарно (целиком, без вмешательства других запросов).
    Второй запрос увидит уже 0 и ничего не изменит — rowcount (число
    изменённых строк) будет 0, и мы скажем клиенту «всё разобрали».
    """
    if delta > 0:
        result = await session.execute(
            update(IssueItem)
            .where(IssueItem.id == item.id, IssueItem.stock_left >= delta)
            .values(stock_left=IssueItem.stock_left - delta)
        )
        if result.rowcount == 0:
            raise ServiceError("Больше нет — всё разобрали.")
    else:
        await session.execute(
            update(IssueItem).where(IssueItem.id == item.id).values(stock_left=IssueItem.stock_left - delta)
        )
    # Объект item в памяти не знает об изменении в базе — перечитываем остаток.
    await session.refresh(item, ["stock_left"])


async def return_stock(session: AsyncSession, cart_item: CartItem) -> None:
    """Вернуть в остаток порции из строки корзины (бронь снята) и удалить строку."""
    if cart_item.issue_item.is_stock:
        await reserve(session, cart_item.issue_item, -cart_item.quantity)
    await session.delete(cart_item)


async def release_expired(session: AsyncSession, minutes: int) -> int:
    """
    Снять бронь блюд вне лавки, если корзину не трогали дольше minutes.

    Вызывается фоновой задачей (bot/jobs.py). Возвращает, сколько строк снято.
    """
    border = utcnow() - timedelta(minutes=minutes)
    expired = list(
        await session.scalars(
            select(CartItem).join(IssueItem).where(IssueItem.stock_left.is_not(None), CartItem.updated_at <= border)
        )
    )
    for cart_item in expired:
        await return_stock(session, cart_item)
    return len(expired)


async def clear_cart(session: AsyncSession, customer: Customer, issue: Issue) -> None:
    """
    Очистить корзину клиента по выпуску ПОСЛЕ оформления заказа.

    Остаток блюд вне лавки не возвращаем: порции перешли из брони в заказ.
    """
    for cart_item in await get_cart(session, customer, issue):
        await session.delete(cart_item)
