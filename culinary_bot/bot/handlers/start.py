"""
Вход в бота: /start (в том числе по ссылкам), нижние кнопки, /delete_me.

/start может прийти с параметром из ссылки t.me/бот?start=...:
    menu          — кнопка «Заказать» под постом в группе → сразу меню;
    admin_<токен> — приглашение в администраторы.
"""

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import DeleteMeCb
from culinary_bot.bot.handlers import client_menu
from culinary_bot.bot.publisher import notify_admins
from culinary_bot.bot.ui import replace_message
from culinary_bot.db.models import Customer
from culinary_bot.services import ServiceError
from culinary_bot.services import admins as admin_service
from culinary_bot.services import customers as customer_service
from culinary_bot.services import settings as settings_service

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    customer: Customer,
    is_admin: bool,
    bot: Bot,
) -> None:
    """
    /start — приветствие; с параметром — меню или приглашение в админы.

    Параметры функции aiogram заполняет сам: message — само сообщение,
    command — разобранная команда (command.args — текст после /start),
    state — FSM (см. client_checkout.py), остальное — из middleware.
    """
    # Любой /start сбрасывает незавершённый диалог (например, ввод адреса).
    await state.clear()
    payload = command.args or ""

    if payload.startswith("admin_"):
        token = payload.removeprefix("admin_")
        try:
            admin = await admin_service.accept_invite(session, token, message.from_user.id, message.from_user.full_name)
        except ServiceError as error:
            await message.answer(str(error))
            return
        await message.answer("Вы стали администратором ✅", reply_markup=keyboards.main_menu(True))
        await notify_admins(bot, session, f"👥 Новый администратор: {texts.esc(admin.name)}")
        return

    await message.answer(texts.WELCOME, reply_markup=keyboards.main_menu(is_admin))
    if payload == "menu":
        await client_menu.show_menu(message, session, customer)


@router.message(F.text == texts.BTN_MENU)
@router.message(Command("menu"))
async def btn_menu(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Кнопка «🍽 Меню» — показать карусель текущего выпуска."""
    await state.clear()
    await client_menu.show_menu(message, session, customer)


@router.message(F.text == texts.BTN_WRITE_CHEF)
@router.message(Command("chef"))
async def btn_write_chef(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """«✉️ Написать шефу» — кнопка-ссылка в личку шефа (контакт из настроек)."""
    await state.clear()
    url = await settings_service.get_chef_contact_url(session)
    if url is None:
        await message.answer(texts.NO_CHEF_CONTACT)
        return
    await message.answer("Вопрос о составе, доставке или заказе — пишите шефу:", reply_markup=keyboards.url_button(texts.BTN_WRITE_CHEF, url))


@router.message(Command("delete_me"))
async def cmd_delete_me(message: Message) -> None:
    """/delete_me — спросить подтверждение удаления личных данных (сценарий К7)."""
    await message.answer(texts.DELETE_ME_CONFIRM, reply_markup=keyboards.delete_me())


@router.callback_query(DeleteMeCb.filter())
async def cb_delete_me(callback: CallbackQuery, callback_data: DeleteMeCb, session: AsyncSession, customer: Customer) -> None:
    """Ответ на вопрос об удалении данных."""
    if callback_data.action == "yes":
        await customer_service.delete_personal_data(session, customer)
        await replace_message(callback, texts.DELETE_ME_DONE)
    else:
        await replace_message(callback, "Хорошо, ничего не удаляем.")
    # Каждый callback нужно "ответить", иначе у кнопки будут крутиться часики.
    await callback.answer()
