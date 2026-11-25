# Culinary Bot

Telegram-бот для «Тайной лавки шефа»: шеф собирает выпуск из карточек блюд,
бот публикует пост в группу, клиенты заказывают в боте, шеф получает заказы,
таблицу производства и лист доставки.

- Что строим и почему — [docs/product.md](docs/product.md)
- Сценарии по шагам — [docs/scenarios.md](docs/scenarios.md)
- Как устроен код и как его менять — [docs/architecture.md](docs/architecture.md)
- Таблицы базы — [docs/data-model.md](docs/data-model.md)
- Инструкция для шефа — [docs/chef-guide.md](docs/chef-guide.md)

## Запуск локально (Docker)

Нужен установленный и запущенный Docker Desktop.

```powershell
# 1. Создать файл настроек из шаблона и заполнить его
Copy-Item .env.example .env
notepad .env          # BOT_TOKEN, OWNER_TG_ID, POSTGRES_PASSWORD

# 2. Собрать и запустить бота и базу в фоне
docker compose up --build -d

# 3. Посмотреть журнал бота (выход — Ctrl+C, бот продолжит работать)
docker compose logs -f bot
```

В журнале должна появиться строка `Бот @имя_бота запущен`.

Остановить: `docker compose down`. Данные базы при этом сохраняются
(они в томе `pgdata`). Удалить и данные: `docker compose down -v` — осторожно.

## Первая настройка в Telegram

1. Напиши боту `/start` — у владельца (OWNER_TG_ID) появится кнопка «⚙️ Админка».
2. Добавь бота в группу администратором с правами «Публиковать» и «Удалять сообщения».
3. Напиши в группе `/connect`.
4. Дальше — по [инструкции для шефа](docs/chef-guide.md).

## Тесты

Тесты гоняются без Docker и Telegram — на базе SQLite в памяти.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest
```

## Структура

```text
culinary_bot/
├── __main__.py      запуск бота
├── config.py        настройки из .env
├── texts.py         все тексты бота
├── db/              таблицы базы
├── services/        правила: заказы, выпуски, корзина (без Telegram)
└── bot/             Telegram: кнопки, обработчики, публикации
migrations/          изменения структуры базы (Alembic)
tests/               автоматические тесты
docs/                документация
```
