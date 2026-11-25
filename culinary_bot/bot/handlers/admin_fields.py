"""
Универсальная правка полей: карточки блюда, позиции выпуска, выпуска, настроек.

Вместо отдельного обработчика на каждую кнопку «✏️ Название», «✏️ Цена»...
есть ОДИН механизм:
1. Кнопка FieldCb(entity, obj_id, field) → бот спрашивает новое значение
   и запоминает в FSM, что именно правим.
2. Следующее сообщение админа → apply_value() сохраняет его в нужное поле.

Чтобы добавить новое редактируемое поле — см. рецепт в docs/architecture.md:
обычно достаточно кнопки в keyboards.py и строки в FIELD_PROMPTS.
"""

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards, publisher
from culinary_bot.bot.callbacks import FieldCb
from culinary_bot.db.models import IssueStatus
from culinary_bot.services import ServiceError
from culinary_bot.services import catalog as catalog_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services import settings as settings_service
from culinary_bot.services.money import parse_price

router = Router(name="admin_fields")


class EditField(StatesGroup):
    """Ждём новое значение поля."""

    value = State()


# Вопрос, который задаёт бот для каждого поля. Ключ — имя поля.
FIELD_PROMPTS: dict[str, str] = {
    "name": "Новое название:",
    "title": "Название выпуска:",
    "emoji": "Эмодзи блюда (один значок, например 🍲):",
    "description": "Описание:",
    "portion": "Порция (например «350/100 г» или «5 шт.»):",
    "price_cents": "Цена в евро (например 10 или 10.5):",
    "photo_file_id": "Пришлите фото блюда:",
    "header": "Шапка поста — текст перед списком блюд:",
    "footer": "Текст после списка блюд (например «Доставку планирую в конце недели»):",
    "pickup_address": "Адрес самовывоза:",
    "order_post_header": "Первая строка поста о заказе (например «Поступил заказ на следующие позиции:»):",
    "order_post_footer": "Последняя строка поста о заказе (например «Спасибо за поддержку!»):",
}
# Поля, которые можно очистить (необязательные).
CLEARABLE = {"emoji", "description", "portion", "photo_file_id", "header", "footer", "pickup_address"}


@router.callback_query(FieldCb.filter(F.clear.is_(False)))
async def cb_edit_field(callback: CallbackQuery, callback_data: FieldCb, state: FSMContext) -> None:
    """Нажата кнопка «✏️ ...» — спросить новое значение."""
    field = callback_data.field
    if field not in FIELD_PROMPTS:
        await callback.answer("Это поле нельзя изменить.", show_alert=True)
        return
    await state.set_state(EditField.value)
    await state.update_data(entity=callback_data.entity, obj_id=callback_data.obj_id, field=field)
    kb = keyboards.skip_field(callback_data.entity, callback_data.obj_id, field) if field in CLEARABLE else None
    await callback.message.answer(FIELD_PROMPTS[field] + "\n\n<i>/cancel — отменить</i>", reply_markup=kb)
    await callback.answer()


@router.callback_query(FieldCb.filter(F.clear.is_(True)))
async def cb_clear_field(
    callback: CallbackQuery, callback_data: FieldCb, state: FSMContext, session: AsyncSession, bot: Bot, bot_username: str
) -> None:
    """Кнопка «🧹 Очистить» — записать пустое значение."""
    field = callback_data.field
    if field not in CLEARABLE:
        await callback.answer("Это поле обязательно.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await apply_value(callback.message, session, bot, bot_username, callback_data.entity, callback_data.obj_id, field, None)
    await callback.answer()


@router.message(EditField.value, F.text == "/cancel")
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=keyboards.admin_home())


@router.message(EditField.value, F.photo | F.text)
async def on_value(message: Message, state: FSMContext, session: AsyncSession, bot: Bot, bot_username: str) -> None:
    """Пришло новое значение — проверить и сохранить."""
    data = await state.get_data()
    field = data["field"]
    if field == "photo_file_id":
        if not message.photo:
            await message.answer("Нужна фотография. Пришлите фото или /cancel.")
            return
        # message.photo — список размеров одного фото; последний — самый большой.
        value: object = message.photo[-1].file_id
    elif field == "price_cents":
        try:
            value = parse_price(message.text or "")
        except ServiceError as error:
            await message.answer(str(error))
            return
    else:
        if not message.text:
            await message.answer("Пришлите текст или /cancel.")
            return
        value = message.text.strip()
    await state.clear()
    await apply_value(message, session, bot, bot_username, data["entity"], data["obj_id"], field, value)


async def apply_value(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    bot_username: str,
    entity: str,
    obj_id: int,
    field: str,
    value: object,
) -> None:
    """
    Сохранить значение в нужный объект и показать обновлённый экран.

    Отдельный случай — цена позиции в ОТКРЫТОМ выпуске, если её уже заказывали:
    шеф выбирает, применять ли новую цену к оформленным заказам (А3).
    """
    # Импорт внутри функции: модули admin_catalog/admin_issues сами импортируют
    # этот файл, и импорт наверху создал бы «круговую зависимость».
    from culinary_bot.bot.handlers import admin_catalog, admin_issues

    try:
        if entity == "dish":
            dish = await catalog_service.get_dish(session, obj_id)
            catalog_service.update_dish_field(dish, field, value)
            await session.flush()
            await admin_catalog.send_dish(message, dish)

        elif entity == "issue":
            issue = await issue_service.get_issue(session, obj_id)
            issue_service.update_issue_field(issue, field, value)
            await session.flush()
            await admin_issues.send_issue(message, session, issue)

        elif entity == "item":
            item = await issue_service.get_item(session, obj_id)
            if field == "price_cents":
                await admin_issues.handle_price_change(message, session, bot, item, int(value))
                return
            issue_service.update_item_field(item, field, value)
            await session.flush()
            # Описание/порция/название поменялись в открытом выпуске — уведомить клиентов (К8).
            if item.issue.status == IssueStatus.OPEN and field in ("description", "portion", "name"):
                for order in await issue_service.orders_with_item(session, item):
                    await publisher.send_safe(bot, order.customer.tg_id, texts.item_changed(item, order))
            await admin_issues.send_item(message, session, item)

        elif entity == "setting":
            await settings_service.set_value(session, field, value if value is None else str(value))
            await session.flush()
            await message.answer("Сохранено ✅", reply_markup=keyboards.admin_home())
    except ServiceError as error:
        await message.answer(str(error))

