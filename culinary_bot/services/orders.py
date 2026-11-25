"""
Заказы: оформление, отмена, доставка, позиции от шефа, итоги по клиентам.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import (
    CartItem,
    Customer,
    Issue,
    IssueItem,
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
    session: AsyncSession, customer: Customer, issue: Issue, comment: str | None, base: Order | None = None
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

    base — для «дозаказа» (добавить к своему заказу): контакты и адрес берутся
    из уже оформленного заказа, а не из профиля — доставка одна, адрес тот же.
    Внутри добавка — отдельная запись Order (у неё своё окно правки и свой пост
    в группе), но клиенту и в таблице все части показываются как один заказ.
    """
    if issue.status != IssueStatus.OPEN:
        raise ServiceError("Приём заказов уже закрыт.")
    cart = await cart_service.get_cart(session, customer, issue)
    if not cart:
        raise ServiceError("Корзина пуста — добавьте хотя бы одно блюдо.")
    source = base if base is not None else customer
    if base is None and not customer.has_saved_data():
        raise ServiceError("Не заполнены контактные данные.")

    order = Order(
        issue_id=issue.id,
        customer_id=customer.id,
        status=OrderStatus.ACCEPTED,
        name=source.name,
        phone=source.phone,
        username=customer.username,
        delivery_type=source.delivery_type,
        address=source.address,
        latitude=source.latitude,
        longitude=source.longitude,
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


# ---------------------------------------------------------------------------
# Окно правки: первые N минут после оформления (решение от 25.11)
#
# Пока окно открыто (confirmed_at пустой), клиент может нажать «✏️ Изменить»,
# а шеф и группа о заказе ещё не знают. Когда окно закрылось, фоновая задача
# (bot/jobs.py) «подтверждает» заказ: ставит confirmed_at, публикует пост
# и уведомляет шефа.
# ---------------------------------------------------------------------------


def _aware(moment: datetime) -> datetime:
    """SQLite в тестах отдаёт время без часового пояса — считаем его UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def edit_deadline(order: Order, minutes: int) -> datetime:
    """До какого момента заказ можно изменить."""
    return _aware(order.created_at) + timedelta(minutes=minutes)


def is_editable(order: Order, minutes: int) -> bool:
    """Можно ли ещё нажать «✏️ Изменить»: окно не истекло, заказ не подтверждён, лавка открыта."""
    return (
        order.status == OrderStatus.ACCEPTED
        and order.confirmed_at is None
        and order.issue.status == IssueStatus.OPEN
        and utcnow() < edit_deadline(order, minutes)
    )


async def revert_to_cart(session: AsyncSession, order: Order, minutes: int) -> None:
    """
    «✏️ Изменить»: вернуть позиции заказа в корзину и удалить заказ.

    Заказ удаляем совсем, а не отменяем: о нём ещё никто не знает (ни шеф,
    ни группа), и «отменённый» заказ в списке шефа только сбивал бы с толку.
    Если в корзине уже что-то лежит — количества складываются.
    """
    if not is_editable(order, minutes):
        raise ServiceError("Время на изменение заказа вышло. Можно отменить заказ или дозаказать.")
    for item in order.items:
        if item.issue_item_id is None:
            continue
        issue_item = await session.get(IssueItem, item.issue_item_id)
        if issue_item is None or not issue_item.is_active:
            continue  # блюдо успели снять с продажи — в корзину не вернётся
        existing = await session.scalar(
            select(CartItem).where(CartItem.customer_id == order.customer_id, CartItem.issue_item_id == issue_item.id)
        )
        if existing:
            existing.quantity += item.quantity
        else:
            session.add(CartItem(customer_id=order.customer_id, issue_item_id=issue_item.id, quantity=item.quantity))
    await session.delete(order)
    await session.flush()


async def orders_to_confirm(session: AsyncSession, minutes: int) -> list[Order]:
    """Заказы, у которых окно правки истекло, а подтверждения ещё не было."""
    border = utcnow() - timedelta(minutes=minutes)
    result = await session.scalars(
        select(Order)
        .where(Order.confirmed_at.is_(None), Order.status == OrderStatus.ACCEPTED, Order.created_at <= border)
        .order_by(Order.id)
    )
    return list(result)


async def unconfirmed_issue_orders(session: AsyncSession, issue: Issue) -> list[Order]:
    """Неподтверждённые заказы выпуска — их подтверждаем сразу при закрытии приёма."""
    result = await session.scalars(
        select(Order)
        .where(Order.issue_id == issue.id, Order.confirmed_at.is_(None), Order.status == OrderStatus.ACCEPTED)
        .order_by(Order.id)
    )
    return list(result)


async def is_reorder(session: AsyncSession, order: Order) -> bool:
    """Есть ли у клиента в этом выпуске другие действующие заказы, оформленные раньше (дозаказ)."""
    earlier = await session.scalar(
        select(Order.id).where(
            Order.customer_id == order.customer_id,
            Order.issue_id == order.issue_id,
            Order.id < order.id,
            Order.status != OrderStatus.CANCELLED,
        )
    )
    return earlier is not None


def mark_confirmed(order: Order) -> None:
    """Отметить заказ сформированным (окно правки закрыто)."""
    order.confirmed_at = utcnow()


async def cancel_customer_issue(session: AsyncSession, customer: Customer, issue: Issue) -> list[Order]:
    """
    Отменить заказ клиента в выпуске целиком — все его части (основной + добавки).

    Клиент видит один заказ, значит и «❌ Отменить» отменяет его целиком.
    Возвращает отменённые части (для уведомлений и удаления постов).
    """
    parts = [o for o in await customer_issue_orders(session, customer, issue) if o.status == OrderStatus.ACCEPTED]
    if not parts:
        raise ServiceError("Этот заказ уже нельзя отменить.")
    for order in parts:
        await cancel_order(session, order, by="customer")
    return parts


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
    return utcnow() - _aware(order.group_message_sent_at) < GROUP_POST_DELETE_WINDOW


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
    map_url: str | None = None
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
                map_url=order.map_url,
            )
            summaries[order.customer_id] = summary
        else:
            summary.name, summary.phone, summary.username = order.name, order.phone, order.username
            summary.delivery_type, summary.address = order.delivery_type, order.address
            summary.map_url = order.map_url
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
