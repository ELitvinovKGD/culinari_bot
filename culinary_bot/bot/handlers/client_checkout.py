"""
Оформление заказа: данные клиента, комментарий, создание заказа.

Здесь используется FSM (finite state machine, «машина состояний»).
Проблема: бот спрашивает «Как вас зовут?», а ответ приходит ОТДЕЛЬНЫМ
сообщением. Как понять, что это сообщение — имя, а не случайный текст?
FSM запоминает для каждого пользователя «на каком шаге диалога он сейчас»:
    state.set_state(Checkout.name)  → следующий текст обработает on_name
    state.set_state(Checkout.phone) → следующий текст обработает on_phone
А state.update_data(...) хранит промежуточные ответы (имя до ввода телефона).

Поток (docs/scenarios.md, К1 и К2):
    «Итого» → [уведомление о данных] → данные (сохранённые или ввод) → комментарий → заказ
"""

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards, publisher
from culinary_bot.bot.callbacks import CheckoutCb
from culinary_bot.db.models import Customer, DeliveryType
from culinary_bot.services import ServiceError
from culinary_bot.services import customers as customer_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import orders as order_service
from culinary_bot.services import settings as settings_service

router = Router(name="client_checkout")


class Checkout(StatesGroup):
    """Шаги диалога оформления. Каждый State — «бот ждёт ответ на вопрос»."""

    name = State()
    phone = State()
    delivery = State()  # ждём нажатия кнопки «Доставка/Самовывоз»
    address = State()
    comment = State()


# ---------------------------------------------------------------------------
# Начало: «Итого → Всё верно»
# ---------------------------------------------------------------------------


