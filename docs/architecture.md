# Архитектура и рецепты изменений

Этот документ — карта проекта и набор «рецептов»: что и в каком порядке
трогать, чтобы добавить кнопку, поле, текст или таблицу.

## Три слоя

```text
Telegram ──► bot/ (обработчики, кнопки)  ──►  services/ (правила)  ──►  db/ (таблицы)
               «как показать»                  «что можно делать»        «где хранить»
```

| Слой | Папка | Знает про Telegram? | Пример |
|---|---|---|---|
| Интерфейс | `culinary_bot/bot/` | Да | «Нажали ➕ — обновить карточку» |
| Правила | `culinary_bot/services/` | Нет | «Нельзя заказать из закрытого выпуска» |
| Данные | `culinary_bot/db/` | Нет | Таблица `orders` и её колонки |

Главное правило: **обработчик не принимает решений**. Он разбирает нажатие,
вызывает функцию из `services/` и показывает результат. Если функция
нарушила правило — она бросает `ServiceError("текст для человека")`,
и обработчик просто показывает этот текст.

Почему так:

- правила в одном месте, их легко найти и проверить тестом (`tests/test_orders.py`);
- Telegram можно заменить или добавить микросайт — правила останутся;
- обработчики короткие и похожи друг на друга.

## Путь одного нажатия

Клиент нажимает «+» на карточке супа:

```text
1. Telegram присылает callback "card:plus:0:15"
2. DbSessionMiddleware  — открывает сессию базы
3. ActorMiddleware      — находит/создаёт Customer, проверяет, админ ли
4. Фильтры роутеров     — admin-роутер пропускает (не админ), client_menu ловит CardCb
5. client_menu.cb_card  — проверяет, что позиция из текущего выпуска
6. cart.change_quantity — правило: выпуск открыт, блюдо в продаже, количество 0..50
7. card_view + replace_message — карточка перерисовывается с новым количеством
8. DbSessionMiddleware  — commit: изменения сохранены
```

Если на любом шаге случилось исключение — `rollback`, база не меняется.

## Карта файлов

| Файл | Что внутри |
|---|---|
| `__main__.py` | Запуск: база, бот, middleware, роутеры, polling |
| `config.py` | Настройки из `.env` |
| `texts.py` | Все тексты и форматирование сообщений |
| `db/models.py` | Таблицы (см. `data-model.md`) |
| `services/catalog.py` | Каталог блюд |
| `services/issues.py` | Выпуски, позиции, переходы состояний |
| `services/cart.py` | Корзина |
| `services/orders.py` | Заказы, отмена, позиции шефа, итоги по клиентам |
| `services/customers.py` | Клиенты, телефон, удаление данных |
| `services/admins.py` | Админы и приглашения |
| `services/settings.py` | Настройки шефа (группа, самовывоз, тексты) |
| `services/export.py` | Excel |
| `services/money.py` | Цены: разбор и вывод |
| `services/geocode.py` | Адрес по точке на карте (OpenStreetMap), необязательная подсказка |
| `bot/middlewares.py` | Сессия базы и «кто пишет» |
| `bot/filters.py` | Фильтр `IsAdmin` |
| `bot/callbacks.py` | Форматы данных инлайн-кнопок |
| `bot/keyboards.py` | Все клавиатуры |
| `bot/publisher.py` | Посты в группу, уведомления, рассылки |
| `bot/jobs.py` | Фоновая задача: подтверждение заказов после окна правки |
| `bot/ui.py` | `replace_message` — показать экран на месте старого (текст или фото) |
| `bot/handlers/start.py` | `/start`, нижние кнопки, `/delete_me` |
| `bot/handlers/client_menu.py` | Карусель, список, «Итого» |
| `bot/handlers/client_checkout.py` | Ввод данных и создание заказа (FSM) |
| `bot/handlers/client_orders.py` | «Мои заказы», отмена |
| `bot/handlers/admin_main.py` | Меню админки, настройки, админы |
| `bot/handlers/admin_setup.py` | Мастер первой настройки и чек-лист готовности |
| `bot/handlers/admin_fields.py` | Универсальная правка полей |
| `bot/handlers/admin_catalog.py` | Каталог и создание блюда |
| `bot/handlers/admin_issues.py` | Выпуски, публикация, закрытие, лишние порции |
| `bot/handlers/admin_orders.py` | Заказ в админке, позиции шефа |
| `bot/handlers/group.py` | `/connect` в группе |

## Термины, которые встречаются в коде

- **Роутер** — набор обработчиков. Порядок подключения роутеров важен: событие получает первый подходящий (`bot/handlers/__init__.py`).
- **Middleware** — код, который выполняется до каждого обработчика.
- **FSM (машина состояний)** — память «на каком шаге диалога пользователь», чтобы понять, что следующее сообщение — это телефон, а не имя.
- **Callback data** — короткая строка, зашитая в инлайн-кнопку.
- **Сессия и транзакция** — изменения в базе сохраняются все вместе в конце обработки или не сохраняются вовсе.
- **Миграция** — файл, который меняет структуру базы (`migrations/versions/`).
  Иногда миграция меняет и данные: см. `UPDATE orders SET confirmed_at = …`
  во второй миграции — старые заказы помечены подтверждёнными, чтобы шефу
  не пришли повторные уведомления.

---

# Рецепты

## 1. Поменять текст

