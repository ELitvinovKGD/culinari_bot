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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from culinary_bot.config import settings
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


def local_time(moment: datetime) -> str:
    """
    Время из базы (UTC) → «25.11, 18:14» в часовом поясе лавки (TIMEZONE в .env).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)  # SQLite в тестах хранит без пояса
    return moment.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m, %H:%M")


def order_label(order: Order) -> str:
    """
    Как клиент видит свой заказ: «заказ от 25.11, 18:14».

    Номер заказа клиенту не показываем (решение от 25.11): «№1» или «1-1»
    подсказывает, как мало заказов. Номер видит только шеф.
    """
    return f"заказ от {local_time(order.created_at)}"


# ---------------------------------------------------------------------------
# Общие фразы
# ---------------------------------------------------------------------------

BTN_MENU = "🍽 Меню"
BTN_MY_ORDERS = "🧾 Мои заказы"
BTN_ADMIN = "⚙️ Админка"
BTN_WRITE_CHEF = "✉️ Написать шефу"
BTN_MY_DATA = "👤 Мои данные"
BTN_STOCK = "🧺 В наличии"
STOCK_EMPTY = "У шефа всё разобрали. Загляните позже 🙂"
NO_CHEF_CONTACT = "Шеф пока не указал контакт. Напишите ему в группе."

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
ASK_ADDRESS = "Адрес доставки? Напишите текстом: город, улица, дом, подъезд, этаж."
ASK_POINT = (
    "Поставьте точку на карте, чтобы шеф вас быстрее нашёл:\n"
    "📎 → Геопозиция → выберите место. Или вставьте ссылку из Google Maps.\n\n"
    "Если не нужно — «Пропустить»."
)
ASK_REC_NAME = "🎁 Заказ другому человеку.\nКак зовут получателя?"
ASK_REC_PHONE = "Телефон получателя — для связи при доставке:"
ASK_PAYER = "Кто платит за этот заказ?"
PAYER_LABEL = {"customer": "оплата: заказчик", "recipient": "оплата: получатель"}
ASK_ADDRESS_NOTE = "Точка получена 📍\nУточните подъезд, этаж или ориентир — или пропустите."
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
    for item in issue.items:
        if not item.is_active:
            continue
        if item.is_stock:
            if item.stock_left and item.stock_left > 0:
                parts.append(f"{item_block(item)}\nОсталось: {item.stock_left}")
        else:
            parts.append(item_block(item))
    if issue.footer:
        parts.append(esc(issue.footer))
    if order_link:
        parts.append(f'👉 <a href="{order_link}">Заказать</a>')
    return "\n\n".join(parts)


def card_text(item: IssueItem, index: int, total: int, in_order: int = 0) -> str:
    """
    Карточка блюда в карусели клиента (состав — по кнопке «📋 Состав»).

    in_order — сколько этого блюда УЖЕ в оформленном заказе клиента
    (при дозаказе кнопки −/+ меняют только добавку).
    """
    already = f"\n🧾 В вашем заказе: {in_order}" if in_order else ""
    return f"{item_block(item)}{already}\n\n<i>Блюдо {index + 1} из {total}</i>"


# Всплывающее окно Telegram (show_alert) вмещает максимум 200 символов.
ALERT_LIMIT = 200


def composition_text(item: IssueItem) -> str:
    """Текст «📋 Состав» — простой текст без HTML (во всплывающем окне теги не работают)."""
    return f"Состав: {item.composition}"


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


def summary_text(cart: list[CartItem], total_cents: int, existing: CustomerSummary | None = None) -> str:
    """
    Экран «Итого».

    existing — уже оформленный заказ клиента в этом выпуске (дозаказ).
    Тогда показываем: что уже заказано, что добавляется, что получится в итоге.
    """
    if existing is None:
        return f"<b>Ваш заказ</b>\n\n{cart_lines(cart)}\n\n<b>Итого: {format_price(total_cents)}</b>"
    # Что получится в итоге: уже заказанное + добавка, по названию блюда.
    result: dict[str, int] = {}
    for (name, _portion, _price, _extra), qty in existing.lines.items():
        result[name] = result.get(name, 0) + qty
    for ci in cart:
        result[ci.issue_item.name] = result.get(ci.issue_item.name, 0) + ci.quantity
    together = ", ".join(f"{esc(name)} × {qty}" for name, qty in result.items())
    return (
        f"<b>Уже заказано:</b>\n{summary_lines(existing)}\n\n"
        f"<b>Добавляете:</b>\n{cart_lines(cart)}\n\n"
        f"<b>В итоге:</b> {together}\n"
        f"<b>К оплате: {format_price(existing.total_cents + total_cents)}</b>"
    )


def where_to_add_text(cart: list[CartItem], total_cents: int) -> str:
    """Несколько заказов (себе и получателям): сначала только «Куда добавить?» (решение от 26.11)."""
    return f"<b>Добавляете:</b>\n{cart_lines(cart)}\n\nНа сумму {format_price(total_cents)}.\n\n<b>Куда добавить?</b>"


def summary_lines(summary: CustomerSummary) -> str:
    """Позиции клиента по всем частям заказа: «• Суп × 3 — 30 €»."""
    return "\n".join(
        f"• {esc(name)}{' (от шефа)' if extra else ''} × {qty} — {format_price(price * qty)}"
        for (name, _portion, price, extra), qty in summary.lines.items()
    )


def contact_text(
    name: str | None, phone: str | None, delivery_type: str | None, address: str | None, has_point: bool = False
) -> str:
    """Сохранённые данные клиента для вопроса «Всё верно?»."""
    lines = [f"Имя: {esc(name)}", f"Телефон: {esc(phone)}"]
    if delivery_type == DeliveryType.PICKUP:
        lines.append("Получение: самовывоз")
    else:
        lines.append("Получение: доставка")
        point = "📍 точка на карте" if has_point else ""
        lines.append(f"Адрес: {' · '.join(x for x in (point, esc(address)) if x)}")
    return "\n".join(lines)


def saved_data_question(name, phone, delivery_type, address, has_point: bool = False) -> str:
    return f"<b>Ваши данные</b>\n\n{contact_text(name, phone, delivery_type, address, has_point)}\n\nВсё верно?"


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
        f"✅ <b>Ваш заказ принят</b>\n\n{order_lines(order)}\n\n"
        f"Итого: {format_price(order.total_cents)}\n\n"
        f"Изменить заказ можно в течение {settings.order_edit_minutes} минут.\n"
        "Когда будет готово, шеф сообщит здесь."
    )


def order_for_customer(order: Order) -> str:
    """Заказ в разделе «Мои заказы»."""
    return (
        f"<b>{order_label(order).capitalize()}</b> · {esc(order.issue.title)} · {ORDER_STATUS_LABEL[order.status]}\n"
        f"{order_lines(order)}\nСумма: {format_price(order.total_cents)}"
    )


def order_added(summary: CustomerSummary) -> str:
    """Сообщение клиенту после дозаказа: что теперь в заказе целиком."""
    return (
        f"✅ <b>Добавлено к заказу</b>\n\n<b>{order_title(summary)} теперь:</b>\n{summary_lines(summary)}\n\n"
        f"К оплате: <b>{format_price(summary.total_cents)}</b>\n\n"
        f"Изменить добавку можно в течение {settings.order_edit_minutes} минут."
    )


def order_title(summary: CustomerSummary) -> str:
    """
    «Ваш заказ» или «🎁 Заказ получателю: Маша».

    Имя не склоняем («для Маша» звучит коряво), поэтому «получателю: Имя».
    """
    return f"🎁 Заказ получателю: {esc(summary.name)}" if summary.recipient_id else "Ваш заказ"


def payer_note(summary: CustomerSummary) -> str:
    """Для подарка — кто платит."""
    if not summary.recipient_id:
        return ""
    return " (платит получатель)" if summary.payer == "recipient" else " (платите вы)"


def issue_order_card(issue: Issue, summary: CustomerSummary, status: str) -> str:
    """Одна карточка заказа в «Моих заказах» (все части вместе): себе или получателю."""
    where = ""
    if summary.recipient_id:
        where = f"\nКуда: {esc(summary.address) or '📍 точка на карте'}"
    return (
        f"<b>{order_title(summary)}</b> · «{esc(issue.title)}» · {status}{where}\n\n{summary_lines(summary)}\n\n"
        f"К оплате: <b>{format_price(summary.total_cents)}</b>{payer_note(summary)}"
    )


def order_reverted() -> str:
    return "Заказ возвращён в корзину ✏️ Поправьте и оформите снова — данные вводить не придётся."


# ---------------------------------------------------------------------------
# Сообщения шефу
# ---------------------------------------------------------------------------


def order_status_for_admin(order: Order) -> str:
    """Статус для шефа; у заказа в окне правки — «⏳ клиент ещё может изменить»."""
    if order.status == OrderStatus.ACCEPTED and order.confirmed_at is None:
        return "⏳ оформлен, клиент ещё может изменить"
    return ORDER_STATUS_LABEL[order.status]


def order_for_admin(order: Order, title: str = "Заказ") -> str:
    """Карточка заказа для админа: состав, сумма, контакты."""
    username = f" (@{esc(order.username)})" if order.username else ""
    if order.is_gift:
        customer = order.customer
        orderer = esc(customer.name) or "клиент"
        who = f"{esc(order.name)} 🎁 (заказал(а) {orderer}{username}, {PAYER_LABEL.get(order.payer, '')})"
    else:
        who = f"{esc(order.name)}{username}"
    lines = [
        f"<b>{title} №{order.id}</b> — {who}",
        f"Статус: {order_status_for_admin(order)} · от {local_time(order.created_at)}",
        "",
        order_lines(order),
        "",
        f"Итого: <b>{format_price(order.total_cents)}</b> · {DELIVERY_LABEL.get(order.delivery_type, order.delivery_type)}",
        f"Телефон: {esc(order.phone)}",
    ]
    if order.delivery_type == DeliveryType.DELIVERY:
        if order.address:
            # <code> — в Telegram такой текст копируется одним нажатием.
            lines.append(f"Адрес: <code>{esc(order.address)}</code>")
        if order.map_url:
            lines.append(f'📍 <a href="{order.map_url}">Точка на карте</a>')
    if order.comment:
        lines.append(f"Комментарий: {esc(order.comment)}")
    return "\n".join(lines)


def new_order_notification(order: Order, *, excel_outdated: bool) -> str:
    """
    Уведомление шефу о новом сформированном заказе.

    excel_outdated — таблицу по выпуску уже скачивали, она устарела.
    Добавка к существующему заказу — см. order_change_notification.
    """
    text = "🆕 " + order_for_admin(order, "Новый заказ")
    if excel_outdated:
        text += "\n\n⚠️ Таблица изменилась — пересоберите «📊 Excel», если уже распечатали."
    return text


def order_change_notification(order: Order, new_total_cents: int, *, excel_outdated: bool) -> str:
    """
    Клиент добавил блюда к своему заказу: «✏️ Изменение заказа — Ева: + Суп × 1».

    Формулировка «изменение», а не «дозаказ» — так же потом можно будет
    показывать и уменьшение порций (если шеф это разрешит).
    """
    username = f" (@{esc(order.username)})" if order.username else ""
    added = "\n".join(f"+ {esc(item.name)} × {item.quantity}" for item in order.items)
    text = (
        f"✏️ <b>Изменение заказа</b> — {esc(order.name)}{username} (часть №{order.id})\n\n{added}\n\n"
        f"Новый итог клиента: <b>{format_price(new_total_cents)}</b>"
    )
    if order.comment:
        text += f"\nКомментарий: {esc(order.comment)}"
    if excel_outdated:
        text += "\n\n⚠️ Таблица изменилась — пересоберите «📊 Excel», если уже распечатали."
    return text


def cancel_notification(order: Order, by: str) -> str:
    who = "клиентом" if by == "customer" else "администратором"
    return f"❌ <b>Заказ №{order.id} отменён {who}</b>\n\n{order_for_admin(order)}"


def shorten(text: str, limit: int = 120) -> str:
    """Обрезать длинный текст для экрана админки: «Last call, хомячки мои! Лечу…»."""
    text = " ".join(text.split())  # переносы строк → пробелы
    return text if len(text) <= limit else text[: limit - 1] + "…"


def issue_for_admin(issue: Issue, orders_count: int, total_cents: int) -> str:
    """Экран выпуска в админке."""
    lines = [f"<b>{esc(issue.title)}</b>", ISSUE_STATUS_LABEL[issue.status], ""]
    lines.append(f"<b>Шапка:</b> {esc(shorten(issue.header)) if issue.header else '— не заполнена'}")
    lines.append(f"<b>После списка:</b> {esc(shorten(issue.footer)) if issue.footer else '—'}")
    lines.append("")
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


def issue_closed_message(issue: Issue) -> str:
    """Автоматическое сообщение клиентам при закрытии приёма."""
    return (
        f"Приём заказов в «{esc(issue.title)}» закрыт.\n"
        "Ваш заказ принят в работу — шеф сообщит здесь, когда будет готово."
    )


def announcement_message(
    issue: Issue, summaries: list[CustomerSummary], texts_by_type: dict[str, str | None], pickup_address: str | None
) -> str:
    """
    «📣 Объявить выдачу» — ОДНО сообщение заказчику со всеми его заказами
    (себе и в подарок), решение от 26.11:

        Всё готово, развожу во вторник.

        Ваш заказ: • Суп × 2 — 20 €
        🎁 Заказ получателю: Маша (Будва, Морская 5): • Сач × 1 — 17 €

        К оплате вами: 37 €
    """
    lines: list[str] = []
    shown_texts: set[str] = set()
    for summary in summaries:
        text = texts_by_type.get(summary.delivery_type) or texts_by_type.get(DeliveryType.DELIVERY) or texts_by_type.get(DeliveryType.PICKUP)
        if text and text not in shown_texts:
            lines.append(esc(text))
            shown_texts.add(text)
    total_by_me = 0
    for summary in summaries:
        where = f" ({esc(summary.address) or '📍 точка на карте'})" if summary.recipient_id else ""
        lines.append(f"\n<b>{order_title(summary)}{where}:</b>")
        lines.append(summary_lines(summary))
        if summary.recipient_id and summary.payer == "recipient":
            lines.append(f"Оплачивает получатель: {format_price(summary.total_cents)}")
        else:
            total_by_me += summary.total_cents
        if summary.delivery_type == DeliveryType.PICKUP and pickup_address:
            lines.append(f"Самовывоз: {esc(pickup_address)}")
    lines.append(f"\n<b>К оплате вами: {format_price(total_by_me)}</b>")
    return "\n".join(lines)


def my_data_text(customer, recipients: list) -> str:
    """Экран «👤 Мои данные»: основные данные и получатели."""
    if customer.has_saved_data():
        own = contact_text(customer.name, customer.phone, customer.delivery_type, customer.address, customer.latitude is not None)
    else:
        own = "Пока не заполнены — заполнятся при первом заказе."
    lines = ["<b>👤 Мои данные</b>", "", own, "", "<b>Получатели</b> (для заказов другим людям):"]
    if recipients:
        lines += [f"• {esc(r.short())}" for r in recipients]
    else:
        lines.append("пока нет — появятся, когда закажете кому-то «🎁 Другому получателю».")
    return "\n".join(lines)


def leftovers_post(lines: list[tuple[str, int]]) -> str:
    """Пост «Остались порции» в группу."""
    body = "\n".join(f"• {esc(name)} — {qty}" for name, qty in lines)
    return f"🍱 <b>Остались свободные порции:</b>\n\n{body}\n\nПишите шефу, пока не разобрали!"


def price_changed(item: IssueItem, old_cents: int, order: Order) -> str:
    return (
        f"Шеф изменил цену: <b>{esc(item.name)}</b> {format_price(old_cents)} → {format_price(item.price_cents)}.\n"
        f"Ваш {order_label(order)}, новый итог: <b>{format_price(order.total_cents)}</b>.\n"
        "Если не подходит — заказ можно отменить."
    )


def item_changed(item: IssueItem, order: Order) -> str:
    return f"Шеф обновил блюдо в вашем заказе ({order_label(order)}):\n\n{item_block(item)}"


def item_removed(item: IssueItem, order: Order) -> str:
    return (
        f"⚠️ Блюдо <b>{esc(item.name)}</b> из вашего заказа ({order_label(order)}) закончилось.\n"
        "Шеф свяжется с вами."
    )


def item_added(item: IssueItem) -> str:
    return f"В лавку добавили новое блюдо:\n\n{item_block(item)}"


def extra_added(order: Order, name: str, qty: int, price_cents: int) -> str:
    return (
        f"Шеф добавил в ваш заказ ({order_label(order)}): {esc(name)} × {qty}, {format_price(price_cents * qty)}.\n"
        f"Итого: <b>{format_price(order.total_cents)}</b>"
    )


def cancelled_by_admin(order: Order) -> str:
    return f"Шеф отменил ваш {order_label(order)}. Если это неожиданно — напишите ему."



# ---------------------------------------------------------------------------
# «🧺 Блюда вне лавки»
# ---------------------------------------------------------------------------


def stock_view_text(issue: Issue, hold_minutes: int) -> str:
    """Шапка экрана «🧺 В наличии» у клиента."""
    header = f"{esc(issue.header)}\n\n" if issue.header else ""
    return (
        f"🧺 <b>В наличии у шефа</b>\n\n{header}"
        f"Выберите количество. Корзина бронирует порции на {hold_minutes} мин.\n"
        "Название — открыть карточку с фото."
    )


def stock_item_card(item: IssueItem) -> str:
    """Карточка блюда вне лавки: как в лавке + остаток и состав."""
    composition = f"\n\n<i>Состав: {esc(item.composition)}</i>" if item.composition else ""
    return f"{item_block(item)}\nОсталось: {item.stock_left}{composition}"


def stock_where_text() -> str:
    return (
        "У вас есть заказ в лавке. Добавить к нему — тогда шеф привезёт всё вместе?\n"
        "Или оформить отдельно — шеф отдаст, когда будет готов."
    )


def stock_offer(items: list[IssueItem]) -> str:
    """Предложение после заказа из лавки: «Ещё у шефа в наличии: …»."""
    names = ", ".join(esc(item.name) for item in items[:5])
    more = " и другое" if len(items) > 5 else ""
    return f"🧺 Ещё у шефа в наличии: {names}{more}."


def stock_for_admin(issue: Issue, pending: int) -> str:
    """Экран «🧺 Блюда вне лавки» в админке."""
    lines = [f"🧺 <b>{esc(issue.title)}</b>", ""]
    lines.append(f"<b>Шапка поста:</b> {esc(shorten(issue.header)) if issue.header else '— не заполнена'}")
    lines.append("")
    if not issue.items:
        lines.append("Блюд пока нет — нажмите «➕ Блюдо» и укажите, сколько есть.")
    for item in issue.items:
        off = "" if item.is_active else " ⛔ снято"
        lines.append(f"{esc(item.name)} — {format_price(item.price_cents)} · осталось {item.stock_left}{off}")
    lines += ["", f"Неотданных заказов: {pending}"]
    return "\n".join(lines)
