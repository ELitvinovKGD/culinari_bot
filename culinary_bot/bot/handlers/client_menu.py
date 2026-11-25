"""
Меню для клиента: карусель карточек, полный список и экран «Итого».

Поток (docs/scenarios.md, К1):
    карусель → «Корзина» → полный список → «Дальше» → «Итого» → оформление

Все экраны — одно сообщение, которое обновляется на месте при нажатии
кнопок, чтобы не засорять чат.
"""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import CardCb, CheckoutCb, ListCb
from culinary_bot.db.models import Customer, Issue, IssueItem
from culinary_bot.services import ServiceError
from culinary_bot.services import cart as cart_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services.money import format_price

router = Router(name="client_menu")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


async def replace_message(callback: CallbackQuery, text: str, reply_markup, photo: str | None = None) -> None:
    """
    Показать новый экран вместо текущего сообщения.

    Telegram умеет менять сообщение «на месте», но не умеет превращать
    текстовое сообщение в фото и обратно. Поэтому:
    - фото → фото: edit_media (меняем картинку и подпись);
    - текст → текст: edit_text;
    - тип поменялся: удаляем старое сообщение и отправляем новое.
    """
    message = callback.message
    has_photo = bool(message.photo)
    try:
        if photo and has_photo:
            await message.edit_media(InputMediaPhoto(media=photo, caption=text), reply_markup=reply_markup)
            return
        if not photo and not has_photo:
            await message.edit_text(text, reply_markup=reply_markup)
            return
    except TelegramBadRequest as error:
        # Нажали ту же кнопку дважды — содержимое не изменилось, это не ошибка.
        if "not modified" in str(error):
            return
        raise
    # Тип сообщения меняется: удалить старое и прислать новое.
    try:
        await message.delete()
    except TelegramBadRequest:
        pass  # сообщение могло быть уже удалено — не страшно
    if photo:
        await message.answer_photo(photo, caption=text, reply_markup=reply_markup)
    else:
        await message.answer(text, reply_markup=reply_markup)


async def current_issue_or_none(session: AsyncSession) -> Issue | None:
    """Открытый выпуск или None."""
    return await issue_service.get_open_issue(session)


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
    return texts.card_text(item, index, len(items)), keyboards.card(index, len(items), item, qty, cart_text), item.photo_file_id


async def list_view(session: AsyncSession, customer: Customer, issue: Issue):
    """Собрать экран «полный список»: (текст, клавиатура)."""
    items = issue_service.active_items(issue)
    cart = await cart_service.get_cart(session, customer, issue)
    total = cart_service.cart_total_cents(cart)
    return texts.list_view_text(issue), keyboards.list_view(items, cart_service.quantities(cart), format_price(total))


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
    await replace_message(callback, texts.summary_text(cart, cart_service.cart_total_cents(cart)), keyboards.summary())
    await callback.answer()
