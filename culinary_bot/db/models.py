"""
Таблицы базы данных.

Каждый класс здесь = одна таблица, каждый атрибут `Mapped[...]` = одна колонка.
SQLAlchemy по этим классам строит SQL, а Alembic — миграции (создание таблиц).

Как таблицы связаны между собой (подробно — docs/data-model.md):

    Dish (каталог блюд) ──< IssueItem >── Issue (выпуск)
                               │
                 CartItem ─────┤
                               ↓
                           OrderItem >── Order >── Customer

Главная идея: блюдо в каталоге (Dish), позиция в выпуске (IssueItem) и
позиция в заказе (OrderItem) — РАЗНЫЕ записи. Каждая следующая копирует
название, порцию и цену из предыдущей. Поэтому правка карточки блюда
не меняет старые выпуски, а правка выпуска — старые заказы.

Если меняешь здесь класс (добавляешь колонку), нужно создать миграцию:
    alembic revision --autogenerate -m "что изменилось"
(подробный рецепт — в docs/architecture.md).
"""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from culinary_bot.db.base import Base


def utcnow() -> datetime:
    """
    Текущее время в UTC.

    Время в базе всегда храним в UTC (всемирное время, без часовых поясов),
    а переводим в местное только при показе. Иначе при переезде сервера
    в другой часовой пояс все даты "поехали" бы.
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Перечисления (enum) — фиксированные наборы значений.
#
# StrEnum: значения — обычные строки ("open", "closed"), поэтому в базе они
# хранятся как читаемый текст. Если ошибиться в написании статуса в коде,
# Python сразу подскажет: IssueStatus.OPN не существует.
# ---------------------------------------------------------------------------


class IssueStatus(StrEnum):
    """Состояния выпуска (см. таблицу в docs/product.md)."""

    DRAFT = "draft"  # черновик: шеф собирает, клиенты не видят
    OPEN = "open"  # пост в группе, идёт приём заказов
    CLOSED = "closed"  # приём закрыт, готовим и развозим
    FINISHED = "finished"  # всё развезено, выпуск в архиве


class OrderStatus(StrEnum):
    """Статусы заказа."""

    ACCEPTED = "accepted"  # принят (сразу после оформления)
    DELIVERED = "delivered"  # доставлен
    CANCELLED = "cancelled"  # отменён клиентом или шефом


class DeliveryType(StrEnum):
    """Способ получения заказа."""

    DELIVERY = "delivery"
    PICKUP = "pickup"


# ---------------------------------------------------------------------------
# Люди
# ---------------------------------------------------------------------------


class Customer(Base):
    """
    Клиент — любой, кто написал боту.

    Здесь хранятся СОХРАНЁННЫЕ данные для следующих заказов (имя, телефон,
    адрес). В сам заказ они копируются на момент оформления — см. Order.
    """

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Telegram ID пользователя. BigInteger — потому что ID в Telegram давно
    # не помещаются в обычный 32-битный Integer.
    # unique=True — один человек = одна запись клиента.
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    # @username может отсутствовать (не у всех он задан) — поэтому `| None`.
    username: Mapped[str | None] = mapped_column(String(64))

    name: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(32))
    delivery_type: Mapped[str | None] = mapped_column(String(16))
    address: Mapped[str | None] = mapped_column(Text)
    # Точка на карте, если клиент отправил геопозицию вместо (или вместе с) адресом.
    # Float здесь допустим: координаты — не деньги, крошечная неточность не важна.
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    # Видел ли клиент уведомление о личных данных (показываем один раз).
    privacy_accepted: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    orders: Mapped[list["Order"]] = relationship(back_populates="customer")

    def has_saved_data(self) -> bool:
        """Есть ли сохранённые данные, чтобы предложить «Ваши данные, всё верно?»."""
        if not (self.name and self.phone and self.delivery_type):
            return False
        # Для доставки обязателен адрес или точка на карте, для самовывоза — нет.
        return self.delivery_type == DeliveryType.PICKUP or bool(self.address) or self.latitude is not None


class Admin(Base):
    """
    Администратор бота (шеф, помощник, разработчик).

    Первый админ (владелец) создаётся автоматически из OWNER_TG_ID в .env.
    Остальные добавляются по одноразовой ссылке (AdminInvite).
    """

    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(100))
    # Владельца нельзя удалить через бота — защита от ситуации "не осталось админов".
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    # Личные переключатели уведомлений (например, курьеру не нужны отмены).
    notify_orders: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_cancels: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminInvite(Base):
    """
    Одноразовое приглашение в администраторы.

    Ссылка выглядит как t.me/имя_бота?start=admin_<token>. Токен —
    длинная случайная строка, угадать её невозможно. После использования
    заполняется used_at, и повторно ссылка не сработает.
    """

    __tablename__ = "admin_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by_tg_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_tg_id: Mapped[int | None] = mapped_column(BigInteger)


# ---------------------------------------------------------------------------
# Каталог и выпуски
# ---------------------------------------------------------------------------


class Dish(Base):
    """
    Карточка блюда в каталоге шефа — постоянная база.

    Цена хранится в центах (целое число): 10.50 € → 1050.
    Почему не дробным числом: float в компьютере неточен
    (0.1 + 0.2 = 0.30000000000000004), а с деньгами так нельзя.
    """

    __tablename__ = "dishes"

    id: Mapped[int] = mapped_column(primary_key=True)
    emoji: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    # Порция — свободный текст: "350/100 г", "5 шт.", "700 г".
    portion: Mapped[str | None] = mapped_column(String(100))
    # Состав — показывается клиенту по кнопке «📋 Состав», в пост группы не идёт.
    composition: Mapped[str | None] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer)
    # file_id — идентификатор фото на серверах Telegram. Сам файл мы не храним:
    # Telegram отдаёт его по file_id, сколько угодно раз.
    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    # Блюда не удаляем, а архивируем — на них ссылаются старые выпуски.
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Issue(Base):
    """
    Выпуск (меню лавки) — например «Тайная лавка шефа | GREAT HITS».
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # Шапка поста — свободный текст шефа ("Last call, хомячки мои! ...").
    header: Mapped[str | None] = mapped_column(Text)
    # Текст после списка блюд ("Доставку планирую в конце недели").
    footer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=IssueStatus.DRAFT, index=True)

    # Последнее объявление выдачи («📣 Объявить выдачу»): текст для клиентов
    # с доставкой и отдельный — для самовывоза.
    closing_text: Mapped[str | None] = mapped_column(Text)
    pickup_text: Mapped[str | None] = mapped_column(Text)
    # Когда последний раз скачивали Excel. Если после этого пришёл заказ,
    # шеф получит предупреждение «таблица изменилась — пересоберите».
    excel_exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Где лежит пост выпуска в группе — чтобы потом его обновить.
    # JSON-список ID сообщений (альбом — это несколько сообщений).
    group_message_ids: Mapped[list[int] | None] = mapped_column(JSON)
    # ID сообщения, в котором текст меню (подпись к альбому или отдельный текст).
    group_text_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # True — текст это подпись к фото (меняется edit_caption),
    # False — обычное сообщение (меняется edit_text).
    group_text_is_caption: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # order_by="IssueItem.position" — позиции всегда приходят в порядке номеров.
    items: Mapped[list["IssueItem"]] = relationship(
        back_populates="issue",
        order_by="IssueItem.position",
        cascade="all, delete-orphan",
        # lazy="selectin": позиции загружаются сразу вместе с выпуском.
        # В асинхронном SQLAlchemy "ленивая" догрузка при обращении к
        # issue.items невозможна (упадёт с ошибкой MissingGreenlet),
        # поэтому связанные данные, которые нужны почти всегда, грузим заранее.
        lazy="selectin",
    )


