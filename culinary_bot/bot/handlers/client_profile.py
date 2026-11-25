"""
«👤 Мои данные»: свои имя, телефон, адрес и записная книжка получателей
(решение от 26.11).
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import ProfileCb
from culinary_bot.bot.handlers import client_checkout
from culinary_bot.db.models import Customer
from culinary_bot.services import ServiceError
from culinary_bot.services import recipients as recipient_service

router = Router(name="client_profile")


async def send_my_data(message: Message, session: AsyncSession, customer: Customer) -> None:
    recipients = await recipient_service.list_recipients(session, customer)
    await message.answer(texts.my_data_text(customer, recipients), reply_markup=keyboards.my_data(recipients))


@router.message(F.text == texts.BTN_MY_DATA)
@router.message(Command("me"))
async def my_data(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    await state.clear()
    await send_my_data(message, session, customer)


@router.callback_query(ProfileCb.filter(F.action == "edit"))
async def cb_edit(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    """Изменить свои данные — тот же ввод, что при заказе, но без оформления заказа."""
    await state.clear()
    await client_checkout.start_contact_input(callback.message, state, customer, editing=True, mode="profile")
    await callback.answer()


@router.callback_query(ProfileCb.filter(F.action == "del_rec"))
async def cb_del_rec(callback: CallbackQuery, callback_data: ProfileCb) -> None:
    """Удалить получателя — сначала подтверждение."""
    await callback.message.answer(
        "Удалить получателя из списка? Уже оформленные заказы останутся.",
        reply_markup=keyboards.setup_buttons(("🗑 Да, удалить", ProfileCb(action="del_rec_yes", rid=callback_data.rid))),
    )
    await callback.answer()


@router.callback_query(ProfileCb.filter(F.action == "del_rec_yes"))
async def cb_del_rec_yes(callback: CallbackQuery, callback_data: ProfileCb, session: AsyncSession, customer: Customer) -> None:
    try:
        # БЕЗОПАСНОСТЬ: delete_recipient проверяет, что получатель — этого клиента.
        await recipient_service.delete_recipient(session, customer, callback_data.rid)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.edit_text("Получатель удалён.")
    await session.flush()
    await send_my_data(callback.message, session, customer)
    await callback.answer()
