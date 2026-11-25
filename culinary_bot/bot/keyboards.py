"""
Клавиатуры (кнопки) бота.

Два вида кнопок в Telegram:
- Reply-клавиатура — большие кнопки внизу экрана вместо обычной клавиатуры.
  Нажатие отправляет обычное текстовое сообщение ("🍽 Меню").
- Инлайн-кнопки — кнопки прямо под сообщением. Нажатие присылает боту
  callback (см. callbacks.py), а сообщение можно обновлять на месте.

InlineKeyboardBuilder — помощник aiogram: добавляем кнопки по одной,
а `adjust(2, 1)` раскладывает их по рядам (2 в первом ряду, 1 во втором...).
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from culinary_bot import texts
from culinary_bot.bot import paging
from culinary_bot.bot.callbacks import (
    AdmCb,
    AdminsCb,
    AOrderCb,
    CardCb,
    CheckoutCb,
    DeleteMeCb,
    DishCb,
    FieldCb,
    IssueCb,
    ItemCb,
    LeftCb,
    ListCb,
    MyOrderCb,
    PickCb,
    ProfileCb,
    SetCb,
    SetupCb,
    StockCb,
)
from culinary_bot.db.models import Admin, Dish, Issue, IssueItem, IssueStatus, Order, OrderStatus, Recipient
from culinary_bot.services.money import format_price

# ---------------------------------------------------------------------------
# Общее: стрелки страниц
# ---------------------------------------------------------------------------


def nav_row(page: int, pages: int, make_cb) -> list[InlineKeyboardButton]:
    """
    Ряд «← 2/5 →» для длинных списков (bot/paging.py).

    make_cb — функция «номер страницы → данные кнопки». Пустой список, если
    страница одна: тогда стрелок нет совсем.
    """
    if pages <= 1:
        return []
    return [
        InlineKeyboardButton(text="←", callback_data=make_cb(max(page - 1, 0)).pack()),
        InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=make_cb(page).pack()),
        InlineKeyboardButton(text="→", callback_data=make_cb(min(page + 1, pages - 1)).pack()),
    ]


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    """Нижняя клавиатура: «Меню», «Мои заказы» и для админов — «Админка»."""
    rows = [
        [KeyboardButton(text=texts.BTN_MENU), KeyboardButton(text=texts.BTN_STOCK)],
        [KeyboardButton(text=texts.BTN_MY_ORDERS), KeyboardButton(text=texts.BTN_MY_DATA)],
        [KeyboardButton(text=texts.BTN_WRITE_CHEF)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=texts.BTN_ADMIN)])
    # resize_keyboard — кнопки компактные, а не на пол-экрана.
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def card(index: int, total: int, item: IssueItem, qty: int, cart_text: str, stock_link: bool = False) -> InlineKeyboardMarkup:
    """
    Кнопки под карточкой блюда в карусели:

        [ − ] [ 2 ] [ + ]
        [ ← ] [1/5] [ → ]
        [ 🛒 Корзина: 3 шт. · 30 € → оформить ]
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="−", callback_data=CardCb(action="minus", index=index, item_id=item.id))
    kb.button(text=str(qty), callback_data=CardCb(action="noop", index=index))
    kb.button(text="+", callback_data=CardCb(action="plus", index=index, item_id=item.id))
    # Карусель закольцована: с последней карточки "→" ведёт на первую.
    kb.button(text="←", callback_data=CardCb(action="show", index=(index - 1) % total))
    kb.button(text=f"{index + 1}/{total}", callback_data=CardCb(action="noop", index=index))
    kb.button(text="→", callback_data=CardCb(action="show", index=(index + 1) % total))
    sizes = [3, 3]
    if item.composition:
        kb.button(text="📋 Состав", callback_data=CardCb(action="comp", index=index, item_id=item.id))
        sizes.append(1)
    if total >= paging.CAROUSEL_LIST_BUTTON_FROM:
        # Блюд много — листать карточки долго, даём перейти к списку.
        kb.button(text=f"📋 Весь список ({total})", callback_data=CheckoutCb(action="list"))
        sizes.append(1)
    kb.button(text=cart_text, callback_data=CheckoutCb(action="list"))
    sizes.append(1)
    if stock_link:
        kb.button(text="🧺 Ещё вне лавки →", callback_data=StockCb(action="open"))
        sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def hide_message(index: int) -> InlineKeyboardMarkup:
    """Кнопка «Скрыть» под длинным составом — удаляет это сообщение."""
    kb = InlineKeyboardBuilder()
    kb.button(text="← К выбору", callback_data=CardCb(action="hide", index=index))
    return kb.as_markup()


