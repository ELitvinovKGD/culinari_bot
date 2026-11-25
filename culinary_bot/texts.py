"""
Все тексты бота в одном месте.

Хочешь поменять формулировку — меняй здесь, логику трогать не нужно.

Тексты отправляются в режиме HTML (parse_mode="HTML" в __main__.py):
<b>жирный</b>, <i>курсив</i>, <a href="...">ссылка</a>.

ВАЖНО (безопасность): всё, что ввёл человек (имя, адрес, комментарий,
название блюда), перед вставкой в текст пропускаем через esc().
Иначе символ "<" в комментарии клиента сломал бы сообщение, а специально
подобранный текст мог бы, например, вставить чужую ссылку.
"""

import html
import re

from culinary_bot.db.models import CartItem, DeliveryType, Issue, IssueItem, IssueStatus, Order, OrderStatus
from culinary_bot.services.money import format_price
from culinary_bot.services.orders import CustomerSummary


def esc(text: str | None) -> str:
    """Экранировать пользовательский текст для HTML: "<" → "&lt;" и т.п."""
    return html.escape(text or "")


def visible_length(html_text: str) -> int:
    """
    Длина текста так, как её считает Telegram: без HTML-тегов.

    Нужна для правила «подпись к фото — максимум 1024 символа».
    """
    without_tags = re.sub(r"<[^>]+>", "", html_text)
    return len(html.unescape(without_tags))


# ---------------------------------------------------------------------------
# Общие фразы
# ---------------------------------------------------------------------------

BTN_MENU = "🍽 Меню"
BTN_MY_ORDERS = "🧾 Мои заказы"
BTN_ADMIN = "⚙️ Админка"

WELCOME = "Привет! Здесь можно заказать блюда из «Тайной лавки шефа».\nНажмите «🍽 Меню», чтобы посмотреть текущий выпуск."
NO_OPEN_ISSUE = "Шеф ещё не открыл лавку. Новый выпуск появится в группе."
ISSUE_CLOSED = "Приём заказов уже закрыт."
CART_EMPTY = "Корзина пуста — добавьте хотя бы одно блюдо."
SOMETHING_WRONG = "Что-то пошло не так. Попробуйте ещё раз или напишите шефу."
NOT_ADMIN = "Эта команда только для администраторов."

PRIVACY_NOTICE = (
    "Перед первым заказом коротко о данных:\n\n"
    "Имя, телефон и адрес нужны только для доставки. Их видит только шеф.\n"
    "Данные сохранятся, чтобы в следующий раз не вводить их заново.\n"
    "Удалить их можно командой /delete_me."
)

ASK_NAME = "Как вас зовут?"
ASK_PHONE = "Ваш телефон для связи?\nНапример: +995 555 123 456"
ASK_DELIVERY = "Доставка или самовывоз?"
ASK_ADDRESS = "Адрес доставки?\nУлица, дом, подъезд, этаж — всё, что поможет найти."
ASK_COMMENT = "Комментарий к заказу, если нужен.\nНапример: позвонить за час, пожелание по времени."
DELIVERY_LABEL = {DeliveryType.DELIVERY: "доставка", DeliveryType.PICKUP: "самовывоз"}

DELETE_ME_CONFIRM = (
    "Удалить ваши сохранённые данные (имя, телефон, адрес) и корзину?\n"
    "Уже оформленные заказы останутся у шефа, чтобы он смог их доставить."
)
DELETE_ME_DONE = "Готово, данные удалены."

ORDER_STATUS_LABEL = {
    OrderStatus.ACCEPTED: "✅ принят",
    OrderStatus.DELIVERED: "🚚 доставлен",
    OrderStatus.CANCELLED: "❌ отменён",
}
ISSUE_STATUS_LABEL = {
    IssueStatus.DRAFT: "📝 черновик",
    IssueStatus.OPEN: "🟢 приём открыт",
    IssueStatus.CLOSED: "🔒 приём закрыт",
    IssueStatus.FINISHED: "📦 завершён",
}


# ---------------------------------------------------------------------------
# Блюда и пост выпуска
# ---------------------------------------------------------------------------


def item_number(item: IssueItem) -> str:
    """Номер позиции в стиле шефа: 1 → "01"."""
    return f"{item.position:02d}"


def item_block(item: IssueItem) -> str:
    """
    Одно блюдо в посте выпуска, как в постах шефа:

        🍲 01 / Литовский гороховый суп
        Плотный суп с жареной грудинкой.
        700 г / 10 €
    """
    title = f"{esc(item.emoji)} " if item.emoji else ""
    title += f"<b>{item_number(item)} / {esc(item.name)}</b>"
    lines = [title]
    if item.description:
        lines.append(esc(item.description))
    price_line = format_price(item.price_cents)
    if item.portion:
        price_line = f"{esc(item.portion)} / {price_line}"
    lines.append(price_line)
    return "\n".join(lines)


