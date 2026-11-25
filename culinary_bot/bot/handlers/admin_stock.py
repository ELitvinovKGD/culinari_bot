"""
«🧺 Блюда вне лавки» в админке (решение от 26.11).

Как лавка, но вместо сроков — количество. Блюда берутся из общего каталога.
Шеф:
- выставляет блюдо и количество, пополняет остаток;
- пишет шапку и публикует пост в группу (кнопка «Заказать» → «🧺 В наличии»);
- смотрит заказы, пишет клиентам сам; массово: «📣 Сообщить всем» и «✅ Все отданы».

Общие экраны (позиция, заказы, Excel, предпросмотр) — в admin_issues.py:
постоянный выпуск «вне лавки» — тот же Issue, только kind = STOCK.
"""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards, publisher
from culinary_bot.bot.callbacks import IssueCb, ItemCb
from culinary_bot.db.models import Issue, OrderStatus
from culinary_bot.services import ServiceError
from culinary_bot.services import catalog as catalog_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import orders as order_service
from culinary_bot.services import settings as settings_service
from culinary_bot.services.money import format_price

router = Router(name="admin_stock")


class StockInput(StatesGroup):
    """Ответы шефа текстом."""

    quantity = State()  # сколько выставить (новое блюдо) или новый остаток
    notify_text = State()  # сообщение всем клиентам с неотданными заказами


async def send_stock(message: Message, session: AsyncSession, issue: Issue | None = None) -> None:
    """Экран «🧺 Блюда вне лавки»."""
    issue = issue or await issue_service.get_stock_issue(session)
    pending = [o for o in await order_service.issue_orders(session, issue) if o.status == OrderStatus.ACCEPTED]
    await message.answer(texts.stock_for_admin(issue, len(pending)), reply_markup=keyboards.stock_admin(issue))


# ---------------------------------------------------------------------------
# Блюда и остаток
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "stock_add"))
async def cb_add(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    dishes = await catalog_service.list_dishes(session)
    await callback.message.answer(
        "Какое блюдо выставить «в наличие»? (Если уже было — остаток пополнится.)",
        reply_markup=keyboards.stock_add_choice(callback_data.issue_id, dishes),
    )
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "stock_dish"))
async def cb_dish(callback: CallbackQuery, callback_data: IssueCb, state: FSMContext) -> None:
    await state.set_state(StockInput.quantity)
    await state.update_data(dish_id=callback_data.dish_id, item_id=None)
    await callback.message.answer("Сколько порций/банок есть? Например: <i>8</i>\n\n<i>/cancel — отменить</i>")
    await callback.answer()


@router.callback_query(ItemCb.filter(F.action == "stock"))
async def cb_set_stock(callback: CallbackQuery, callback_data: ItemCb, state: FSMContext) -> None:
    """«📦 Остаток — изменить» у позиции вне лавки."""
    await state.set_state(StockInput.quantity)
    await state.update_data(dish_id=None, item_id=callback_data.item_id)
    await callback.message.answer(
        "Сколько осталось сейчас? Например: <i>3</i> (0 — закончилось).\n"
        "Порции в корзинах и заказах клиентов это не затронет.\n\n<i>/cancel — отменить</i>"
    )
    await callback.answer()


