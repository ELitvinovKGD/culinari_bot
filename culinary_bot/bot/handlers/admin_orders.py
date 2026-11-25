"""
Заказ в админке: просмотр, «доставлен», отмена шефом, позиции от шефа (А6).
"""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards, publisher
from culinary_bot.bot.callbacks import AOrderCb
from culinary_bot.bot.ui import replace_message
from culinary_bot.db.models import Order
from culinary_bot.services import ServiceError
from culinary_bot.services import orders as order_service
from culinary_bot.services.money import parse_price

router = Router(name="admin_orders")


class ExtraItem(StatesGroup):
    """Добавление позиции от шефа: название → количество → цена."""

    name = State()
    quantity = State()
    price = State()


async def send_order(message: Message, order: Order) -> None:
    await message.answer(texts.order_for_admin(order), reply_markup=keyboards.admin_order(order))


@router.callback_query(AOrderCb.filter())
async def cb_order(callback: CallbackQuery, callback_data: AOrderCb, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    try:
        order = await order_service.get_order(session, callback_data.order_id)
        action = callback_data.action

        if action == "view":
            await send_order(callback.message, order)

        elif action == "delivered":
            order_service.mark_delivered(order)
            await replace_message(callback, texts.order_for_admin(order), reply_markup=keyboards.admin_order(order))

        elif action == "cancel":
            await callback.message.edit_reply_markup(
                reply_markup=keyboards.confirm(
                    "❌ Да, отменить заказ",
                    AOrderCb(action="cancel_yes", order_id=order.id),
                    AOrderCb(action="view", order_id=order.id),
                )
            )

        elif action == "cancel_yes":
            await order_service.cancel_order(session, order, by="admin")
            await replace_message(callback, texts.order_for_admin(order), reply_markup=keyboards.admin_order(order))
            await publisher.send_safe(bot, order.customer.tg_id, texts.cancelled_by_admin(order))
            await publisher.delete_order_post(bot, session, order)

        elif action == "extra":
            await state.set_state(ExtraItem.name)
            await state.update_data(order_id=order.id)
            await callback.message.answer(
                f"Позиция от шефа в заказ №{order.id}.\nНазвание (например «Наливка»):\n\n<i>/cancel — отменить</i>"
            )

        elif action == "del_extra":
            await order_service.remove_extra_item(session, order, callback_data.extra_id)
            await replace_message(callback, texts.order_for_admin(order), reply_markup=keyboards.admin_order(order))

    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.answer()


@router.message(F.text == "/cancel", ExtraItem.name)
@router.message(F.text == "/cancel", ExtraItem.quantity)
@router.message(F.text == "/cancel", ExtraItem.price)
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


@router.message(ExtraItem.name, F.text)
async def on_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip()[:200])
    await state.set_state(ExtraItem.quantity)
    await message.answer("Количество (число):")


@router.message(ExtraItem.quantity, F.text)
async def on_quantity(message: Message, state: FSMContext) -> None:
    # isdigit() — строка состоит только из цифр; защищает int() от ошибки.
    if not message.text.strip().isdigit():
        await message.answer("Введите количество числом, например 1.")
        return
    await state.update_data(quantity=int(message.text.strip()))
    await state.set_state(ExtraItem.price)
    await message.answer("Цена за штуку в евро:")


@router.message(ExtraItem.price, F.text)
async def on_price(message: Message, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    try:
        cents = parse_price(message.text)
        data = await state.get_data()
        order = await order_service.get_order(session, data["order_id"])
        await order_service.add_extra_item(session, order, name=data["name"], quantity=data["quantity"], price_cents=cents)
    except ServiceError as error:
        await message.answer(str(error))
        return
    await state.clear()
    await publisher.send_safe(bot, order.customer.tg_id, texts.extra_added(order, data["name"], data["quantity"], cents))
    await message.answer("Добавлено, клиент уведомлён ✅")
    await send_order(message, order)
