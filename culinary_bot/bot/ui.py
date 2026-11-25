"""
Общие приёмы показа экранов.

replace_message — главный помощник: показать новый экран «на месте»
старого сообщения, независимо от того, было оно текстом или фото.
Используй его вместо callback.message.edit_text(...): edit_text падает
с ошибкой "there is no text in the message to edit", если под кнопкой
было фото (например, карточка блюда).
"""

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto


async def replace_message(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None, photo: str | None = None
) -> None:
    """
    Показать новый экран вместо текущего сообщения.

    Telegram умеет менять сообщение «на месте», но не умеет превращать
    текстовое сообщение в фото и обратно. Поэтому:
    - фото → фото: edit_media (меняем картинку и подпись);
    - текст → текст: edit_text;
    - тип поменялся: удаляем старое сообщение и отправляем новое.
    """
    message = callback.message
    has_photo = bool(message.photo)
    try:
        if photo and has_photo:
            await message.edit_media(InputMediaPhoto(media=photo, caption=text), reply_markup=reply_markup)
            return
        if not photo and not has_photo:
            await message.edit_text(text, reply_markup=reply_markup)
            return
    except TelegramBadRequest as error:
        # Нажали ту же кнопку дважды — содержимое не изменилось, это не ошибка.
        if "not modified" in str(error):
            return
        raise
    # Тип сообщения меняется: удалить старое и прислать новое.
    try:
        await message.delete()
    except TelegramBadRequest:
        pass  # сообщение могло быть уже удалено — не страшно
    if photo:
        await message.answer_photo(photo, caption=text, reply_markup=reply_markup)
    else:
        await message.answer(text, reply_markup=reply_markup)
