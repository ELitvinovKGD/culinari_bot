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
from culinary_bot.bot import keyboards, publisher
from culinary_bot.bot.callbacks import IssueCb, ItemCb, LeftCb
from culinary_bot.bot.ui import replace_message
from culinary_bot.db.models import Issue, IssueItem, IssueStatus, OrderStatus
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
    closing_text = State()  # текст для клиентов при закрытии приёма


class Leftovers(StatesGroup):
    """Выбор лишних порций (количество хранится в данных FSM)."""

    choosing = State()


# ---------------------------------------------------------------------------
# Показ экранов (используются и из других модулей)
# ---------------------------------------------------------------------------


async def send_issue(message: Message, session: AsyncSession, issue: Issue) -> None:
    """Отправить экран выпуска с кнопками по его состоянию."""
    orders = await order_service.issue_orders(session, issue, include_cancelled=False)
    total = sum(o.total_cents for o in orders)
    await message.answer(texts.issue_for_admin(issue, len(orders), total), reply_markup=keyboards.issue(issue))


async def send_item(message: Message, session: AsyncSession, item: IssueItem) -> None:
    """Отправить позицию выпуска (как её увидит клиент) с кнопками правки."""
    can_remove = not await issue_service.item_has_orders(session, item)
    status = "" if item.is_active else "\n\n⛔ <i>Снято с продажи</i>"
    caption = texts.item_block(item) + status
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
    await message.answer("Черновик создан. Добавьте шапку и блюда, затем — предпросмотр и публикация.")
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
            bot, order.customer.tg_id, texts.price_changed(item, old, order), keyboards.my_order(order)
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
    content = export_service.build_issue_workbook(issue, orders)
    # BufferedInputFile — файл из памяти (байты) с именем, под которым его увидит шеф.
    file = BufferedInputFile(content, filename=f"vypusk_{issue.id}.xlsx")
    await callback.message.answer_document(file, caption=f"📊 {texts.esc(issue.title)}: производство и доставка")
    await callback.answer()


# ---------------------------------------------------------------------------
# Закрытие приёма и завершение выпуска
# ---------------------------------------------------------------------------


@router.callback_query(IssueCb.filter(F.action == "close"))
async def cb_close(callback: CallbackQuery, callback_data: IssueCb, state: FSMContext) -> None:
    """Попросить текст для клиентов."""
    await state.set_state(IssueInput.closing_text)
    await state.update_data(issue_id=callback_data.issue_id)
    await callback.message.answer(
        "Напишите сообщение для клиентов этого выпуска, например:\n"
        "«Всё готово, развожу во вторник после 17:00».\n\n"
        "Каждый клиент получит его вместе со своим заказом и итогом.\n<i>/cancel — отменить</i>"
    )
    await callback.answer()


@router.message(IssueInput.closing_text, F.text)
async def on_closing_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Текст получен — показать, сколько клиентов получат сообщение, и спросить подтверждение."""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    data = await state.get_data()
    await state.update_data(closing_text=message.text.strip())
    issue = await issue_service.get_issue(session, data["issue_id"])
    orders = await order_service.issue_orders(session, issue, include_cancelled=False)
    customers = len(order_service.summarize_by_customer(orders))
    await message.answer(
        f"Закрыть приём в «{texts.esc(issue.title)}»? Сообщение получат клиентов: {customers}.",
        reply_markup=keyboards.confirm(
            "🔒 Да, закрыть", IssueCb(action="close_yes", issue_id=issue.id), IssueCb(action="view", issue_id=issue.id)
        ),
    )


@router.callback_query(IssueCb.filter(F.action == "close_yes"))
async def cb_close_yes(callback: CallbackQuery, callback_data: IssueCb, state: FSMContext, session: AsyncSession, bot: Bot) -> None:
    """Закрыть приём и разослать клиентам сообщение о доставке (А7)."""
    data = await state.get_data()
    closing_text = data.get("closing_text")
    if not closing_text or data.get("issue_id") != callback_data.issue_id:
        await callback.answer("Сначала нажмите «🔒 Закрыть приём» и напишите текст.", show_alert=True)
        return
    await state.clear()
    issue = await load_issue(callback, session, callback_data.issue_id)
    if not issue:
        return
    try:
        await issue_service.close_issue(session, issue, closing_text)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)

    pickup_address = await settings_service.get_value(session, settings_service.PICKUP_ADDRESS)
    orders = await order_service.issue_orders(session, issue, include_cancelled=False)
    # tg_id клиентов: у заказа есть customer, у сводки — только customer_id.
    tg_ids = {o.customer_id: o.customer.tg_id for o in orders}
    failed: list[str] = []
    summaries = order_service.summarize_by_customer(orders)
    for summary in summaries:
        ok = await publisher.send_safe(bot, tg_ids[summary.customer_id], texts.closing_message(issue, summary, pickup_address))
        if not ok:
            failed.append(f"{texts.esc(summary.name)} {texts.esc(summary.phone)}")
    report = f"🔒 Приём закрыт. Сообщение отправлено: {len(summaries) - len(failed)} из {len(summaries)}."
    if failed:
        report += "\n\nНе доставлено (бот заблокирован) — свяжитесь вручную:\n" + "\n".join(failed)
    await callback.message.answer(report)
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
        # Кнопка «Написать шефу» ведёт в личку того, кто публикует.
        # Нет @username — кнопку не ставим, в тексте и так написано «пишите шефу».
        username = callback.from_user.username
        kb = keyboards.url_button("✉️ Написать шефу", f"https://t.me/{username}") if username else None
        try:
            await bot.send_message(chat_id, texts.leftovers_post(lines), reply_markup=kb)
        except TelegramAPIError as error:
            await callback.answer(f"Не удалось опубликовать: {error}", show_alert=True)
            return
        await state.clear()
        await replace_message(callback, "🍱 Пост о лишних порциях опубликован.")
    await callback.answer()
