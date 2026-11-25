"""
Тесты основной логики: корзина, оформление, отмена, цены, итоги по клиентам.

Каждый тест — один сценарий из docs/scenarios.md в миниатюре.
Структура почти всегда одна: подготовка → действие → проверка (assert).
Запуск: `pytest` в корне проекта.
"""

import pytest

from culinary_bot.db.models import IssueStatus, OrderStatus
from culinary_bot.services import ServiceError, cart, issues, orders
from culinary_bot.services.customers import normalize_phone
from culinary_bot.services.money import format_price, parse_price
from tests.conftest import make_customer


async def test_place_order_copies_prices_and_clears_cart(session, open_issue):
    """К1: заказ сохраняет цены на момент оформления, корзина очищается."""
    customer = await make_customer(session)
    soup, patties = open_issue.items
    await cart.change_quantity(session, customer, soup, +1)
    await cart.change_quantity(session, customer, soup, +1)
    await cart.change_quantity(session, customer, patties, +1)

    order = await orders.place_order(session, customer, open_issue, comment="позвонить за час")

    assert order.status == OrderStatus.ACCEPTED
    assert order.total_cents == 2 * 1000 + 1250
    assert order.address == "ул. Руставели 1"
    assert await cart.get_cart(session, customer, open_issue) == []

    # Шеф меняет цену только для новых заказов — старый заказ не меняется.
    await issues.change_item_price(session, soup, 1500, apply_to_orders=False)
    assert order.total_cents == 2 * 1000 + 1250


async def test_price_change_applied_to_orders(session, open_issue):
    """А3: «рыба подорожала» — шеф применяет новую цену к оформленным заказам."""
    customer = await make_customer(session)
    soup = open_issue.items[0]
    await cart.change_quantity(session, customer, soup, +1)
    order = await orders.place_order(session, customer, open_issue, comment=None)

    changed = await issues.change_item_price(session, soup, 1200, apply_to_orders=True)

    assert [o.id for o in changed] == [order.id]
    assert order.total_cents == 1200


async def test_cannot_order_from_closed_issue(session, open_issue):
    """К1, особый случай: пока клиент оформлял, шеф закрыл приём."""
    customer = await make_customer(session)
    await cart.change_quantity(session, customer, open_issue.items[0], +1)
    await issues.close_issue(session, open_issue)

    with pytest.raises(ServiceError):
        await orders.place_order(session, customer, open_issue, comment=None)
    # При закрытии корзины очищаются.
    assert await cart.get_cart(session, customer, open_issue) == []


async def test_empty_cart_is_rejected(session, open_issue):
    """Нельзя оформить пустую корзину."""
    customer = await make_customer(session)
    with pytest.raises(ServiceError):
        await orders.place_order(session, customer, open_issue, comment=None)


async def test_deactivated_item_leaves_cart(session, open_issue):
    """Снятое с продажи блюдо исчезает из корзины и его нельзя добавить."""
    customer = await make_customer(session)
    soup = open_issue.items[0]
    await cart.change_quantity(session, customer, soup, +1)

    await issues.set_item_active(session, soup, False)

    assert await cart.get_cart(session, customer, open_issue) == []
    with pytest.raises(ServiceError):
        await cart.change_quantity(session, customer, soup, +1)


async def test_cancel_only_accepted(session, open_issue):
    """К4: отменить можно принятый заказ, доставленный — нельзя."""
    customer = await make_customer(session)
    await cart.change_quantity(session, customer, open_issue.items[0], +1)
    order = await orders.place_order(session, customer, open_issue, comment=None)

    await orders.cancel_order(session, order, by="customer")
    assert order.status == OrderStatus.CANCELLED
    with pytest.raises(ServiceError):
        await orders.cancel_order(session, order, by="customer")


async def test_reorder_is_summed_per_customer(session, open_issue):
    """К3: два заказа одного клиента складываются, отменённый не учитывается."""
    eva = await make_customer(session, tg_id=1, name="Ева")
    ivan = await make_customer(session, tg_id=2, name="Иван")
    soup, patties = open_issue.items

    await cart.change_quantity(session, eva, soup, +1)
    first = await orders.place_order(session, eva, open_issue, comment=None)
    await cart.change_quantity(session, eva, soup, +1)
    await cart.change_quantity(session, eva, patties, +1)
    await orders.place_order(session, eva, open_issue, comment="домофон 12")
    await cart.change_quantity(session, ivan, patties, +1)
    ivan_order = await orders.place_order(session, ivan, open_issue, comment=None)
    await orders.cancel_order(session, ivan_order, by="customer")

    # Позиция от шефа: входит в сумму, но не в производство.
    await orders.add_extra_item(session, first, name="Наливка", quantity=1, price_cents=800)

    all_orders = await orders.issue_orders(session, open_issue)
    summaries = orders.summarize_by_customer(all_orders)

    assert len(summaries) == 1  # Иван отменил — его нет
    eva_summary = summaries[0]
    assert eva_summary.menu_quantities == {soup.id: 2, patties.id: 1}
    assert eva_summary.total_cents == 2 * 1000 + 1250 + 800
    assert eva_summary.comments == ["домофон 12"]


async def test_only_one_open_issue(session, open_issue):
    """А2: второй выпуск нельзя открыть, пока первый принимает заказы."""
    second = await issues.create_draft(session, "Второй")
    with pytest.raises(ServiceError):
        await issues.open_issue(session, second)


