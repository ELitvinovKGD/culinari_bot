"""
Заказы: оформление, отмена, доставка, позиции от шефа, итоги по клиентам.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import (
    Customer,
    Issue,
    IssueStatus,
    Order,
    OrderItem,
    OrderStatus,
    utcnow,
)
from culinary_bot.services import ServiceError
from culinary_bot.services import cart as cart_service

# Telegram разрешает боту удалять сообщения только в течение 48 часов.
# Берём с небольшим запасом, чтобы не упереться в границу.
GROUP_POST_DELETE_WINDOW = timedelta(hours=47, minutes=50)


async def place_order(
    session: AsyncSession, customer: Customer, issue: Issue, comment: str | None
) -> Order:
    """
    Оформить заказ из корзины клиента.

    Порядок проверок важен:
    1. Выпуск всё ещё открыт — пока клиент вводил адрес, шеф мог закрыть приём.
    2. В корзине есть хоть что-то (снятые с продажи позиции не учитываются).
    3. У клиента сохранены контакты.

    Название, порция и цена копируются в заказ (снимок на момент оформления).
    Контакты тоже копируются — см. комментарий у models.Order.
    После успешного оформления корзина очищается.
    """
    if issue.status != IssueStatus.OPEN:
        raise ServiceError("Приём заказов уже закрыт.")
    cart = await cart_service.get_cart(session, customer, issue)
    if not cart:
        raise ServiceError("Корзина пуста — добавьте хотя бы одно блюдо.")
    if not customer.has_saved_data():
        raise ServiceError("Не заполнены контактные данные.")

    order = Order(
        issue_id=issue.id,
        customer_id=customer.id,
        status=OrderStatus.ACCEPTED,
        name=customer.name,
        phone=customer.phone,
        username=customer.username,
        delivery_type=customer.delivery_type,
        address=customer.address,
        comment=(comment or "").strip() or None,
        items=[
            OrderItem(
                issue_item_id=ci.issue_item.id,
                name=ci.issue_item.name,
                portion=ci.issue_item.portion,
                price_cents=ci.issue_item.price_cents,
                quantity=ci.quantity,
            )
            for ci in cart
        ],
    )
    session.add(order)
    await cart_service.clear_cart(session, customer, issue)
    # flush, чтобы получить номер заказа (id) до commit.
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: int) -> Order:
    """Заказ по номеру или ServiceError."""
    order = await session.get(Order, order_id)
    if order is None:
        raise ServiceError("Заказ не найден.")
    return order


async def cancel_order(session: AsyncSession, order: Order, *, by: str) -> None:
    """
    Отменить заказ. by = "customer" или "admin".

    Решение из допроса: клиент может отменить в любой момент до доставки
    (правило ещё уточняется у шефа).
    """
    if order.status != OrderStatus.ACCEPTED:
        raise ServiceError("Этот заказ уже нельзя отменить.")
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = utcnow()
    order.cancelled_by = by


def mark_delivered(order: Order) -> None:
    """Отметить заказ доставленным."""
    if order.status != OrderStatus.ACCEPTED:
        raise ServiceError("Отметить доставку можно только у принятого заказа.")
    order.status = OrderStatus.DELIVERED
    order.delivered_at = utcnow()


async def add_extra_item(session: AsyncSession, order: Order, *, name: str, quantity: int, price_cents: int) -> OrderItem:
    """
    Добавить в заказ позицию от шефа (не из меню): «Наливка ×1, 8 €».

    Такая позиция входит в сумму и лист доставки, но не в таблицу производства.
    """
    if order.status == OrderStatus.CANCELLED:
        raise ServiceError("Заказ отменён.")
    if not name.strip():
        raise ServiceError("Название не может быть пустым.")
    if not 1 <= quantity <= 100:
        raise ServiceError("Количество — от 1 до 100.")
    extra = OrderItem(name=name.strip(), quantity=quantity, price_cents=price_cents, is_extra=True)
    order.items.append(extra)
    await session.flush()
    return extra


async def remove_extra_item(session: AsyncSession, order: Order, order_item_id: int) -> None:
    """Удалить позицию шефа. Позиции из меню так удалить нельзя — только отменой заказа."""
    for item in order.items:
        if item.id == order_item_id:
            if not item.is_extra:
                raise ServiceError("Удалить можно только позицию, добавленную шефом.")
            order.items.remove(item)
            await session.flush()
            return
    raise ServiceError("Позиция не найдена.")


def can_delete_group_post(order: Order) -> bool:
    """Можно ли ещё удалить пост о заказе в группе (прошло меньше 48 часов)."""
    if order.group_message_id is None or order.group_message_sent_at is None:
        return False
    sent_at = order.group_message_sent_at
    # SQLite в тестах возвращает время без часового пояса — считаем его UTC.
    if sent_at.tzinfo is None:
        from datetime import timezone

        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return utcnow() - sent_at < GROUP_POST_DELETE_WINDOW


# ---------------------------------------------------------------------------
# Списки и итоги
# ---------------------------------------------------------------------------


async def issue_orders(session: AsyncSession, issue: Issue, *, include_cancelled: bool = True) -> list[Order]:
    """Все заказы выпуска по номеру."""
    query = select(Order).where(Order.issue_id == issue.id).order_by(Order.id)
    if not include_cancelled:
        query = query.where(Order.status != OrderStatus.CANCELLED)
    return list(await session.scalars(query))


async def customer_orders(session: AsyncSession, customer: Customer, limit: int = 20) -> list[Order]:
    """Последние заказы клиента, новые первыми."""
    result = await session.scalars(
        select(Order).where(Order.customer_id == customer.id).order_by(Order.id.desc()).limit(limit)
    )
    return list(result)


async def customer_issue_orders(session: AsyncSession, customer: Customer, issue: Issue) -> list[Order]:
    """Действующие заказы клиента в одном выпуске (для общей суммы и дозаказа)."""
    result = await session.scalars(
        select(Order)
        .where(
            Order.customer_id == customer.id,
            Order.issue_id == issue.id,
            Order.status != OrderStatus.CANCELLED,
        )
        .order_by(Order.id)
    )
    return list(result)


@dataclass
class CustomerSummary:
    """
    Все действующие заказы одного клиента в выпуске, сложенные вместе.

    Нужна для решения «несколько заказов — одна доставка»: в сообщении
    о доставке, в листе доставки и в таблице производства клиент — одна строка.

    @dataclass — сокращённая запись класса для хранения данных: Python сам
    создаёт __init__ по перечисленным полям.
    """

    customer_id: int
    name: str
    phone: str
    username: str | None
    delivery_type: str
    address: str | None
    order_ids: list[int] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    # {(название, порция, цена, это_позиция_шефа): количество}
    lines: dict[tuple[str, str | None, int, bool], int] = field(default_factory=dict)
    # {id позиции выпуска: количество} — для таблицы производства
    menu_quantities: dict[int, int] = field(default_factory=dict)

    @property
    def total_cents(self) -> int:
        """Общая сумма по всем заказам клиента в выпуске."""
        return sum(price * qty for (_, _, price, _), qty in self.lines.items())


def summarize_by_customer(orders: list[Order]) -> list[CustomerSummary]:
    """
    Сложить заказы по клиентам (отменённые пропускаются).

    Контакты берём из ПОСЛЕДНЕГО заказа клиента — он самый актуальный
    (вдруг человек сменил адрес между заказами).
    """
    summaries: dict[int, CustomerSummary] = {}
    for order in sorted(orders, key=lambda o: o.id):
        if order.status == OrderStatus.CANCELLED:
            continue
        summary = summaries.get(order.customer_id)
        if summary is None:
            summary = CustomerSummary(
                customer_id=order.customer_id,
                name=order.name,
                phone=order.phone,
                username=order.username,
                delivery_type=order.delivery_type,
                address=order.address,
            )
            summaries[order.customer_id] = summary
        else:
            summary.name, summary.phone, summary.username = order.name, order.phone, order.username
            summary.delivery_type, summary.address = order.delivery_type, order.address
        summary.order_ids.append(order.id)
        if order.comment:
            summary.comments.append(order.comment)
        for item in order.items:
            key = (item.name, item.portion, item.price_cents, item.is_extra)
            summary.lines[key] = summary.lines.get(key, 0) + item.quantity
            if item.issue_item_id is not None and not item.is_extra:
                summary.menu_quantities[item.issue_item_id] = (
                    summary.menu_quantities.get(item.issue_item_id, 0) + item.quantity
                )
    return list(summaries.values())
