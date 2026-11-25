"""
«🧺 В наличии» у клиента — блюда вне лавки (решение от 26.11).

Экран — один список с −/+ (как полный список лавки), нажатие на название
открывает карточку с фото. Корзина бронирует порции (см. services/cart.reserve).

Оформление:
- есть заказ в лавке (открытой или ещё не развезённой) → «Куда добавить?»:
  к нему или отдельно;
- нет — отдельный заказ: данные клиента → комментарий → заказ.
Сами шаги данных и комментария — общие, в client_checkout.py (target = "stock"
или "stock_sep").
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import StockCb
from culinary_bot.bot.handlers import client_checkout
from culinary_bot.bot.ui import replace_message
from culinary_bot.config import settings
from culinary_bot.db.models import Customer, Issue, IssueItem
from culinary_bot.services import ServiceError
from culinary_bot.services import cart as cart_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import orders as order_service
from culinary_bot.services.money import format_price

router = Router(name="client_stock")


async def visible_items(session: AsyncSession, customer: Customer, stock: Issue) -> tuple[list[IssueItem], dict[int, int], int]:
    """
    Что показать клиенту: блюда в наличии + то, что уже лежит у него в корзине
    (даже если остаток стал 0 — это его бронь, её можно уменьшить).
    Возвращает (блюда, {id: количество в корзине}, сумма корзины).
    """
    cart = await cart_service.get_cart(session, customer, stock)
    qty = cart_service.quantities(cart)
    items = [item for item in stock.items if item.is_active and ((item.stock_left or 0) > 0 or item.id in qty)]
    return items, qty, cart_service.cart_total_cents(cart)


async def stock_screen(session: AsyncSession, customer: Customer):
    """(текст, клавиатура) экрана «🧺 В наличии» или None, если всё разобрали."""
    stock = await issue_service.get_stock_issue(session)
    items, qty, total = await visible_items(session, customer, stock)
    if not items:
        return None
    return texts.stock_view_text(stock, settings.stock_hold_minutes), keyboards.stock_list(items, qty, format_price(total))


async def show_stock(message: Message, session: AsyncSession, customer: Customer) -> None:
    """Отправить экран «🧺 В наличии» новым сообщением."""
    screen = await stock_screen(session, customer)
    if screen is None:
        await message.answer(texts.STOCK_EMPTY)
        return
    await message.answer(screen[0], reply_markup=screen[1])


@router.message(F.text == texts.BTN_STOCK)
@router.message(Command("stock"))
async def btn_stock(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    await state.clear()
    await show_stock(message, session, customer)


@router.callback_query(StockCb.filter(F.action == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "open"))
async def cb_open(callback: CallbackQuery, session: AsyncSession, customer: Customer) -> None:
    await show_stock(callback.message, session, customer)
    await callback.answer()


@router.callback_query(StockCb.filter(F.action.in_({"plus", "minus", "list"})))
async def cb_qty(callback: CallbackQuery, callback_data: StockCb, session: AsyncSession, customer: Customer) -> None:
    """+/− (бронь) и возврат к списку из карточки."""
    if callback_data.action in ("plus", "minus"):
        item = await session.get(IssueItem, callback_data.item_id)
        if item is None or not item.is_stock:  # кнопка не от блюда вне лавки — подделка или устарела
            await callback.answer(texts.SOMETHING_WRONG, show_alert=True)
            return
        try:
            await cart_service.change_quantity(session, customer, item, +1 if callback_data.action == "plus" else -1)
        except ServiceError as error:
            await callback.answer(str(error), show_alert=True)
            return
    screen = await stock_screen(session, customer)
    if screen is None:
        await replace_message(callback, texts.STOCK_EMPTY)
    else:
        await replace_message(callback, screen[0], screen[1])
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "card"))
async def cb_card(callback: CallbackQuery, callback_data: StockCb, session: AsyncSession) -> None:
    item = await session.get(IssueItem, callback_data.item_id)
    if item is None or not item.is_stock:
        await callback.answer(texts.SOMETHING_WRONG, show_alert=True)
        return
    await replace_message(callback, texts.stock_item_card(item), keyboards.stock_card_back(), item.photo_file_id)
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "checkout"))
async def cb_checkout(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """«🛒 Оформить»: к заказу в лавке или отдельно."""
    stock = await issue_service.get_stock_issue(session)
    if not await cart_service.get_cart(session, customer, stock):
        await callback.answer(texts.CART_EMPTY, show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    attachable = await order_service.attachable_orders(session, customer)
    if attachable:
        await callback.message.answer(texts.stock_where_text(), reply_markup=keyboards.stock_where(attachable))
    else:
        await client_checkout.start_stock_separate(callback.message, state, customer)
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "to"))
async def cb_to(callback: CallbackQuery, callback_data: StockCb, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Добавить к заказу в лавке (проверяем, что заказ этого клиента и к нему можно добавить)."""
    allowed = {o.id for o in await order_service.attachable_orders(session, customer)}
    if callback_data.order_id not in allowed:  # БЕЗОПАСНОСТЬ: чужой или уже закрытый заказ
        await callback.answer("К этому заказу уже нельзя добавить.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await state.update_data(target="stock", stock_base=callback_data.order_id)
    await client_checkout.ask_comment(callback.message, state, adding=True)
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "separate"))
async def cb_separate(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await client_checkout.start_stock_separate(callback.message, state, customer)
    await callback.answer()
