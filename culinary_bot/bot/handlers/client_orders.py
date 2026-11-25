"""
«Мои заказы» и отмена заказа клиентом (docs/scenarios.md, К4 и К5).
"""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards, publisher
from culinary_bot.bot.callbacks import MyOrderCb
from culinary_bot.bot.handlers import client_menu
from culinary_bot.bot.ui import replace_message
from culinary_bot.config import settings
from culinary_bot.db.models import Customer, IssueStatus, Order, OrderStatus
from culinary_bot.services import ServiceError
from culinary_bot.services import orders as order_service
from culinary_bot.services import settings as settings_service
from culinary_bot.services.money import format_price

router = Router(name="client_orders")


async def order_keyboard(session: AsyncSession, order: Order):
    """Кнопки под заказом клиента (зависят от времени, статуса и открытости лавки)."""
    return keyboards.my_order(
        order,
        editable=order_service.is_editable(order, settings.order_edit_minutes),
        issue_open=order.issue.status == IssueStatus.OPEN,
        chef_url=await settings_service.get_chef_contact_url(session),
    )


@router.message(F.text == texts.BTN_MY_ORDERS)
@router.message(Command("orders"))
async def my_orders(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """
    Показать заказы: текущие — отдельными сообщениями с кнопкой «Отменить»,
    прошлые — одним сообщением-историей.
    """
    await state.clear()
    orders = await order_service.customer_orders(session, customer)
    if not orders:
        await message.answer("Заказов пока нет. Загляните в «🍽 Меню».")
        return

    # «Текущие» — из выпусков, которые ещё не завершены.
    current = [o for o in orders if o.issue.status != IssueStatus.FINISHED]
    past = [o for o in orders if o.issue.status == IssueStatus.FINISHED]

    for order in reversed(current):  # по возрастанию номера
        await message.answer(texts.order_for_customer(order), reply_markup=await order_keyboard(session, order))

    # Итог за выпуск, если действующих заказов в одном выпуске несколько.
    active = [o for o in current if o.status != OrderStatus.CANCELLED]
    if len(active) > 1:
        total = sum(o.total_cents for o in active)
        await message.answer(f"<b>Итого за выпуск: {format_price(total)}</b> — доставка одна.")

    if past:
        history = "\n\n".join(texts.order_for_customer(o) for o in past[:10])
        await message.answer(f"<b>История</b>\n\n{history}")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """/cancel — выйти из любого незаконченного диалога (например, оформления)."""
    await state.clear()
    await message.answer("Отменено.")


@router.callback_query(MyOrderCb.filter(F.action == "edit"))
async def cb_edit(callback: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, customer: Customer) -> None:
    """«✏️ Изменить»: заказ возвращается в корзину, клиент правит и оформляет снова."""
    try:
        order = await order_service.get_order(session, callback_data.order_id)
        if order.customer_id != customer.id:  # БЕЗОПАСНОСТЬ: только свой заказ
            raise ServiceError("Заказ не найден.")
        issue = order.issue
        await order_service.revert_to_cart(session, order, settings.order_edit_minutes)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(texts.order_reverted())
    text, kb = await client_menu.list_view(session, customer, issue)
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(MyOrderCb.filter(F.action == "more"))
async def cb_more(callback: CallbackQuery, session: AsyncSession, customer: Customer) -> None:
    """«➕ Дозаказать» — открыть меню текущего выпуска."""
    await client_menu.show_menu(callback.message, session, customer)
    await callback.answer()


@router.callback_query(MyOrderCb.filter(F.action == "cancel"))
async def cb_cancel_ask(callback: CallbackQuery, callback_data: MyOrderCb) -> None:
    """Первое нажатие «Отменить» — спросить подтверждение (защита от случайного нажатия)."""
    await callback.message.edit_reply_markup(reply_markup=keyboards.confirm_cancel(callback_data.order_id))
    await callback.answer("Точно отменить?")


@router.callback_query(MyOrderCb.filter(F.action == "cancel_yes"))
async def cb_cancel_yes(
    callback: CallbackQuery, callback_data: MyOrderCb, session: AsyncSession, customer: Customer, bot: Bot
) -> None:
    """Подтверждённая отмена."""
    try:
        order = await order_service.get_order(session, callback_data.order_id)
        # БЕЗОПАСНОСТЬ: номер заказа пришёл из кнопки — проверяем, что заказ
        # принадлежит именно этому клиенту. Иначе подделанной кнопкой можно
        # было бы отменить чужой заказ.
        if order.customer_id != customer.id:
            raise ServiceError("Заказ не найден.")
        await order_service.cancel_order(session, order, by="customer")
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return

    await replace_message(callback, texts.order_for_customer(order))
    await callback.answer("Заказ отменён")
    if order.confirmed_at is None:
        return  # отменён в окне правки: шеф и группа о нём ещё не знали — сообщать нечего
    await publisher.delete_order_post(bot, session, order)
    await publisher.notify_admins(
        bot,
        session,
        texts.cancel_notification(order, "customer"),
        keyboards.cancel_notification(order.id, order.issue_id),
        cancels=True,
    )