def menu_post_text(issue: Issue, order_link: str | None = None) -> str:
    """
    Текст поста выпуска: шапка, название, блюда, текст после списка.

    order_link — если передан, в конце добавляется ссылка «👉 Заказать»
    (для альбома, под которым нельзя поставить кнопку).
    """
    parts = []
    if issue.header:
        parts.append(esc(issue.header))
    parts.append(f"<b>{esc(issue.title)}</b>")
    parts.extend(item_block(item) for item in issue.items if item.is_active)
    if issue.footer:
        parts.append(esc(issue.footer))
    if order_link:
        parts.append(f'👉 <a href="{order_link}">Заказать</a>')
    return "\n\n".join(parts)


def card_text(item: IssueItem, index: int, total: int) -> str:
    """Карточка блюда в карусели клиента."""
    return f"{item_block(item)}\n\n<i>Блюдо {index + 1} из {total}</i>"


def cart_button(count: int, total_cents: int) -> str:
    """Подпись кнопки корзины под карточкой."""
    if count == 0:
        return "🛒 Корзина пуста"
    return f"🛒 Корзина: {count} шт. · {format_price(total_cents)} → оформить"


def list_view_text(issue: Issue) -> str:
    """Шапка экрана «полный список блюд» (сами блюда — кнопками)."""
    return f"<b>{esc(issue.title)}</b>\n\nПроверьте количество и нажмите «Дальше».\nНазвание блюда — открыть карточку с фото."


def cart_lines(cart: list[CartItem]) -> str:
    """Строки корзины: «01 Суп × 2 — 20 €»."""
    lines = []
    for ci in cart:
        item = ci.issue_item
        lines.append(f"{item_number(item)} {esc(item.name)} × {ci.quantity} — {format_price(item.price_cents * ci.quantity)}")
    return "\n".join(lines)


def summary_text(cart: list[CartItem], total_cents: int) -> str:
    """Экран «Итого»."""
    return f"<b>Ваш заказ</b>\n\n{cart_lines(cart)}\n\n<b>Итого: {format_price(total_cents)}</b>"


def contact_text(name: str | None, phone: str | None, delivery_type: str | None, address: str | None) -> str:
    """Сохранённые данные клиента для вопроса «Всё верно?»."""
    lines = [f"Имя: {esc(name)}", f"Телефон: {esc(phone)}"]
    if delivery_type == DeliveryType.PICKUP:
        lines.append("Получение: самовывоз")
    else:
        lines.append("Получение: доставка")
        lines.append(f"Адрес: {esc(address)}")
    return "\n".join(lines)


def saved_data_question(name, phone, delivery_type, address) -> str:
    return f"<b>Ваши данные</b>\n\n{contact_text(name, phone, delivery_type, address)}\n\nВсё верно?"


def order_lines(order: Order) -> str:
    """Позиции заказа: обычные и добавленные шефом."""
    lines = []
    for item in order.items:
        mark = " (от шефа)" if item.is_extra else ""
        lines.append(f"• {esc(item.name)}{mark} × {item.quantity} — {format_price(item.price_cents * item.quantity)}")
    return "\n".join(lines)


def order_accepted(order: Order) -> str:
    """Сообщение клиенту после оформления."""
    return (
        f"✅ <b>Заказ №{order.id} принят</b>\n\n{order_lines(order)}\n\n"
        f"Итого: {format_price(order.total_cents)}\n\n"
        "Дату доставки шеф сообщит после закрытия приёма.\n"
        "Отменить или посмотреть заказ — «🧾 Мои заказы»."
    )


def order_for_customer(order: Order) -> str:
    """Заказ в разделе «Мои заказы»."""
    return (
        f"<b>№{order.id}</b> · {esc(order.issue.title)} · {ORDER_STATUS_LABEL[order.status]}\n"
        f"{order_lines(order)}\nСумма: {format_price(order.total_cents)}"
    )


# ---------------------------------------------------------------------------
# Сообщения шефу
# ---------------------------------------------------------------------------