def list_view(
    items: list[IssueItem],
    qty: dict[int, int],
    total_text: str,
    in_order: dict[int, int] | None = None,
    stock_link: bool = False,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """
    Полный список блюд: в каждой строке название с ценой и −/количество/+.

    in_order — сколько каждого блюда уже в оформленном заказе (при дозаказе):
    «01 Суп · 10 € · в заказе 2», а −/+ меняют только добавку.
    Больше 12 блюд — страницами (bot/paging.py).
    """
    in_order = in_order or {}
    shown, page, pages = paging.page_of(items, page, paging.CLIENT_LIST_PAGE)
    kb = InlineKeyboardBuilder()
    for item in shown:
        # Название обрезаем, чтобы кнопка не была слишком длинной.
        name = item.name if len(item.name) <= 22 else item.name[:21] + "…"
        already = f" · в заказе {in_order[item.id]}" if in_order.get(item.id) else ""
        kb.row(
            InlineKeyboardButton(
                text=f"{item.position:02d} {name} · {format_price(item.price_cents)}{already}",
                callback_data=ListCb(action="card", item_id=item.id, page=page).pack(),
            )
        )
        kb.row(
            InlineKeyboardButton(text="−", callback_data=ListCb(action="minus", item_id=item.id, page=page).pack()),
            InlineKeyboardButton(text=str(qty.get(item.id, 0)), callback_data=ListCb(action="noop").pack()),
            InlineKeyboardButton(text="+", callback_data=ListCb(action="plus", item_id=item.id, page=page).pack()),
        )
    nav = nav_row(page, pages, lambda p: ListCb(action="page", page=p))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=f"Дальше → {total_text}", callback_data=ListCb(action="next").pack()))
    if stock_link:
        kb.row(InlineKeyboardButton(text="🧺 Ещё вне лавки →", callback_data=StockCb(action="open").pack()))
    return kb.as_markup()


