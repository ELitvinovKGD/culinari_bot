"""
«🧺 Вне лавки» у клиента — блюда вне лавки (решения от 26.11).

Экран — один список с −/+ (как полный список лавки), нажатие на название
открывает карточку с фото. Корзина бронирует порции (см. services/cart.reserve).

Оформление — правило «пока у клиента есть заказ в лавке, всё едет с ней»:
- есть заказ в лавке (открытой или ещё не развезённой) → «Куда добавить?»:
  «➕ К моему заказу» / «➕ К заказу получателю: Маша» / «🎁 Другому получателю»;
- нет — отдельный заказ вне лавки: данные → комментарий → заказ
  (шеф напишет, когда будет готово).
Шаги данных, получателя и комментария — общие, в client_checkout.py
(цели stock / stock_gift / stock_sep / stock_sep_gift).
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


async def stock_screen(session: AsyncSession, customer: Customer, page: int = 0):
    """(текст, клавиатура) экрана «🧺 Вне лавки» или None, если всё разобрали."""
    stock = await issue_service.find_stock_issue(session)
    if stock is None:
        return None
    items, qty, total = await visible_items(session, customer, stock)
    if not items:
        return None
    attachable = await order_service.attachable_orders(session, customer)
    lavka_title = attachable[-1].issue.title if attachable else None
    lavka_open = await issue_service.get_open_issue(session) is not None
    text = texts.stock_view_text(stock, settings.stock_hold_minutes, lavka_title)
    return text, keyboards.stock_list(items, qty, format_price(total), lavka_link=lavka_open, page=page)


async def show_stock(message: Message, session: AsyncSession, customer: Customer) -> None:
    """Отправить экран «🧺 В наличии» новым сообщением."""
    screen = await stock_screen(session, customer)
    if screen is None:
        await message.answer(texts.STOCK_EMPTY)
        return
    await message.answer(screen[0], reply_markup=screen[1])


@router.message(F.text.in_({texts.BTN_STOCK, texts.OLD_BTN_STOCK}))
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
    screen = await stock_screen(session, customer, callback_data.page)
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
    await replace_message(callback, texts.stock_item_card(item), keyboards.stock_card_back(callback_data.page), item.photo_file_id)
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "checkout"))
async def cb_checkout(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """«🛒 Оформить»: есть заказ в лавке — «Куда добавить?», нет — отдельный заказ."""
    stock = await issue_service.get_stock_issue(session)
    cart = await cart_service.get_cart(session, customer, stock)
    if not cart:
        await callback.answer(texts.CART_EMPTY, show_alert=True)
        return
    attachable = await order_service.attachable_orders(session, customer)
    if attachable:
        text = texts.stock_where_text(cart, cart_service.cart_total_cents(cart))
        await replace_message(callback, text, keyboards.stock_where(attachable))
    else:
        await callback.message.edit_reply_markup(reply_markup=None)
        await client_checkout.start_stock_separate(callback.message, state, customer)
    await callback.answer()


async def allowed_base(session: AsyncSession, customer: Customer, order_id: int):
    """Заказ в лавке, к которому можно добавить, или None (чужой / уже нельзя)."""
    for order in await order_service.attachable_orders(session, customer):
        if order.id == order_id:
            return order
    return None


@router.callback_query(StockCb.filter(F.action == "pick"))
async def cb_pick(callback: CallbackQuery, callback_data: StockCb, session: AsyncSession, customer: Customer) -> None:
    """Выбран заказ в лавке: показать итог именно по нему и «✅ Завершить заказ»."""
    base = await allowed_base(session, customer, callback_data.order_id)
    if base is None:  # БЕЗОПАСНОСТЬ: чужой или уже закрытый заказ
        await callback.answer("К этому заказу уже нельзя добавить.", show_alert=True)
        return
    stock = await issue_service.get_stock_issue(session)
    cart = await cart_service.get_cart(session, customer, stock)
    if not cart:
        await callback.answer(texts.CART_EMPTY, show_alert=True)
        return
    parts = [o for o in await order_service.customer_issue_orders(session, customer, base.issue) if o.recipient_id == base.recipient_id]
    target = order_service.summarize_by_customer(parts)[0]
    text = f"{texts.order_title(target)} · «{texts.esc(base.issue.title)}»\n\n"
    text += texts.summary_text(cart, cart_service.cart_total_cents(cart), target)
    await replace_message(callback, text, keyboards.stock_add_confirm(base.id))
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "to"))
async def cb_to(callback: CallbackQuery, callback_data: StockCb, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """«✅ Завершить заказ» — добавить к выбранному заказу в лавке; остаётся комментарий."""
    if await allowed_base(session, customer, callback_data.order_id) is None:
        await callback.answer("К этому заказу уже нельзя добавить.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await state.update_data(target="stock", stock_base=callback_data.order_id)
    await client_checkout.ask_comment(callback.message, state, adding=True)
    await callback.answer()


@router.callback_query(StockCb.filter(F.action == "gift"))
async def cb_gift(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """
    «🎁 Другому получателю» при заказе в лавке — новый заказ получателю
    в составе лавки (едет с ней). Лавка — та, где у клиента последний заказ.
    """
    attachable = await order_service.attachable_orders(session, customer)
    if not attachable:
        await callback.answer("Заказа в лавке уже нет — оформите заново.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await client_checkout.start_gift(
        callback.message, state, session, customer, "stock_gift", stock_lavka=attachable[-1].issue_id
    )
    await callback.answer()
