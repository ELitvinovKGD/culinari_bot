"""
Выпуски (меню лавки) и позиции в них.

Жизненный цикл выпуска: черновик → открыт → закрыт → завершён.
Все переходы между состояниями — только через функции этого модуля:
здесь проверяется, что переход допустим (например, нельзя открыть второй
выпуск, пока первый принимает заказы).
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import (
    CartItem,
    Dish,
    Issue,
    IssueItem,
    IssueKind,
    IssueStatus,
    Order,
    OrderItem,
    OrderStatus,
    utcnow,
)
from culinary_bot.services import ServiceError
from culinary_bot.services.cart import return_stock

# Поля позиции выпуска, которые шеф может менять (цена — отдельной функцией,
# потому что у неё особое правило с уже оформленными заказами).
EDITABLE_ITEM_FIELDS = {"name", "emoji", "description", "portion", "composition", "photo_file_id"}
# Поля самого выпуска.
EDITABLE_ISSUE_FIELDS = {"title", "header", "footer"}


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------


async def get_issue(session: AsyncSession, issue_id: int) -> Issue:
    """Выпуск по id или ServiceError."""
    issue = await session.get(Issue, issue_id)
    if issue is None:
        raise ServiceError("Выпуск не найден.")
    return issue


async def get_item(session: AsyncSession, item_id: int) -> IssueItem:
    """Позиция выпуска по id или ServiceError."""
    item = await session.get(IssueItem, item_id)
    if item is None:
        raise ServiceError("Позиция не найдена.")
    return item


async def get_open_issue(session: AsyncSession) -> Issue | None:
    """Лавка, которая сейчас принимает заказы (она всегда одна или ни одной)."""
    return await session.scalar(
        select(Issue).where(Issue.status == IssueStatus.OPEN, Issue.kind == IssueKind.LAVKA)
    )


async def list_active_issues(session: AsyncSession) -> list[Issue]:
    """Незавершённые лавки (черновики, открытая, закрытые) — для админки."""
    result = await session.scalars(
        select(Issue)
        .where(Issue.status != IssueStatus.FINISHED, Issue.kind == IssueKind.LAVKA)
        .order_by(Issue.id.desc())
    )
    return list(result)


async def list_finished_issues(session: AsyncSession, limit: int = 10) -> list[Issue]:
    """Последние завершённые лавки (архив)."""
    result = await session.scalars(
        select(Issue)
        .where(Issue.status == IssueStatus.FINISHED, Issue.kind == IssueKind.LAVKA)
        .order_by(Issue.id.desc())
        .limit(limit)
    )
    return list(result)


# ---------------------------------------------------------------------------
# «🧺 Блюда вне лавки» (решение от 26.11)
#
# Устроены как ОДИН постоянный выпуск вида STOCK: он создаётся сам при первом
# обращении и всегда открыт. Его позиции — блюда из общего каталога с остатком
# (stock_left). Так переиспользуются корзина, заказы, Excel и посты лавки.
# ---------------------------------------------------------------------------

STOCK_TITLE = "Блюда вне лавки"


async def get_stock_issue(session: AsyncSession) -> Issue:
    """Постоянный выпуск «Блюда вне лавки»; создаётся при первом обращении."""
    issue = await session.scalar(select(Issue).where(Issue.kind == IssueKind.STOCK))
    if issue is None:
        issue = Issue(title=STOCK_TITLE, kind=IssueKind.STOCK, status=IssueStatus.OPEN, opened_at=utcnow(), items=[])
        session.add(issue)
        await session.flush()
    return issue


def stock_available(issue: Issue) -> list[IssueItem]:
    """Блюда вне лавки, которые сейчас можно заказать (в продаже и остаток > 0)."""
    return [item for item in issue.items if item.is_active and (item.stock_left or 0) > 0]


async def add_stock_dish(session: AsyncSession, issue: Issue, dish: Dish, quantity: int) -> IssueItem:
    """
    Выставить блюдо из каталога «в наличие» с количеством.

    Если блюдо уже было — пополняем остаток и возвращаем в продажу
    (цена и описание остаются те, что шеф выставил раньше).
    """
    if quantity < 1 or quantity > 1000:
        raise ServiceError("Количество — от 1 до 1000.")
    for item in issue.items:
        if item.dish_id == dish.id:
            item.stock_left = (item.stock_left or 0) + quantity
            item.is_active = True
            return item
    item = await add_dish_to_issue(session, issue, dish)
    item.stock_left = quantity
    return item


def set_stock(item: IssueItem, quantity: int) -> None:
    """Задать остаток вручную («осталось 3»). Бронь в корзинах не трогаем."""
    if not item.is_stock:
        raise ServiceError("Остаток есть только у блюд вне лавки.")
    if quantity < 0 or quantity > 1000:
        raise ServiceError("Остаток — от 0 до 1000.")
    item.stock_left = quantity


def active_items(issue: Issue) -> list[IssueItem]:
    """Позиции, которые сейчас продаются (без снятых с продажи)."""
    return [item for item in issue.items if item.is_active]


async def orders_with_item(session: AsyncSession, item: IssueItem) -> list[Order]:
    """
    Действующие (не отменённые) заказы, в которых есть эта позиция.

    Нужны, чтобы уведомить клиентов об изменении цены, описания или о снятии блюда.
    """
    result = await session.scalars(
        select(Order)
        .join(OrderItem)
        .where(OrderItem.issue_item_id == item.id, Order.status != OrderStatus.CANCELLED)
        .distinct()
    )
    return list(result)


async def item_has_orders(session: AsyncSession, item: IssueItem) -> bool:
    """Заказывали ли эту позицию хоть раз (в том числе в отменённых заказах)."""
    count = await session.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.issue_item_id == item.id))
    return bool(count)


# ---------------------------------------------------------------------------
# Создание и правка
# ---------------------------------------------------------------------------


async def create_draft(session: AsyncSession, title: str) -> Issue:
    """Создать черновик выпуска с названием."""
    if not title.strip():
        raise ServiceError("Название выпуска не может быть пустым.")
    issue = Issue(title=title.strip(), status=IssueStatus.DRAFT, items=[])
    session.add(issue)
    await session.flush()
    return issue


def ensure_editable(issue: Issue) -> None:
    """Править можно только черновик и открытый выпуск (блюда вне лавки — всегда открыты)."""
    if issue.status not in (IssueStatus.DRAFT, IssueStatus.OPEN):
        raise ServiceError("Закрытый или завершённый выпуск менять нельзя.")


def update_issue_field(issue: Issue, field: str, value: str | None) -> None:
    """Изменить название, шапку или текст после списка."""
    ensure_editable(issue)
    if field not in EDITABLE_ISSUE_FIELDS:
        raise ServiceError("Это поле нельзя изменить.")
    if field == "title" and not (value or "").strip():
        raise ServiceError("Название выпуска не может быть пустым.")
    setattr(issue, field, value)


async def add_dish_to_issue(session: AsyncSession, issue: Issue, dish: Dish) -> IssueItem:
    """
    Добавить блюдо из каталога в выпуск.

    Значения КОПИРУЮТСЯ из карточки — с этого момента позиция живёт своей
    жизнью: её цену можно поменять только для этого выпуска.
    Позиция встаёт в конец списка.
    """
    ensure_editable(issue)
    if any(item.dish_id == dish.id for item in issue.items):
        raise ServiceError("Это блюдо уже есть в выпуске.")
    next_position = max((item.position for item in issue.items), default=0) + 1
    item = IssueItem(
        dish_id=dish.id,
        position=next_position,
        emoji=dish.emoji,
        name=dish.name,
        description=dish.description,
        portion=dish.portion,
        composition=dish.composition,
        price_cents=dish.price_cents,
        photo_file_id=dish.photo_file_id,
    )
    issue.items.append(item)
    await session.flush()
    return item


def update_item_field(item: IssueItem, field: str, value: str | None) -> None:
    """Изменить поле позиции (кроме цены — для неё change_item_price)."""
    ensure_editable(item.issue)
    if field not in EDITABLE_ITEM_FIELDS:
        raise ServiceError("Это поле нельзя изменить.")
    if field == "name" and not (value or "").strip():
        raise ServiceError("Название не может быть пустым.")
    setattr(item, field, value)


async def change_item_price(
    session: AsyncSession, item: IssueItem, price_cents: int, *, apply_to_orders: bool
) -> list[Order]:
    """
    Изменить цену позиции.

    apply_to_orders=True — новая цена записывается и в уже оформленные
    (не отменённые и не доставленные) заказы. Это решение шефа
    («рыба подорожала на 2 €»); клиентов затем уведомляет обработчик.
    Возвращает список заказов, где цена изменилась (пустой, если не применяли).
    """
    ensure_editable(item.issue)
    item.price_cents = price_cents
    if not apply_to_orders:
        return []

    changed: list[Order] = []
    for order in await orders_with_item(session, item):
        if order.status != OrderStatus.ACCEPTED:
            continue  # доставленный заказ уже оплачен по старой цене
        for order_item in order.items:
            if order_item.issue_item_id == item.id:
                order_item.price_cents = price_cents
        changed.append(order)
    return changed


async def set_item_active(session: AsyncSession, item: IssueItem, active: bool) -> list[Order]:
    """
    Снять позицию с продажи («закончилось») или вернуть в продажу.

    При снятии позиция удаляется из корзин клиентов — иначе они попытались бы
    её заказать. Уже оформленные заказы не трогаем: что с ними делать,
    решает шеф (обработчик уведомляет клиентов).
    Возвращает заказы с этой позицией.
    """
    ensure_editable(item.issue)
    item.is_active = active
    if not active:
        carts = await session.scalars(select(CartItem).where(CartItem.issue_item_id == item.id))
        for cart_item in carts:
            await return_stock(session, cart_item)
    return await orders_with_item(session, item)


async def remove_item(session: AsyncSession, item: IssueItem) -> None:
    """
    Убрать позицию из выпуска совсем.

    Нельзя, если позицию уже заказывали: на неё ссылаются заказы.
    Для таких — только «снять с продажи».
    """
    ensure_editable(item.issue)
    if await item_has_orders(session, item):
        raise ServiceError("Эту позицию уже заказывали — её можно только снять с продажи.")
    issue = item.issue
    issue.items.remove(item)  # cascade delete-orphan удалит строку из базы
    # Перенумеровать оставшиеся, чтобы не было дыр: 01, 02, 03...
    for number, other in enumerate(issue.items, start=1):
        other.position = number


def move_item(item: IssueItem, direction: int) -> None:
    """
    Сдвинуть позицию вверх (direction=-1) или вниз (+1) в списке выпуска.

    Меняем местами номера с соседом. Если позиция уже крайняя — ничего не делаем.
    """
    ensure_editable(item.issue)
    items = sorted(item.issue.items, key=lambda it: it.position)
    index = items.index(item)
    neighbour_index = index + direction
    if not 0 <= neighbour_index < len(items):
        return
    neighbour = items[neighbour_index]
    item.position, neighbour.position = neighbour.position, item.position
    # Порядок списка issue.items в памяти обновляем вручную: order_by
    # из models применяется только при загрузке из базы.
    item.issue.items.sort(key=lambda it: it.position)


# ---------------------------------------------------------------------------
# Переходы между состояниями
# ---------------------------------------------------------------------------


async def open_issue(session: AsyncSession, issue: Issue) -> None:
    """
    Опубликовать выпуск: черновик → открыт.

    Правила: открыт может быть только один выпуск; в выпуске должна быть
    хотя бы одна позиция в продаже.
    """
    if issue.status != IssueStatus.DRAFT:
        raise ServiceError("Опубликовать можно только черновик.")
    current = await get_open_issue(session)
    if current is not None:
        raise ServiceError(f"Сначала закройте приём в выпуске «{current.title}».")
    if not active_items(issue):
        raise ServiceError("Добавьте в выпуск хотя бы одно блюдо.")
    issue.status = IssueStatus.OPEN
    issue.opened_at = utcnow()


async def close_issue(session: AsyncSession, issue: Issue) -> None:
    """
    Закрыть приём заказов: открыт → закрыт.

    Только останавливает заказы (решение от 25.11). Сообщение «приём закрыт»
    клиентам рассылает обработчик; когда еда будет готова, шеф отдельно
    нажмёт «📣 Объявить выдачу» (announce).
    Корзины всех клиентов по этому выпуску очищаются.
    """
    if issue.status != IssueStatus.OPEN:
        raise ServiceError("Закрыть можно только открытый выпуск.")
    issue.status = IssueStatus.CLOSED
    issue.closed_at = utcnow()
    item_ids = [item.id for item in issue.items]
    if item_ids:
        carts = await session.scalars(select(CartItem).where(CartItem.issue_item_id.in_(item_ids)))
        for cart_item in carts:
            await session.delete(cart_item)


def announce(issue: Issue, delivery_text: str | None, pickup_text: str | None) -> None:
    """
    Запомнить объявление выдачи («Всё готово, развожу во вторник»).

    Рассылку делает обработчик. Объявлять можно повторно («Задерживаюсь на час») —
    каждый раз сохраняется последний текст.
    """
    if issue.status != IssueStatus.CLOSED:
        raise ServiceError("Объявить выдачу можно после закрытия приёма.")
    if not (delivery_text or pickup_text):
        raise ServiceError("Текст объявления пустой.")
    issue.closing_text = delivery_text
    issue.pickup_text = pickup_text


def finish_issue(issue: Issue, orders: list[Order], *, mark_delivered: bool) -> None:
    """
    Завершить выпуск: закрыт → завершён (всё развезено, уходит в архив).

    orders — заказы выпуска. Если среди них есть «Принят» и mark_delivered=False —
    ошибка: шеф, видимо, забыл отметить доставку. С mark_delivered=True
    все такие заказы отмечаются доставленными.
    """
    if issue.status != IssueStatus.CLOSED:
        raise ServiceError("Завершить можно только выпуск с закрытым приёмом.")
    pending = [order for order in orders if order.status == OrderStatus.ACCEPTED]
    if pending and not mark_delivered:
        raise ServiceError(f"Есть неотмеченные заказы: {len(pending)}.")
    now = utcnow()
    for order in pending:
        order.status = OrderStatus.DELIVERED
        order.delivered_at = now
    issue.status = IssueStatus.FINISHED
    issue.finished_at = now


async def delete_draft(session: AsyncSession, issue: Issue) -> None:
    """Удалить черновик (опубликованные выпуски не удаляются — на них ссылаются заказы)."""
    if issue.status != IssueStatus.DRAFT:
        raise ServiceError("Удалить можно только черновик.")
    await session.delete(issue)
