"""
Отправка сообщений «наружу»: посты в группу, уведомления админам, рассылки клиентам.

Общее правило модуля: ошибка Telegram (бота удалили из группы, клиент
заблокировал бота, сеть моргнула) НЕ должна ломать основное действие.
Заказ уже сохранён — значит, ошибку отправки ловим, записываем в журнал
и сообщаем админам, но исключение дальше не пускаем. Иначе middleware
откатил бы транзакцию, и заказ пропал бы из-за упавшего поста в группе.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards
from culinary_bot.db.models import Issue, IssueKind, Order, utcnow
from culinary_bot.services import admins as admin_service
from culinary_bot.services import orders as order_service
from culinary_bot.services import settings as settings_service

# Журнал (лог). __name__ — имя модуля, по нему в логах видно, откуда сообщение.
log = logging.getLogger(__name__)

# Ограничения Telegram.
CAPTION_LIMIT = 1024  # подпись к фото/альбому
ALBUM_LIMIT = 10  # фото в одном альбоме


def menu_payload(issue: Issue) -> str:
    """Куда ведёт «Заказать» под постом: меню лавки или «🧺 В наличии»."""
    return "stock" if issue.kind == IssueKind.STOCK else "menu"


def deep_link(bot_username: str, payload: str) -> str:
    """
    Ссылка, открывающая бота с параметром: t.me/имя_бота?start=menu.

    Когда человек переходит по ней, бот получает команду "/start menu"
    и сразу показывает меню — это и есть кнопка «Заказать» под постом.
    """
    return f"https://t.me/{bot_username}?start={payload}"


async def send_safe(bot: Bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    """
    Отправить сообщение, не падая при ошибке. Возвращает True, если доставлено.

    Типичная ошибка — клиент заблокировал бота: Telegram отвечает 403 Forbidden.
    """
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except TelegramAPIError as error:
        log.warning("Не удалось отправить сообщение в %s: %s", chat_id, error)
        return False


async def notify_admins(
    bot: Bot,
    session: AsyncSession,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    orders: bool = False,
    cancels: bool = False,
) -> None:
    """Разослать уведомление админам (с учётом их личных переключателей)."""
    for admin in await admin_service.admins_to_notify(session, orders=orders, cancels=cancels):
        await send_safe(bot, admin.tg_id, text, reply_markup)


# ---------------------------------------------------------------------------
# Пост выпуска
# ---------------------------------------------------------------------------


async def send_menu_post(bot: Bot, chat_id: int, issue: Issue, bot_username: str) -> tuple[list[int], int, bool]:
    """
    Отправить пост выпуска в чат (в группу или админу как предпросмотр).

    Формат выбирается по числу фото (решение из допроса, docs/scenarios.md А2):
    - нет фото        → текст + кнопка «Заказать»;
    - 1 фото          → фото с подписью + кнопка;
    - 2–10 фото и текст ≤ 1024 → альбом с подписью и ссылкой «Заказать»
      (под альбомом Telegram не показывает кнопки);
    - иначе           → альбом из первых 10 фото + отдельный текст с кнопкой.

    Возвращает (все id сообщений, id сообщения с текстом, текст_это_подпись).
    Исключения Telegram НЕ ловит — решать, что делать при ошибке, вызывающему.
    """
    link = deep_link(bot_username, menu_payload(issue))
    photos = [
        item.photo_file_id
        for item in issue.items
        if item.is_active and item.photo_file_id and (not item.is_stock or (item.stock_left or 0) > 0)
    ]
    plain_text = texts.menu_post_text(issue)
    button = keyboards.order_button(link)

    if not photos:
        msg = await bot.send_message(chat_id, plain_text, reply_markup=button)
        return [msg.message_id], msg.message_id, False

    if len(photos) == 1 and texts.visible_length(plain_text) <= CAPTION_LIMIT:
        msg = await bot.send_photo(chat_id, photos[0], caption=plain_text, reply_markup=button)
        return [msg.message_id], msg.message_id, True

    text_with_link = texts.menu_post_text(issue, order_link=link)
    if 2 <= len(photos) <= ALBUM_LIMIT and texts.visible_length(text_with_link) <= CAPTION_LIMIT:
        # Подпись альбома ставится на первое фото — так её показывает Telegram.
        media = [InputMediaPhoto(media=photos[0], caption=text_with_link)]
        media += [InputMediaPhoto(media=file_id) for file_id in photos[1:]]
        messages = await bot.send_media_group(chat_id, media)
        ids = [m.message_id for m in messages]
        return ids, ids[0], True

    # Фото слишком много или текст слишком длинный: альбом + отдельный текст.
    ids: list[int] = []
    album = photos[:ALBUM_LIMIT]
    if len(album) == 1:
        ids.append((await bot.send_photo(chat_id, album[0])).message_id)
    else:
        ids += [m.message_id for m in await bot.send_media_group(chat_id, [InputMediaPhoto(media=p) for p in album])]
    text_msg = await bot.send_message(chat_id, plain_text, reply_markup=button)
    ids.append(text_msg.message_id)
    return ids, text_msg.message_id, False


async def update_menu_post(bot: Bot, session: AsyncSession, issue: Issue, bot_username: str) -> str:
    """
    Обновить текст поста выпуска в группе после правок.

    Меняется только текст (цены, описания, состав). Фото альбома Telegram
    так не обновляет — если фото поменялись, нужно опубликовать пост заново.
    Возвращает текст-результат для админа.
    """
    chat_id = await settings_service.get_group_chat_id(session)
    if chat_id is None or issue.group_text_message_id is None:
        return "Пост в группе не найден — возможно, группа не подключена."
    try:
        link = deep_link(bot_username, menu_payload(issue))
        if issue.group_text_is_caption:
            # Если подпись у одиночного фото — кнопка есть, ссылка не нужна;
            # у альбома (несколько сообщений) — нужна ссылка в тексте.
            is_album = len(issue.group_message_ids or []) > 1
            caption = texts.menu_post_text(issue, order_link=link if is_album else None)
            if texts.visible_length(caption) > CAPTION_LIMIT:
                return "Текст стал длиннее 1024 символов — в подпись к фото он не помещается. Сократите шапку или описания."
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=issue.group_text_message_id,
                caption=caption,
                reply_markup=None if is_album else keyboards.order_button(link),
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=issue.group_text_message_id,
                text=texts.menu_post_text(issue),
                reply_markup=keyboards.order_button(link),
            )
    except TelegramAPIError as error:
        # "message is not modified" — текст не изменился, это не ошибка.
        if "not modified" in str(error):
            return "Пост уже актуален."
        log.warning("Не удалось обновить пост выпуска: %s", error)
        return f"Не удалось обновить пост: {error}"
    return "Пост в группе обновлён ✅"


# ---------------------------------------------------------------------------
# Пост о заказе
# ---------------------------------------------------------------------------


async def publish_order(bot: Bot, session: AsyncSession, order: Order) -> None:
    """
    Опубликовать анонимный пост о заказе в группе (сценарий Б1).

    Защита от дублей: если у заказа уже есть group_message_id — второй раз
    не публикуем. При ошибке админы получают готовый текст для ручной
    пересылки (сценарий Б3).
    """
    if order.group_message_id is not None:
        return
    chat_id = await settings_service.get_group_chat_id(session)
    header = await settings_service.get_value(session, settings_service.ORDER_POST_HEADER) or ""
    footer = await settings_service.get_value(session, settings_service.ORDER_POST_FOOTER) or ""
    text = texts.order_group_post(order, header, footer)
    if chat_id is None:
        return  # группа не подключена — шеф и так получил уведомление о заказе
    try:
        msg = await bot.send_message(chat_id, text)
    except TelegramAPIError as error:
        log.warning("Пост о заказе №%s не опубликован: %s", order.id, error)
        await notify_admins(
            bot, session, f"⚠️ Не удалось опубликовать пост о заказе №{order.id} в группе ({error}).\nТекст для пересылки:\n\n{text}"
        )
        return
    order.group_message_id = msg.message_id
    order.group_message_sent_at = utcnow()


async def delete_order_post(bot: Bot, session: AsyncSession, order: Order) -> None:
    """
    Удалить пост о заказе при отмене (сценарий Б2).

    Только если прошло меньше 48 часов — позже Telegram удалять не даёт,
    и пост остаётся (решение из допроса: «главное, чтобы шеф знал»).
    """
    chat_id = await settings_service.get_group_chat_id(session)
    if chat_id is None or not order_service.can_delete_group_post(order):
        return
    try:
        await bot.delete_message(chat_id, order.group_message_id)
    except TelegramAPIError as error:
        log.warning("Не удалось удалить пост о заказе №%s: %s", order.id, error)
