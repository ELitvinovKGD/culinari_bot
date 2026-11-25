"""
Обработчики Telegram, разложенные по роутерам.

Роутер (Router) — набор обработчиков. Главный роутер собирает остальные.
Порядок подключения важен: aiogram проверяет роутеры по очереди и отдаёт
событие ПЕРВОМУ подходящему обработчику.

    group         — команды в группе (/connect)
    start         — /start, нижние кнопки, /delete_me
    admin_*       — админка (закрыта фильтром IsAdmin)
    client_*      — меню, оформление и «Мои заказы» клиента
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from culinary_bot.bot.filters import IsAdmin
from culinary_bot.bot.handlers import (
    admin_catalog,
    admin_fields,
    admin_issues,
    admin_main,
    admin_orders,
    admin_picker,
    admin_setup,
    admin_stock,
    client_checkout,
    client_menu,
    client_orders,
    client_profile,
    client_stock,
    group,
    start,
)


def build_router() -> Router:
    """Собрать все роутеры в один главный."""
    root = Router(name="root")

    # Команды из группы — отдельно, остальным роутерам группы не нужны.
    root.include_router(group.router)

    # Всё остальное — только в личных сообщениях с ботом.
    # F.chat.type == "private" — фильтр "чат — личный".
    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.include_router(start.router)

    # Админка: один фильтр IsAdmin на весь роутер защищает все её обработчики.
    admin = Router(name="admin")
    admin.message.filter(IsAdmin())
    admin.callback_query.filter(IsAdmin())
    for module in (admin_main, admin_setup, admin_picker, admin_fields, admin_catalog, admin_stock, admin_issues, admin_orders):
        admin.include_router(module.router)
    private.include_router(admin)

    # client_orders раньше client_checkout: кнопка «Мои заказы» должна
    # сработать, даже если клиент бросил оформление на шаге «введите имя»,
    # а не записаться в имя.
    for module in (client_orders, client_profile, client_stock, client_menu, client_checkout):
        private.include_router(module.router)

    root.include_router(private)

    # Последний рубеж: нажали кнопку, которую никто не обработал (например,
    # кнопку из старого сообщения после перезапуска бота). Отвечаем, чтобы
    # у кнопки не крутились часики, и подсказываем, что делать.
    fallback = Router(name="fallback")

    @fallback.callback_query()
    async def stale_button(callback: CallbackQuery) -> None:
        await callback.answer("Эта кнопка устарела. Откройте меню заново.", show_alert=True)

    root.include_router(fallback)
    return root