def where_to_add(has_own: bool, gifts: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """«Куда добавить?» — к своему заказу, к заказу получателя или новый заказ получателю."""
    kb = InlineKeyboardBuilder()
    if has_own:
        kb.button(text="➕ К моему заказу", callback_data=CheckoutCb(action="pick_to", rid=0))
    else:
        kb.button(text="✅ Новый заказ себе", callback_data=CheckoutCb(action="confirm"))
    for rid, name in gifts:
        kb.button(text=f"➕ К заказу получателю: {name}", callback_data=CheckoutCb(action="pick_to", rid=rid))
    kb.button(text="🎁 Другому получателю", callback_data=CheckoutCb(action="gift"))
    kb.button(text="✏️ Изменить корзину", callback_data=CheckoutCb(action="list"))
    kb.adjust(1)
    return kb.as_markup()


def add_confirm(rid: int) -> InlineKeyboardMarkup:
    """После выбора заказа: итог по нему и «✅ Завершить заказ»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Завершить заказ", callback_data=CheckoutCb(action="add_to", rid=rid))
    kb.button(text="← Куда добавить", callback_data=ListCb(action="next"))
    kb.adjust(1)
    return kb.as_markup()


def summary(has_own: bool = False, gifts: list[tuple[int, str]] | None = None) -> InlineKeyboardMarkup:
    """
    Кнопки «Итого».

    Нет заказов в выпуске — «✅ Всё верно».
    Уже есть заказ себе — «✅ Завершить заказ» (добавить к своему заказу).
    Есть заказы в подарок — по кнопке «Добавить к заказу для Маши» на каждый
    (решение от 26.11: дозаказ спрашивает, в какой заказ добавить)
    и «🎁 Другому получателю» — отдельный новый заказ.
    """
    gifts = gifts or []
    kb = InlineKeyboardBuilder()
    if not has_own and not gifts:
        kb.button(text="✅ Всё верно", callback_data=CheckoutCb(action="confirm"))
    else:
        if has_own:
            kb.button(text="✅ Завершить заказ (добавить к моему)", callback_data=CheckoutCb(action="add_to", rid=0))
        else:
            kb.button(text="✅ Оформить себе", callback_data=CheckoutCb(action="confirm"))
        for rid, name in gifts:
            kb.button(text=f"🎁 Добавить к заказу: {name}", callback_data=CheckoutCb(action="add_to", rid=rid))
        kb.button(text="🎁 Другому получателю", callback_data=CheckoutCb(action="gift"))
    kb.button(text="✏️ Изменить", callback_data=CheckoutCb(action="list"))
    return kb.as_markup()


def single(text: str, action: str) -> InlineKeyboardMarkup:
    """Одна кнопка шага оформления (например, «Понятно» или «Пропустить»)."""
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=CheckoutCb(action=action))
    return kb.as_markup()


def saved_data(allow_gift: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, оформить", callback_data=CheckoutCb(action="data_ok"))
    kb.button(text="✏️ Изменить", callback_data=CheckoutCb(action="data_edit"))
    if allow_gift:
        kb.button(text="🎁 Другому получателю", callback_data=CheckoutCb(action="gift"))
    kb.adjust(2, 1)
    return kb.as_markup()


def recipients_choice(recipients: list[Recipient]) -> InlineKeyboardMarkup:
    """Кому заказ: получатели из записной книжки + «➕ Новый получатель»."""
    kb = InlineKeyboardBuilder()
    for r in recipients:
        kb.button(text=f"🎁 {r.short()}", callback_data=CheckoutCb(action="pick_rec", rid=r.id))
    kb.button(text="➕ Новый получатель", callback_data=CheckoutCb(action="new_rec"))
    kb.adjust(1)
    return kb.as_markup()


def payer_choice() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Я", callback_data=CheckoutCb(action="payer_me"))
    kb.button(text="🎁 Получатель", callback_data=CheckoutCb(action="payer_rec"))
    return kb.as_markup()


def my_data(recipients: list[Recipient]) -> InlineKeyboardMarkup:
    """Кнопки экрана «👤 Мои данные»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить мои данные", callback_data=ProfileCb(action="edit"))
    for r in recipients:
        kb.button(text=f"🗑 {r.short()}", callback_data=ProfileCb(action="del_rec", rid=r.id))
    kb.adjust(1)
    return kb.as_markup()


def keep_previous(value: str) -> InlineKeyboardMarkup:
    """Кнопка «Оставить: <прежнее значение>» при изменении данных."""
    shown = value if len(value) <= 30 else value[:29] + "…"
    return single(f"Оставить: {shown}", "keep")


def delivery_choice() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Доставка", callback_data=CheckoutCb(action="delivery"))
    kb.button(text="🏠 Самовывоз", callback_data=CheckoutCb(action="pickup"))
    return kb.as_markup()


def my_order(order: Order, *, editable: bool, issue_open: bool, chef_url: str | None) -> InlineKeyboardMarkup | None:
    """
    Кнопки под заказом клиента:
    ✏️ Изменить (первые минуты), ❌ Отменить, ➕ Дозаказать (пока лавка открыта),
    ✉️ Написать шефу.
    """
    kb = InlineKeyboardBuilder()
    if editable:
        kb.button(text="✏️ Изменить", callback_data=MyOrderCb(action="edit", order_id=order.id))
    if order.status == OrderStatus.ACCEPTED:
        kb.button(text="❌ Отменить", callback_data=MyOrderCb(action="cancel", order_id=order.id))
    if issue_open and order.status != OrderStatus.CANCELLED:
        kb.button(text="➕ Дозаказать", callback_data=MyOrderCb(action="more", order_id=order.id))
    if chef_url:
        kb.button(text=texts.BTN_WRITE_CHEF, url=chef_url)
    kb.adjust(2, 2)
    return kb.as_markup() if kb.buttons else None


def confirm_cancel(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, отменить", callback_data=MyOrderCb(action="cancel_yes", order_id=order_id))
    return kb.as_markup()


def delete_me() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=DeleteMeCb(action="yes"))
    kb.button(text="Нет", callback_data=DeleteMeCb(action="no"))
    return kb.as_markup()


def order_button(url: str) -> InlineKeyboardMarkup:
    """Кнопка-ссылка «Заказать» под постом в группе (ведёт в бота)."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Заказать", url=url)]])


def url_button(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------


def admin_home() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Выпуски", callback_data=AdmCb(section="issues"))
    kb.button(text="🧺 Блюда вне лавки", callback_data=AdmCb(section="stock"))
    kb.button(text="🍲 Каталог блюд", callback_data=AdmCb(section="catalog"))
    kb.button(text="⚙️ Настройки", callback_data=AdmCb(section="settings"))
    kb.button(text="👥 Админы", callback_data=AdmCb(section="admins"))
    kb.button(text="🔔 Мои уведомления", callback_data=AdmCb(section="notify"))
    kb.button(text="🧭 Мастер настройки", callback_data=SetupCb(action="start"))
    kb.adjust(2, 1, 2, 1, 1)
    return kb.as_markup()


def back(text: str, callback_data) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=callback_data)
    return kb.as_markup()


def dish(d: Dish) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for field, label in [
        ("name", "Название"),
        ("emoji", "Эмодзи"),
        ("description", "Описание"),
        ("portion", "Порция"),
        ("composition", "Состав"),
        ("price_cents", "Цена"),
        ("photo_file_id", "Фото"),
    ]:
        kb.button(text=f"✏️ {label}", callback_data=FieldCb(entity="dish", obj_id=d.id, field=field))
    if d.is_archived:
        kb.button(text="↩️ Вернуть из архива", callback_data=DishCb(action="unarchive", dish_id=d.id))
    else:
        kb.button(text="🗄 В архив", callback_data=DishCb(action="archive", dish_id=d.id))
    kb.button(text="← Каталог", callback_data=AdmCb(section="catalog"))
    kb.adjust(2, 2, 2, 1, 1, 1)
    return kb.as_markup()


def issues_list(items: list[Issue], archive: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if not archive:
        kb.button(text="➕ Новый выпуск", callback_data=IssueCb(action="new"))
    for issue in items:
        label = texts.ISSUE_STATUS_LABEL[issue.status].split()[0]
        kb.button(text=f"{label} {issue.title}", callback_data=IssueCb(action="view", issue_id=issue.id))
    if archive:
        kb.button(text="← Выпуски", callback_data=AdmCb(section="issues"))
    else:
        kb.button(text="📦 Архив выпусков", callback_data=AdmCb(section="archive"))
        kb.button(text="← Админка", callback_data=AdmCb(section="home"))
    kb.adjust(1)
    return kb.as_markup()


def issue(i: Issue) -> InlineKeyboardMarkup:
    """
    Кнопки выпуска — набор зависит от состояния (таблица в docs/product.md).
    """
    kb = InlineKeyboardBuilder()
    sizes: list[int] = []

    def add(text: str, cb) -> None:
        kb.button(text=text, callback_data=cb)

    iid = i.id
    if i.status in (IssueStatus.DRAFT, IssueStatus.OPEN):
        add("✏️ Название", FieldCb(entity="issue", obj_id=iid, field="title"))
        add("✏️ Шапка", FieldCb(entity="issue", obj_id=iid, field="header"))
        add("✏️ Текст после списка", FieldCb(entity="issue", obj_id=iid, field="footer"))
        add("➕ Блюдо", IssueCb(action="add", issue_id=iid))
        sizes += [2, 2]
        for item in i.items:
            off = "" if item.is_active else " ⛔"
            add(f"{item.position:02d} {item.name}{off}", ItemCb(action="view", item_id=item.id))
            sizes.append(1)
        add("👁 Предпросмотр", IssueCb(action="preview", issue_id=iid))
        sizes.append(1)
    if i.status == IssueStatus.DRAFT:
        add("🚀 Опубликовать", IssueCb(action="publish", issue_id=iid))
        add("🗑 Удалить черновик", IssueCb(action="delete", issue_id=iid))
        sizes += [1, 1]
    if i.status == IssueStatus.OPEN:
        add("🔄 Обновить пост в группе", IssueCb(action="update_post", issue_id=iid))
        sizes.append(1)
    if i.status != IssueStatus.DRAFT:
        add("📋 Заказы", IssueCb(action="orders", issue_id=iid))
        add("📊 Excel", IssueCb(action="excel", issue_id=iid))
        sizes.append(2)
    if i.status in (IssueStatus.OPEN, IssueStatus.CLOSED):
        add("🍱 Лишние порции", IssueCb(action="leftovers", issue_id=iid))
        sizes.append(1)
    if i.status == IssueStatus.OPEN:
        add("🔒 Закрыть приём", IssueCb(action="close", issue_id=iid))
        sizes.append(1)
    if i.status == IssueStatus.CLOSED:
        add("📣 Объявить выдачу", IssueCb(action="announce", issue_id=iid))
        add("✅ Выпуск развезён", IssueCb(action="finish", issue_id=iid))
        sizes += [1, 1]
    add("← Выпуски", AdmCb(section="issues"))
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def item(it: IssueItem, can_remove: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for field, label in [
        ("price_cents", "Цена"),
        ("portion", "Порция"),
        ("description", "Описание"),
        ("composition", "Состав"),
        ("name", "Название"),
        ("emoji", "Эмодзи"),
        ("photo_file_id", "Фото"),
    ]:
        kb.button(text=f"✏️ {label}", callback_data=FieldCb(entity="item", obj_id=it.id, field=field))
    if it.is_stock:
        kb.button(text=f"📦 Остаток: {it.stock_left} — изменить", callback_data=ItemCb(action="stock", item_id=it.id))
    kb.button(text="⬆️ Выше", callback_data=ItemCb(action="up", item_id=it.id))
    kb.button(text="⬇️ Ниже", callback_data=ItemCb(action="down", item_id=it.id))
    if it.is_active:
        kb.button(text="⛔ Снять с продажи", callback_data=ItemCb(action="off", item_id=it.id))
    else:
        kb.button(text="✅ Вернуть в продажу", callback_data=ItemCb(action="on", item_id=it.id))
    sizes = [2, 2, 2, 1] + ([1] if it.is_stock else []) + [2, 1]
    if can_remove:
        kb.button(text="🗑 Убрать из выпуска", callback_data=ItemCb(action="remove", item_id=it.id))
        sizes.append(1)
    kb.button(text="← Выпуск", callback_data=IssueCb(action="view", issue_id=it.issue_id))
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def price_apply(item_id: int, cents: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, ко всем заказам", callback_data=ItemCb(action="price_all", item_id=item_id, value=cents))
    kb.button(text="Нет, только для новых", callback_data=ItemCb(action="price_new", item_id=item_id, value=cents))
    kb.adjust(1)
    return kb.as_markup()


def confirm(text: str, cb, back_cb) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=cb)
    kb.button(text="Отмена", callback_data=back_cb)
    kb.adjust(1)
    return kb.as_markup()


def orders_list(issue_id: int, orders: list[Order], page: int = 0, show_all: bool = False) -> InlineKeyboardMarkup:
    """
    Заказы выпуска у шефа: по 15 на странице; по умолчанию только неотданные,
    «Показать все» — с доставленными и отменёнными (решение от 26.11).
    """
    shown, page, pages = paging.page_of(orders, page, paging.ORDERS_PAGE)
    kb = InlineKeyboardBuilder()
    for o in shown:
        icon = texts.order_status_for_admin(o).split()[0]
        kb.row(
            InlineKeyboardButton(
                text=f"{icon} №{o.id} {o.name} · {format_price(o.total_cents)}",
                callback_data=AOrderCb(action="view", order_id=o.id).pack(),
            )
        )
    flag = 1 if show_all else 0
    nav = nav_row(page, pages, lambda p: IssueCb(action="orders", issue_id=issue_id, page=p, show_all=flag))
    if nav:
        kb.row(*nav)
    toggle = "Только неотданные" if show_all else "Показать все"
    kb.row(
        InlineKeyboardButton(
            text=toggle, callback_data=IssueCb(action="orders", issue_id=issue_id, show_all=1 - flag).pack()
        )
    )
    kb.row(InlineKeyboardButton(text="← Выпуск", callback_data=IssueCb(action="view", issue_id=issue_id).pack()))
    return kb.as_markup()


def admin_order(o: Order) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if o.username:
        # Ссылка на личку клиента — шеф пишет сам («готово, завтра привезу»).
        kb.button(text="✉️ Написать клиенту", url=f"https://t.me/{o.username}")
    if o.status == OrderStatus.ACCEPTED:
        kb.button(text="🚚 Доставлен", callback_data=AOrderCb(action="delivered", order_id=o.id))
        kb.button(text="❌ Отменить", callback_data=AOrderCb(action="cancel", order_id=o.id))
    if o.status != OrderStatus.CANCELLED:
        kb.button(text="➕ Позиция от шефа", callback_data=AOrderCb(action="extra", order_id=o.id))
    for it in o.items:
        if it.is_extra:
            kb.button(text=f"🗑 {it.name} × {it.quantity}", callback_data=AOrderCb(action="del_extra", order_id=o.id, extra_id=it.id))
    kb.button(text="← Заказы", callback_data=IssueCb(action="orders", issue_id=o.issue_id))
    kb.adjust(1)
    return kb.as_markup()


def cancel_notification(order_id: int, issue_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🍱 Предложить эти порции в группе", callback_data=IssueCb(action="leftovers_from", issue_id=issue_id, order_id=order_id))
    return kb.as_markup()


def new_order_notification(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Открыть заказ", callback_data=AOrderCb(action="view", order_id=order_id))
    return kb.as_markup()


def leftovers(items: list[IssueItem], qty: dict[int, int], page: int = 0) -> InlineKeyboardMarkup:
    shown, page, pages = paging.page_of(items, page, paging.LEFTOVERS_PAGE)
    kb = InlineKeyboardBuilder()
    for it in shown:
        kb.row(InlineKeyboardButton(text=f"{it.position:02d} {it.name}", callback_data=LeftCb(action="noop").pack()))
        kb.row(
            InlineKeyboardButton(text="−", callback_data=LeftCb(action="minus", item_id=it.id, page=page).pack()),
            InlineKeyboardButton(text=str(qty.get(it.id, 0)), callback_data=LeftCb(action="noop").pack()),
            InlineKeyboardButton(text="+", callback_data=LeftCb(action="plus", item_id=it.id, page=page).pack()),
        )
    nav = nav_row(page, pages, lambda p: LeftCb(action="page", page=p))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="📣 Опубликовать в группе", callback_data=LeftCb(action="publish").pack()))
    kb.row(InlineKeyboardButton(text="Отмена", callback_data=LeftCb(action="cancel").pack()))
    return kb.as_markup()


def settings(pickup_enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Контакт для клиентов", callback_data=FieldCb(entity="setting", field="chef_contact"))
    kb.button(text="🔄 Сменить группу", callback_data=SetupCb(action="change_group"))
    kb.button(text=f"Самовывоз: {'включён ✅' if pickup_enabled else 'выключен ⛔'}", callback_data=SetCb(action="pickup_toggle"))
    kb.button(text="✏️ Адрес самовывоза", callback_data=FieldCb(entity="setting", field="pickup_address"))
    kb.button(text="✏️ Шапка поста о заказе", callback_data=FieldCb(entity="setting", field="order_post_header"))
    kb.button(text="✏️ Концовка поста о заказе", callback_data=FieldCb(entity="setting", field="order_post_footer"))
    kb.button(text="↩️ Стандартные тексты поста", callback_data=SetCb(action="reset_post"))
    kb.button(text="← Админка", callback_data=AdmCb(section="home"))
    kb.adjust(1)
    return kb.as_markup()


def admins_list(admins: list[Admin]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Пригласить админа", callback_data=AdminsCb(action="invite"))
    for a in admins:
        if not a.is_owner:
            kb.button(text=f"🗑 Удалить {a.name or a.tg_id}", callback_data=AdminsCb(action="remove", admin_id=a.id))
    kb.button(text="← Админка", callback_data=AdmCb(section="home"))
    kb.adjust(1)
    return kb.as_markup()


def notify(admin: Admin) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Новые заказы: {'вкл ✅' if admin.notify_orders else 'выкл ⛔'}", callback_data=AdminsCb(action="notify_orders"))
    kb.button(text=f"Отмены: {'вкл ✅' if admin.notify_cancels else 'выкл ⛔'}", callback_data=AdminsCb(action="notify_cancels"))
    kb.button(text="← Админка", callback_data=AdmCb(section="home"))
    kb.adjust(1)
    return kb.as_markup()


def skip_field(entity: str, obj_id: int, field: str) -> InlineKeyboardMarkup:
    """Кнопка «Очистить поле» при правке необязательных полей."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🧹 Очистить", callback_data=FieldCb(entity=entity, obj_id=obj_id, field=field, clear=True))
    return kb.as_markup()


