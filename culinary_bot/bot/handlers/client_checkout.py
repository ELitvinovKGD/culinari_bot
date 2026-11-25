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
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import CheckoutCb
from culinary_bot.bot.handlers import client_orders
from culinary_bot.db.models import Customer, DeliveryType
from culinary_bot.services import ServiceError, geocode
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
    address = State()  # текст адреса ИЛИ геопозиция
    address_note = State()  # после геопозиции: подъезд, этаж, ориентир
    comment = State()


# ---------------------------------------------------------------------------
# Начало: «Итого → Всё верно»
# ---------------------------------------------------------------------------


@router.callback_query(CheckoutCb.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """
    Клиент подтвердил состав. Дальше — уведомление о данных или сами данные.

    Дозаказ (заказ в этом выпуске уже есть): данные не спрашиваем — адрес тот же,
    сразу комментарий к добавке.
    """
    await callback.message.edit_reply_markup(reply_markup=None)
    issue = await issue_service.get_open_issue(session)
    if issue is not None and await order_service.customer_issue_orders(session, customer, issue):
        await ask_comment(callback.message, state, adding=True)
    elif not customer.privacy_accepted:
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
            texts.saved_data_question(
                customer.name, customer.phone, customer.delivery_type, customer.address, customer.latitude is not None
            ),
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
        await state.update_data(delivery_type=DeliveryType.PICKUP, address=None, latitude=None, longitude=None)
        await finish_contact(callback.message, state, customer)
    else:
        await state.update_data(delivery_type=DeliveryType.DELIVERY)
        await ask_address(callback.message, state, customer)
    await callback.answer()


async def ask_address(message: Message, state: FSMContext, customer: Customer) -> None:
    data = await state.get_data()
    await state.set_state(Checkout.address)
    has_previous = customer.address or customer.latitude is not None
    previous = customer.address or "📍 точка на карте"
    kb = keyboards.keep_previous(previous) if data.get("editing") and has_previous else None
    await message.answer(texts.ASK_ADDRESS, reply_markup=kb)


@router.message(Checkout.address, F.text)
async def on_address(message: Message, state: FSMContext, customer: Customer) -> None:
    """Пришёл адрес текстом."""
    address = message.text.strip()
    if len(address) < 5:
        await message.answer("Адрес слишком короткий — напишите подробнее.")
        return
    await state.update_data(address=address, latitude=None, longitude=None)
    await finish_contact(message, state, customer)


@router.message(Checkout.address, F.location)
async def on_location(message: Message, state: FSMContext) -> None:
    """
    Пришла точка на карте (📎 → Геопозиция).

    Координаты точные, но подъезд и этаж по ним не понять — спрашиваем уточнение.
    """
    lat, lon = message.location.latitude, message.location.longitude
    # Адрес текстом рядом с точкой: если клиент выбрал из списка конкретное место,
    # Telegram присылает его название и адрес (venue). Иначе спрашиваем у карт.
    if message.venue:
        found = ", ".join(x for x in (message.venue.title, message.venue.address) if x)
    else:
        found = await geocode.reverse(lat, lon)
    await state.update_data(latitude=lat, longitude=lon, found_address=found)
    await state.set_state(Checkout.address_note)
    hint = f"\n📍 Похоже, это: <b>{texts.esc(found)}</b>" if found else ""
    await message.answer(texts.ASK_ADDRESS_NOTE + hint, reply_markup=keyboards.single("Пропустить", "skip_note"))


@router.message(Checkout.address_note, F.text)
async def on_address_note(message: Message, state: FSMContext, customer: Customer) -> None:
    """Уточнение к точке: адрес из карт (если нашёлся) + то, что написал клиент."""
    data = await state.get_data()
    parts = [data.get("found_address"), message.text.strip()[:300]]
    await state.update_data(address=", ".join(x for x in parts if x))
    await finish_contact(message, state, customer)


@router.callback_query(Checkout.address_note, CheckoutCb.filter(F.action == "skip_note"))
async def cb_skip_note(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await state.update_data(address=data.get("found_address"))
    await finish_contact(callback.message, state, customer)
    await callback.answer()


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
        await state.update_data(address=customer.address, latitude=customer.latitude, longitude=customer.longitude)
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
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
    )
    await ask_comment(message, state)


# ---------------------------------------------------------------------------
# Комментарий и создание заказа
# ---------------------------------------------------------------------------


async def ask_comment(message: Message, state: FSMContext, adding: bool = False) -> None:
    await state.set_state(Checkout.comment)
    text = "Комментарий к добавке, если нужен." if adding else texts.ASK_COMMENT
    await message.answer(text, reply_markup=keyboards.single("Пропустить", "skip"))


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
    Создать заказ.

    Пост в группу и уведомление шефу отправляются НЕ сразу: первые минуты
    клиент может нажать «✏️ Изменить». Когда окно закроется, всё разошлёт
    фоновая задача (bot/jobs.py).
    """
    await state.clear()
    issue = await issue_service.get_open_issue(session)
    if issue is None:
        await message.answer(texts.ISSUE_CLOSED)
        return
    # Уже есть заказ в этом выпуске — это добавка: адрес и контакты берём из него.
    parts = await order_service.customer_issue_orders(session, customer, issue)
    base = parts[-1] if parts else None
    try:
        order = await order_service.place_order(session, customer, issue, comment, base=base)
    except ServiceError as error:
        await message.answer(str(error))
        return

    if base is None:
        await message.answer(texts.order_accepted(order), reply_markup=await client_orders.order_keyboard(session, order))
    else:
        summary = order_service.summarize_by_customer(parts + [order])[0]
        await message.answer(texts.order_added(summary), reply_markup=await client_orders.order_keyboard(session, order))


# Если в середине диалога прислали не текст (стикер, фото) — подсказываем.
@router.message(StateFilter(Checkout.name, Checkout.phone, Checkout.address_note, Checkout.comment))
async def on_non_text(message: Message) -> None:
    await message.answer("Пожалуйста, ответьте текстом.")


@router.message(Checkout.address)
async def on_address_other(message: Message) -> None:
    await message.answer("Напишите адрес текстом или отправьте точку: 📎 → Геопозиция.")
