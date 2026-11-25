"""
Выпуски в админке: создание, блюда, публикация, правки, закрытие приёма,
завершение, Excel, лишние порции (docs/scenarios.md, А2–А3, А7–А10).
"""

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import jobs, keyboards, publisher
from culinary_bot.bot.callbacks import IssueCb, ItemCb, LeftCb
from culinary_bot.bot.handlers import admin_stock, client_orders
from culinary_bot.bot.ui import replace_message
from culinary_bot.db.models import DeliveryType, Issue, IssueItem, IssueKind, IssueStatus, OrderStatus, utcnow
from culinary_bot.services import ServiceError
from culinary_bot.services import catalog as catalog_service
from culinary_bot.services import export as export_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import orders as order_service
from culinary_bot.services import settings as settings_service
from culinary_bot.services.money import format_price

router = Router(name="admin_issues")


class IssueInput(StatesGroup):
    """Текстовые ответы шефа в разделе выпусков."""

    title = State()  # название нового выпуска
    header = State()  # шапка — спрашиваем сразу после названия
    delivery_text = State()  # объявление выдачи для клиентов с доставкой
    pickup_text = State()  # объявление выдачи для клиентов с самовывозом


class Leftovers(StatesGroup):
    """Выбор лишних порций (количество хранится в данных FSM)."""

    choosing = State()


# ---------------------------------------------------------------------------
# Показ экранов (используются и из других модулей)
# ---------------------------------------------------------------------------


async def send_issue(message: Message, session: AsyncSession, issue: Issue) -> None:
    """Отправить экран выпуска с кнопками по его состоянию (для «вне лавки» — свой экран)."""
    if issue.kind == IssueKind.STOCK:
        await admin_stock.send_stock(message, session, issue)
        return
    orders = await order_service.issue_orders(session, issue, include_cancelled=False)
    total = sum(o.total_cents for o in orders)
    await message.answer(texts.issue_for_admin(issue, len(orders), total), reply_markup=keyboards.issue(issue))


async def send_item(message: Message, session: AsyncSession, item: IssueItem) -> None:
    """Отправить позицию выпуска (как её увидит клиент) с кнопками правки."""
    can_remove = not await issue_service.item_has_orders(session, item)
    status = "" if item.is_active else "\n\n⛔ <i>Снято с продажи</i>"
    composition = f"\n\n<i>Состав: {texts.esc(item.composition)}</i>" if item.composition else ""
    caption = texts.item_block(item) + composition + status
    kb = keyboards.item(item, can_remove)
    if item.photo_file_id:
        await message.answer_photo(item.photo_file_id, caption=caption, reply_markup=kb)
    else:
        await message.answer(caption, reply_markup=kb)


async def after_item_added(message: Message, session: AsyncSession, issue: Issue, bot: Bot | None = None) -> None:
    """
    После добавления блюда: в открытом выпуске уведомить клиентов с заказами (К8)
    и напомнить обновить пост; затем показать выпуск.
    """
    if issue.status == IssueStatus.OPEN:
        item = issue.items[-1]
        if bot is None:
            bot = message.bot
        orders = await order_service.issue_orders(session, issue, include_cancelled=False)
        notified: set[int] = set()
        for order in orders:
            if order.customer.tg_id in notified:
                continue  # у клиента несколько заказов — уведомляем один раз
            notified.add(order.customer.tg_id)
            await publisher.send_safe(bot, order.customer.tg_id, texts.item_added(item))
        await message.answer(
            f"Блюдо добавлено. Уведомлено клиентов: {len(notified)}.\nНе забудьте «🔄 Обновить пост в группе»."
        )
    await send_issue(message, session, issue)


async def load_issue(callback: CallbackQuery, session: AsyncSession, issue_id: int) -> Issue | None:
    """Загрузить выпуск; если не найден — сообщить и вернуть None."""
    try:
        return await issue_service.get_issue(session, issue_id)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return None