def order_for_admin(order: Order, title: str = "Заказ") -> str:
    """Карточка заказа для админа: состав, сумма, контакты."""
    username = f" (@{esc(order.username)})" if order.username else ""
    lines = [
        f"<b>{title} №{order.id}</b> — {esc(order.name)}{username}",
        f"Статус: {ORDER_STATUS_LABEL[order.status]}",
        "",
        order_lines(order),
        "",
        f"Итого: <b>{format_price(order.total_cents)}</b> · {DELIVERY_LABEL.get(order.delivery_type, order.delivery_type)}",
        f"Телефон: {esc(order.phone)}",
    ]
    if order.delivery_type == DeliveryType.DELIVERY:
        lines.append(f"Адрес: {esc(order.address)}")
    if order.comment:
        lines.append(f"Комментарий: {esc(order.comment)}")
    return "\n".join(lines)


def new_order_notification(order: Order) -> str:
    return "🆕 " + order_for_admin(order, "Новый заказ")


def cancel_notification(order: Order, by: str) -> str:
    who = "клиентом" if by == "customer" else "администратором"
    return f"❌ <b>Заказ №{order.id} отменён {who}</b>\n\n{order_for_admin(order)}"


def issue_for_admin(issue: Issue, orders_count: int, total_cents: int) -> str:
    """Экран выпуска в админке."""
    lines = [f"<b>{esc(issue.title)}</b>", ISSUE_STATUS_LABEL[issue.status], ""]
    if not issue.items:
        lines.append("Блюд пока нет — нажмите «➕ Блюдо».")
    for item in issue.items:
        off = "" if item.is_active else " ⛔ снято"
        lines.append(f"{item_number(item)} {esc(item.name)} — {format_price(item.price_cents)}{off}")
    if issue.status != IssueStatus.DRAFT:
        lines += ["", f"Заказов: {orders_count} · на сумму {format_price(total_cents)}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Посты в группу и рассылки
# ---------------------------------------------------------------------------


def order_group_post(order: Order, header: str, footer: str) -> str:
    """
    Анонимный пост о заказе в группе.

    Только блюда из меню и количество: имя, телефон, адрес, комментарий,
    номер заказа и позиции шефа сюда НЕ попадают (решение из допроса).
    """
    lines = [f"• {esc(item.name)} — {item.quantity}" for item in order.items if not item.is_extra]
    return f"{esc(header)}\n\n" + "\n".join(lines) + f"\n\n{esc(footer)}"


def closing_message(issue: Issue, summary: CustomerSummary, pickup_address: str | None) -> str:
    """Сообщение клиенту при закрытии приёма: текст шефа + его позиции + общий итог."""
    lines = [esc(issue.closing_text), "", f"<b>Ваш заказ в «{esc(issue.title)}»:</b>"]
    for (name, _portion, price, _extra), qty in summary.lines.items():
        lines.append(f"• {esc(name)} × {qty} — {format_price(price * qty)}")
    lines.append(f"\n<b>Итого: {format_price(summary.total_cents)}</b>")
    if summary.delivery_type == DeliveryType.PICKUP and pickup_address:
        lines.append(f"\nСамовывоз: {esc(pickup_address)}")
    return "\n".join(lines)


def leftovers_post(lines: list[tuple[str, int]]) -> str:
    """Пост «Остались порции» в группу."""
    body = "\n".join(f"• {esc(name)} — {qty}" for name, qty in lines)
    return f"🍱 <b>Остались свободные порции:</b>\n\n{body}\n\nПишите шефу, пока не разобрали!"


def price_changed(item: IssueItem, old_cents: int, order: Order) -> str:
    return (
        f"Шеф изменил цену: <b>{esc(item.name)}</b> {format_price(old_cents)} → {format_price(item.price_cents)}.\n"
        f"Ваш заказ №{order.id}, новый итог: <b>{format_price(order.total_cents)}</b>.\n"
        "Если не подходит — заказ можно отменить."
    )


def item_changed(item: IssueItem, order: Order) -> str:
    return f"Шеф обновил блюдо в вашем заказе №{order.id}:\n\n{item_block(item)}"


def item_removed(item: IssueItem, order: Order) -> str:
    return (
        f"⚠️ Блюдо <b>{esc(item.name)}</b> из вашего заказа №{order.id} закончилось.\n"
        "Шеф свяжется с вами."
    )


def item_added(item: IssueItem) -> str:
    return f"В лавку добавили новое блюдо:\n\n{item_block(item)}"


def extra_added(order: Order, name: str, qty: int, price_cents: int) -> str:
    return (
        f"Шеф добавил в ваш заказ №{order.id}: {esc(name)} × {qty}, {format_price(price_cents * qty)}.\n"
        f"Итого: <b>{format_price(order.total_cents)}</b>"
    )


def cancelled_by_admin(order: Order) -> str:
    return f"Шеф отменил ваш заказ №{order.id}. Если это неожиданно — напишите ему."
