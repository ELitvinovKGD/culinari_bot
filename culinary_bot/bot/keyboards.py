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
    SetCb,
)
from culinary_bot.db.models import Admin, Dish, Issue, IssueItem, IssueStatus, Order, OrderStatus
from culinary_bot.services.money import format_price

# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    """Нижняя клавиатура: «Меню», «Мои заказы» и для админов — «Админка»."""
    rows = [[KeyboardButton(text=texts.BTN_MENU), KeyboardButton(text=texts.BTN_MY_ORDERS)]]
    if is_admin:
        rows.append([KeyboardButton(text=texts.BTN_ADMIN)])
    # resize_keyboard — кнопки компактные, а не на пол-экрана.
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def card(index: int, total: int, item: IssueItem, qty: int, cart_text: str) -> InlineKeyboardMarkup:
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
    kb.button(text=cart_text, callback_data=CheckoutCb(action="list"))
    kb.adjust(3, 3, 1)
    return kb.as_markup()


def list_view(items: list[IssueItem], qty: dict[int, int], total_text: str) -> InlineKeyboardMarkup:
    """
    Полный список блюд: в каждой строке название с ценой и −/количество/+.
    """
    kb = InlineKeyboardBuilder()
    for item in items:
        # Название обрезаем, чтобы кнопка не была слишком длинной.
        name = item.name if len(item.name) <= 22 else item.name[:21] + "…"
        kb.row(
            InlineKeyboardButton(
                text=f"{item.position:02d} {name} · {format_price(item.price_cents)}",
                callback_data=ListCb(action="card", item_id=item.id).pack(),
            )
        )
        kb.row(
            InlineKeyboardButton(text="−", callback_data=ListCb(action="minus", item_id=item.id).pack()),
            InlineKeyboardButton(text=str(qty.get(item.id, 0)), callback_data=ListCb(action="noop").pack()),
            InlineKeyboardButton(text="+", callback_data=ListCb(action="plus", item_id=item.id).pack()),
        )
    kb.row(InlineKeyboardButton(text=f"Дальше → {total_text}", callback_data=ListCb(action="next").pack()))
    return kb.as_markup()


def summary() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Всё верно", callback_data=CheckoutCb(action="confirm"))
    kb.button(text="✏️ Изменить", callback_data=CheckoutCb(action="list"))
    return kb.as_markup()


def single(text: str, action: str) -> InlineKeyboardMarkup:
    """Одна кнопка шага оформления (например, «Понятно» или «Пропустить»)."""
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=CheckoutCb(action=action))
    return kb.as_markup()


def saved_data() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, оформить", callback_data=CheckoutCb(action="data_ok"))
    kb.button(text="✏️ Изменить", callback_data=CheckoutCb(action="data_edit"))
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


def my_order(order: Order) -> InlineKeyboardMarkup | None:
    """Кнопка «Отменить» — только у принятых заказов."""
    if order.status != OrderStatus.ACCEPTED:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить заказ", callback_data=MyOrderCb(action="cancel", order_id=order.id))
    return kb.as_markup()


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
    kb.button(text="🍲 Каталог блюд", callback_data=AdmCb(section="catalog"))
    kb.button(text="⚙️ Настройки", callback_data=AdmCb(section="settings"))
    kb.button(text="👥 Админы", callback_data=AdmCb(section="admins"))
    kb.button(text="🔔 Мои уведомления", callback_data=AdmCb(section="notify"))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def back(text: str, callback_data) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=text, callback_data=callback_data)
    return kb.as_markup()


