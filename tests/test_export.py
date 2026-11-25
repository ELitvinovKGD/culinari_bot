"""
Тест Excel-таблицы: числа на листе «Производство» совпадают с заказами.
"""

from io import BytesIO

from openpyxl import load_workbook

from culinary_bot.services import cart, orders
from culinary_bot.services.export import build_issue_workbook
from tests.conftest import make_customer


async def test_production_sheet(session, open_issue):
    soup, patties = open_issue.items
    eva = await make_customer(session, tg_id=1, name="Ева")
    ivan = await make_customer(session, tg_id=2, name="Иван")
    # Ева: 2 заказа (1 суп, потом 1 суп + 1 биточки) — в таблице одна колонка.
    await cart.change_quantity(session, eva, soup, +1)
    first = await orders.place_order(session, eva, open_issue, comment=None)
    await cart.change_quantity(session, eva, soup, +1)
    await cart.change_quantity(session, eva, patties, +1)
    await orders.place_order(session, eva, open_issue, comment="без лука")
    await orders.add_extra_item(session, first, name="Наливка", quantity=1, price_cents=800)
    # Иван: 3 биточки.
    for _ in range(3):
        await cart.change_quantity(session, ivan, patties, +1)
    await orders.place_order(session, ivan, open_issue, comment=None)

    content = build_issue_workbook(open_issue, await orders.issue_orders(session, open_issue))
    ws = load_workbook(BytesIO(content))["Производство"]
    rows = {row[1]: row for row in ws.iter_rows(values_only=True)}

    assert ws["E1"].value.startswith("Ева *")  # у Евы есть комментарий — звёздочка
    assert ws["F1"].value.startswith("Иван\n")
    assert ws["E1"].comment.text == "без лука"
    # Блюдо, цена, всего порций, Ева, Иван
    assert rows["Гороховый суп 700 г"][2:6] == (10, 2, 2, None)
    assert rows["Биточки 5 шт."][2:6] == (12.5, 4, 1, 3)
    assert rows["От шефа, €"][4:6] == (8, None)
    # Ева: 2×10 + 12.5 + 8 = 40.5; Иван: 3×12.5 = 37.5
    assert rows["Итого к оплате, €"][4:6] == (40.5, 37.5)
    assert "Оплачено" in rows
