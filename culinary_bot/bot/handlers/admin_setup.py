"""
Первая настройка лавки: мастер по шагам и чек-лист готовности.

Шеф настраивает бота сам, с телефона, без разработчика рядом (решение от 25.11).

Мастер (кнопка «🧭 Мастер настройки» или автоматически при первом входе):
    1. Группа — инструкция прямо в шаге; после /connect бот сам пишет «Дальше →».
    2. Контакт для клиентов — @username шефа.
    3. Самовывоз — будет или нет; если да — адрес.
    4. Подсказка: первое блюдо → первый выпуск.
Мастер можно прервать: незаполненное видно в чек-листе на главном экране админки.

Чек-лист — вверху главного экрана админки, пока лавка не настроена полностью.
"""

from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.bot.callbacks import AdmCb, DishCb, FieldCb, SetupCb
from culinary_bot.db.models import Dish, Issue
from culinary_bot.services import ServiceError
from culinary_bot.services import settings as settings_service

router = Router(name="admin_setup")


class Setup(StatesGroup):
    """Шаги мастера, где бот ждёт текст."""

    contact = State()
    pickup_address = State()


# ---------------------------------------------------------------------------
# Чек-лист готовности
# ---------------------------------------------------------------------------


@dataclass
class CheckItem:
    """Один пункт чек-листа: подпись, выполнен ли, кнопка для исправления."""

    label: str
    done: bool
    required: bool
    fix_text: str
    fix_cb: object


async def readiness(session: AsyncSession) -> list[CheckItem]:
    """Собрать пункты чек-листа по текущему состоянию базы."""
    group = await settings_service.get_group_chat_id(session)
    group_title = texts.esc(await settings_service.get_value(session, settings_service.GROUP_TITLE) or "без названия")
    contact = await settings_service.get_value(session, settings_service.CHEF_CONTACT)
    pickup = await settings_service.is_pickup_enabled(session)
    address = await settings_service.get_value(session, settings_service.PICKUP_ADDRESS)
    dishes = await session.scalar(select(func.count()).select_from(Dish).where(Dish.is_archived.is_(False)))
    issues = await session.scalar(select(func.count()).select_from(Issue))

    if pickup:
        pickup_item = CheckItem(
            f"Самовывоз: адрес {'указан' if address else 'не указан'}", bool(address), True,
            "📍 Указать адрес самовывоза", FieldCb(entity="setting", field="pickup_address"),
        )
    else:
        pickup_item = CheckItem("Самовывоз: выключен", True, True, "", None)

    return [
        CheckItem(
            f"Группа подключена: «{group_title}»" if group else "Группа не подключена", bool(group), True,
            "👥 Подключить группу", SetupCb(action="step", value="group"),
        ),
        CheckItem(
            f"Контакт для клиентов: @{contact}" if contact else "Контакт для клиентов не указан", bool(contact), True,
            "✉️ Указать контакт", SetupCb(action="step", value="contact"),
        ),
        pickup_item,
        CheckItem(f"Блюд в каталоге: {dishes}", bool(dishes), False, "🍲 Завести первое блюдо", DishCb(action="new")),
        CheckItem(
            "Первый выпуск собран" if issues else "Выпусков пока нет", bool(issues), False,
            "📦 Собрать выпуск", AdmCb(section="issues"),
        ),
    ]


async def home_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    """
    Главный экран админки: чек-лист (пока не всё готово) + меню разделов.

    Чек-лист сворачивается в «Лавка настроена ✅», когда заполнено
    обязательное И есть хотя бы одно блюдо и один выпуск.
    """
    items = await readiness(session)
    menu = keyboards.admin_home()
    if all(item.done for item in items):
        return "<b>Админка</b> · Лавка настроена ✅\nЧто будем делать?", menu

    lines = ["<b>Админка</b>", "", "<b>Готовность лавки</b>"]
    fix_rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        icon = "✅" if item.done else ("⚠️" if item.required else "⬜")
        lines.append(f"{icon} {item.label}")
        if not item.done and item.fix_cb is not None:
            fix_rows.append([InlineKeyboardButton(text=item.fix_text, callback_data=item.fix_cb.pack())])
    lines += ["", "⚠️ — обязательно до первого выпуска."]
    # Сначала кнопки «исправить», потом обычное меню админки.
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=fix_rows + menu.inline_keyboard)


