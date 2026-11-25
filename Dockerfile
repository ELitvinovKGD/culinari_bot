# Dockerfile — рецепт сборки образа бота.
#
# Образ — это "коробка" с Linux, Python, зависимостями и нашим кодом.
# Из образа запускается контейнер — работающий экземпляр бота.
# Одинаковый образ работает и на твоём Windows (через Docker Desktop),
# и на сервере Oracle с процессором ARM.

# Базовый образ: минимальный Debian с Python 3.12.
# "slim" — без лишних программ, образ получается меньше.
FROM python:3.12-slim

# Не писать .pyc-файлы и не буферизовать вывод:
# так логи бота сразу видны в `docker compose logs`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Рабочая папка внутри контейнера.
WORKDIR /app

# Сначала копируем ТОЛЬКО список зависимостей и устанавливаем их.
# Почему отдельно от кода: Docker кэширует каждый шаг. Пока requirements.txt
# не меняется, этот долгий шаг берётся из кэша, и пересборка после правки кода
# занимает секунды, а не минуты.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Теперь копируем код и всё, что нужно для миграций.
COPY alembic.ini .
COPY migrations ./migrations
COPY culinary_bot ./culinary_bot

# Команда при запуске контейнера:
# 1) `alembic upgrade head` — довести структуру базы до последней версии
#    (создать новые таблицы/колонки, если код обновился);
# 2) `python -m culinary_bot` — запустить бота.
# `&&` значит: бот стартует, только если миграции прошли без ошибок.
CMD ["sh", "-c", "alembic upgrade head && python -m culinary_bot"]