def preview_actions(issue: Issue) -> InlineKeyboardMarkup:
    """Кнопки под предпросмотром — чтобы не листать вверх к экрану выпуска."""
    kb = InlineKeyboardBuilder()
    if issue.status == IssueStatus.DRAFT:
        kb.button(text="🚀 Опубликовать", callback_data=IssueCb(action="publish", issue_id=issue.id))
    kb.button(text="✏️ Вернуться к выпуску", callback_data=IssueCb(action="view", issue_id=issue.id))
    kb.adjust(1)
    return kb.as_markup()


def skip_header(issue_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=IssueCb(action="view", issue_id=issue_id))
    return kb.as_markup()


def setup_buttons(*buttons: tuple[str, object]) -> InlineKeyboardMarkup:
    """Кнопки мастера настройки: список (текст, callback_data), по одной в ряд."""
    kb = InlineKeyboardBuilder()
    for text, cb in buttons:
        kb.button(text=text, callback_data=cb)
    kb.adjust(1)
    return kb.as_markup()


def setup_start() -> InlineKeyboardMarkup:
    return setup_buttons(("▶️ Начать настройку", SetupCb(action="step", value="group")), ("Позже", SetupCb(action="later")))



# ---------------------------------------------------------------------------
# «🧺 В наличии» (клиент)
# ---------------------------------------------------------------------------