Все тексты — в `culinary_bot/texts.py`. Найди фразу поиском (Ctrl+Shift+F в VS Code),
поменяй, перезапусти бота (`docker compose up --build -d`). Логику трогать не нужно.

Если вставляешь в текст то, что ввёл человек, — оборачивай в `esc(...)`.

## 2. Добавить кнопку, которая что-то делает

Пример: кнопка «📞 Позвонить клиенту» в карточке заказа админа.

1. **Данные кнопки.** Если подходит существующий класс в `bot/callbacks.py`
   (здесь `AOrderCb`) — просто придумай новое `action`, например `"call"`.
   Иначе — новый класс `class MyCb(CallbackData, prefix="my2")`.
2. **Кнопка.** В `bot/keyboards.py`, в функции `admin_order`:
   ```python
   kb.button(text="📞 Позвонить", callback_data=AOrderCb(action="call", order_id=o.id))
   ```
3. **Обработчик.** В `bot/handlers/admin_orders.py`, внутри `cb_order`, новая ветка
   `elif action == "call": ...`. Или отдельная функция:
   ```python
   @router.callback_query(AOrderCb.filter(F.action == "call"))
   async def cb_call(callback: CallbackQuery, callback_data: AOrderCb, session: AsyncSession) -> None:
       order = await order_service.get_order(session, callback_data.order_id)
       await callback.message.answer(f"Телефон: {order.phone}")
       await callback.answer()
   ```
4. **Не забудь `await callback.answer()`** — иначе у кнопки будут крутиться часики.
5. Если действие меняет данные по правилам — правило пиши в `services/`, а не в обработчике.

## 3. Добавить поле блюду (например, «аллергены»)

1. `db/models.py` — в классы `Dish` и `IssueItem` (позиция выпуска копирует поля блюда):
   ```python
   allergens: Mapped[str | None] = mapped_column(Text)
   ```
2. Миграция (см. рецепт 5) — колонка появится в базе.
3. `services/catalog.py` — добавь `"allergens"` в `EDITABLE_FIELDS`;
   `services/issues.py` — в `EDITABLE_ITEM_FIELDS` и в `add_dish_to_issue` (копирование из `dish`).
4. `bot/handlers/admin_fields.py` — вопрос в `FIELD_PROMPTS["allergens"]`,
   при желании — в `CLEARABLE`.
5. `bot/keyboards.py` — кнопка `("allergens", "Аллергены")` в списках функций `dish` и `item`.
6. `texts.py` — показать поле в `item_block`, если оно заполнено.
7. Тест: `pytest`.

Шаги 3–5 и есть «универсальная правка полей»: отдельный обработчик писать не нужно.

## 4. Добавить настройку шефа (например, «мин. сумма заказа»)

1. `services/settings.py` — ключ `MIN_ORDER = "min_order"` и значение в `DEFAULTS`.
2. Кнопка в `keyboards.settings`: `FieldCb(entity="setting", field="min_order")`.
3. Вопрос в `admin_fields.FIELD_PROMPTS`.
4. Читай где нужно: `await settings_service.get_value(session, settings_service.MIN_ORDER)`.

Миграция не нужна — настройки хранятся парами «ключ → значение».

## 5. Изменить структуру базы (миграция)

После изменения классов в `db/models.py`:

```powershell
# Запустить базу (если не запущена)
docker compose up -d db

# Создать файл миграции: Alembic сравнит models.py с базой и запишет разницу
docker compose run --rm bot alembic revision --autogenerate -m "add allergens to dishes"
```

Файл появится в `migrations/versions/` — **открой и проверь** его:
autogenerate иногда ошибается (например, переименование видит как
«удалить колонку + создать новую», и данные пропадут).

Применяется миграция сама при запуске бота (`alembic upgrade head` в Dockerfile).
Откатить последнюю: `docker compose run --rm bot alembic downgrade -1`.

Миграцию коммить вместе с изменением `models.py` — это одно изменение.

## 6. Новый шаг диалога (FSM)

Пример — ввод данных клиента в `client_checkout.py`:

1. Состояние в классе `StatesGroup`: `floor = State()`.
2. Перед вопросом: `await state.set_state(Checkout.floor)`.
3. Обработчик ответа: `@router.message(Checkout.floor, F.text)`.
4. Ответ сохранить: `await state.update_data(floor=message.text)`,
   прочитать потом: `data = await state.get_data()`.
5. В конце диалога — `await state.clear()`.

## 7. Действие «потом» (фоновая задача)

Пример — подтверждение заказа через 10 минут (`bot/jobs.py`).
Не используй `asyncio.sleep(600)` на каждый случай: таймеры в памяти пропадут
при перезапуске бота. Правильно: записать в базу «когда» (например, `created_at`
и пустое `confirmed_at`), а фоновая задача раз в N секунд ищет в базе то,
что пора сделать, и делает. Перезапуск ничего не теряет.

## 8. Проверить, что ничего не сломалось

```powershell
pytest                 # все тесты
pytest -k cancel       # только тесты, в имени которых есть "cancel"
```

`tests/test_bot_flow.py` прогоняет весь путь от создания блюда до закрытия
приёма с «поддельным» Telegram — если сломалась кнопка или обработчик, он упадёт.

Новое правило в `services/` → новый тест в `tests/test_orders.py`
по образцу соседних: подготовка → действие → `assert`.
