"""
Клиенты: регистрация при первом сообщении, сохранение контактов, удаление данных.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import CartItem, Customer, DeliveryType
from culinary_bot.services import ServiceError
from culinary_bot.services.cart import return_stock


async def get_or_create(session: AsyncSession, tg_id: int, username: str | None) -> Customer:
    """
    Найти клиента по Telegram ID или создать нового.

    Вызывается на каждое действие клиента. Заодно обновляем @username —
    человек мог его поменять, а шефу нужен актуальный, чтобы написать.
    """
    customer = await session.scalar(select(Customer).where(Customer.tg_id == tg_id))
    if customer is None:
        customer = Customer(tg_id=tg_id, username=username)
        session.add(customer)
        # flush — отправить INSERT в базу сейчас (но ещё не commit),
        # чтобы у объекта появился id: он понадобится, например, для корзины.
        await session.flush()
    elif customer.username != username:
        customer.username = username
    return customer


def normalize_phone(text: str) -> str:
    """
    Проверить и очистить телефон, который клиент ввёл вручную.

    Решение из допроса: формат свободный (люди живут в разных странах и пишут
    по-разному), проверяем только, что в номере есть хотя бы 7 цифр.
    Возвращает номер без лишних пробелов по краям.
    """
    digits = [ch for ch in text if ch.isdigit()]
    if len(digits) < 7 or len(digits) > 15:
        raise ServiceError("Похоже, в номере ошибка. Введите телефон ещё раз, например +995 555 123 456")
    # Разрешаем только цифры, пробелы, плюс, скобки и дефисы — защита от того,
    # чтобы в поле "телефон" не прислали произвольный текст.
    allowed = set("0123456789+-() ")
    if any(ch not in allowed for ch in text.strip()):
        raise ServiceError("В номере могут быть только цифры, пробелы, +, - и скобки")
    return text.strip()


def save_contact(
    customer: Customer,
    *,
    name: str,
    phone: str,
    delivery_type: DeliveryType,
    address: str | None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> None:
    """
    Сохранить контактные данные клиента для следующих заказов.

    Символ * в списке параметров означает: дальше аргументы можно передавать
    только по имени (save_contact(c, name="Ева", ...)). Так при вызове
    невозможно перепутать местами имя и телефон.
    """
    customer.name = name.strip()[:100]
    customer.phone = phone
    customer.delivery_type = delivery_type
    # При самовывозе адрес не нужен — не храним лишних личных данных.
    is_delivery = delivery_type == DeliveryType.DELIVERY
    customer.address = address.strip() if (address and is_delivery) else None
    customer.latitude = latitude if is_delivery else None
    customer.longitude = longitude if is_delivery else None


async def delete_personal_data(session: AsyncSession, customer: Customer) -> None:
    """
    Удалить сохранённые личные данные клиента (/delete_me).

    Уже оформленные заказы хранят свою копию данных — шефу нужно их доставить.
    Удаляем данные профиля и корзину.
    """
    customer.name = None
    customer.phone = None
    customer.address = None
    customer.latitude = None
    customer.longitude = None
    customer.delivery_type = None
    customer.privacy_accepted = False
    carts = await session.scalars(select(CartItem).where(CartItem.customer_id == customer.id))
    for item in carts:
        await return_stock(session, item)  # бронь блюд вне лавки возвращается в остаток