async def is_fresh(session: AsyncSession) -> bool:
    """Ничего не настроено — самое начало (показываем приглашение в мастер)."""
    group = await settings_service.get_group_chat_id(session)
    contact = await settings_service.get_value(session, settings_service.CHEF_CONTACT)
    return group is None and not contact


# ---------------------------------------------------------------------------
# Мастер
# ---------------------------------------------------------------------------

GROUP_HELP = (
    "<b>Шаг 1 из 3 · Группа</b>\n\n"
    "Бот публикует выпуски и заказы в вашу группу. Подключите её:\n\n"
    "1. Откройте группу → нажмите на её название → «Добавить участника» → найдите этого бота.\n"
    "2. Там же: «Администраторы» → «Добавить администратора» → выберите бота. "
    "Включите право <b>«Удаление сообщений»</b>.\n"
    "3. Напишите в группе команду <code>/connect</code>.\n\n"
    "Как только группа подключится, я напишу сюда, и продолжим."
)
CONTACT_HELP = (
    "<b>Шаг 2 из 3 · Контакт для клиентов</b>\n\n"
    "Ваш @username в Telegram — кнопки «✉️ Написать шефу» будут вести сюда.\n"
    "Его видно в профиле: Настройки → Имя пользователя.\n\n"
    "Например: <i>@chef_anton</i>"
)
PICKUP_HELP = "<b>Шаг 3 из 3 · Самовывоз</b>\n\nКлиенты смогут забирать заказы сами?"
DONE_TEXT = (
    "Готово, основное настроено ✅\n\n"
    "Дальше:\n1. Заведите блюда в каталоге.\n2. Соберите выпуск и опубликуйте.\n\n"
    "Совет: первый выпуск сделайте в тестовой группе и закажите сами — посмотрите, как всё выглядит."
)


CHANGE_GROUP_HELP = (
    "<b>Сменить группу</b>\n\n"
    "Сейчас подключена: «{title}».\n\n"
    "Чтобы перейти в другую (например, с тестовой на настоящую):\n"
    "1. Добавьте бота в новую группу и сделайте администратором с правом «Удаление сообщений».\n"
    "2. Напишите в новой группе <code>/connect</code>.\n\n"
    "Новая группа заменит старую. Совет: меняйте группу между выпусками — "
    "пост уже открытого выпуска останется в старой группе."
)


async def show_step(message: Message, state: FSMContext, step: str, session: AsyncSession | None = None) -> None:
    """Показать шаг мастера: group, contact, pickup, done."""
    await state.clear()
    group_id = await settings_service.get_group_chat_id(session) if session is not None and step == "group" else None
    if step == "group" and group_id is not None:
        title = await settings_service.get_value(session, settings_service.GROUP_TITLE) or "без названия"
        await message.answer(
            "<b>Шаг 1 из 3 · Группа</b>\n\n" + CHANGE_GROUP_HELP.format(title=texts.esc(title)).split("\n\n", 1)[1],
            reply_markup=keyboards.setup_buttons(("Оставить эту группу, дальше →", SetupCb(action="step", value="contact"))),
        )
    elif step == "group":
        await message.answer(
            GROUP_HELP,
            reply_markup=keyboards.setup_buttons(
                ("🔄 Проверить подключение", SetupCb(action="group_check")),
                ("Пропустить →", SetupCb(action="step", value="contact")),
            ),
        )
    elif step == "contact":
        await state.set_state(Setup.contact)
        await message.answer(CONTACT_HELP, reply_markup=keyboards.setup_buttons(("Пропустить →", SetupCb(action="step", value="pickup"))))
    elif step == "pickup":
        await message.answer(
            PICKUP_HELP,
            reply_markup=keyboards.setup_buttons(
                ("🏠 Да, будет самовывоз", SetupCb(action="pickup_yes")),
                ("🚚 Нет, только доставка", SetupCb(action="pickup_no")),
            ),
        )
    else:
        await message.answer(
            DONE_TEXT,
            reply_markup=keyboards.setup_buttons(
                ("🍲 Завести блюдо", DishCb(action="new")),
                ("⚙️ Админка", AdmCb(section="home")),
            ),
        )


