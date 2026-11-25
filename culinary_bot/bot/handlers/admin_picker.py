"""
Выбор блюда из каталога: страницы, недавние сверху и поиск (решение от 26.11).

Один экран на четыре случая (mode):
    cat   — каталог блюд (нажатие открывает карточку);
    arch  — архив блюд;
    lavka — добавить блюдо в выпуск лавки (issue_id);
    stock — выставить блюдо «вне лавки» (issue_id).

Поиск: шеф нажимает «🔍 Найти» и пишет часть слова обычным сообщением.
Ищется в названии, составе и описании: «курица» найдёт «Сациви».
Кнопка поиска появляется, только когда блюд больше, чем на одной странице.
"""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards, paging
from culinary_bot.bot.callbacks import PickCb
from culinary_bot.bot.ui import replace_message
from culinary_bot.db.models import Dish
from culinary_bot.services import catalog as catalog_service
from culinary_bot.services import issues as issue_service

router = Router(name="admin_picker")


class SearchDish(StatesGroup):
    """Ждём текст поиска."""

    query = State()


TITLES = {
    "cat": "<b>Каталог блюд</b>",
    "arch": "<b>Архив блюд</b>",
    "lavka": "<b>Какое блюдо добавить в выпуск?</b>",
    "stock": "<b>Какое блюдо выставить вне лавки?</b>\nЕсли уже было — остаток пополнится.",
}


async def dishes_for(session: AsyncSession, mode: str, issue_id: int) -> tuple[list[Dish], set[int]]:
    """Список блюд для режима и id «недавних» (для пометки 🕘)."""
    if mode == "arch":
        return await catalog_service.list_dishes(session, archived=True), set()
    dishes, recent = await catalog_service.list_dishes_recent_first(session, paging.RECENT_COUNT)
    if mode == "lavka":
        issue = await issue_service.get_issue(session, issue_id)
        used = {item.dish_id for item in issue.items}  # уже в выпуске — не предлагаем
        dishes = [d for d in dishes if d.id not in used]
    return dishes, recent


async def picker_screen(
    session: AsyncSession, mode: str, issue_id: int = 0, page: int = 0, query: str | None = None
) -> tuple[str, object]:
    """(текст, клавиатура) экрана выбора — весь список или результаты поиска."""
    dishes, recent = await dishes_for(session, mode, issue_id)
    total = len(dishes)
    if query:
        found = catalog_service.search_dishes(dishes, query)
        reasons = {d.id: why for d, why in found if why}
        text = f"{TITLES[mode]}\n\n🔍 «{texts.esc(query)}»: найдено {len(found)}"
        if not found:
            text += "\nПопробуйте часть слова короче — например «блин» вместо «блинчики»."
        kb = keyboards.dish_picker(
            mode, [d for d, _ in found], issue_id=issue_id, page=page, reasons=reasons, found=True
        )
        return text, kb
    extra = ""
    if mode in ("cat", "lavka", "stock") and recent:
        extra = "\n🕘 — недавние: были в последних выпусках."
    if not dishes:
        extra = "\nПусто." if mode == "arch" else "\nВ каталоге пока нет блюд — нажмите «➕ Новое блюдо»."
    text = f"{TITLES[mode]} · {total}{extra}"
    kb = keyboards.dish_picker(
        mode, dishes, issue_id=issue_id, page=page, recent_ids=recent, total_in_catalog=total
    )
    return text, kb


async def send_picker(message: Message, session: AsyncSession, mode: str, issue_id: int = 0) -> None:
    """Прислать экран выбора новым сообщением (из других модулей админки)."""
    text, kb = await picker_screen(session, mode, issue_id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(PickCb.filter(F.action == "page"))
async def cb_page(callback: CallbackQuery, callback_data: PickCb, state: FSMContext, session: AsyncSession) -> None:
    """Страница списка (или результатов поиска — запрос хранится в данных FSM)."""
    query = None
    if callback_data.found:
        query = (await state.get_data()).get("dish_query")
    text, kb = await picker_screen(session, callback_data.mode, callback_data.issue_id, callback_data.page, query)
    await replace_message(callback, text, kb)
    await callback.answer()


@router.callback_query(PickCb.filter(F.action == "search"))
async def cb_search(callback: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    await state.set_state(SearchDish.query)
    await state.update_data(pick_mode=callback_data.mode, pick_issue=callback_data.issue_id)
    await callback.message.answer(
        "🔍 Напишите часть названия или продукт из состава.\n"
        "Например: <i>блин</i> или <i>курица</i>\n\n<i>/cancel — отменить</i>"
    )
    await callback.answer()


@router.message(SearchDish.query, F.text)
async def on_query(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    query = message.text.strip()[:50]
    # set_state(None) — выйти из режима «ждём текст», но СОХРАНИТЬ данные:
    # запрос понадобится, чтобы листать страницы результатов.
    await state.set_state(None)
    await state.update_data(dish_query=query)
    text, kb = await picker_screen(session, data.get("pick_mode", "cat"), data.get("pick_issue", 0), 0, query)
    await message.answer(text, reply_markup=kb)


@router.callback_query(PickCb.filter(F.action == "reset"))
async def cb_reset(callback: CallbackQuery, callback_data: PickCb, state: FSMContext, session: AsyncSession) -> None:
    await state.update_data(dish_query=None)
    text, kb = await picker_screen(session, callback_data.mode, callback_data.issue_id)
    await replace_message(callback, text, kb)
    await callback.answer()