def stock_list(
    items: list[IssueItem], qty: dict[int, int], total_text: str, lavka_link: bool = False, page: int = 0
) -> InlineKeyboardMarkup:
    """Список блюд вне лавки: название · цена · остаток, и −/количество/+ (страницами)."""
    shown, page, pages = paging.page_of(items, page, paging.STOCK_PAGE)
    kb = InlineKeyboardBuilder()
    for item in shown:
        name = item.name if len(item.name) <= 20 else item.name[:19] + "…"
        kb.row(
            InlineKeyboardButton(
                text=f"{name} · {format_price(item.price_cents)} · ост. {item.stock_left}",
                callback_data=StockCb(action="card", item_id=item.id, page=page).pack(),
            )
        )
        kb.row(
            InlineKeyboardButton(text="−", callback_data=StockCb(action="minus", item_id=item.id, page=page).pack()),
            InlineKeyboardButton(text=str(qty.get(item.id, 0)), callback_data=StockCb(action="noop").pack()),
            InlineKeyboardButton(text="+", callback_data=StockCb(action="plus", item_id=item.id, page=page).pack()),
        )
    nav = nav_row(page, pages, lambda p: StockCb(action="list", page=p))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text=f"🛒 Оформить → {total_text}", callback_data=StockCb(action="checkout").pack()))
    if lavka_link:
        kb.row(InlineKeyboardButton(text="🍽 Меню лавки →", callback_data=CardCb(action="show", index=0).pack()))
    return kb.as_markup()


