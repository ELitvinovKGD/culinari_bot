"""
Главное меню админки, настройки, список админов и личные уведомления.

Весь этот роутер закрыт фильтром IsAdmin (см. handlers/__init__.py),
поэтому внутри обработчиков проверять «админ ли это» уже не нужно.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import AdmCb, AdminsCb, SetCb
from culinary_bot.bot.publisher import deep_link
from culinary_bot.bot.ui import replace_message
from culinary_bot.db.models import Admin
from culinary_bot.services import ServiceError
from culinary_bot.services import admins as admin_service
from culinary_bot.services import catalog as catalog_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import settings as settings_service

router = Router(name="admin_main")

HOME_TEXT = "<b>Админка</b>\nЧто будем делать?"


@router.message(F.text == texts.BTN_ADMIN)
@router.message(Command("admin"))
async def admin_home(message: Message, state: FSMContext) -> None:
    """Кнопка «⚙️ Админка» или команда /admin."""
    await state.clear()
    await message.answer(HOME_TEXT, reply_markup=keyboards.admin_home())


async def settings_text(session: AsyncSession) -> str:
    """Текст экрана настроек с текущими значениями."""
    group = await settings_service.get_group_chat_id(session)
    address = await settings_service.get_value(session, settings_service.PICKUP_ADDRESS)
    header = await settings_service.get_value(session, settings_service.ORDER_POST_HEADER)
    footer = await settings_service.get_value(session, settings_service.ORDER_POST_FOOTER)
    return (
        "<b>Настройки</b>\n\n"
        f"Группа: {'подключена ✅' if group else 'не подключена — добавьте бота в группу админом и напишите там /connect'}\n"
        f"Адрес самовывоза: {texts.esc(address) or '—'}\n\n"
        f"<b>Пост о заказе в группе:</b>\n{texts.esc(header)}\n• Блюдо — 2\n{texts.esc(footer)}"
    )


@router.callback_query(AdmCb.filter())
async def cb_section(
    callback: CallbackQuery, callback_data: AdmCb, state: FSMContext, session: AsyncSession, admin: Admin
) -> None:
    """Переход по разделам главного меню."""
    await state.clear()
    section = callback_data.section
    if section == "home":
        await replace_message(callback, HOME_TEXT, reply_markup=keyboards.admin_home())
    elif section == "issues":
        issues = await issue_service.list_active_issues(session)
        await replace_message(callback, "<b>Выпуски</b>", reply_markup=keyboards.issues_list(issues, archive=False))
    elif section == "archive":
        issues = await issue_service.list_finished_issues(session)
        await replace_message(callback, "<b>Архив выпусков</b>", reply_markup=keyboards.issues_list(issues, archive=True))
    elif section == "catalog":
        dishes = await catalog_service.list_dishes(session)
        await replace_message(callback, "<b>Каталог блюд</b>", reply_markup=keyboards.catalog(dishes, archived=False))
    elif section == "archived":
        dishes = await catalog_service.list_dishes(session, archived=True)
        await replace_message(callback, "<b>Архив блюд</b>", reply_markup=keyboards.catalog(dishes, archived=True))
    elif section == "settings":
        pickup = await settings_service.is_pickup_enabled(session)
        await replace_message(callback, await settings_text(session), reply_markup=keyboards.settings(pickup))
    elif section == "admins":
        await show_admins(callback, session)
    elif section == "notify":
        await replace_message(callback, "<b>Мои уведомления</b>", reply_markup=keyboards.notify(admin))
    await callback.answer()


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------


@router.callback_query(SetCb.filter())
async def cb_settings(callback: CallbackQuery, callback_data: SetCb, session: AsyncSession) -> None:
    """Переключатель самовывоза и сброс текстов поста о заказе."""
    if callback_data.action == "pickup_toggle":
        enabled = await settings_service.is_pickup_enabled(session)
        await settings_service.set_value(session, settings_service.PICKUP_ENABLED, "0" if enabled else "1")
    elif callback_data.action == "reset_post":
        await settings_service.reset_value(session, settings_service.ORDER_POST_HEADER)
        await settings_service.reset_value(session, settings_service.ORDER_POST_FOOTER)
    # flush — чтобы следующие чтения в этой же сессии увидели новые значения.
    await session.flush()
    pickup = await settings_service.is_pickup_enabled(session)
    await replace_message(callback, await settings_text(session), reply_markup=keyboards.settings(pickup))
    await callback.answer("Сохранено")


# ---------------------------------------------------------------------------
# Админы и уведомления
# ---------------------------------------------------------------------------


async def show_admins(callback: CallbackQuery, session: AsyncSession) -> None:
    admins = await admin_service.list_admins(session)
    lines = [f"{'👑' if a.is_owner else '•'} {texts.esc(a.name) or a.tg_id}" for a in admins]
    await replace_message(callback, "<b>Админы</b>\n\n" + "\n".join(lines), reply_markup=keyboards.admins_list(admins))


@router.callback_query(AdminsCb.filter())
async def cb_admins(
    callback: CallbackQuery, callback_data: AdminsCb, session: AsyncSession, admin: Admin, bot_username: str
) -> None:
    """Приглашение, удаление админа, личные переключатели уведомлений."""
    action = callback_data.action
    if action == "invite":
        token = await admin_service.create_invite(session, callback.from_user.id)
        link = deep_link(bot_username, f"admin_{token}")
        await callback.message.answer(
            "Перешлите эту ссылку человеку, которого хотите сделать админом.\n"
            "Ссылка одноразовая: после первого перехода она перестанет работать.\n\n"
            f"{link}"
        )
    elif action == "remove":
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.confirm(
                "Да, удалить админа",
                AdminsCb(action="remove_yes", admin_id=callback_data.admin_id),
                AdmCb(section="admins"),
            )
        )
    elif action == "remove_yes":
        try:
            await admin_service.remove_admin(session, callback_data.admin_id)
        except ServiceError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await session.flush()
        await show_admins(callback, session)
    elif action in ("notify_orders", "notify_cancels"):
        # Переключаем у СЕБЯ: admin — это автор нажатия (из middleware).
        if action == "notify_orders":
            admin.notify_orders = not admin.notify_orders
        else:
            admin.notify_cancels = not admin.notify_cancels
        await callback.message.edit_reply_markup(reply_markup=keyboards.notify(admin))
    await callback.answer()
