"""
Тесты «🧺 Блюд вне лавки»: остаток, бронь, защита от перебора, заказ отдельно
и в составе заказа лавки.
"""

from datetime import timedelta

import pytest

from culinary_bot.db.models import IssueKind, IssueStatus
from culinary_bot.services import ServiceError, cart, catalog, issues, orders
from tests.conftest import make_customer


@pytest.fixture
async def stock(session):
    """Выпуск «Блюда вне лавки» с капустой: 3 банки по 6 €."""
    issue = await issues.get_stock_issue(session)
    dish = await catalog.create_dish(session, name="Капуста маринованная", price_cents=600, portion="0.5 л")
    await issues.add_stock_dish(session, issue, dish, 3)
    return issue


async def test_stock_issue_is_separate_from_lavka(session, stock):
    """Постоянный выпуск «вне лавки» не мешает открыть лавку и не виден в списке лавок."""
    assert stock.kind == IssueKind.STOCK and stock.status == IssueStatus.OPEN
    assert await issues.get_stock_issue(session) is stock  # второй раз не создаётся
    assert await issues.get_open_issue(session) is None
    assert stock not in await issues.list_active_issues(session)


async def test_reserve_and_oversell(session, stock):
    """Корзина бронирует; больше остатка не взять; «−» возвращает в остаток."""
    eva = await make_customer(session, tg_id=1)
    ivan = await make_customer(session, tg_id=2)
    item = stock.items[0]
    await cart.change_quantity(session, eva, item, +1)
    await cart.change_quantity(session, eva, item, +1)
    await cart.change_quantity(session, ivan, item, +1)
    assert item.stock_left == 0
    with pytest.raises(ServiceError):
        await cart.change_quantity(session, ivan, item, +1)  # четвёртой банки нет
    await cart.change_quantity(session, eva, item, -1)
    assert item.stock_left == 1


async def test_reservation_expires(session, stock):
    """Через час без оформления бронь снимается и банки возвращаются в остаток."""
    eva = await make_customer(session)
    item = stock.items[0]
    await cart.change_quantity(session, eva, item, +2)
    assert await cart.release_expired(session, minutes=60) == 0  # ещё рано
    for ci in await cart.get_cart(session, eva, stock):
        ci.updated_at = ci.updated_at - timedelta(minutes=61)
    await session.flush()
    assert await cart.release_expired(session, minutes=60) == 1
    await session.refresh(item)
    assert item.stock_left == 3
    assert await cart.get_cart(session, eva, stock) == []


async def test_separate_stock_order_and_cancel(session, stock):
    """Отдельный заказ «в наличии»; отмена возвращает банки в остаток."""
    eva = await make_customer(session)
    item = stock.items[0]
    await cart.change_quantity(session, eva, item, +2)
    order = await orders.place_stock_order(session, eva, stock, comment=None)
    assert order.issue_id == stock.id and order.total_cents == 1200
    assert item.stock_left == 1  # бронь перешла в заказ
    await orders.cancel_order(session, order, by="customer")
    assert item.stock_left == 3


async def test_stock_added_to_lavka_order(session, stock, open_issue):
    """При заказе в лавке блюдо вне лавки добавляется к нему (едет вместе)."""
    eva = await make_customer(session)
    await cart.change_quantity(session, eva, open_issue.items[0], +1)
    lavka_order = await orders.place_order(session, eva, open_issue, comment=None)

    assert [o.id for o in await orders.attachable_orders(session, eva)] == [lavka_order.id]
    await cart.change_quantity(session, eva, stock.items[0], +1)
    part = await orders.place_stock_order(session, eva, stock, comment=None, base=lavka_order)

    assert part.issue_id == open_issue.id  # часть заказа лавки
    summary = orders.summarize_by_customer(await orders.issue_orders(session, open_issue))[0]
    assert summary.menu_quantities == {open_issue.items[0].id: 1, stock.items[0].id: 1}

    # Лавку закрыли — добавлять ещё можно (её ещё не развезли).
    await issues.close_issue(session, open_issue)
    assert await orders.attachable_orders(session, eva)


async def test_lavka_table_has_stock_rows(session, stock, open_issue):
    """В таблице лавки блюда вне лавки идут отдельными строками «🧺»."""
    from io import BytesIO

    from openpyxl import load_workbook

    from culinary_bot.services.export import build_issue_workbook

    eva = await make_customer(session)
    await cart.change_quantity(session, eva, open_issue.items[0], +1)
    base = await orders.place_order(session, eva, open_issue, comment=None)
    await cart.change_quantity(session, eva, stock.items[0], +2)
    await orders.place_stock_order(session, eva, stock, comment=None, base=base)

    ws = load_workbook(BytesIO(build_issue_workbook(open_issue, await orders.issue_orders(session, open_issue))))["Производство"]
    rows = {r[1]: r for r in ws.iter_rows(values_only=True)}
    assert rows["🧺 Капуста маринованная 0.5 л"][3] == 2
