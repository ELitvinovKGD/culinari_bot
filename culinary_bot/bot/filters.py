"""
Фильтры — условия, при которых обработчик вообще вызывается.

Фильтр IsAdmin вешается на целый роутер админки (см. handlers/__init__.py):
все обработчики внутри автоматически недоступны не-админам.
Это главная защита админки — не нужно помнить о проверке в каждой функции.
"""

from aiogram.filters import Filter
from aiogram.types import TelegramObject


class IsAdmin(Filter):
    """Пропускает событие, только если его автор — администратор."""

    async def __call__(self, event: TelegramObject, is_admin: bool = False) -> bool:
        # is_admin кладёт в данные ActorMiddleware (middlewares.py).
        # aiogram передаёт фильтру значения из data по имени параметра.
        return is_admin