def stock_card_back(page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="← К списку", callback_data=StockCb(action="list", page=page))
    return kb.as_markup()


def stock_where(orders: list[Order]) -> InlineKeyboardMarkup:
    """
    «Куда добавить?» для блюд вне лавки (решение от 26.11): к своему заказу
    в лавке, к заказу получателя или «🎁 Другому получателю». Всё едет с лавкой.
    Если заказы в разных лавках (одна закрыта, другая открыта) — в подписи лавка.
    """
    several = len({o.issue_id for o in orders}) > 1
    kb = InlineKeyboardBuilder()
    for o in orders:
        label = f"➕ К заказу получателю: {o.name}" if o.is_gift else "➕ К моему заказу"
        if several:
            label += f" («{o.issue.title}»)"
        kb.button(text=label[:60], callback_data=StockCb(action="pick", order_id=o.id))
    kb.button(text="🎁 Другому получателю", callback_data=StockCb(action="gift"))
    kb.button(text="← Изменить корзину", callback_data=StockCb(action="list"))
    kb.adjust(1)
    return kb.as_markup()


def stock_add_confirm(order_id: int) -> InlineKeyboardMarkup:
    """После выбора заказа: итог по нему и «✅ Завершить заказ»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Завершить заказ", callback_data=StockCb(action="to", order_id=order_id))
    kb.button(text="← Куда добавить", callback_data=StockCb(action="checkout"))
    kb.adjust(1)
    return kb.as_markup()


def stock_open() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧺 Посмотреть", callback_data=StockCb(action="open"))
    return kb.as_markup()



def stock_admin(issue: Issue) -> InlineKeyboardMarkup:
    """Кнопки экрана «🧺 Блюда вне лавки» в админке."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Шапка поста", callback_data=FieldCb(entity="issue", obj_id=issue.id, field="header"))
    kb.button(text="➕ Блюдо", callback_data=IssueCb(action="stock_add", issue_id=issue.id))
    for item in issue.items:
        off = "" if item.is_active else " ⛔"
        kb.button(text=f"{item.name} · ост. {item.stock_left}{off}", callback_data=ItemCb(action="view", item_id=item.id))
    kb.button(text="👁 Предпросмотр поста", callback_data=IssueCb(action="preview", issue_id=issue.id))
    kb.button(text="📣 Опубликовать пост", callback_data=IssueCb(action="stock_post", issue_id=issue.id))
    kb.button(text="📋 Заказы", callback_data=IssueCb(action="orders", issue_id=issue.id))
    kb.button(text="📊 Excel", callback_data=IssueCb(action="excel", issue_id=issue.id))
    kb.button(text="📣 Сообщить всем с заказами", callback_data=IssueCb(action="stock_notify", issue_id=issue.id))
    kb.button(text="✅ Все заказы отданы", callback_data=IssueCb(action="stock_done", issue_id=issue.id))
    kb.button(text="← Админка", callback_data=AdmCb(section="home"))
    kb.adjust(2, *([1] * len(issue.items)), 2, 2, 1, 1, 1)
    return kb.as_markup()