async def test_finish_requires_delivery_marks(session, open_issue):
    """А9: завершение с неотмеченными заказами — только с явным подтверждением."""
    customer = await make_customer(session)
    await cart.change_quantity(session, customer, open_issue.items[0], +1)
    order = await orders.place_order(session, customer, open_issue, comment=None)
    await issues.close_issue(session, open_issue)
    all_orders = await orders.issue_orders(session, open_issue)

    with pytest.raises(ServiceError):
        issues.finish_issue(open_issue, all_orders, mark_delivered=False)
    issues.finish_issue(open_issue, all_orders, mark_delivered=True)

    assert open_issue.status == IssueStatus.FINISHED
    assert order.status == OrderStatus.DELIVERED


async def test_item_with_orders_cannot_be_removed(session, open_issue):
    """Позицию, которую заказывали, можно только снять с продажи, не удалить."""
    customer = await make_customer(session)
    soup = open_issue.items[0]
    await cart.change_quantity(session, customer, soup, +1)
    await orders.place_order(session, customer, open_issue, comment=None)

    with pytest.raises(ServiceError):
        await issues.remove_item(session, soup)


@pytest.mark.parametrize(
    ("text", "cents"),
    [("10", 1000), ("10.5", 1050), ("10,50", 1050), ("12 €", 1200), ("9е", 900), ("10.15", 1015)],
)
def test_parse_price(text, cents):
    """Шеф пишет цену как привык — бот понимает."""
    assert parse_price(text) == cents


def test_format_price():
    assert format_price(1000) == "10 €"
    assert format_price(1050) == "10.50 €"


@pytest.mark.parametrize("bad", ["abc", "12", "+7 (999) abc-12-34"])
def test_bad_phone(bad):
    with pytest.raises(ServiceError):
        normalize_phone(bad)


def test_good_phone():
    assert normalize_phone(" 557692377 ") == "557692377"
    assert normalize_phone("+995 (557) 69-23-77") == "+995 (557) 69-23-77"


async def test_edit_window(session, open_issue):
    """«✏️ Изменить»: в окне правки заказ возвращается в корзину; после подтверждения — нельзя."""
    customer = await make_customer(session)
    soup = open_issue.items[0]
    await cart.change_quantity(session, customer, soup, +1)
    order = await orders.place_order(session, customer, open_issue, comment=None)
    await cart.change_quantity(session, customer, soup, +1)  # в корзине уже 1 суп

    assert orders.is_editable(order, minutes=10)
    await orders.revert_to_cart(session, order, minutes=10)

    basket = await cart.get_cart(session, customer, open_issue)
    assert cart.quantities(basket) == {soup.id: 2}  # 1 из корзины + 1 из заказа
    assert await orders.issue_orders(session, open_issue) == []  # заказ удалён

    second = await orders.place_order(session, customer, open_issue, comment=None)
    orders.mark_confirmed(second)
    with pytest.raises(ServiceError):
        await orders.revert_to_cart(session, second, minutes=10)


async def test_orders_to_confirm(session, open_issue):
    """Фоновая задача берёт только заказы с истёкшим окном и без подтверждения."""
    customer = await make_customer(session)
    await cart.change_quantity(session, customer, open_issue.items[0], +1)
    order = await orders.place_order(session, customer, open_issue, comment=None)

    assert await orders.orders_to_confirm(session, minutes=10) == []  # окно ещё идёт
    assert [o.id for o in await orders.orders_to_confirm(session, minutes=0)] == [order.id]
    orders.mark_confirmed(order)
    assert await orders.orders_to_confirm(session, minutes=0) == []


async def test_is_reorder(session, open_issue):
    customer = await make_customer(session)
    soup = open_issue.items[0]
    await cart.change_quantity(session, customer, soup, +1)
    first = await orders.place_order(session, customer, open_issue, comment=None)
    await cart.change_quantity(session, customer, soup, +1)
    second = await orders.place_order(session, customer, open_issue, comment=None)
    assert not await orders.is_reorder(session, first)
    assert await orders.is_reorder(session, second)


async def test_announce_only_after_close(session, open_issue):
    with pytest.raises(ServiceError):
        issues.announce(open_issue, "Развожу во вторник", None)
    await issues.close_issue(session, open_issue)
    issues.announce(open_issue, "Развожу во вторник", "Забрать в среду")
    assert open_issue.pickup_text == "Забрать в среду"


@pytest.mark.parametrize("raw", ["@chef_anton", "chef_anton", "t.me/chef_anton", "https://t.me/chef_anton/"])
def test_normalize_username(raw):
    from culinary_bot.services.settings import normalize_username

    assert normalize_username(raw) == "chef_anton"


@pytest.mark.parametrize("bad", ["@ab", "шеф", "chef anton"])
def test_bad_username(bad):
    from culinary_bot.services.settings import normalize_username

    with pytest.raises(ServiceError):
        normalize_username(bad)


@pytest.mark.parametrize(
    ("url", "coords"),
    [
        ("https://www.google.com/maps/@42.2864,18.8400,17z", (42.2864, 18.84)),
        ("https://maps.google.com/?q=42.28,18.84", (42.28, 18.84)),
        ("https://www.google.com/maps/place/X/@42.27,18.83,15z/data=!3m1!4b1!8m2!3d42.2911!4d18.8403", (42.2911, 18.8403)),
        ("https://www.google.com/maps/search/?api=1&query=42.1%2C19.1", (42.1, 19.1)),
    ],
)
def test_coords_from_maps_link(url, coords):
    """Ссылка из Google Maps превращается в точку (для точного места берётся !3d/!4d, а не центр карты)."""
    from culinary_bot.services.geocode import coords_from_url

    assert coords_from_url(url) == coords


def test_find_maps_link():
    from culinary_bot.services.geocode import find_maps_link

    assert find_maps_link("вот https://maps.app.goo.gl/AbC123 спасибо") == "https://maps.app.goo.gl/AbC123"
    assert find_maps_link("ул. Морская 5, Будва") is None
