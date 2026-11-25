"""
Настройки, которые шеф меняет прямо в боте (таблица settings).

Чтобы добавить новую настройку:
1. Добавь константу-ключ ниже.
2. Если нужно значение по умолчанию — добавь его в DEFAULTS.
3. Читай через `await get_value(session, ТВОЙ_КЛЮЧ)`.
Миграция базы не нужна: таблица хранит пары "ключ → значение".
"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot.db.models import Setting
from culinary_bot.services import ServiceError

# Ключи настроек.
GROUP_CHAT_ID = "group_chat_id"  # ID подключённой группы (команда /connect)
GROUP_TITLE = "group_title"  # название группы — чтобы показывать шефу, какая подключена
PICKUP_ENABLED = "pickup_enabled"  # "1" — самовывоз включён, "0" — выключен
PICKUP_ADDRESS = "pickup_address"  # адрес самовывоза
ORDER_POST_HEADER = "order_post_header"  # первая строка поста о заказе в группе
ORDER_POST_FOOTER = "order_post_footer"  # последняя строка поста о заказе
CHEF_CONTACT = "chef_contact"  # @username шефа для кнопок «✉️ Написать шефу» (без @)

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


# Имя пользователя в Telegram: 4–32 символа, латиница, цифры и подчёркивание.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def normalize_username(text: str) -> str:
    """
    Привести контакт шефа к виду "username" (без @).

    Шеф может прислать как угодно: "@chef", "chef", "t.me/chef",
    "https://t.me/chef" — всё превращается в "chef".
    Если получилось не похоже на username — ServiceError с подсказкой.
    """
    value = text.strip()
    for prefix in ("https://", "http://", "t.me/", "telegram.me/", "@"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
    value = value.strip("/")
    if not USERNAME_RE.match(value):
        raise ServiceError(
            "Не похоже на имя пользователя Telegram. Пришлите в виде @username — "
            "его видно в профиле Telegram (Настройки → Имя пользователя)."
        )
    return value


async def get_chef_contact_url(session: AsyncSession) -> str | None:
    """Ссылка «написать шефу» (https://t.me/username) или None, если контакт не задан."""
    username = await get_value(session, CHEF_CONTACT)
    return f"https://t.me/{username}" if username else None