def dish_picker(
    mode: str,
    dishes: list[Dish],
    *,
    issue_id: int = 0,
    page: int = 0,
    recent_ids: set[int] | None = None,
    reasons: dict[int, str] | None = None,
    found: bool = False,
    total_in_catalog: int = 0,
) -> InlineKeyboardMarkup:
    """
    Один и тот же список блюд для каталога, архива, добавления в лавку и «вне лавки».

    mode: cat | arch | lavka | stock — от него зависит, что делает нажатие на блюдо
    и какие кнопки внизу. Недавние помечены 🕘, найденные не по названию —
    подсказкой «состав: …». Кнопка «🔍 Найти» — только если список длинный.
    """
    recent_ids = recent_ids or set()
    reasons = reasons or {}
    shown, page, pages = paging.page_of(dishes, page, paging.CATALOG_PAGE)
    kb = InlineKeyboardBuilder()
    for d in shown:
        mark = "🕘" if d.id in recent_ids and not found else (d.emoji or "•")
        text = f"{mark} {d.name} · {format_price(d.price_cents)}"
        if d.id in reasons:
            text += f" · {reasons[d.id]}"
        if mode == "lavka":
            cb = IssueCb(action="add_dish", issue_id=issue_id, dish_id=d.id)
        elif mode == "stock":
            cb = IssueCb(action="stock_dish", issue_id=issue_id, dish_id=d.id)
        else:
            cb = DishCb(action="view", dish_id=d.id)
        kb.row(InlineKeyboardButton(text=text[:64], callback_data=cb.pack()))

    found_flag = 1 if found else 0
    nav = nav_row(page, pages, lambda p: PickCb(mode=mode, issue_id=issue_id, page=p, found=found_flag))
    if nav:
        kb.row(*nav)
    if found:
        kb.row(InlineKeyboardButton(text="✖ Сбросить поиск", callback_data=PickCb(mode=mode, action="reset", issue_id=issue_id).pack()))
    elif total_in_catalog > paging.CATALOG_PAGE:
        kb.row(InlineKeyboardButton(text="🔍 Найти", callback_data=PickCb(mode=mode, action="search", issue_id=issue_id).pack()))

    if mode == "cat":
        kb.row(InlineKeyboardButton(text="➕ Новое блюдо", callback_data=DishCb(action="new").pack()))
        kb.row(
            InlineKeyboardButton(text="🗄 Архив блюд", callback_data=PickCb(mode="arch").pack()),
            InlineKeyboardButton(text="← Админка", callback_data=AdmCb(section="home").pack()),
        )
    elif mode == "arch":
        kb.row(InlineKeyboardButton(text="← Каталог", callback_data=PickCb(mode="cat").pack()))
    elif mode == "lavka":
        kb.row(InlineKeyboardButton(text="➕ Новое блюдо", callback_data=DishCb(action="new", issue_id=issue_id).pack()))
        kb.row(InlineKeyboardButton(text="← Выпуск", callback_data=IssueCb(action="view", issue_id=issue_id).pack()))
    elif mode == "stock":
        kb.row(InlineKeyboardButton(text="➕ Новое блюдо в каталог", callback_data=DishCb(action="new").pack()))
        kb.row(InlineKeyboardButton(text="← Назад", callback_data=AdmCb(section="stock").pack()))
    return kb.as_markup()
