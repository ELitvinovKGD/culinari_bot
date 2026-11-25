"""
Каталог блюд в админке: просмотр карточки, создание нового блюда, архив.

Создание блюда — пошаговый диалог (FSM, см. client_checkout.py):
    название → эмодзи → описание → порция → цена → фото → готово
Необязательные шаги можно пропустить кнопкой или словом «-».
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import DishCb
from culinary_bot.db.models import Dish
from culinary_bot.services import ServiceError
from culinary_bot.services import catalog as catalog_service
from culinary_bot.services import issues as issue_service
from culinary_bot.services.money import format_price, parse_price

router = Router(name="admin_catalog")


class NewDish(StatesGroup):
    """Шаги создания блюда."""

    name = State()
    emoji = State()
    description = State()
    portion = State()
    price = State()
    photo = State()


# Шаги по порядку: (состояние, ключ в данных FSM, вопрос, можно ли пропустить).
STEPS = [
    (NewDish.name, "name", "Название блюда:", False),
    (NewDish.emoji, "emoji", "Эмодзи (например 🍲):", True),
    (NewDish.description, "description", "Описание (одна-две фразы):", True),
    (NewDish.portion, "portion", "Порция (например «350/100 г» или «5 шт.»):", True),
    (NewDish.price, "price_cents", "Цена в евро (например 10 или 10.5):", False),
    (NewDish.photo, "photo_file_id", "Фото блюда:", True),
]


def skip_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=DishCb(action="skip"))
    return kb.as_markup()


def dish_caption(dish: Dish) -> str:
    """Текст карточки в каталоге (похож на то, что увидит клиент)."""
    title = f"{texts.esc(dish.emoji)} " if dish.emoji else ""
    lines = [f"{title}<b>{texts.esc(dish.name)}</b>"]
    if dish.description:
        lines.append(texts.esc(dish.description))
    price = format_price(dish.price_cents)
    lines.append(f"{texts.esc(dish.portion)} / {price}" if dish.portion else price)
    if dish.is_archived:
        lines.append("\n🗄 <i>В архиве</i>")
    if not dish.photo_file_id:
        lines.append("\n<i>Без фото</i>")
    return "\n".join(lines)


async def send_dish(message: Message, dish: Dish) -> None:
    """Отправить карточку блюда с кнопками правки."""
    if dish.photo_file_id:
        await message.answer_photo(dish.photo_file_id, caption=dish_caption(dish), reply_markup=keyboards.dish(dish))
    else:
        await message.answer(dish_caption(dish), reply_markup=keyboards.dish(dish))


@router.callback_query(DishCb.filter(F.action == "view"))
async def cb_view(callback: CallbackQuery, callback_data: DishCb, session: AsyncSession) -> None:
    try:
        dish = await catalog_service.get_dish(session, callback_data.dish_id)
    except ServiceError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await send_dish(callback.message, dish)
    await callback.answer()


@router.callback_query(DishCb.filter(F.action.in_({"archive", "unarchive"})))
async def cb_archive(callback: CallbackQuery, callback_data: DishCb, session: AsyncSession) -> None:
    dish = await catalog_service.get_dish(session, callback_data.dish_id)
    catalog_service.set_archived(dish, callback_data.action == "archive")
    await callback.message.edit_reply_markup(reply_markup=keyboards.dish(dish))
    await callback.answer("В архиве" if dish.is_archived else "Возвращено")


# ---------------------------------------------------------------------------
# Создание блюда
# ---------------------------------------------------------------------------


@router.callback_query(DishCb.filter(F.action == "new"))
async def cb_new(callback: CallbackQuery, callback_data: DishCb, state: FSMContext) -> None:
    """Начать создание. issue_id ≠ 0 — блюдо создаётся из выпуска и добавится в него."""
    await state.clear()
    await state.update_data(issue_id=callback_data.issue_id)
    await ask_step(callback.message, state, 0)
    await callback.answer()


async def ask_step(message: Message, state: FSMContext, index: int) -> None:
    """Задать вопрос шага с номером index."""
    st, _key, prompt, optional = STEPS[index]
    await state.set_state(st)
    await state.update_data(step=index)
    await message.answer(prompt + "\n\n<i>/cancel — отменить</i>", reply_markup=skip_kb() if optional else None)


async def next_step(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Перейти к следующему шагу или, если шаги кончились, создать блюдо."""
    data = await state.get_data()
    index = data["step"] + 1
    if index < len(STEPS):
        await ask_step(message, state, index)
        return
    await state.clear()
    try:
        dish = await catalog_service.create_dish(
            session,
            name=data["name"],
            price_cents=data["price_cents"],
            emoji=data.get("emoji"),
            description=data.get("description"),
            portion=data.get("portion"),
            photo_file_id=data.get("photo_file_id"),
        )
        await message.answer("Блюдо сохранено в каталоге ✅")
        if data.get("issue_id"):
            # Блюдо создавали из выпуска — сразу добавляем туда.
            from culinary_bot.bot.handlers import admin_issues  # см. комментарий в admin_fields.apply_value

            issue = await issue_service.get_issue(session, data["issue_id"])
            await issue_service.add_dish_to_issue(session, issue, dish)
            await admin_issues.after_item_added(message, session, issue)
        else:
            await send_dish(message, dish)
    except ServiceError as error:
        await message.answer(str(error))


@router.message(F.text == "/cancel", NewDish.name)
@router.message(F.text == "/cancel", NewDish.emoji)
@router.message(F.text == "/cancel", NewDish.description)
@router.message(F.text == "/cancel", NewDish.portion)
@router.message(F.text == "/cancel", NewDish.price)
@router.message(F.text == "/cancel", NewDish.photo)
async def on_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Создание блюда отменено.", reply_markup=keyboards.admin_home())


@router.callback_query(DishCb.filter(F.action == "skip"))
async def cb_skip(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Пропустить необязательный шаг."""
    if await state.get_state() is None:
        await callback.answer()
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await next_step(callback.message, state, session)
    await callback.answer()


@router.message(NewDish.photo, F.photo)
async def on_photo(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.update_data(photo_file_id=message.photo[-1].file_id)
    await next_step(message, state, session)


@router.message(NewDish.photo)
async def on_photo_wrong(message: Message) -> None:
    await message.answer("Пришлите фото или нажмите «Пропустить».")


@router.message(NewDish.price, F.text)
async def on_price(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        cents = parse_price(message.text)
    except ServiceError as error:
        await message.answer(str(error))
        return
    await state.update_data(price_cents=cents)
    await next_step(message, state, session)


@router.message(NewDish.name, F.text)
@router.message(NewDish.emoji, F.text)
@router.message(NewDish.description, F.text)
@router.message(NewDish.portion, F.text)
async def on_text_step(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Текстовые шаги. «-» — пропустить необязательный шаг."""
    data = await state.get_data()
    _st, key, _prompt, optional = STEPS[data["step"]]
    value = message.text.strip()
    if value == "-" and optional:
        value = None
    elif not value:
        await message.answer("Пришлите текст.")
        return
    await state.update_data(**{key: value})
    await next_step(message, state, session)