# ---------------------------------------------------------------------------
# Новый выпуск
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "new"))
async def cb_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(IssueInput.title)
    await callback.message.answer("Название выпуска (например «Тайная лавка шефа | GREAT HITS»):\n\n<i>/cancel — отменить</i>")
    await callback.answer()


@router.message(IssueInput.title, F.text)
async def on_title(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    await state.clear()
    try:
        issue = await issue_service.create_draft(session, message.text)
    except ServiceError as error:
        await message.answer(str(error))
        return
    # Шапку спрашиваем сразу: шеф придумал название — тут же пишет шапку
    # (решение по итогам тестов). Текст после списка — потом, отдельной кнопкой.
    await state.set_state(IssueInput.header)
    await state.update_data(issue_id=issue.id)
    await message.answer(
        "Черновик создан ✅\n\nТеперь шапка поста — текст перед списком блюд.\n"
        "Например: <i>Last call, хомячки мои! Лечу в Цогимар за рыбой…</i>",
        reply_markup=keyboards.skip_header(issue.id),
    )


@router.message(IssueInput.header, F.text)
async def on_header(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Шапка нового выпуска."""
    data = await state.get_data()
    await state.clear()
    issue = await issue_service.get_issue(session, data["issue_id"])
    if message.text != "/cancel":
        issue_service.update_issue_field(issue, "header", message.text.strip())
        await session.flush()
    await message.answer("Дальше — добавьте блюда: «➕ Блюдо».")
    await send_issue(message, session, issue)


# ---------------------------------------------------------------------------
# Просмотр выпуска и добавление блюд
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "view"))
async def cb_view(callback: CallbackQuery, callback_data: IssueCb, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    issue = await load_issue(callback, session, callback_data.issue_id)
    if issue:
        await send_issue(callback.message, session, issue)
        await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "add"))
async def cb_add(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    """Список блюд каталога, которых ещё нет в выпуске."""
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    used = {item.dish_id for item in issue.items}
    dishes = [d for d in await catalog_service.list_dishes(session) if d.id not in used]
    await callback.message.answer(
        "Выберите блюдо из каталога или создайте новое:", reply_markup=keyboards.add_dish_choice(issue.id, dishes)
    )
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "add_dish"))
async def cb_add_dish(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession, bot: Bot) -> None:
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    try:
        dish = await catalog_service.get_dish(session, callback_data.dish_id)
        await issue_service.add_dish_to_issue(session, issue, dish)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.answer("Добавлено")
    await after_item_added(callback.message, session, issue, bot)


# ---------------------------------------------------------------------------
# Позиция выпуска
# ---------------------------------------------------------------------------


@router.callback_query(ItemCb.filter())
async def cb_item(callback: CallbackQuery, callback_data: ItemCb, session: AsyncSession, bot: Bot) -> None:
    """Все кнопки позиции: просмотр, порядок, снятие с продажи, удаление, цена."""
    try:
        item = await issue_service.get_item(session, callback_data.item_id)
        action = callback_data.action

        if action == "view":
            await send_item(callback.message, session, item)

        elif action in ("up", "down"):
            issue_service.move_item(item, -1 if action == "up" else +1)
            await session.flush()
            await send_issue(callback.message, session, item.issue)

        elif action in ("off", "on"):
            orders = await issue_service.set_item_active(session, item, action == "on")
            await session.flush()
            if action == "off" and item.issue.status == IssueStatus.OPEN:
                for order in orders:
                    if order.status == OrderStatus.ACCEPTED:
                        await publisher.send_safe(bot, order.customer.tg_id, texts.item_removed(item, order))
                await callback.message.answer(
                    f"Снято с продажи. Клиентов с этим блюдом уведомили: {len(orders)}.\n"
                    "Обновите пост в группе, чтобы блюдо пропало и там."
                )
            await send_item(callback.message, session, item)

        elif action == "remove":
            await callback.message.answer(
                f"Убрать «{texts.esc(item.name)}» из выпуска?",
                reply_markup=keyboards.confirm(
                    "Да, убрать", ItemCb(action="remove_yes", item_id=item.id), ItemCb(action="view", item_id=item.id)
                ),
            )

        elif action == "remove_yes":
            issue = item.issue
            await issue_service.remove_item(session, item)
            await session.flush()
            await send_issue(callback.message, session, issue)

        elif action in ("price_all", "price_new"):
            await apply_price(callback.message, session, bot, item, callback_data.value, action == "price_all")
            await callback.message.edit_reply_markup(reply_markup=None)

    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.answer()


async def handle_price_change(message: Message, session: AsyncSession, bot: Bot, item: IssueItem, cents: int) -> None:
    """
    Шеф ввёл новую цену позиции (вызывается из admin_fields).

    Если выпуск открыт и позицию уже заказывали — спросить, применять ли
    цену к оформленным заказам. Иначе — просто сохранить.
    """
    issue_service.ensure_editable(item.issue)
    orders = await issue_service.orders_with_item(session, item)
    active = [o for o in orders if o.status == OrderStatus.ACCEPTED]
    if item.issue.status == IssueStatus.OPEN and active:
        await message.answer(
            f"Новая цена {format_price(cents)}. Это блюдо уже в {len(active)} заказ(ах).\n"
            "Применить новую цену к оформленным заказам? Клиенты получат уведомление с новым итогом.",
            reply_markup=keyboards.price_apply(item.id, cents),
        )
        return
    await apply_price(message, session, bot, item, cents, apply_to_orders=False)


async def apply_price(message: Message, session: AsyncSession, bot: Bot, item: IssueItem, cents: int, apply_to_orders: bool) -> None:
    """Сохранить цену и, если применили к заказам, уведомить клиентов (К8)."""
    old = item.price_cents
    changed = await issue_service.change_item_price(session, item, cents, apply_to_orders=apply_to_orders)
    await session.flush()
    for order in changed:
        await publisher.send_safe(
            bot, order.customer.tg_id, texts.price_changed(item, old, order), await client_orders.order_keyboard(session, order)
        )
    note = f" Уведомлено заказов: {len(changed)}." if changed else ""
    extra = "\nНе забудьте «🔄 Обновить пост в группе»." if item.issue.status == IssueStatus.OPEN else ""
    await message.answer(f"Цена сохранена: {format_price(cents)}.{note}{extra}")
    await send_item(message, session, item)


# ---------------------------------------------------------------------------
# Предпросмотр и публикация
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "preview"))
async def cb_preview(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession, bot: Bot, bot_username: str) -> None:
    """Прислать пост ровно таким, каким он уйдёт в группу."""
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    if not issue_service.active_items(issue):
        await callback.answer("В выпуске нет блюд.", show_alert=True)
        return
    await callback.message.answer("👁 Так пост будет выглядеть в группе:")
    await publisher.send_menu_post(bot, callback.message.chat.id, issue, bot_username)
    # Кнопки сразу под предпросмотром — не нужно листать вверх к экрану выпуска.
    await callback.message.answer("Всё верно?", reply_markup=keyboards.preview_actions(issue))
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "publish"))
async def cb_publish(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    """Спросить подтверждение публикации."""
    if await settings_service.get_group_chat_id(session) is None:
        await callback.answer(
            "Группа не подключена: добавьте бота в группу админом и напишите там /connect.", show_alert=True
        )
        return
    await callback.message.answer(
        "Опубликовать выпуск в группе и открыть приём заказов?",
        reply_markup=keyboards.confirm(
            "🚀 Да, опубликовать",
            IssueCb(action="publish_yes", issue_id=callback_data.issue_id),
            IssueCb(action="view", issue_id=callback_data.issue_id),
        ),
    )
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "publish_yes"))
async def cb_publish_yes(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession, bot: Bot, bot_username: str) -> None:
    """
    Открыть выпуск и опубликовать пост.

    Сначала проверяем правила (open_issue), потом шлём пост. Если Telegram
    не дал опубликовать — выпуск всё равно открыт (заказы через бота идут),
    а шеф получает предупреждение и может переслать предпросмотр вручную (Б3).
    """
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    try:
        await issue_service.open_issue(session, issue)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    chat_id = await settings_service.get_group_chat_id(session)
    try:
        ids, text_id, is_caption = await publisher.send_menu_post(bot, chat_id, issue, bot_username)
        issue.group_message_ids, issue.group_text_message_id, issue.group_text_is_caption = ids, text_id, is_caption
        await callback.message.answer("🚀 Выпуск опубликован, приём заказов открыт!")
    except TelegramAPIError as error:
        await callback.message.answer(
            f"⚠️ Приём открыт, но пост в группу не отправился ({error}).\n"
            "Проверьте права бота в группе. Можно нажать «👁 Предпросмотр» и переслать пост вручную."
        )
    await send_issue(callback.message, session, issue)
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "update_post"))
async def cb_update_post(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession, bot: Bot, bot_username: str) -> None:
    issue = await load_issue(callback, session, callback_data.issue_id)
    if issue:
        result = await publisher.update_menu_post(bot, session, issue, bot_username)
        await callback.answer(result, show_alert=True)


@router.callback_query(IssueCb.filter(F.action.in_({"delete", "delete_yes"})))
async def cb_delete(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    """Удалить черновик (с подтверждением)."""
    if callback_data.action == "delete":
        await callback.message.answer(
            "Удалить черновик?",
            reply_markup=keyboards.confirm(
                "🗑 Да, удалить",
                IssueCb(action="delete_yes", issue_id=callback_data.issue_id),
                IssueCb(action="view", issue_id=callback_data.issue_id),
            ),
        )
        await callback.answer()
        return
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    try:
        await issue_service.delete_draft(session, issue)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await replace_message(callback, "Черновик удалён.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Заказы и Excel
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "orders"))
async def cb_orders(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    orders = await order_service.issue_orders(session, issue)
    text = f"<b>Заказы — {texts.esc(issue.title)}</b>" if orders else "Заказов пока нет."
    await callback.message.answer(text, reply_markup=keyboards.orders_list(issue.id, orders))
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "excel"))
async def cb_excel(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    """Собрать Excel и отправить документом."""
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    orders = await order_service.issue_orders(session, issue)
    if issue.kind == IssueKind.STOCK:
        # Вне лавки «выпуск» постоянный — в таблицу только ещё не отданные заказы.
        orders = [o for o in orders if o.status == OrderStatus.ACCEPTED]
    content = export_service.build_issue_workbook(issue, orders)
    # Запоминаем время выгрузки: если потом придёт заказ, шеф получит
    # «⚠️ Таблица изменилась — пересоберите, если распечатали».
    issue.excel_exported_at = utcnow()
    # BufferedInputFile — файл из памяти (байты) с именем, под которым его увидит шеф.
    file = BufferedInputFile(content, filename=f"vypusk_{issue.id}.xlsx")
    await callback.message.answer_document(file, caption=f"📊 {texts.esc(issue.title)}: производство и доставка")
    await callback.answer()


# ---------------------------------------------------------------------------
# Закрытие приёма и завершение выпуска
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "close"))
async def cb_close(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    """«🔒 Закрыть приём» — спросить подтверждение."""
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    await callback.message.answer(
        f"Закрыть приём заказов в «{texts.esc(issue.title)}»?\n\n"
        "Клиенты получат короткое сообщение «приём закрыт, заказ в работе».\n"
        "Когда всё будет готово — нажмите «📣 Объявить выдачу».",
        reply_markup=keyboards.confirm(
            "🔒 Да, закрыть", IssueCb(action="close_yes", issue_id=issue.id), IssueCb(action="view", issue_id=issue.id)
        ),
    )
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action == "close_yes"))
async def cb_close_yes(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession, bot: Bot) -> None:
    """
    Закрыть приём (А7, решение от 25.11 — без текста шефа).

    Заказы, у которых ещё идёт окно правки, подтверждаем сразу: приём закрыт,
    менять уже нечего, а шеф должен увидеть их в списке и таблице.
    """
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    try:
        await issue_service.close_issue(session, issue)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    for order in await order_service.unconfirmed_issue_orders(session, issue):
        await jobs.finalize_order(bot, session, order)

    orders = await order_service.issue_orders(session, issue, include_cancelled=False)
    customer_tg_ids = {o.customer.tg_id for o in orders}
    for tg_id in customer_tg_ids:
        await publisher.send_safe(bot, tg_id, texts.issue_closed_message(issue))
    await callback.message.answer(
        f"🔒 Приём закрыт. Клиентов с заказами: {len(customer_tg_ids)}.\n"
        "Когда всё будет готово — «📣 Объявить выдачу»."
    )
    await send_issue(callback.message, session, issue)
    await callback.answer()


# ---------------------------------------------------------------------------
# Объявление выдачи (решение от 25.11)
# ---------------------------------------------------------------------------


async def announce_plan(session: AsyncSession, issue) -> tuple[list, bool, bool]:
    """Сводки клиентов и есть ли среди них доставка / самовывоз."""
    orders = await order_service.issue_orders(session, issue, include_cancelled=False)
    summaries = order_service.summarize_by_customer(orders)
    has_delivery = any(s.delivery_type == DeliveryType.DELIVERY for s in summaries)
    has_pickup = any(s.delivery_type == DeliveryType.PICKUP for s in summaries)
    return summaries, has_delivery, has_pickup


@router.callback_query(IssueCb.filter(F.action == "announce"))
async def cb_announce(callback: CallbackQuery, callback_data: IssueCb, state: FSMContext, session: AsyncSession) -> None:
    """
    «📣 Объявить выдачу»: спросить текст(ы).

    Если есть клиенты и с доставкой, и с самовывозом — два текста по очереди.
    """
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    summaries, has_delivery, has_pickup = await announce_plan(session, issue)
    if not summaries:
        await callback.answer("В выпуске нет действующих заказов.", show_alert=True)
        return
    await state.update_data(issue_id=issue.id, delivery_text=None, pickup_text=None)
    if has_delivery:
        await state.set_state(IssueInput.delivery_text)
        await callback.message.answer(
            "Текст для клиентов <b>с доставкой</b>.\nНапример: <i>Всё готово, развожу во вторник после 17:00</i>\n\n"
            "Каждый получит его вместе со своим заказом и суммой к оплате.\n<i>/cancel — отменить</i>"
        )
    else:
        await ask_pickup_text(callback.message, state, session)
    await callback.answer()


async def ask_pickup_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    address = await settings_service.get_value(session, settings_service.PICKUP_ADDRESS)
    warning = "" if address else "\n\n⚠️ Адрес самовывоза не заполнен (⚙️ Настройки) — клиенты его не увидят."
    await state.set_state(IssueInput.pickup_text)
    await message.answer(
        "Текст для клиентов <b>с самовывозом</b>.\nНапример: <i>Забрать можно в среду с 12 до 18</i>\n"
        f"Адрес самовывоза бот добавит сам.{warning}\n<i>/cancel — отменить</i>"
    )


@router.message(IssueInput.delivery_text, F.text)
async def on_delivery_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    await state.update_data(delivery_text=message.text.strip())
    data = await state.get_data()
    issue = await issue_service.get_issue(session, data["issue_id"])
    _, _, has_pickup = await announce_plan(session, issue)
    if has_pickup:
        await ask_pickup_text(message, state, session)
    else:
        await confirm_announce(message, state, session)


@router.message(IssueInput.pickup_text, F.text)
async def on_pickup_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    await state.update_data(pickup_text=message.text.strip())
    await confirm_announce(message, state, session)


async def confirm_announce(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Показать, сколько клиентов получат объявление, и спросить подтверждение."""
    data = await state.get_data()
    issue = await issue_service.get_issue(session, data["issue_id"])
    summaries, _, _ = await announce_plan(session, issue)
    customers = len({s.customer_id for s in summaries})
    await message.answer(
        f"Отправить объявление клиентам «{texts.esc(issue.title)}»? Получат: {customers}.",
        reply_markup=keyboards.confirm(
            "📣 Да, отправить", IssueCb(action="announce_yes", issue_id=issue.id), IssueCb(action="view", issue_id=issue.id)
        ),
    )


@router.callback_query(IssueCb.filter(F.action == "announce_yes"))
async def cb_announce_yes(
    callback: CallbackQuery, callback_data: IssueCb, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    """Разослать объявление выдачи каждому клиенту выпуска."""
    data = await state.get_data()
    if data.get("issue_id") != callback_data.issue_id or not (data.get("delivery_text") or data.get("pickup_text")):
        await callback.answer("Сначала нажмите «📣 Объявить выдачу» и напишите текст.", show_alert=True)
        return
    await state.clear()
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    try:
        issue_service.announce(issue, data.get("delivery_text"), data.get("pickup_text"))
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)

    pickup_address = await settings_service.get_value(session, settings_service.PICKUP_ADDRESS)
    chef_url = await settings_service.get_chef_contact_url(session)
    kb = keyboards.url_button(texts.BTN_WRITE_CHEF, chef_url) if chef_url else None
    orders = await order_service.issue_orders(session, issue, include_cancelled=False)
    tg_ids = {o.customer_id: o.customer.tg_id for o in orders}
    texts_by_type = {DeliveryType.DELIVERY: issue.closing_text, DeliveryType.PICKUP: issue.pickup_text}
    # Одно сообщение заказчику со ВСЕМИ его заказами — себе и в подарок
    # (решение от 26.11). Сводки уже идут подряд по клиентам.
    by_customer: dict[int, list] = {}
    for summary in order_service.summarize_by_customer(orders):
        by_customer.setdefault(summary.customer_id, []).append(summary)
    failed: list[str] = []
    for customer_id, summaries in by_customer.items():
        message_text = texts.announcement_message(issue, summaries, texts_by_type, pickup_address)
        if not await publisher.send_safe(bot, tg_ids[customer_id], message_text, kb):
            own = summaries[0]
            failed.append(f"{texts.esc(own.orderer_name or own.name)} {texts.esc(own.phone)}")
    report = f"📣 Объявление отправлено: {len(by_customer) - len(failed)} из {len(by_customer)}."
    if failed:
        report += "\n\nНе доставлено (бот заблокирован) — свяжитесь вручную:\n" + "\n".join(failed)
    await callback.message.answer(report)
    # Напоминание: заказы вне лавки в эту рассылку не попали (решение от 26.11).
    stock = await issue_service.find_stock_issue(session)
    pending = await order_service.pending_stock_count(session, stock) if stock else 0
    if pending:
        await callback.message.answer(
            texts.stock_pending_reminder(pending),
            reply_markup=keyboards.setup_buttons(("📣 Написать им", IssueCb(action="stock_notify", issue_id=stock.id))),
        )
    await send_issue(callback.message, session, issue)
    await callback.answer()


@router.callback_query(IssueCb.filter(F.action.in_({"finish", "finish_yes"})))
async def cb_finish(callback: CallbackQuery, callback_data: IssueCb, session: AsyncSession) -> None:
    """Завершить выпуск (А9). Если есть неотмеченные заказы — спросить."""
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    orders = await order_service.issue_orders(session, issue)
    pending = [o for o in orders if o.status == OrderStatus.ACCEPTED]
    if callback_data.action == "finish" and pending:
        await callback.message.answer(
            f"Не отмечены доставленными заказов: {len(pending)}. Отметить все и завершить выпуск?",
            reply_markup=keyboards.confirm(
                "✅ Да, все доставлены", IssueCb(action="finish_yes", issue_id=issue.id), IssueCb(action="view", issue_id=issue.id)
            ),
        )
        await callback.answer()
        return
    try:
        issue_service.finish_issue(issue, orders, mark_delivered=True)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.answer("📦 Выпуск завершён и перенесён в архив.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Лишние порции (А10)
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action.in_({"leftovers", "leftovers_from"})))
async def cb_leftovers(callback: CallbackQuery, callback_data: IssueCb, state: FSMContext, session: AsyncSession) -> None:
    """
    Начать выбор лишних порций. Из уведомления об отмене (leftovers_from) —
    сразу с блюдами и количеством из отменённого заказа.
    """
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    qty: dict[str, int] = {}
    if callback_data.action == "leftovers_from":
        order = await order_service.get_order(session, callback_data.order_id)
        for item in order.items:
            if item.issue_item_id is not None:
                # Ключи — строки: данные FSM хранятся как JSON, а у JSON-словаря ключи всегда строки.
                qty[str(item.issue_item_id)] = qty.get(str(item.issue_item_id), 0) + item.quantity
    await state.set_state(Leftovers.choosing)
    await state.update_data(issue_id=issue.id, qty=qty)
    await callback.message.answer(
        "Сколько порций осталось? Выберите количество и опубликуйте.",
        reply_markup=keyboards.leftovers(issue.items, {int(k): v for k, v in qty.items()}),
    )
    await callback.answer()


@router.callback_query(Leftovers.choosing, LeftCb.filter())
async def cb_leftovers_edit(callback: CallbackQuery, callback_data: LeftCb, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    data = await state.get_data()
    issue = await issue_service.get_issue(session, data["issue_id"])
    qty: dict[str, int] = data["qty"]
    action = callback_data.action

    if action in ("plus", "minus"):
        key = str(callback_data.item_id)
        qty[key] = max(0, qty.get(key, 0) + (1 if action == "plus" else -1))
        await state.update_data(qty=qty)
        await callback.message.edit_reply_markup(reply_markup=keyboards.leftovers(issue.items, {int(k): v for k, v in qty.items()}))
    elif action == "cancel":
        await state.clear()
        await replace_message(callback, "Отменено.")
    elif action == "publish":
        lines = [(item.name, qty.get(str(item.id), 0)) for item in issue.items if qty.get(str(item.id), 0) > 0]
        if not lines:
            await callback.answer("Выберите хотя бы одну порцию.", show_alert=True)
            return
        chat_id = await settings_service.get_group_chat_id(session)
        if chat_id is None:
            await callback.answer("Группа не подключена.", show_alert=True)
            return
        # Кнопка ведёт на «Контакт для клиентов» из настроек — к шефу,
        # кто бы ни публиковал пост (раньше вела к публикующему — исправлено).
        chef_url = await settings_service.get_chef_contact_url(session)
        if chef_url is None:
            await callback.answer(
                "Сначала укажите контакт для клиентов: ⚙️ Настройки → ✏️ Контакт для клиентов.", show_alert=True
            )
            return
        kb = keyboards.url_button(texts.BTN_WRITE_CHEF, chef_url)
        try:
            await bot.send_message(chat_id, texts.leftovers_post(lines), reply_markup=kb)
        except TelegramAPIError as error:
            await callback.answer(f"Не удалось опубликовать: {error}", show_alert=True)
            return
        await state.clear()
        await replace_message(callback, "🍱 Пост о лишних порциях опубликован.")
    await callback.answer()
