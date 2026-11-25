"""
Меню для клиента: карусель карточек, полный список и экран «Итого».

Поток (docs/scenarios.md, К1):
    карусель → «Корзина» → полный список → «Дальше» → «Итого» → оформление

Все экраны — одно сообщение, которое обновляется на месте при нажатии
кнопок, чтобы не засорять чат.
"""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import CardCb, CheckoutCb, ListCb
from culinary_bot.bot.ui import replace_message
from culinary_bot.db.models import Customer, Issue, IssueItem
from culinary_bot.services import ServiceError
from culinary_bot.services import cart as cart_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import orders as order_service
from culinary_bot.services.money import format_price
from culinary_bot.services.orders import CustomerSummary

router = Router(name="client_menu")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


async def current_issue_or_none(session: AsyncSession) -> Issue | None:
    """Открытый выпуск или None."""
    return await issue_service.get_open_issue(session)


async def existing_orders(session: AsyncSession, customer: Customer, issue: Issue) -> list[CustomerSummary]:
    """Все оформленные заказы клиента в этом выпуске: себе и получателям (по сводке на каждый)."""
    parts = await order_service.customer_issue_orders(session, customer, issue)
    return order_service.summarize_by_customer(parts)


async def existing_order(session: AsyncSession, customer: Customer, issue: Issue) -> CustomerSummary | None:
    """
    Уже оформленный заказ клиента СЕБЕ в этом выпуске (все части вместе) или None.

    Если он есть, меню работает в режиме «дозаказа»: показывает, сколько уже
    заказано, а корзина — это добавка к заказу.
    """
    for summary in await existing_orders(session, customer, issue):
        if summary.recipient_id is None:
            return summary
    return None


async def card_view(session: AsyncSession, customer: Customer, issue: Issue, index: int):
    """
    Собрать карточку: (текст, клавиатура, фото).

    index берётся по модулю числа блюд (%), поэтому если блюдо сняли
    с продажи и список укоротился, старая кнопка не сломается.
    """
    items = issue_service.active_items(issue)
    index %= len(items)
    item = items[index]
    cart = await cart_service.get_cart(session, customer, issue)
    qty = cart_service.quantities(cart).get(item.id, 0)
    cart_text = texts.cart_button(cart_service.cart_count(cart), cart_service.cart_total_cents(cart))
    existing = await existing_order(session, customer, issue)
    in_order = existing.menu_quantities.get(item.id, 0) if existing else 0
    text = texts.card_text(item, index, len(items), in_order)
    return text, keyboards.card(index, len(items), item, qty, cart_text), item.photo_file_id


async def list_view(session: AsyncSession, customer: Customer, issue: Issue):
    """Собрать экран «полный список»: (текст, клавиатура)."""
    items = issue_service.active_items(issue)
    cart = await cart_service.get_cart(session, customer, issue)
    total = cart_service.cart_total_cents(cart)
    existing = await existing_order(session, customer, issue)
    in_order = existing.menu_quantities if existing else None
    return texts.list_view_text(issue), keyboards.list_view(items, cart_service.quantities(cart), format_price(total), in_order)