class IssueItem(Base):
    """
    Позиция в выпуске — блюдо со СВОИМИ ценой, порцией и описанием.

    При добавлении в выпуск значения копируются из карточки (Dish), после чего
    их можно поменять только для этого выпуска. Карточка при этом не меняется.
    """

    __tablename__ = "issue_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    # Связь с каталогом — для будущей статистики ("сколько раз заказывали колбаски").
    dish_id: Mapped[int] = mapped_column(ForeignKey("dishes.id"))
    # Порядковый номер в выпуске (1, 2, 3...) — превращается в "01 /", "02 /".
    position: Mapped[int] = mapped_column(Integer)

    emoji: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    portion: Mapped[str | None] = mapped_column(String(100))
    composition: Mapped[str | None] = mapped_column(Text)
    price_cents: Mapped[int] = mapped_column(Integer)
    photo_file_id: Mapped[str | None] = mapped_column(String(255))

    # False — снято с продажи ("закончилось"): клиенты больше не видят.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    issue: Mapped[Issue] = relationship(back_populates="items", lazy="selectin")


class CartItem(Base):
    """
    Корзина клиента: одна строка = одно блюдо и его количество.

    Корзина хранится в базе, а не в памяти бота, — чтобы она не пропадала
    при перезапуске или обновлении бота.
    """

    __tablename__ = "cart_items"
    # Ограничение уникальности: у одного клиента одна позиция встречается
    # в корзине только один раз (количество меняется в той же строке).
    __table_args__ = (UniqueConstraint("customer_id", "issue_item_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    issue_item_id: Mapped[int] = mapped_column(ForeignKey("issue_items.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer)

    issue_item: Mapped[IssueItem] = relationship(lazy="selectin")


# ---------------------------------------------------------------------------
# Заказы
# ---------------------------------------------------------------------------


class Order(Base):
    """
    Заказ клиента.

    id — он же номер заказа (сквозной: №1, №2, ... по всем выпускам).

    Контактные данные КОПИРУЮТСЯ сюда из Customer в момент оформления.
    Так заказ остаётся неизменным, даже если клиент потом поменяет адрес
    или удалит свои данные командой /delete_me.
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.ACCEPTED, index=True)

    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(32))
    username: Mapped[str | None] = mapped_column(String(64))
    delivery_type: Mapped[str] = mapped_column(String(16))
    address: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    comment: Mapped[str | None] = mapped_column(Text)

    # Когда заказ «сформирован»: через 10 минут после оформления (время на правку)
    # или раньше, если шеф закрыл приём. Пока None — клиент может нажать
    # «✏️ Изменить», а шеф и группа о заказе ещё не знают.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # Пост о заказе в группе: ID сообщения и время отправки
    # (удалить пост Telegram позволяет только в течение 48 часов).
    group_message_id: Mapped[int | None] = mapped_column(BigInteger)
    group_message_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Кто отменил: "customer" или "admin".
    cancelled_by: Mapped[str | None] = mapped_column(String(16))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # lazy="selectin" — по той же причине, что и у Issue.items: в асинхронном
    # коде связанные данные надо загружать заранее, а не при обращении.
    customer: Mapped[Customer] = relationship(back_populates="orders", lazy="selectin")
    issue: Mapped[Issue] = relationship(lazy="selectin")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        order_by="OrderItem.id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def map_url(self) -> str | None:
        """Ссылка на точку в Google Maps, если клиент отправил геопозицию."""
        if self.latitude is None or self.longitude is None:
            return None
        return f"https://maps.google.com/?q={self.latitude:.6f},{self.longitude:.6f}"

    @property
    def total_cents(self) -> int:
        """Сумма заказа в центах (вычисляется, в базе не хранится — не разойдётся с позициями)."""
        return sum(item.price_cents * item.quantity for item in self.items)


class OrderItem(Base):
    """
    Позиция заказа — СНИМОК блюда на момент оформления.

    name / portion / price_cents копируются из IssueItem. Если шеф потом
    поменяет цену в выпуске, старые заказы останутся с той ценой, по которой
    их оформили (если только шеф явно не выберет «применить к заказам»).

    is_extra=True — позицию добавил шеф вручную ("наливка ×1"). У неё нет
    issue_item_id, она входит в сумму и лист доставки, но не в таблицу
    производства (её не надо готовить по меню).
    """

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    issue_item_id: Mapped[int | None] = mapped_column(ForeignKey("issue_items.id"))
    name: Mapped[str] = mapped_column(String(200))
    portion: Mapped[str | None] = mapped_column(String(100))
    price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    is_extra: Mapped[bool] = mapped_column(Boolean, default=False)

    order: Mapped[Order] = relationship(back_populates="items")


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


class Setting(Base):
    """
    Настройки, которые шеф меняет через бота (а не через .env).

    Простая таблица "ключ → значение": group_chat_id, pickup_enabled,
    pickup_address, order_post_header, order_post_footer.
    Новая настройка не требует миграции — достаточно нового ключа
    (список ключей и значения по умолчанию — в services/settings.py).
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
