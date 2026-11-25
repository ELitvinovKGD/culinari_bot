"""
Данные инлайн-кнопок (callback data).

Когда человек нажимает инлайн-кнопку, Telegram присылает боту не текст,
а короткую строку, которую мы «зашили» в кнопку при создании,
например "card:plus:15:2". Классы ниже описывают формат этих строк:
aiogram сам упаковывает поля в строку и распаковывает обратно.

    CardCb(action="plus", item_id=15, index=2).pack()  →  "card:plus:15:2"

Обработчик ловит нужные кнопки фильтром:
    @router.callback_query(CardCb.filter(F.action == "plus"))

Ограничение Telegram: строка не длиннее 64 байт — поэтому префиксы короткие.

БЕЗОПАСНОСТЬ: данные кнопки присылает клиентское приложение, и в теории их
можно подделать. Поэтому обработчик ВСЕГДА перепроверяет права:
что заказ принадлежит этому клиенту, что человек — админ и т.д.
"""

from aiogram.filters.callback_data import CallbackData

# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------


class CardCb(CallbackData, prefix="card"):
    """
    Карусель карточек: show (перейти к карточке), plus, minus, noop (ничего),
    comp (показать состав), hide (закрыть сообщение с длинным составом).
    """

    action: str
    index: int  # номер карточки в карусели (0, 1, 2...)
    item_id: int = 0


class ListCb(CallbackData, prefix="lst"):
    """
    Полный список блюд: plus, minus, card (открыть карточку), next (к «Итого»),
    page (другая страница), noop. page — на какой странице список (см. bot/paging.py).
    """

    action: str
    item_id: int = 0
    page: int = 0


class CheckoutCb(CallbackData, prefix="chk"):
    """
    Шаги оформления: list (к списку), confirm (итого верно), privacy (понятно),
    data_ok, data_edit, delivery, pickup, keep (оставить прежнее), skip (пропустить),
    skip_point, skip_note, gift (другому получателю), pick_rec, new_rec,
    payer_me, payer_rec, add_to (дозаказ: добавить к заказу получателя rid; 0 — к своему).

    rid — id получателя из записной книжки (0 — заказ себе).
    """

    action: str
    rid: int = 0


class MyOrderCb(CallbackData, prefix="my"):
    """«Мои заказы»: cancel (спросить), cancel_yes (подтвердить), edit (вернуть в корзину), more (дозаказать)."""

    action: str
    order_id: int


class DeleteMeCb(CallbackData, prefix="delme"):
    """Удаление данных: yes / no."""

    action: str


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------


class AdmCb(CallbackData, prefix="adm"):
    """Разделы главного меню админки: home, issues, archive, catalog, archived, settings, admins, notify."""

    section: str


class DishCb(CallbackData, prefix="dish"):
    """
    Карточка в каталоге: view, new, archive, unarchive.

    issue_id — если блюдо создаётся из выпуска («➕ Блюдо → Новое блюдо»),
    после создания оно сразу добавится в этот выпуск.
    """

    action: str
    dish_id: int = 0
    issue_id: int = 0


class IssueCb(CallbackData, prefix="iss"):
    """
    Выпуск: view, new, add (выбрать блюдо), add_dish (добавить конкретное),
    preview, publish, publish_yes, update_post, orders, excel, close, finish,
    finish_yes, delete, delete_yes, leftovers, leftovers_from (из уведомления об отмене),
    close_yes, announce (объявить выдачу), announce_yes.
    """

    action: str
    issue_id: int = 0
    dish_id: int = 0
    order_id: int = 0
    page: int = 0  # страница списка заказов
    show_all: int = 0  # 1 — заказы со всеми статусами, 0 — только неотданные


class ItemCb(CallbackData, prefix="itm"):
    """
    Позиция выпуска: view, up, down, off, on, remove, remove_yes, price_all, price_new.

    value — новая цена в центах для price_all/price_new: шеф ввёл цену,
    а бот спрашивает «применить к оформленным заказам?». Цена ждёт ответа
    прямо в кнопке — так не нужно хранить её где-то ещё.
    """

    action: str
    item_id: int
    value: int = 0


class AOrderCb(CallbackData, prefix="aord"):
    """Заказ в админке: view, delivered, cancel, cancel_yes, extra, del_extra."""

    action: str
    order_id: int
    extra_id: int = 0


class FieldCb(CallbackData, prefix="fld"):
    """
    Универсальная кнопка «изменить поле».

    entity — что меняем: dish, item, issue, setting;
    obj_id — id объекта (для setting не нужен);
    field  — имя поля или ключ настройки;
    clear  — True для кнопки «🧹 Очистить» (записать пустое значение).
    Одна кнопка и один обработчик на все поля — см. handlers/admin_fields.py.
    """

    entity: str
    obj_id: int = 0
    field: str
    clear: bool = False


class SetCb(CallbackData, prefix="set"):
    """Настройки: pickup_toggle, reset (вернуть стандартный текст; key в value)."""

    action: str
    value: str = ""


class AdminsCb(CallbackData, prefix="adms"):
    """Админы: invite, remove, remove_yes, notify_orders, notify_cancels."""

    action: str
    admin_id: int = 0


class LeftCb(CallbackData, prefix="left"):
    """Лишние порции: plus, minus, publish, cancel, page, noop."""

    action: str
    item_id: int = 0
    page: int = 0


class SetupCb(CallbackData, prefix="setup"):
    """
    Мастер первой настройки: start, later, group_check, step (перейти к шагу value),
    pickup_yes, pickup_no, change_group (инструкция по смене группы из настроек).
    """

    action: str
    value: str = ""


class ProfileCb(CallbackData, prefix="prof"):
    """«👤 Мои данные»: edit (изменить свои), del_rec / del_rec_yes (удалить получателя rid)."""

    action: str
    rid: int = 0


class StockCb(CallbackData, prefix="stk"):
    """
    «🧺 Вне лавки» у клиента: open (показать список), plus, minus, card (карточка
    блюда), list (вернуться к списку), checkout (оформить), pick (выбран заказ
    в лавке order_id — показать итог), to (добавить к нему), gift (другому
    получателю), noop.
    """

    action: str
    item_id: int = 0
    order_id: int = 0
    page: int = 0


class PickCb(CallbackData, prefix="pick"):
    """
    Выбор блюда из каталога со страницами и поиском (решение от 26.11).

    mode — где выбираем: cat (каталог), arch (архив), lavka (в выпуск issue_id),
    stock (вне лавки issue_id). action: page (страница page), search (начать
    поиск), reset (сбросить поиск). found=1 — листаем результаты поиска.
    """

    mode: str
    action: str = "page"
    issue_id: int = 0
    page: int = 0
    found: int = 0
