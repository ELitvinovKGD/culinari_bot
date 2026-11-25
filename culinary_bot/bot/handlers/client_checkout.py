"""
Оформление заказа: данные клиента, получатель, адрес, комментарий, создание заказа.

Здесь используется FSM (finite state machine, «машина состояний»).
Проблема: бот спрашивает «Как вас зовут?», а ответ приходит ОТДЕЛЬНЫМ
сообщением. Как понять, что это сообщение — имя, а не случайный текст?
FSM запоминает для каждого пользователя «на каком шаге диалога он сейчас»:
    state.set_state(Checkout.name)  → следующий текст обработает on_name
    state.set_state(Checkout.phone) → следующий текст обработает on_phone
А state.update_data(...) хранит промежуточные ответы (имя до ввода телефона).

Три пути оформления (docs/scenarios.md, решения от 25–26.11):

    СЕБЕ:      «Итого» → [данные] → «Ваши данные — всё верно?» → комментарий → заказ
    ДРУГОМУ:   ... → «🎁 Другому получателю» → получатель (из списка или новый)
               → «Кто платит?» → комментарий → заказ
    ДОЗАКАЗ:   «Итого» → «Добавить к заказу (себе / для Маши)» → комментарий → заказ

Адрес (свой или получателя) вводится по порядку: сначала текстом, потом,
по желанию, точка на карте. Можно и сразу точкой или ссылкой из Google Maps.

Режим ввода хранится в данных FSM под ключом "mode":
    own     — свои данные при оформлении заказа;
    profile — свои данные из «👤 Мои данные» (без заказа);
    rec     — данные нового получателя.
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
from culinary_bot.config import settings
from culinary_bot.db.models import Customer, DeliveryType, PayerType
from culinary_bot.services import ServiceError, geocode
from culinary_bot.services import customers as customer_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import orders as order_service
from culinary_bot.services import recipients as recipient_service
from culinary_bot.services import settings as settings_service
from culinary_bot.services.money import format_price

router = Router(name="client_checkout")


class Checkout(StatesGroup):
    """Шаги диалога оформления. Каждый State — «бот ждёт ответ на вопрос»."""

    name = State()
    phone = State()
    delivery = State()  # ждём нажатия кнопки «Доставка/Самовывоз»
    address = State()  # адрес: текст, точка или ссылка из Google Maps
    point = State()  # после текстового адреса: точка на карте по желанию
    address_note = State()  # после точки: подъезд, этаж, ориентир
    rec_name = State()  # имя получателя
    rec_phone = State()  # телефон получателя
    comment = State()


# ---------------------------------------------------------------------------
# Начало: «Итого»
# ---------------------------------------------------------------------------


@router.callback_query(CheckoutCb.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    """Новый заказ себе: уведомление о данных (один раз) → данные."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await state.update_data(target="own")
    if not customer.privacy_accepted:
        await callback.message.answer(texts.PRIVACY_NOTICE, reply_markup=keyboards.single("Понятно", "privacy"))
    else:
        await ask_contact_or_saved(callback.message, state, customer)
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "add_to"))
async def cb_add_to(callback: CallbackQuery, callback_data: CheckoutCb, state: FSMContext) -> None:
    """
    Дозаказ: добавить корзину к уже оформленному заказу (себе — rid=0, или
    получателю rid). Данные не спрашиваем — адрес тот же. Только комментарий.
    """
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await state.update_data(target="add", add_rid=callback_data.rid)
    await ask_comment(callback.message, state, adding=True)
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "privacy"))
async def cb_privacy(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Клиент прочитал уведомление о личных данных — продолжаем, откуда пришёл."""
    customer.privacy_accepted = True
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    if data.get("target") == "gift":
        await ask_recipient(callback.message, state, session, customer)
    else:
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
    await state.update_data(target="own")
    await ask_comment(callback.message, state)
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "data_edit"))
async def cb_data_edit(callback: CallbackQuery, state: FSMContext, customer: Customer) -> None:
    """Клиент хочет поменять свои данные — ввод заново, с кнопками «Оставить прежнее»."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(target="own")
    await start_contact_input(callback.message, state, customer, editing=True)
    await callback.answer()


# ---------------------------------------------------------------------------
# Свои данные: имя → телефон → способ получения → адрес
# ---------------------------------------------------------------------------


async def start_contact_input(
    message: Message, state: FSMContext, customer: Customer, *, editing: bool, mode: str = "own"
) -> None:
    """Начать ввод своих данных. editing=True — показывать кнопки «Оставить: …»."""
    await state.set_state(Checkout.name)
    await state.update_data(editing=editing, mode=mode)
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
        await address_done(callback.message, state, session, customer)
    else:
        await state.update_data(delivery_type=DeliveryType.DELIVERY)
        await ask_address(callback.message, state, customer)
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
        await address_done(callback.message, state, session, customer)
    await callback.answer()


# ---------------------------------------------------------------------------
# Адрес: текст → точка (по желанию); или сразу точка/ссылка → уточнение.
# Общий для своих данных и для получателя (mode в данных FSM).
# ---------------------------------------------------------------------------


async def ask_address(message: Message, state: FSMContext, customer: Customer | None = None) -> None:
    data = await state.get_data()
    await state.set_state(Checkout.address)
    await state.update_data(address=None, latitude=None, longitude=None, found_address=None)
    kb = None
    if customer is not None and data.get("editing") and data.get("mode") != "rec":
        if customer.address or customer.latitude is not None:
            kb = keyboards.keep_previous(customer.address or "📍 точка на карте")
    await message.answer(texts.ASK_ADDRESS, reply_markup=kb)


@router.message(Checkout.address, F.text)
async def on_address(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """
    Пришёл адрес текстом — или ссылка из Google Maps.

    Текст → дальше предлагаем точку на карте (по желанию).
    Ссылка из Карт («Поделиться» → «Копировать ссылку») → это сразу точка.
    """
    link = geocode.find_maps_link(message.text)
    if link:
        coords = await geocode.coords_from_link(link)
        if coords:
            await ask_point_note(message, state, *coords)
        else:
            await message.answer("Не получилось прочитать точку из ссылки. Напишите адрес текстом.")
        return
    address = message.text.strip()
    if len(address) < 5:
        await message.answer("Адрес слишком короткий — напишите подробнее.")
        return
    await state.update_data(address=address[:300])
    await state.set_state(Checkout.point)
    await message.answer(texts.ASK_POINT, reply_markup=keyboards.single("Пропустить", "skip_point"))


@router.message(Checkout.address, F.location)
async def on_location(message: Message, state: FSMContext) -> None:
    """Сразу точка вместо текста (📎 → Геопозиция) — определим адрес и спросим уточнение."""
    found = None
    if message.venue:
        # Клиент выбрал из списка конкретное место — Telegram прислал название и адрес.
        found = ", ".join(x for x in (message.venue.title, message.venue.address) if x)
    await ask_point_note(message, state, message.location.latitude, message.location.longitude, found)


async def ask_point_note(message: Message, state: FSMContext, lat: float, lon: float, found: str | None = None) -> None:
    """Точка без текстового адреса: определить адрес по карте и спросить подъезд/этаж."""
    if found is None:
        found = await geocode.reverse(lat, lon)
    await state.update_data(latitude=lat, longitude=lon, found_address=found)
    await state.set_state(Checkout.address_note)
    hint = f"\n📍 Похоже, это: <b>{texts.esc(found)}</b>" if found else ""
    await message.answer(texts.ASK_ADDRESS_NOTE + hint, reply_markup=keyboards.single("Пропустить", "skip_note"))


@router.message(Checkout.point, F.location)
async def on_point(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Точка после текстового адреса — адрес уже есть, просто добавляем координаты."""
    await state.update_data(latitude=message.location.latitude, longitude=message.location.longitude)
    await address_done(message, state, session, customer)


@router.message(Checkout.point, F.text)
async def on_point_text(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """На шаге «точка» прислали текст: ссылка из Карт — берём точку; иначе подсказываем."""
    link = geocode.find_maps_link(message.text)
    coords = await geocode.coords_from_link(link) if link else None
    if coords is None:
        await message.answer("Отправьте точку (📎 → Геопозиция) или ссылку из Google Maps — или нажмите «Пропустить».")
        return
    await state.update_data(latitude=coords[0], longitude=coords[1])
    await address_done(message, state, session, customer)


@router.callback_query(Checkout.point, CheckoutCb.filter(F.action == "skip_point"))
async def cb_skip_point(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await address_done(callback.message, state, session, customer)
    await callback.answer()


@router.message(Checkout.address_note, F.text)
async def on_address_note(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Уточнение к точке: адрес из карт (если нашёлся) + то, что написал клиент."""
    data = await state.get_data()
    parts = [data.get("found_address"), message.text.strip()[:300]]
    await state.update_data(address=", ".join(x for x in parts if x))
    await address_done(message, state, session, customer)


@router.callback_query(Checkout.address_note, CheckoutCb.filter(F.action == "skip_note"))
async def cb_skip_note(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    await state.update_data(address=data.get("found_address"))
    await address_done(callback.message, state, session, customer)
    await callback.answer()


async def address_done(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Адрес собран — дальше зависит от режима: свои данные или получатель."""
    data = await state.get_data()
    if data.get("mode") == "rec":
        await finish_recipient(message, state, session, customer)
    else:
        await finish_contact(message, state, customer)


async def finish_contact(message: Message, state: FSMContext, customer: Customer) -> None:
    """Свои данные собраны — сохраняем в профиль; дальше комментарий (или конец, если из «Мои данные»)."""
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
    if data.get("mode") == "profile":
        await state.clear()
        await message.answer("Данные сохранены ✅")
        return
    await ask_comment(message, state)


# ---------------------------------------------------------------------------
# Другому получателю
# ---------------------------------------------------------------------------


@router.callback_query(CheckoutCb.filter(F.action == "gift"))
async def cb_gift(callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """«🎁 Другому получателю» — выбрать из записной книжки или добавить нового."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await state.update_data(target="gift")
    if not customer.privacy_accepted:
        await callback.message.answer(texts.PRIVACY_NOTICE, reply_markup=keyboards.single("Понятно", "privacy"))
    else:
        await ask_recipient(callback.message, state, session, customer)
    await callback.answer()


async def ask_recipient(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    recipients = await recipient_service.list_recipients(session, customer)
    if not recipients:
        await start_recipient_input(message, state)
        return
    await message.answer("Кому доставить?", reply_markup=keyboards.recipients_choice(recipients))


@router.callback_query(CheckoutCb.filter(F.action == "pick_rec"))
async def cb_pick_rec(
    callback: CallbackQuery, callback_data: CheckoutCb, state: FSMContext, session: AsyncSession, customer: Customer
) -> None:
    """Выбран получатель из списка (проверяем, что он этого клиента)."""
    try:
        recipient = await recipient_service.get_recipient(session, customer, callback_data.rid)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(target="gift", recipient_id=recipient.id)
    await ask_payer(callback.message)
    await callback.answer()


@router.callback_query(CheckoutCb.filter(F.action == "new_rec"))
async def cb_new_rec(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await start_recipient_input(callback.message, state)
    await callback.answer()


async def start_recipient_input(message: Message, state: FSMContext) -> None:
    await state.update_data(target="gift", mode="rec")
    await state.set_state(Checkout.rec_name)
    await message.answer(texts.ASK_REC_NAME)


@router.message(Checkout.rec_name, F.text)
async def on_rec_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or len(name) > 100:
        await message.answer("Введите имя получателя (до 100 символов).")
        return
    await state.update_data(rec_name=name)
    await state.set_state(Checkout.rec_phone)
    await message.answer(texts.ASK_REC_PHONE)


@router.message(Checkout.rec_phone, F.text)
async def on_rec_phone(message: Message, state: FSMContext) -> None:
    try:
        phone = customer_service.normalize_phone(message.text)
    except ServiceError as error:
        await message.answer(str(error))
        return
    await state.update_data(rec_phone=phone)
    await ask_address(message, state)


async def finish_recipient(message: Message, state: FSMContext, session: AsyncSession, customer: Customer) -> None:
    """Новый получатель собран — сохраняем в записную книжку и спрашиваем, кто платит."""
    data = await state.get_data()
    try:
        recipient = await recipient_service.create_recipient(
            session,
            customer,
            name=data["rec_name"],
            phone=data["rec_phone"],
            address=data.get("address"),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
        )
    except ServiceError as error:
        await message.answer(str(error))
        return
    await state.update_data(recipient_id=recipient.id)
    await message.answer(f"Получатель сохранён: {texts.esc(recipient.short())} ✅\nВ следующий раз его можно выбрать из списка.")
    await ask_payer(message)


async def ask_payer(message: Message) -> None:
    await message.answer(texts.ASK_PAYER, reply_markup=keyboards.payer_choice())


@router.callback_query(CheckoutCb.filter(F.action.in_({"payer_me", "payer_rec"})))
async def cb_payer(callback: CallbackQuery, callback_data: CheckoutCb, state: FSMContext) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    payer = PayerType.CUSTOMER if callback_data.action == "payer_me" else PayerType.RECIPIENT
    await state.update_data(payer=payer)
    await ask_comment(callback.message, state)
    await callback.answer()


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
    await place(message, state, session, customer, comment=message.text[:1000])


@router.callback_query(Checkout.comment, CheckoutCb.filter(F.action == "skip"))
async def cb_skip_comment(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, customer: Customer, bot: Bot
) -> None:
    """«Пропустить» комментарий — оформляем заказ без него."""
    await callback.message.edit_reply_markup(reply_markup=None)
    await place(callback.message, state, session, customer, comment=None)
    await callback.answer()


async def place(message: Message, state: FSMContext, session: AsyncSession, customer: Customer, *, comment: str | None) -> None:
    """
    Создать заказ (себе, другому получателю или добавку к существующему).

    Пост в группу и уведомление шефу отправляются НЕ сразу: первые минуты
    клиент может нажать «✏️ Изменить». Когда окно закроется, всё разошлёт
    фоновая задача (bot/jobs.py).
    """
    data = await state.get_data()
    await state.clear()
    issue = await issue_service.get_open_issue(session)
    if issue is None:
        await message.answer(texts.ISSUE_CLOSED)
        return

    target = data.get("target", "own")
    all_parts = await order_service.customer_issue_orders(session, customer, issue)
    recipient = None
    payer = None
    if target == "add":
        # Добавка к заказу себе (rid=0) или получателю.
        rid = data.get("add_rid") or None
        parts = [o for o in all_parts if o.recipient_id == rid]
    elif target == "gift":
        try:
            recipient = await recipient_service.get_recipient(session, customer, data["recipient_id"])
        except (ServiceError, KeyError):
            await message.answer(texts.SOMETHING_WRONG)
            return
        payer = data.get("payer", PayerType.CUSTOMER)
        # Тому же получателю уже заказывали в этом выпуске — это добавка к его заказу.
        parts = [o for o in all_parts if o.recipient_id == recipient.id]
    else:
        # Себе. Если заказ себе уже есть — тоже добавка (двух «себе» не бывает).
        parts = [o for o in all_parts if o.recipient_id is None]
    base = parts[-1] if parts else None

    try:
        order = await order_service.place_order(
            session, customer, issue, comment, base=base, recipient=None if base else recipient, payer=payer
        )
    except ServiceError as error:
        await message.answer(str(error))
        return

    kb = await client_orders.order_keyboard(session, order)
    summary = order_service.summarize_by_customer(parts + [order])[0]
    if base is None and not order.is_gift:
        await message.answer(texts.order_accepted(order), reply_markup=kb)
    elif base is None:
        await message.answer(
            f"🎁 <b>Заказ получателю {texts.esc(order.name)} принят</b>\n\n{texts.summary_lines(summary)}\n\n"
            f"Итого: {format_price(order.total_cents)}{texts.payer_note(summary)}\n\n"
            f"Изменить заказ можно в течение {settings.order_edit_minutes} минут.",
            reply_markup=kb,
        )
    else:
        await message.answer(texts.order_added(summary), reply_markup=kb)


# Если в середине диалога прислали не текст (стикер, фото) — подсказываем.
@router.message(StateFilter(Checkout.name, Checkout.phone, Checkout.address_note, Checkout.comment, Checkout.rec_name, Checkout.rec_phone))
async def on_non_text(message: Message) -> None:
    await message.answer("Пожалуйста, ответьте текстом.")


@router.message(Checkout.address)
async def on_address_other(message: Message) -> None:
    await message.answer("Напишите адрес текстом — или отправьте точку: 📎 → Геопозиция.")


@router.message(Checkout.point)
async def on_point_other(message: Message) -> None:
    await message.answer("Отправьте точку (📎 → Геопозиция) или нажмите «Пропустить».")