def catalog(dishes: list[Dish], archived: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if not archived:
        kb.button(text="➕ Новое блюдо", callback_data=DishCb(action="new"))
    for dish in dishes:
        kb.button(text=f"{dish.emoji or '•'} {dish.name} · {format_price(dish.price_cents)}", callback_data=DishCb(action="view", dish_id=dish.id))
    if archived:
        kb.button(text="← Каталог", callback_data=AdmCb(section="catalog"))
    else:
        kb.button(text="🗄 Архив блюд", callback_data=AdmCb(section="archived"))
        kb.button(text="← Админка", callback_data=AdmCb(section="home"))
    kb.adjust(1)
    return kb.as_markup()


def dish(d: Dish) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for field, label in [
        ("name", "Название"),
        ("emoji", "Эмодзи"),
        ("description", "Описание"),
        ("portion", "Порция"),
        ("price_cents", "Цена"),
        ("photo_file_id", "Фото"),
    ]:
        kb.button(text=f"✏️ {label}", callback_data=FieldCb(entity="dish", obj_id=d.id, field=field))
    if d.is_archived:
        kb.button(text="↩️ Вернуть из архива", callback_data=DishCb(action="unarchive", dish_id=d.id))
    else:
        kb.button(text="🗄 В архив", callback_data=DishCb(action="archive", dish_id=d.id))
    kb.button(text="← Каталог", callback_data=AdmCb(section="catalog"))
    kb.adjust(2, 2, 2, 1, 1)
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
        add("✅ Выпуск развезён", IssueCb(action="finish", issue_id=iid))
        sizes.append(1)
    add("← Выпуски", AdmCb(section="issues"))
    sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


def add_dish_choice(issue_id: int, dishes: list[Dish]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новое блюдо", callback_data=DishCb(action="new", issue_id=issue_id))
    for d in dishes:
        kb.button(
            text=f"{d.emoji or '•'} {d.name} · {format_price(d.price_cents)}",
            callback_data=IssueCb(action="add_dish", issue_id=issue_id, dish_id=d.id),
        )
    kb.button(text="← Выпуск", callback_data=IssueCb(action="view", issue_id=issue_id))
    kb.adjust(1)
    return kb.as_markup()


def item(it: IssueItem, can_remove: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for field, label in [
        ("price_cents", "Цена"),
        ("portion", "Порция"),
        ("description", "Описание"),
        ("name", "Название"),
        ("emoji", "Эмодзи"),
        ("photo_file_id", "Фото"),
    ]:
        kb.button(text=f"✏️ {label}", callback_data=FieldCb(entity="item", obj_id=it.id, field=field))
    kb.button(text="⬆️ Выше", callback_data=ItemCb(action="up", item_id=it.id))
    kb.button(text="⬇️ Ниже", callback_data=ItemCb(action="down", item_id=it.id))
    if it.is_active:
        kb.button(text="⛔ Снять с продажи", callback_data=ItemCb(action="off", item_id=it.id))
    else:
        kb.button(text="✅ Вернуть в продажу", callback_data=ItemCb(action="on", item_id=it.id))
    sizes = [2, 2, 2, 2, 1]
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


def orders_list(issue_id: int, orders: list[Order]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for o in orders:
        icon = texts.ORDER_STATUS_LABEL[o.status].split()[0]
        kb.button(text=f"{icon} №{o.id} {o.name} · {format_price(o.total_cents)}", callback_data=AOrderCb(action="view", order_id=o.id))
    kb.button(text="← Выпуск", callback_data=IssueCb(action="view", issue_id=issue_id))
    kb.adjust(1)
    return kb.as_markup()


def admin_order(o: Order) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
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


def leftovers(items: list[IssueItem], qty: dict[int, int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.row(InlineKeyboardButton(text=f"{it.position:02d} {it.name}", callback_data=LeftCb(action="noop").pack()))
        kb.row(
            InlineKeyboardButton(text="−", callback_data=LeftCb(action="minus", item_id=it.id).pack()),
            InlineKeyboardButton(text=str(qty.get(it.id, 0)), callback_data=LeftCb(action="noop").pack()),
            InlineKeyboardButton(text="+", callback_data=LeftCb(action="plus", item_id=it.id).pack()),
        )
    kb.row(InlineKeyboardButton(text="📣 Опубликовать в группе", callback_data=LeftCb(action="publish").pack()))
    kb.row(InlineKeyboardButton(text="Отмена", callback_data=LeftCb(action="cancel").pack()))
    return kb.as_markup()


def settings(pickup_enabled: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
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
