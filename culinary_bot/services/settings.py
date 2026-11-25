"""
Настройки, которые шеф меняет прямо в боте (таблица settings).

Чтобы добавить новую настройку:
1. Добавь константу-ключ ниже.
2. Если нужно значение по умолчанию — добавь его в DEFAULTS.
3. Читай через `await get_value(session, ТВОЙ_КЛЮЧ)`.
Миграция базы не нужна: таблица хранит пары "ключ → значение".
"""

from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import Setting

# Ключи настроек.
GROUP_CHAT_ID = "group_chat_id"  # ID подключённой группы (команда /connect)
PICKUP_ENABLED = "pickup_enabled"  # "1" — самовывоз включён, "0" — выключен
PICKUP_ADDRESS = "pickup_address"  # адрес самовывоза
ORDER_POST_HEADER = "order_post_header"  # первая строка поста о заказе в группе
ORDER_POST_FOOTER = "order_post_footer"  # последняя строка поста о заказе

# Значения по умолчанию — используются, пока шеф не задал своё.
DEFAULTS: dict[str, str] = {
    PICKUP_ENABLED: "1",
    PICKUP_ADDRESS: "",
    ORDER_POST_HEADER: "Поступил заказ на следующие позиции:",
    ORDER_POST_FOOTER: "Спасибо за поддержку! ❤️",
}


async def get_value(session: AsyncSession, key: str) -> str | None:
    """Прочитать настройку; если её нет в базе — вернуть значение по умолчанию (или None)."""
    setting = await session.get(Setting, key)
    if setting is None or setting.value is None:
        return DEFAULTS.get(key)
    return setting.value


async def set_value(session: AsyncSession, key: str, value: str | None) -> None:
    """Сохранить настройку (создать, если её ещё не было)."""
    setting = await session.get(Setting, key)
    if setting is None:
        session.add(Setting(key=key, value=value))
    else:
        setting.value = value


async def reset_value(session: AsyncSession, key: str) -> None:
    """Вернуть стандартное значение — просто удаляем запись из базы."""
    setting = await session.get(Setting, key)
    if setting is not None:
        await session.delete(setting)


async def get_group_chat_id(session: AsyncSession) -> int | None:
    """ID подключённой группы или None, если группа ещё не подключена."""
    value = await get_value(session, GROUP_CHAT_ID)
    return int(value) if value else None


async def is_pickup_enabled(session: AsyncSession) -> bool:
    """Включён ли самовывоз."""
    return await get_value(session, PICKUP_ENABLED) == "1"
