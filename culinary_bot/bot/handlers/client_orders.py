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
from culinary_bot.db.models import Customer, IssueKind, IssueStatus, Order, OrderStatus
from culinary_bot.services import ServiceError
from culinary_bot.services import orders as order_service
from culinary_bot.services import settings as settings_service

router = Router(name="client_orders")


async def order_keyboard(session: AsyncSession, order: Order):
    """
    Кнопки под заказом клиента.

    «✏️ Изменить» относится к последней части (основной заказ или добавка),
    пока у неё идёт окно правки. «❌ Отменить» отменяет заказ в выпуске целиком.
    """
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
    «Мои заказы»: по каждому незавершённому выпуску — ОДНА карточка
    (основной заказ и все добавки вместе), ниже — история.
    """
    await state.clear()
    orders = await order_service.customer_orders(session, customer, limit=50)
    if not orders:
        await message.answer("Заказов пока нет. Загляните в «🍽 Меню».")
        return

    def is_current(order: Order) -> bool:
        # Лавка — пока её не развезли. Блюда вне лавки — пока заказ не отдан.
        if order.issue.kind == IssueKind.STOCK:
            return order.status == OrderStatus.ACCEPTED
        return order.issue.status != IssueStatus.FINISHED

    current = [o for o in orders if is_current(o)]
    past = [o for o in orders if not is_current(o)]

    # Группируем части: одна карточка на (выпуск, получатель) — заказ себе
    # и заказ для Маши показываются отдельно, добавки — внутри своего заказа.
    groups: dict[tuple[int, int | None], list[Order]] = {}
    for order in sorted(current, key=lambda o: o.id):
        groups.setdefault((order.issue_id, order.recipient_id), []).append(order)

    for parts in groups.values():
        issue = parts[0].issue
        active = [o for o in parts if o.status != OrderStatus.CANCELLED]
        if not active:
            who = f" получателю {texts.esc(parts[0].name)}" if parts[0].is_gift else ""
            await message.answer(f"Заказ{who} в «{texts.esc(issue.title)}» отменён.")
            continue
        summary = order_service.summarize_by_customer(active)[0]
        delivered = all(o.status == OrderStatus.DELIVERED for o in active)
        status = texts.ORDER_STATUS_LABEL[OrderStatus.DELIVERED if delivered else OrderStatus.ACCEPTED]
        await message.answer(
            texts.issue_order_card(issue, summary, status), reply_markup=await order_keyboard(session, active[-1])
        )

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
        # Клиент видит один заказ на получателя — отменяем все его части
        # (основной + добавки) именно этого заказа: себе или для Маши.
        cancelled = await order_service.cancel_customer_issue(session, customer, order.issue, order.recipient_id)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return

    who = f" получателю {texts.esc(order.name)}" if order.is_gift else ""
    await replace_message(callback, f"Заказ{who} в «{texts.esc(order.issue.title)}» отменён.")
    await callback.answer("Заказ отменён")
    for part in cancelled:
        if part.confirmed_at is None:
            continue  # отменён в окне правки: шеф и группа о нём ещё не знали
        await publisher.delete_order_post(bot, session, part)
        await publisher.notify_admins(
            bot,
            session,
            texts.cancel_notification(part, "customer"),
            keyboards.cancel_notification(part.id, part.issue_id),
            cancels=True,
        )