async def show_menu(message: Message, session: AsyncSession, customer: Customer) -> None:
    """Показать первую карточку открытого выпуска (или «лавка закрыта»)."""
    issue = await current_issue_or_none(session)
    if issue is None or not issue_service.active_items(issue):
        await message.answer(texts.NO_OPEN_ISSUE)
        return
    text, kb, photo = await card_view(session, customer, issue, 0)
    if photo:
        await message.answer_photo(photo, caption=text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


async def get_issue_for_callback(callback: CallbackQuery, session: AsyncSession) -> Issue | None:
    """Открытый выпуск; если его нет — сообщить клиенту и вернуть None."""
    issue = await current_issue_or_none(session)
    if issue is None or not issue_service.active_items(issue):
        # show_alert=True — всплывающее окно, а не короткая подсказка сверху.
        await callback.answer(texts.NO_OPEN_ISSUE, show_alert=True)
        return None
    return issue


# ---------------------------------------------------------------------------
# Карусель
# ---------------------------------------------------------------------------


@router.callback_query(CardCb.filter(F.action == "noop"))
@router.callback_query(ListCb.filter(F.action == "noop"))
async def cb_noop(callback: CallbackQuery) -> None:
    """Кнопки-надписи (номер страницы, количество) — просто ничего не делаем."""
    await callback.answer()


@router.callback_query(CardCb.filter(F.action == "comp"))
async def cb_composition(callback: CallbackQuery, callback_data: CardCb, session: AsyncSession) -> None:
    """
    «📋 Состав» — показать состав блюда.

    Короткий — во всплывающем окне (закрыл — снова выбор блюд).
    Длинный (окно вмещает только 200 символов) — отдельным сообщением
    с кнопкой «← К выбору», которая это сообщение убирает.
    """
    item = await session.get(IssueItem, callback_data.item_id)
    if item is None or not item.composition:
        await callback.answer("Состав не указан.", show_alert=True)
        return
    text = texts.composition_text(item)
    if len(text) <= texts.ALERT_LIMIT:
        await callback.answer(text, show_alert=True)
        return
    await callback.message.answer(
        f"<b>{texts.esc(item.name)}</b>\n\n{texts.esc(text)}", reply_markup=keyboards.hide_message(callback_data.index)
    )
    await callback.answer()


@router.callback_query(CardCb.filter(F.action == "hide"))
async def cb_hide(callback: CallbackQuery) -> None:
    """Убрать сообщение с составом — карточка блюда осталась выше."""
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(CardCb.filter(F.action.in_({"show", "plus", "minus"})))
async def cb_card(callback: CallbackQuery, callback_data: CardCb, session: AsyncSession, customer: Customer) -> None:
    """Листание карусели и +/− на карточке."""
    issue = await get_issue_for_callback(callback, session)
    if issue is None:
        return
    if callback_data.action in ("plus", "minus"):
        item = await session.get(IssueItem, callback_data.item_id)
        # Проверяем, что позиция из ТЕКУЩЕГО выпуска: старая кнопка
        # из прошлого меню не должна добавить в корзину чужое блюдо.
        if item is None or item.issue_id != issue.id:
            await callback.answer(texts.SOMETHING_WRONG, show_alert=True)
            return
        try:
            await cart_service.change_quantity(session, customer, item, +1 if callback_data.action == "plus" else -1)
        except ServiceError as error:
            await callback.answer(str(error), show_alert=True)
            return
    text, kb, photo = await card_view(session, customer, issue, callback_data.index)
    await replace_message(callback, text, kb, photo)
    await callback.answer()


# ---------------------------------------------------------------------------
# Полный список
# ---------------------------------------------------------------------------


@router.callback_query(CheckoutCb.filter(F.action == "list"))
async def cb_open_list(callback: CallbackQuery, session: AsyncSession, customer: Customer) -> None:
    """Кнопка «Корзина» под карточкой или «Изменить» на экране «Итого»."""
    issue = await get_issue_for_callback(callback, session)
    if issue is None:
        return
    text, kb = await list_view(session, customer, issue)
    await replace_message(callback, text, kb)
    await callback.answer()


@router.callback_query(ListCb.filter(F.action.in_({"plus", "minus"})))
async def cb_list_qty(callback: CallbackQuery, callback_data: ListCb, session: AsyncSession, customer: Customer) -> None:
    """+/− в полном списке."""
    issue = await get_issue_for_callback(callback, session)
    if issue is None:
        return
    item = await session.get(IssueItem, callback_data.item_id)
    if item is None or item.issue_id != issue.id:
        await callback.answer(texts.SOMETHING_WRONG, show_alert=True)
        return
    try:
        await cart_service.change_quantity(session, customer, item, +1 if callback_data.action == "plus" else -1)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    text, kb = await list_view(session, customer, issue)
    await replace_message(callback, text, kb)
    await callback.answer()


@router.callback_query(ListCb.filter(F.action == "card"))
async def cb_list_card(callback: CallbackQuery, callback_data: ListCb, session: AsyncSession, customer: Customer) -> None:
    """Нажали на название в списке — открыть карточку этого блюда с фото."""
    issue = await get_issue_for_callback(callback, session)
    if issue is None:
        return
    ids = [item.id for item in issue_service.active_items(issue)]
    index = ids.index(callback_data.item_id) if callback_data.item_id in ids else 0
    text, kb, photo = await card_view(session, customer, issue, index)
    await replace_message(callback, text, kb, photo)
    await callback.answer()


@router.callback_query(ListCb.filter(F.action == "next"))
async def cb_summary(callback: CallbackQuery, session: AsyncSession, customer: Customer) -> None:
    """«Дальше» — экран «Итого» с выбранными позициями."""
    issue = await get_issue_for_callback(callback, session)
    if issue is None:
        return
    cart = await cart_service.get_cart(session, customer, issue)
    if not cart:
        await callback.answer(texts.CART_EMPTY, show_alert=True)
        return
    all_orders = await existing_orders(session, customer, issue)
    own = next((s for s in all_orders if s.recipient_id is None), None)
    gifts = [(s.recipient_id, s.name) for s in all_orders if s.recipient_id is not None]
    total = cart_service.cart_total_cents(cart)
    if gifts:
        # Заказов несколько (себе и/или получателям) — сначала только «Куда добавить?»,
        # «Уже заказано» покажем после выбора (решение от 26.11, вариант б).
        await replace_message(callback, texts.where_to_add_text(cart, total), keyboards.where_to_add(own is not None, gifts))
    else:
        await replace_message(callback, texts.summary_text(cart, total, own), keyboards.summary(has_own=own is not None))
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "pick_to"))
async def cb_pick_to(callback: CallbackQuery, callback_data: CheckoutCb, session: AsyncSession, customer: Customer) -> None:
    """Выбран заказ, к которому добавить: показать итог именно по нему."""
    issue = await get_issue_for_callback(callback, session)
    if issue is None:
        return
    cart = await cart_service.get_cart(session, customer, issue)
    if not cart:
        await callback.answer(texts.CART_EMPTY, show_alert=True)
        return
    rid = callback_data.rid or None
    target = next((s for s in await existing_orders(session, customer, issue) if s.recipient_id == rid), None)
    if target is None:
        await callback.answer("Этот заказ уже нельзя дополнить.", show_alert=True)
        return
    text = texts.summary_text(cart, cart_service.cart_total_cents(cart), target)
    if rid:
        text = f"{texts.order_title(target)}\n\n{text}"
    await replace_message(callback, text, keyboards.add_confirm(callback_data.rid))
    await callback.answer()
