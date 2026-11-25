"""
Записная книжка получателей: «себе и родителям», заказы в подарок.

Получатель принадлежит клиенту. Все функции проверяют владельца — чужого
получателя нельзя ни выбрать, ни удалить (id приходит из кнопки и может
быть подделан).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import Customer, Recipient
from culinary_bot.services import ServiceError


async def list_recipients(session: AsyncSession, customer: Customer) -> list[Recipient]:
    """Получатели клиента (без удалённых), в порядке добавления."""
    result = await session.scalars(
        select(Recipient)
        .where(Recipient.customer_id == customer.id, Recipient.is_deleted.is_(False))
        .order_by(Recipient.id)
    )
    return list(result)


async def get_recipient(session: AsyncSession, customer: Customer, recipient_id: int) -> Recipient:
    """Получатель клиента по id. Чужой или удалённый — ServiceError."""
    recipient = await session.get(Recipient, recipient_id)
    if recipient is None or recipient.customer_id != customer.id or recipient.is_deleted:
        raise ServiceError("Получатель не найден.")
    return recipient


async def create_recipient(
    session: AsyncSession,
    customer: Customer,
    *,
    name: str,
    phone: str,
    address: str | None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> Recipient:
    """Сохранить нового получателя (нужен адрес или точка на карте)."""
    if not (address or latitude is not None):
        raise ServiceError("Нужен адрес или точка на карте.")
    recipient = Recipient(
        customer_id=customer.id,
        name=name.strip()[:100],
        phone=phone,
        address=address,
        latitude=latitude,
        longitude=longitude,
    )
    session.add(recipient)
    await session.flush()
    return recipient


async def delete_recipient(session: AsyncSession, customer: Customer, recipient_id: int) -> None:
    """Убрать получателя из списка (мягко — старые заказы на него ссылаются)."""
    recipient = await get_recipient(session, customer, recipient_id)
    recipient.is_deleted = True
