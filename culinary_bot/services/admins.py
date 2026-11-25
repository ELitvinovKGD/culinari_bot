"""
Администраторы: владелец из .env, приглашения по ссылке, уведомления.
"""

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import Admin, AdminInvite, utcnow
from culinary_bot.services import ServiceError


async def ensure_owner(session: AsyncSession, owner_tg_id: int) -> None:
    """
    Убедиться, что владелец из .env есть в таблице админов.

    Вызывается при каждом запуске бота. Если OWNER_TG_ID в .env поменяли —
    новый владелец тоже появится (старый останется обычным админом).
    """
    admin = await session.scalar(select(Admin).where(Admin.tg_id == owner_tg_id))
    if admin is None:
        session.add(Admin(tg_id=owner_tg_id, is_owner=True, name="Владелец"))
    else:
        admin.is_owner = True


async def get_admin(session: AsyncSession, tg_id: int) -> Admin | None:
    """Админ по Telegram ID или None, если человек не админ."""
    return await session.scalar(select(Admin).where(Admin.tg_id == tg_id))


async def list_admins(session: AsyncSession) -> list[Admin]:
    """Все админы, владелец первым."""
    result = await session.scalars(select(Admin).order_by(Admin.is_owner.desc(), Admin.id))
    return list(result)


async def admins_to_notify(session: AsyncSession, *, orders: bool = False, cancels: bool = False) -> list[Admin]:
    """
    Кому из админов отправить уведомление.

    orders=True — о новых заказах, cancels=True — об отменах.
    Учитываются личные переключатели каждого админа.
    """
    query = select(Admin)
    if orders:
        query = query.where(Admin.notify_orders.is_(True))
    if cancels:
        query = query.where(Admin.notify_cancels.is_(True))
    return list(await session.scalars(query))


async def create_invite(session: AsyncSession, created_by_tg_id: int) -> str:
    """
    Создать одноразовое приглашение и вернуть его токен.

    secrets.token_urlsafe — криптографически случайная строка. Обычный random
    для этого не годится: его значения можно предсказать, а по этой ссылке
    человек получает полный доступ к заказам и данным клиентов.
    Длина 16 байт → ~22 символа: влезает в параметр /start (до 64 символов).
    """
    token = secrets.token_urlsafe(16)
    session.add(AdminInvite(token=token, created_by_tg_id=created_by_tg_id))
    return token


async def accept_invite(session: AsyncSession, token: str, tg_id: int, name: str | None) -> Admin:
    """
    Принять приглашение: сделать человека админом.

    Ошибки: ссылка не существует или уже использована.
    Если человек уже админ — просто возвращаем его.
    """
    invite = await session.scalar(select(AdminInvite).where(AdminInvite.token == token))
    if invite is None or invite.used_at is not None:
        raise ServiceError("Ссылка недействительна или уже использована.")

    existing = await get_admin(session, tg_id)
    invite.used_at = utcnow()
    invite.used_by_tg_id = tg_id
    if existing is not None:
        return existing

    admin = Admin(tg_id=tg_id, name=name)
    session.add(admin)
    await session.flush()
    return admin


async def remove_admin(session: AsyncSession, admin_id: int) -> Admin:
    """Удалить админа. Владельца удалить нельзя."""
    admin = await session.get(Admin, admin_id)
    if admin is None:
        raise ServiceError("Админ не найден.")
    if admin.is_owner:
        raise ServiceError("Владельца нельзя удалить через бота.")
    await session.delete(admin)
    return admin