@router.callback_query(SetupCb.filter(F.action == "start"))
async def cb_start(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "🧭 <b>Настроим лавку за 3 шага</b>: группа, контакт для клиентов, самовывоз.\n"
        "Займёт пару минут. Можно прервать и вернуться позже.",
        reply_markup=keyboards.setup_start(),
    )
    await callback.answer()


@router.callback_query(SetupCb.filter(F.action == "later"))
async def cb_later(callback: CallbackQuery) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Хорошо. Мастер всегда доступен в админке: «🧭 Мастер настройки».")
    await callback.answer()


@router.callback_query(SetupCb.filter(F.action == "step"))
async def cb_step(callback: CallbackQuery, callback_data: SetupCb, state: FSMContext, session: AsyncSession) -> None:
    await show_step(callback.message, state, callback_data.value, session)
    await callback.answer()


@router.callback_query(SetupCb.filter(F.action == "change_group"))
async def cb_change_group(callback: CallbackQuery, session: AsyncSession) -> None:
    """«🔄 Сменить группу» из настроек — инструкция."""
    title = await settings_service.get_value(session, settings_service.GROUP_TITLE) or "без названия"
    await callback.message.answer(CHANGE_GROUP_HELP.format(title=texts.esc(title)))
    await callback.answer()


@router.callback_query(SetupCb.filter(F.action == "group_check"))
async def cb_group_check(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«🔄 Проверить подключение» — вдруг шеф уже написал /connect."""
    if await settings_service.get_group_chat_id(session) is None:
        await callback.answer("Пока не вижу группу. Напишите в группе /connect (бот должен быть админом).", show_alert=True)
        return
    await callback.answer("Группа подключена ✅")
    await show_step(callback.message, state, "contact", session)


@router.message(Setup.contact, F.text)
async def on_contact(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        username = settings_service.normalize_username(message.text)
    except ServiceError as error:
        await message.answer(str(error))
        return
    await settings_service.set_value(session, settings_service.CHEF_CONTACT, username)
    await message.answer(f"Контакт сохранён: @{texts.esc(username)} ✅")
    await show_step(message, state, "pickup")


@router.callback_query(SetupCb.filter(F.action.in_({"pickup_yes", "pickup_no"})))
async def cb_pickup(callback: CallbackQuery, callback_data: SetupCb, state: FSMContext, session: AsyncSession) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    if callback_data.action == "pickup_no":
        await settings_service.set_value(session, settings_service.PICKUP_ENABLED, "0")
        await show_step(callback.message, state, "done")
    else:
        await settings_service.set_value(session, settings_service.PICKUP_ENABLED, "1")
        await state.set_state(Setup.pickup_address)
        await callback.message.answer(
            "Адрес самовывоза — его получат клиенты, выбравшие самовывоз, вместе с объявлением выдачи.\n"
            "Например: <i>Будва, ул. Медитеранска 5, кафе «Лавка»</i>"
        )
    await callback.answer()


@router.message(Setup.pickup_address, F.text)
async def on_pickup_address(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await settings_service.set_value(session, settings_service.PICKUP_ADDRESS, message.text.strip())
    await message.answer("Адрес самовывоза сохранён ✅")
    await show_step(message, state, "done")
