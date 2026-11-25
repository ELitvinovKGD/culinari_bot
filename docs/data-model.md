# Модель данных

Таблицы описаны в `culinary_bot/db/models.py`, структура в базе создаётся
миграциями из `migrations/versions/`.

## Связи

```text
Dish (каталог) ──< IssueItem >── Issue (выпуск)
                       │
         CartItem ─────┤
                       ↓
                   OrderItem >── Order >── Customer

Admin, AdminInvite, Setting — отдельно
```

`──<` — «один ко многим»: у выпуска много позиций, у заказа много строк.

## Главный принцип: снимки

Карточка блюда → позиция выпуска → позиция заказа — три **разные записи**.
Каждая копирует название, порцию и цену из предыдущей.

- Правка карточки в каталоге не меняет уже собранные выпуски.
- Правка позиции выпуска не меняет оформленные заказы — кроме случая,
  когда шеф явно выбрал «применить новую цену к заказам».
- Контакты клиента копируются в заказ: `/delete_me` не ломает доставку.

## Таблицы

### customers — клиенты

| Колонка | Тип | Смысл |
|---|---|---|
| tg_id | bigint, уникальный | Telegram ID |
| username | текст | @username (может не быть) |
| name, phone, address | текст | Сохранённые данные для следующих заказов |
| delivery_type | `delivery` / `pickup` | Способ получения |
| privacy_accepted | bool | Видел уведомление о данных |

### admins, admin_invites — администраторы

| Колонка | Смысл |
|---|---|
| admins.is_owner | Владелец из `.env`, удалить нельзя |
| admins.notify_orders / notify_cancels | Личные переключатели уведомлений |
| admin_invites.token | Одноразовый случайный токен ссылки |
| admin_invites.used_at | Заполнен — ссылка использована |

### dishes — каталог

| Колонка | Смысл |
|---|---|
| emoji, name, description, portion | Как в постах шефа |
| price_cents | Цена в центах: 10.50 € = 1050 |
| photo_file_id | ID фото в Telegram |
| is_archived | Убрано в архив (не удаляем — на блюдо ссылаются выпуски) |

### issues — выпуски

| Колонка | Смысл |
|---|---|
| title, header, footer | Название, шапка, текст после списка |
| status | `draft` → `open` → `closed` → `finished` |
| closing_text | Текст шефа при закрытии приёма |
| group_message_ids, group_text_message_id, group_text_is_caption | Где пост в группе — чтобы обновить |
| opened_at, closed_at, finished_at | Время переходов |

Правило: одновременно `open` не больше одного выпуска (`services/issues.open_issue`).

### issue_items — позиции выпуска

| Колонка | Смысл |
|---|---|
| issue_id, dish_id | Выпуск и исходная карточка |
| position | Номер «01 /», «02 /» |
| emoji, name, description, portion, price_cents, photo_file_id | Копия из карточки, правится отдельно |
| is_active | False — снято с продажи |

### cart_items — корзины

Одна строка = позиция и количество. Уникальная пара (клиент, позиция).
Очищается после заказа и при закрытии приёма.

### orders — заказы

| Колонка | Смысл |
|---|---|
| id | Номер заказа, сквозной |
| status | `accepted` / `delivered` / `cancelled` |
| name, phone, username, delivery_type, address, comment | Снимок данных клиента |
| group_message_id, group_message_sent_at | Пост о заказе в группе (удаляется при отмене в течение 48 ч) |
| cancelled_by | `customer` / `admin` |

Сумма не хранится — считается из позиций (`Order.total_cents`), чтобы не разойтись с ними.

### order_items — позиции заказа

| Колонка | Смысл |
|---|---|
| issue_item_id | Позиция выпуска (пусто у позиций шефа) |
| name, portion, price_cents, quantity | Снимок на момент заказа |
| is_extra | Добавлено шефом («Наливка ×1»): в сумме и доставке, но не в производстве |

### settings — настройки шефа

Пары ключ → значение: `group_chat_id`, `pickup_enabled`, `pickup_address`,
`order_post_header`, `order_post_footer`. Значения по умолчанию — в `services/settings.py`.
