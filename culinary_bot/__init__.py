"""
Пакет culinary_bot — Telegram-бот «Тайной лавки шефа».

Пакет в Python — это папка с файлом __init__.py. Благодаря ему можно писать
`from culinary_bot.config import settings` из любого места проекта.

Устройство пакета (подробная карта — в docs/architecture.md):

    config.py      — настройки из .env
    texts.py       — все тексты, которые видят клиент и шеф
    db/            — таблицы базы данных и подключение к ней
    services/      — бизнес-логика: заказы, выпуски, каталог (без Telegram)
    bot/           — всё, что связано с Telegram: обработчики, кнопки, публикации
    __main__.py    — точка входа: `python -m culinary_bot` запускает бота
"""