@router.message(StockInput.quantity, F.text)
async def on_quantity(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    if not message.text.strip().isdigit():
        await message.answer("Введите число, например 8.")
        return
    quantity = int(message.text.strip())
    data = await state.get_data()
    issue = await issue_service.get_stock_issue(session)
    try:
        if data.get("dish_id"):
            dish = await catalog_service.get_dish(session, data["dish_id"])
            await issue_service.add_stock_dish(session, issue, dish, quantity)
        else:
            item = await issue_service.get_item(session, data["item_id"])
            issue_service.set_stock(item, quantity)
    except ServiceError as error:
        await message.answer(str(error))
        return
    await state.clear()
    await session.flush()
    await message.answer("Сохранено ✅ Не забудьте опубликовать или обновить пост в группе.")
    await send_stock(message, session, issue)


# ---------------------------------------------------------------------------
# Пост в группу
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "stock_post"))
async def cb_post(callback: CallbackQuery, session: AsyncSession, bot: Bot, bot_username: str) -> None:
    """Опубликовать пост «вне лавки» в группу (каждый раз — новый пост)."""
    issue = await issue_service.get_stock_issue(session)
    if not issue_service.stock_available(issue):
        await callback.answer("Нечего публиковать: нет блюд с остатком.", show_alert=True)
        return
    chat_id = await settings_service.get_group_chat_id(session)
    if chat_id is None:
        await callback.answer("Группа не подключена.", show_alert=True)
        return
    try:
        ids, text_id, is_caption = await publisher.send_menu_post(bot, chat_id, issue, bot_username)
    except TelegramAPIError as error:
        await callback.answer(f"Не удалось опубликовать: {error}", show_alert=True)
        return
    issue.group_message_ids, issue.group_text_message_id, issue.group_text_is_caption = ids, text_id, is_caption
    await callback.message.answer("📣 Пост опубликован. Кнопка «Заказать» в нём ведёт в «🧺 В наличии».")
    await callback.answer()


# ---------------------------------------------------------------------------
# Массовые действия по заказам
# ---------------------------------------------------------------------------


async def pending_orders(session: AsyncSession):
    issue = await issue_service.get_stock_issue(session)
    return [o for o in await order_service.issue_orders(session, issue) if o.status == OrderStatus.ACCEPTED]


@router.callback_query(IssueCb.filter(F.action == "stock_notify"))
async def cb_notify(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«📣 Сообщить всем с заказами» — один текст всем, у кого есть неотданные заказы."""
    if not await pending_orders(session):
        await callback.answer("Неотданных заказов нет.", show_alert=True)
        return
    await state.set_state(StockInput.notify_text)
    await callback.message.answer(
        "Текст для всех клиентов с неотданными заказами вне лавки.\n"
        "Например: <i>Капуста готова, завтра развожу после 18:00</i>\n"
        "Каждый получит его со своим заказом и суммой.\n\n<i>/cancel — отменить</i>"
    )
    await callback.answer()


@router.message(StockInput.notify_text, F.text)
async def on_notify_text(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    await state.clear()
    if message.text == "/cancel":
        await message.answer("Отменено.")
        return
    orders = await pending_orders(session)
    chef_url = await settings_service.get_chef_contact_url(session)
    kb = keyboards.url_button(texts.BTN_WRITE_CHEF, chef_url) if chef_url else None
    tg_ids = {o.customer_id: o.customer.tg_id for o in orders}
    by_customer: dict[int, list] = {}
    for summary in order_service.summarize_by_customer(orders):
        by_customer.setdefault(summary.customer_id, []).append(summary)
    sent = 0
    for customer_id, summaries in by_customer.items():
        body = "\n\n".join(texts.summary_lines(s) for s in summaries)
        total = sum(s.total_cents for s in summaries)
        text = f"{texts.esc(message.text)}\n\n<b>Ваш заказ вне лавки:</b>\n{body}\n\n<b>К оплате: {format_price(total)}</b>"
        if await publisher.send_safe(bot, tg_ids[customer_id], text, kb):
            sent += 1
    await message.answer(f"📣 Отправлено: {sent} из {len(by_customer)}.")
    await send_stock(message, session)


@router.callback_query(IssueCb.filter(F.action.in_({"stock_done", "stock_done_yes"})))
async def cb_done(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    """«✅ Все заказы отданы» — отметить все неотданные заказы вне лавки доставленными."""
    orders = await pending_orders(session)
    if not orders:
        await callback.answer("Неотданных заказов нет.", show_alert=True)
        return
    if callback_data.action == "stock_done":
        await callback.message.answer(
            f"Отметить отданными все неотданные заказы вне лавки ({len(orders)})?",
            reply_markup=keyboards.confirm(
                "✅ Да, все отданы",
                IssueCb(action="stock_done_yes", issue_id=callback_data.issue_id),
                IssueCb(action="view", issue_id=callback_data.issue_id),
            ),
        )
        await callback.answer()
        return
    for order in orders:
        order_service.mark_delivered(order)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Отмечено отданными: {len(orders)}.")
    await send_stock(callback.message, session)
    await callback.answer()