@router.callback_query(CheckoutCb.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    """Клиент подтвердил состав заказа. Дальше — уведомление о данных или сами данные."""
    await callback.message.edit_reply_markup(reply_markup=None)
    if not customer.privacy_accepted:
        await callback.message.answer(texts.PRIVACY_NOTICE, reply_markup=keyboards.single("Понятно", "privacy"))
    else:
        await ask_contact_or_saved(callback.message, state, customer)
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "privacy"))
async def cb_privacy(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    """Клиент прочитал уведомление о личных данных."""
    customer.privacy_accepted = True
    await callback.message.edit_reply_markup(reply_markup=None)
    await ask_contact_or_saved(callback.message, state, customer)
    await callback.answer()


async def ask_contact_or_saved(message: Message, state: FSMContext, customer: Customer) -> None:
    """Есть сохранённые данные — спросить «всё верно?», нет — начать ввод с имени."""
    if customer.has_saved_data():
        await message.answer(
            texts.saved_data_question(customer.name, customer.phone, customer.delivery_type, customer.address),
            reply_markup=keyboards.saved_data(),
        )
    else:
        await start_contact_input(message, state, customer, editing=False)


@router.callback_query(CheckoutCb.filter(F.action == "data_ok"))
async def cb_data_ok(callback: CallbackQuery, state: FSMContext) -> None:
    """Сохранённые данные верны — сразу к комментарию."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await ask_comment(callback.message, state)
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "data_edit"))
async def cb_data_edit(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    """Клиент хочет поменять данные — ввод заново, с кнопками «Оставить прежнее»."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await start_contact_input(callback.message, state, customer, editing=True)
    await callback.answer()


# ---------------------------------------------------------------------------
# Ввод данных: имя → телефон → способ получения → адрес
# ---------------------------------------------------------------------------


async def start_contact_input(message: Message, state: FSMContext, customer: Customer, *, editing: bool) -> None:
    """Начать ввод данных. editing=True — показывать кнопки «Оставить: …»."""
    await state.set_state(Checkout.name)
    await state.update_data(editing=editing)
    kb = keyboards.keep_previous(customer.name) if editing and customer.name else None
    await message.answer(texts.ASK_NAME, reply_markup=kb)


@router.message(Checkout.name, F.text)
async def on_name(message: Message, state: FSMContext, customer: Customer) -> None:
    """Пришло имя."""
    name = message.text.strip()
    if not name or len(name) > 100:
        await message.answer("Введите имя (до 100 символов).")
        return
    await state.update_data(name=name)
    await ask_phone(message, state, customer)


async def ask_phone(message: Message, state: FSMContext, customer: Customer) -> None:
    data = await state.get_data()
    await state.set_state(Checkout.phone)
    kb = keyboards.keep_previous(customer.phone) if data.get("editing") and customer.phone else None
    await message.answer(texts.ASK_PHONE, reply_markup=kb)


@router.message(Checkout.phone, F.text)
async def on_phone(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Пришёл телефон — проверяем формат."""
    try:
        phone = customer_service.normalize_phone(message.text)
    except ServiceError as error:
        await message.answer(str(error))
        return  # остаёмся на шаге «телефон» — следующее сообщение снова сюда
    await state.update_data(phone=phone)
    await ask_delivery(message, state, session, customer)


async def ask_delivery(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Спросить способ получения. Если самовывоз выключен — сразу доставка."""
    if await settings_service.is_pickup_enabled(session):
        await state.set_state(Checkout.delivery)
        await message.answer(texts.ASK_DELIVERY, reply_markup=keyboards.delivery_choice())
    else:
        await state.update_data(delivery_type=DeliveryType.DELIVERY)
        await ask_address(message, state, customer)


@router.callback_query(Checkout.delivery, CheckoutCb.filter(F.action.in_({"delivery", "pickup"})))
async def cb_delivery(
    callback: CallbackQuery, callback_data: CheckoutCb, state: FSMContext, session: AsyncSession, customer: Customer
) -> None:
    """Выбран способ получения."""
    await callback.message.edit_reply_markup(reply_markup=None)
    if callback_data.action == "pickup":
        await state.update_data(delivery_type=DeliveryType.PICKUP, address=None)
        await finish_contact(callback.message, state, customer)
    else:
        await state.update_data(delivery_type=DeliveryType.DELIVERY)
        await ask_address(callback.message, state, customer)
    await callback.answer()


async def ask_address(message: Message, state: FSMContext, customer: Customer) -> None:
    data = await state.get_data()
    await state.set_state(Checkout.address)
    kb = keyboards.keep_previous(customer.address) if data.get("editing") and customer.address else None
    await message.answer(texts.ASK_ADDRESS, reply_markup=kb)


@router.message(Checkout.address, F.text)
async def on_address(message: Message, state: FSMContext, customer: Customer) -> None:
    """Пришёл адрес."""
    address = message.text.strip()
    if len(address) < 5:
        await message.answer("Адрес слишком короткий — напишите подробнее.")
        return
    await state.update_data(address=address)
    await finish_contact(message, state, customer)


@router.callback_query(CheckoutCb.filter(F.action == "keep"))
async def cb_keep(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """
    «Оставить прежнее» — берём значение из сохранённых данных и идём дальше.

    На каком мы шаге, подсказывает текущее состояние FSM.
    """
    await callback.message.edit_reply_markup(reply_markup=None)
    current = await state.get_state()
    if current == Checkout.name.state:
        await state.update_data(name=customer.name)
        await ask_phone(callback.message, state, customer)
    elif current == Checkout.phone.state:
        await state.update_data(phone=customer.phone)
        await ask_delivery(callback.message, state, session, customer)
    elif current == Checkout.address.state:
        await state.update_data(address=customer.address)
        await finish_contact(callback.message, state, customer)
    await callback.answer()


async def finish_contact(message: Message, state: FSMContext, customer: Customer) -> None:
    """Все данные собраны — сохраняем в профиль клиента и спрашиваем комментарий."""
    data = await state.get_data()
    customer_service.save_contact(
        customer,
        name=data["name"],
        phone=data["phone"],
        delivery_type=DeliveryType(data["delivery_type"]),
        address=data.get("address"),
    )
    await ask_comment(message, state)


# ---------------------------------------------------------------------------
# Комментарий и создание заказа
# ---------------------------------------------------------------------------


async def ask_comment(message: Message, state: FSMContext) -> None:
    await state.set_state(Checkout.comment)
    await message.answer(texts.ASK_COMMENT, reply_markup=keyboards.single("Пропустить", "skip"))


@router.message(Checkout.comment, F.text)
async def on_comment(message: Message, state: FSMContext, session: AsyncSession, customer: Customer, bot: Bot) -> None:
    """Пришёл комментарий — оформляем заказ."""
    await place(message, state, session, customer, bot, comment=message.text[:1000])


@router.callback_query(Checkout.comment, CheckoutCb.filter(F.action == "skip"))
async def cb_skip_comment(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer, bot: Bot
) -> None:
    """«Пропустить» комментарий — оформляем заказ без него."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await place(callback.message, state, session, customer, bot, comment=None)
    await callback.answer()


async def place(
    message: Message, state: FSMContext, session: AsyncSession, customer: Customer, bot: Bot, *, comment: str | None
) -> None:
    """
    Создать заказ и разослать уведомления.

    Порядок: сначала заказ (главное), потом пост в группу и уведомление шефу.
    Ошибки отправки publisher ловит сам, так что заказ не откатится
    из-за проблем с группой.
    """
    await state.clear()
    issue = await issue_service.get_open_issue(session)
    if issue is None:
        await message.answer(texts.ISSUE_CLOSED)
        return
    try:
        order = await order_service.place_order(session, customer, issue, comment)
    except ServiceError as error:
        await message.answer(str(error))
        return

    await message.answer(texts.order_accepted(order))
    await publisher.publish_order(bot, session, order)
    await publisher.notify_admins(
        bot, session, texts.new_order_notification(order), keyboards.new_order_notification(order.id), orders=True
    )


# Если в середине диалога прислали не текст (стикер, фото) — подсказываем.
@router.message(StateFilter(Checkout.name, Checkout.phone, Checkout.address, Checkout.comment))
async def on_non_text(message: Message) -> None:
    await message.answer("Пожалуйста, ответьте текстом.")
