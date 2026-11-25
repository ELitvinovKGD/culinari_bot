"""
Сквозной тест бота целиком — без настоящего Telegram.

Идея: подменяем «сеть» бота (session) на заглушку FakeSession. Бот думает,
что отправляет запросы в Telegram, а на самом деле заглушка записывает их
в список и возвращает правдоподобные ответы. Обновления (сообщения
и нажатия кнопок) мы скармливаем диспетчеру сами через dp.feed_update.

Так проверяется весь путь: middleware → фильтры → обработчики → services → база,
и ловятся ошибки, которые юнит-тесты services не видят (опечатка в имени
кнопки, неверный параметр обработчика и т.п.).

Сценарий: шеф создаёт блюда и выпуск → подключает группу → публикует →
клиент заказывает → шеф видит заказ → клиент отменяет → шеф закрывает приём.
"""

import itertools
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    EditMessageText,
    GetChatMember,
    GetMe,
    SendDocument,
    SendMediaGroup,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, ChatMemberAdministrator, Location, Message, PhotoSize, Update, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from culinary_bot.bot import jobs
from culinary_bot.bot.callbacks import AdmCb, CardCb, CheckoutCb, DishCb, FieldCb, IssueCb, ListCb, MyOrderCb
from culinary_bot.bot.handlers import build_router
from culinary_bot.bot.middlewares import ActorMiddleware, DbSessionMiddleware
from culinary_bot.db.models import Dish, Issue, IssueStatus, Order, OrderStatus
from culinary_bot.services import admins as admin_service

OWNER = 1
CLIENT = 2
GROUP = -100500
BOT_ID = 999

_ids = itertools.count(1000)


class FakeSession(BaseSession):
    """Заглушка «сети»: записывает все вызовы Telegram API и отвечает правдоподобно."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        # id сообщений-фото: у них нет текста, и Telegram не даёт делать им edit_text.
        self.photo_message_ids: set[int] = set()

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        # Как настоящий Telegram: больше 100 кнопок под сообщением — ошибка
        # («reply markup is too long»). Ловит списки без страниц.
        markup = getattr(method, "reply_markup", None)
        if markup is not None and hasattr(markup, "inline_keyboard"):
            buttons = sum(len(row) for row in markup.inline_keyboard)
            if buttons > 100:
                raise TelegramBadRequest(method=method, message=f"Bad Request: reply markup is too long ({buttons})")
        # Ведём себя как настоящий Telegram: edit_text у фото — ошибка.
        if isinstance(method, EditMessageText) and method.message_id in self.photo_message_ids:
            raise TelegramBadRequest(method=method, message="Bad Request: there is no text in the message to edit")
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, GetChatMember):
            # model_construct — создать объект без проверки всех обязательных полей
            # (их у админа группы десятки, а нам важны два).
            return ChatMemberAdministrator.model_construct(
                status="administrator", user=User(id=BOT_ID, is_bot=True, first_name="Bot"), can_delete_messages=True
            )
        if isinstance(method, SendMediaGroup):
            return [fake_message(method.chat_id) for _ in method.media]
        chat_id = getattr(method, "chat_id", None)
        if chat_id is not None and type(method).__name__.startswith(("Send", "Edit")):
            return fake_message(chat_id)
        if isinstance(method, SendDocument):
            return fake_message(method.chat_id)
        return True

    async def close(self):  # pragma: no cover - нечего закрывать
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    def texts(self) -> list[str]:
        """Все тексты, которые бот отправил или подставил при редактировании."""
        out = []
        for call in self.calls:
            for attr in ("text", "caption"):
                value = getattr(call, attr, None)
                if isinstance(value, str):
                    out.append(value)
        return out


def fake_message(chat_id, text: str = "", user_id: int | None = None, photo: bool = False) -> Message:
    chat_type = "supergroup" if chat_id and int(chat_id) < 0 else "private"
    return Message(
        message_id=next(_ids),
        date=datetime.now(timezone.utc),
        chat=Chat(id=int(chat_id), type=chat_type),
        from_user=User(id=user_id or BOT_ID, is_bot=user_id is None, first_name="X", username=f"u{user_id}"),
        text=None if photo else (text or None),
        photo=[PhotoSize(file_id="ph", file_unique_id="u", width=10, height=10)] if photo else None,
        caption=text if photo else None,
    )


class Harness:
    """Удобные методы «пользователь пишет», «нажимает кнопку», «прошло 10 минут»."""

    def __init__(self, dp: Dispatcher, bot: Bot, fake: FakeSession) -> None:
        self.dp, self.bot, self.fake = dp, bot, fake

    async def send(self, user_id: int, text: str, chat_id: int | None = None) -> None:
        msg = fake_message(chat_id or user_id, text, user_id)
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), message=msg))

    async def time_passes(self) -> None:
        """Окно правки истекло: фоновая задача подтверждает заказы (minutes=0 — сразу)."""
        await jobs.confirm_due_orders(self.bot, self.maker, minutes=0)

    async def location(self, user_id: int, lat: float, lon: float) -> None:
        """Клиент отправил точку на карте."""
        msg = fake_message(user_id, user_id=user_id).model_copy(update={"text": None, "location": Location(latitude=lat, longitude=lon)})
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), message=msg))

    async def click(self, user_id: int, data, photo: bool = False) -> None:
        """Нажатие кнопки. photo=True — кнопка под сообщением-фото (как у карточки блюда)."""
        message = fake_message(user_id, photo=photo)
        if photo:
            self.fake.photo_message_ids.add(message.message_id)
        cb = CallbackQuery(
            id=str(next(_ids)),
            from_user=User(id=user_id, is_bot=False, first_name="X", username=f"u{user_id}"),
            chat_instance="ci",
            message=message,
            data=data.pack(),
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), callback_query=cb))


@pytest.fixture
async def harness(session):
    engine = session.bind
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        await admin_service.ensure_owner(s, OWNER)
        await s.commit()
    fake = FakeSession()
    bot = Bot("42:TEST", session=fake)
    dp = Dispatcher(storage=MemoryStorage(), bot_username="test_bot")
    dp.update.outer_middleware(DbSessionMiddleware(maker))
    dp.update.outer_middleware(ActorMiddleware())
    # Роутеры обработчиков — объекты уровня модуля, и aiogram не даёт
    # подключить один роутер к двум диспетчерам. В боте диспетчер один,
    # а в тестах — новый на каждый тест, поэтому «отцепляем» роутеры вручную.
    from culinary_bot.bot import handlers as handlers_pkg

    for name in dir(handlers_pkg):
        module = getattr(handlers_pkg, name)
        router = getattr(module, "router", None)
        if router is not None:
            router._parent_router = None
    dp.include_router(build_router())
    h = Harness(dp, bot, fake)
    h.maker = maker
    return h


async def test_full_issue_flow(harness):
    h = harness

    # --- Шеф: два блюда в каталоге -------------------------------------
    await h.send(OWNER, "/admin")
    for name, price in [("Гороховый суп", "10"), ("Биточки", "12,5")]:
        await h.click(OWNER, DishCb(action="new"))
        await h.send(OWNER, name)  # название
        await h.click(OWNER, DishCb(action="skip"))  # эмодзи
        await h.send(OWNER, "Очень вкусно")  # описание
        await h.send(OWNER, "горох, рёбрышки, лук")  # состав
        await h.send(OWNER, "700 г")  # порция
        await h.send(OWNER, price)  # цена
        await h.click(OWNER, DishCb(action="skip"))  # фото
    async with h.maker() as s:
        dishes = list(await s.scalars(select(Dish)))
    assert sorted(d.price_cents for d in dishes) == [1000, 1250]

    # --- Шеф: выпуск ----------------------------------------------------
    await h.click(OWNER, IssueCb(action="new"))
    await h.send(OWNER, "GREAT HITS")
    await h.send(OWNER, "Last call, хомячки мои!")  # шапка спрашивается сразу после названия
    async with h.maker() as s:
        issue = await s.scalar(select(Issue))
        assert issue.header == "Last call, хомячки мои!"
    for d in dishes:
        await h.click(OWNER, IssueCb(action="add_dish", issue_id=issue.id, dish_id=d.id))
    await h.click(OWNER, IssueCb(action="preview", issue_id=issue.id))
    assert "Всё верно?" in h.fake.texts()[-1]  # кнопки под предпросмотром

    # Публикация без группы — отказ; подключаем группу и публикуем.
    await h.click(OWNER, IssueCb(action="publish", issue_id=issue.id))
    await h.send(OWNER, "/connect", chat_id=GROUP)
    await h.click(OWNER, IssueCb(action="publish_yes", issue_id=issue.id))
    async with h.maker() as s:
        issue = await s.get(Issue, issue.id)
        assert issue.status == IssueStatus.OPEN
        assert issue.group_text_message_id is not None
        soup, patties = issue.items

    # --- Клиент заказывает ---------------------------------------------
    await h.send(CLIENT, "/start menu")
    await h.click(CLIENT, CardCb(action="comp", index=0, item_id=soup.id))
    assert any("Состав: горох" in t for t in h.fake.texts())
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=soup.id))
    await h.click(CLIENT, CardCb(action="show", index=1))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="plus", item_id=patties.id))
    await h.click(CLIENT, ListCb(action="plus", item_id=patties.id))
    await h.click(CLIENT, ListCb(action="next"))
    await h.click(CLIENT, CheckoutCb(action="confirm"))
    await h.click(CLIENT, CheckoutCb(action="privacy"))
    await h.send(CLIENT, "Ева")
    await h.send(CLIENT, "abc")  # неверный телефон — бот переспросит
    await h.send(CLIENT, "+995 555 123 456")
    await h.click(CLIENT, CheckoutCb(action="delivery"))
    await h.location(CLIENT, 42.28, 18.84)  # точка на карте
    await h.send(CLIENT, "подъезд 2, этаж 3")  # уточнение
    await h.send(CLIENT, "позвонить за час")

    async with h.maker() as s:
        order = await s.scalar(select(Order))
        assert order is not None, "заказ не создан"
        assert order.total_cents == 1000 + 2 * 1250
        assert order.map_url and order.address == "подъезд 2, этаж 3"
        assert order.group_message_id is None  # окно правки: пост ещё не отправлен
    assert not any("Новый заказ" in t for t in h.fake.texts())
    assert "№" not in [t for t in h.fake.texts() if "Ваш заказ принят" in t][0]  # клиенту без номера

    await h.time_passes()
    async with h.maker() as s:
        order = await s.get(Order, order.id)
        assert order.confirmed_at is not None
        assert order.group_message_id is not None  # пост в группе опубликован
    sent = "\n".join(h.fake.texts())
    assert "Поступил заказ" in sent
    assert "Новый заказ" in sent
    assert "+995" not in [t for t in h.fake.texts() if "Поступил заказ" in t][0]  # пост анонимный

    # --- Клиент смотрит заказы и отменяет -------------------------------
    await h.send(CLIENT, "🧾 Мои заказы")
    await h.click(CLIENT, MyOrderCb(action="cancel", order_id=order.id))
    await h.click(CLIENT, MyOrderCb(action="cancel_yes", order_id=order.id))
    async with h.maker() as s:
        order = await s.get(Order, order.id)
        assert order.status == OrderStatus.CANCELLED
    assert any("отменён" in t for t in h.fake.texts())

    # --- Повторный заказ: данные сохранены ------------------------------
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=soup.id))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="next"))
    await h.click(CLIENT, CheckoutCb(action="confirm"))
    await h.click(CLIENT, CheckoutCb(action="data_ok"))
    await h.click(CLIENT, CheckoutCb(action="skip"))
    async with h.maker() as s:
        orders = list(await s.scalars(select(Order).order_by(Order.id)))
        assert len(orders) == 2 and orders[1].status == OrderStatus.ACCEPTED

    # «✏️ Изменить» в окне правки: заказ уходит обратно в корзину, затем оформляем снова.
    await h.click(CLIENT, MyOrderCb(action="edit", order_id=orders[1].id))
    async with h.maker() as s:
        assert await s.get(Order, orders[1].id) is None
    await h.click(CLIENT, ListCb(action="next"))
    await h.click(CLIENT, CheckoutCb(action="confirm"))
    await h.click(CLIENT, CheckoutCb(action="data_ok"))
    await h.click(CLIENT, CheckoutCb(action="skip"))
    async with h.maker() as s:
        orders = list(await s.scalars(select(Order).order_by(Order.id)))
        assert len(orders) == 2 and orders[1].status == OrderStatus.ACCEPTED

    # --- Чужой клиент не может отменить чужой заказ --------------------
    await h.click(3, MyOrderCb(action="cancel_yes", order_id=orders[1].id))
    async with h.maker() as s:
        assert (await s.get(Order, orders[1].id)).status == OrderStatus.ACCEPTED

    # --- Не-админ не попадает в админку ---------------------------------
    calls_before = len(h.fake.calls)
    await h.click(CLIENT, AdmCb(section="issues"))
    assert any("устарела" in (getattr(c, "text", "") or "") for c in h.fake.calls[calls_before:])

    # --- Шеф: Excel, закрытие приёма, завершение ------------------------
    await h.click(OWNER, IssueCb(action="excel", issue_id=issue.id))
    await h.click(OWNER, IssueCb(action="close", issue_id=issue.id))
    await h.click(OWNER, IssueCb(action="close_yes", issue_id=issue.id))
    async with h.maker() as s:
        assert (await s.get(Issue, issue.id)).status == IssueStatus.CLOSED
        # Неподтверждённый (в окне правки) заказ подтверждён сразу при закрытии.
        assert (await s.get(Order, orders[1].id)).confirmed_at is not None
    assert any("Приём заказов" in t and "закрыт" in t for t in h.fake.texts())
    # Первый заказ клиент отменил, поэтому этот — «Новый заказ», а не дозаказ.
    # Excel уже скачивали — в уведомлении предупреждение.
    assert any("Новый заказ" in t and "Таблица изменилась" in t for t in h.fake.texts())

    await h.click(OWNER, IssueCb(action="announce", issue_id=issue.id))
    await h.send(OWNER, "Всё готово, развожу во вторник")
    await h.click(OWNER, IssueCb(action="announce_yes", issue_id=issue.id))
    assert any("развожу во вторник" in t and "К оплате" in t for t in h.fake.texts())

    await h.click(OWNER, IssueCb(action="finish_yes", issue_id=issue.id))
    async with h.maker() as s:
        assert (await s.get(Issue, issue.id)).status == IssueStatus.FINISHED

    # Лавка закрыта — клиент видит понятное сообщение.
    await h.send(CLIENT, "🍽 Меню")
    assert "Шеф ещё не открыл лавку" in h.fake.texts()[-1]


async def test_admin_tools(harness):
    """Правки открытого выпуска, позиции от шефа, лишние порции, приглашение админа."""
    from culinary_bot.bot.callbacks import AdminsCb, AOrderCb, ItemCb, LeftCb
    from culinary_bot.db.models import AdminInvite

    h = harness
    # Быстрая подготовка: блюдо, выпуск, группа, публикация.
    await h.click(OWNER, DishCb(action="new"))
    await h.send(OWNER, "Суп")
    for _ in range(4):  # эмодзи, описание, состав, порция
        await h.click(OWNER, DishCb(action="skip"))
    await h.send(OWNER, "10")
    await h.click(OWNER, DishCb(action="skip"))
    await h.click(OWNER, IssueCb(action="new"))
    await h.send(OWNER, "Выпуск")
    await h.click(OWNER, IssueCb(action="view", issue_id=1))  # «Пропустить» шапку
    async with h.maker() as s:
        issue = await s.scalar(select(Issue))
        dish = await s.scalar(select(Dish))
    await h.click(OWNER, IssueCb(action="add_dish", issue_id=issue.id, dish_id=dish.id))
    await h.send(OWNER, "/connect", chat_id=GROUP)
    await h.click(OWNER, IssueCb(action="publish_yes", issue_id=issue.id))
    async with h.maker() as s:
        item = (await s.get(Issue, issue.id)).items[0]

    # Клиент заказывает 2 супа.
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=item.id))
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=item.id))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="next"))
    await h.click(CLIENT, CheckoutCb(action="confirm"))
    await h.click(CLIENT, CheckoutCb(action="privacy"))
    await h.send(CLIENT, "Ева")
    await h.send(CLIENT, "557692377")
    await h.click(CLIENT, CheckoutCb(action="pickup"))
    await h.click(CLIENT, CheckoutCb(action="skip"))
    async with h.maker() as s:
        order = await s.scalar(select(Order))
    assert order.total_cents == 2000
    await h.time_passes()

    # Шеф меняет цену и применяет к заказам.
    await h.click(OWNER, FieldCb(entity="item", obj_id=item.id, field="price_cents"))
    await h.send(OWNER, "12")
    await h.click(OWNER, ItemCb(action="price_all", item_id=item.id, value=1200))
    async with h.maker() as s:
        assert (await s.get(Order, order.id)).total_cents == 2400
    assert any("новый итог" in t for t in h.fake.texts())

    # Обновить пост в группе.
    await h.click(OWNER, IssueCb(action="update_post", issue_id=issue.id))

    # Позиция от шефа.
    await h.click(OWNER, AOrderCb(action="extra", order_id=order.id))
    await h.send(OWNER, "Наливка")
    await h.send(OWNER, "1")
    await h.send(OWNER, "8")
    async with h.maker() as s:
        assert (await s.get(Order, order.id)).total_cents == 2400 + 800

    # Лишние порции: без контакта шефа публикация не проходит.
    await h.click(OWNER, IssueCb(action="leftovers_from", issue_id=issue.id, order_id=order.id))
    await h.click(OWNER, LeftCb(action="plus", item_id=item.id))
    await h.click(OWNER, LeftCb(action="publish"))
    assert not any("Остались свободные порции" in t for t in h.fake.texts())
    # Указываем контакт в настройках — кнопка «Написать шефу» ведёт к шефу.
    await h.click(OWNER, FieldCb(entity="setting", field="chef_contact"))
    await h.send(OWNER, "https://t.me/chef_anton")
    await h.click(OWNER, IssueCb(action="leftovers_from", issue_id=issue.id, order_id=order.id))
    await h.click(OWNER, LeftCb(action="plus", item_id=item.id))
    await h.click(OWNER, LeftCb(action="publish"))
    assert any("Остались свободные порции" in t and "— 3" in t for t in h.fake.texts())
    leftovers_call = [c for c in h.fake.calls if "Остались свободные порции" in (getattr(c, "text", "") or "")][-1]
    assert leftovers_call.reply_markup.inline_keyboard[0][0].url == "https://t.me/chef_anton"

    # Снять блюдо с продажи — клиент получает уведомление.
    await h.click(OWNER, ItemCb(action="off", item_id=item.id))
    assert any("закончилось" in t for t in h.fake.texts())

    # Приглашение нового админа.
    await h.click(OWNER, AdminsCb(action="invite"))
    async with h.maker() as s:
        invite = await s.scalar(select(AdminInvite))
    await h.send(77, f"/start admin_{invite.token}")
    async with h.maker() as s:
        assert await admin_service.get_admin(s, 77) is not None
    # Повторно ссылка не работает.
    await h.send(78, f"/start admin_{invite.token}")
    async with h.maker() as s:
        assert await admin_service.get_admin(s, 78) is None

    # Экраны админки открываются без ошибок — в том числе по кнопке
    # под фото (регрессия: «← Каталог» под карточкой блюда с фото падал).
    for section in ("home", "issues", "archive", "catalog", "archived", "settings", "admins", "notify"):
        await h.click(OWNER, AdmCb(section=section))
        await h.click(OWNER, AdmCb(section=section), photo=True)



async def test_setup_wizard(harness):
    """Мастер первой настройки: группа → контакт → самовывоз, чек-лист сворачивается."""
    from culinary_bot.bot.callbacks import SetupCb

    h = harness
    await h.send(OWNER, "/admin")
    assert any("Готовность лавки" in t for t in h.fake.texts())
    assert any("ещё не настроена" in t for t in h.fake.texts())

    await h.click(OWNER, SetupCb(action="step", value="group"))
    await h.send(OWNER, "/connect", chat_id=GROUP)
    assert any("подключена ✅" in t for t in h.fake.texts())
    await h.click(OWNER, SetupCb(action="step", value="contact"))
    await h.send(OWNER, "шеф")  # не username — бот переспросит
    await h.send(OWNER, "@chef_anton")
    await h.click(OWNER, SetupCb(action="pickup_yes"))
    await h.send(OWNER, "Будва, ул. Морская 5")
    assert any("Готово, основное настроено" in t for t in h.fake.texts())

    # Ещё нет блюд и выпусков — чек-лист пока виден.
    await h.send(OWNER, "/admin")
    assert "Готовность лавки" in h.fake.texts()[-1]

    # Смена группы: /connect в другой группе заменяет старую.
    await h.send(OWNER, "/connect", chat_id=GROUP - 1)
    assert any("Группа заменена" in t for t in h.fake.texts())

    # Клиент: «✉️ Написать шефу» ведёт на контакт.
    await h.send(CLIENT, "✉️ Написать шефу")
    assert h.fake.calls[-1].reply_markup.inline_keyboard[0][0].url == "https://t.me/chef_anton"


async def test_reorder_adds_to_order(harness, monkeypatch):
    """
    Дозаказ = «добавить к своему заказу»: видно, что уже заказано, итог считается
    вместе, данные не спрашиваются, шефу — «✏️ Изменение заказа», в «Моих
    заказах» — одна карточка, «Отменить» отменяет всё.
    """
    from culinary_bot.services import geocode

    async def fake_reverse(lat, lon):
        return "Медитеранска 5, Будва"

    monkeypatch.setattr(geocode, "reverse", fake_reverse)
    h = harness
    await h.click(OWNER, DishCb(action="new"))
    await h.send(OWNER, "Суп")
    for _ in range(4):
        await h.click(OWNER, DishCb(action="skip"))
    await h.send(OWNER, "10")
    await h.click(OWNER, DishCb(action="skip"))
    await h.click(OWNER, IssueCb(action="new"))
    await h.send(OWNER, "Выпуск")
    await h.click(OWNER, IssueCb(action="view", issue_id=1))
    await h.click(OWNER, IssueCb(action="add_dish", issue_id=1, dish_id=1))
    await h.send(OWNER, "/connect", chat_id=GROUP)
    await h.click(OWNER, IssueCb(action="publish_yes", issue_id=1))

    # Первый заказ: 2 супа, адрес точкой — адрес подставился по карте.
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=1))
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=1))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="next"))
    await h.click(CLIENT, CheckoutCb(action="confirm"))
    await h.click(CLIENT, CheckoutCb(action="privacy"))
    await h.send(CLIENT, "Ева")
    await h.send(CLIENT, "557692377")
    await h.click(CLIENT, CheckoutCb(action="delivery"))
    await h.location(CLIENT, 42.28, 18.84)
    assert "Похоже, это" in h.fake.texts()[-1]
    await h.send(CLIENT, "подъезд 2")
    await h.click(CLIENT, CheckoutCb(action="skip"))
    await h.time_passes()
    async with h.maker() as s:
        first = await s.scalar(select(Order))
        assert first.address == "Медитеранска 5, Будва, подъезд 2"

    # Дозаказ: карточка показывает «в заказе 2», «Итого» — всё вместе.
    await h.send(CLIENT, "🍽 Меню")
    assert "В вашем заказе: 2" in h.fake.texts()[-1]
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=1))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="next"))
    summary = h.fake.texts()[-1]
    assert "Уже заказано" in summary and "Суп × 3" in summary and "30 €" in summary
    await h.click(CLIENT, CheckoutCb(action="add_to", rid=0))  # «Завершить заказ» — данные не спрашиваются
    assert "Комментарий к добавке" in h.fake.texts()[-1]
    await h.click(CLIENT, CheckoutCb(action="skip"))
    assert "Добавлено к заказу" in h.fake.texts()[-1]
    await h.time_passes()
    assert any("Изменение заказа" in t and "+ Суп × 1" in t and "30 €" in t for t in h.fake.texts())

    # «Мои заказы» — одна карточка с итогом; отмена — целиком.
    await h.send(CLIENT, "🧾 Мои заказы")
    assert "Суп × 3" in h.fake.texts()[-1]
    async with h.maker() as s:
        last = (await s.scalars(select(Order).order_by(Order.id.desc()))).first()
    await h.click(CLIENT, MyOrderCb(action="cancel_yes", order_id=last.id))
    async with h.maker() as s:
        assert all(o.status == OrderStatus.CANCELLED for o in await s.scalars(select(Order)))



async def test_gift_order(harness):
    """
    Заказ другому получателю: свой заказ + подарок Маше (адрес текстом, потом точка),
    платит получатель; в таблице подарок рядом с заказчиком; объявление — одно
    сообщение со всеми заказами; дозаказ предлагает выбрать, куда добавить.
    """
    from io import BytesIO

    from openpyxl import load_workbook

    from culinary_bot.db.models import Recipient
    from culinary_bot.services import orders as order_service
    from culinary_bot.services.export import build_issue_workbook

    h = harness
    await h.click(OWNER, DishCb(action="new"))
    await h.send(OWNER, "Суп")
    for _ in range(4):
        await h.click(OWNER, DishCb(action="skip"))
    await h.send(OWNER, "10")
    await h.click(OWNER, DishCb(action="skip"))
    await h.click(OWNER, IssueCb(action="new"))
    await h.send(OWNER, "Выпуск")
    await h.click(OWNER, IssueCb(action="view", issue_id=1))
    await h.click(OWNER, IssueCb(action="add_dish", issue_id=1, dish_id=1))
    await h.send(OWNER, "/connect", chat_id=GROUP)
    await h.click(OWNER, IssueCb(action="publish_yes", issue_id=1))

    # Свой заказ: адрес текстом, точку пропускаем.
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=1))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="next"))
    await h.click(CLIENT, CheckoutCb(action="confirm"))
    await h.click(CLIENT, CheckoutCb(action="privacy"))
    await h.send(CLIENT, "Ева")
    await h.send(CLIENT, "557692377")
    await h.click(CLIENT, CheckoutCb(action="delivery"))
    await h.send(CLIENT, "Будва, Морская 1")
    assert "Поставьте точку на карте" in h.fake.texts()[-1]
    await h.click(CLIENT, CheckoutCb(action="skip_point"))
    await h.click(CLIENT, CheckoutCb(action="skip"))

    # Подарок Маше: «Итого» предлагает выбор — к своему или другому получателю.
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=1))
    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=1))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="next"))
    await h.click(CLIENT, CheckoutCb(action="gift"))
    await h.send(CLIENT, "Маша")
    await h.send(CLIENT, "+382 67 111 222")
    await h.send(CLIENT, "Котор, Доброта 5")
    await h.location(CLIENT, 42.45, 18.76)  # точка после текстового адреса
    assert "Кто платит" in h.fake.texts()[-1]
    await h.click(CLIENT, CheckoutCb(action="payer_rec"))
    await h.click(CLIENT, CheckoutCb(action="skip"))
    assert any("Заказ получателю Маша принят" in t for t in h.fake.texts())

    async with h.maker() as s:
        rec = await s.scalar(select(Recipient))
        assert rec.name == "Маша" and rec.latitude == 42.45 and rec.address == "Котор, Доброта 5"
        orders = list(await s.scalars(select(Order).order_by(Order.id)))
        gift = orders[-1]
        assert gift.recipient_id == rec.id and gift.payer == "recipient" and gift.address == "Котор, Доброта 5"

    # Другой клиент заказывает между ними, потом Ева дозаказывает Маше.
    await h.click(3, CardCb(action="plus", index=0, item_id=1))
    await h.click(3, CheckoutCb(action="list"))
    await h.click(3, ListCb(action="next"))
    await h.click(3, CheckoutCb(action="confirm"))
    await h.click(3, CheckoutCb(action="privacy"))
    await h.send(3, "Иван")
    await h.send(3, "555123456")
    await h.click(3, CheckoutCb(action="pickup"))
    await h.click(3, CheckoutCb(action="skip"))

    await h.click(CLIENT, CardCb(action="plus", index=0, item_id=1))
    await h.click(CLIENT, CheckoutCb(action="list"))
    await h.click(CLIENT, ListCb(action="next"))
    # Заказов два (себе и Маше) — сначала только «Куда добавить?», без «Уже заказано».
    where_call = [c for c in h.fake.calls if "Куда добавить" in (getattr(c, "text", "") or "")][-1]
    assert "Уже заказано" not in where_call.text
    labels = [b.text for row in where_call.reply_markup.inline_keyboard for b in row]
    assert any("получателю: Маша" in t for t in labels) and any("К моему заказу" in t for t in labels)
    await h.click(CLIENT, CheckoutCb(action="pick_to", rid=rec.id))
    assert "Уже заказано" in h.fake.texts()[-1] and "Маша" in h.fake.texts()[-1]
    await h.click(CLIENT, CheckoutCb(action="add_to", rid=rec.id))
    await h.click(CLIENT, CheckoutCb(action="skip"))
    await h.time_passes()

    # Таблица: Ева, Ева → Маша, Иван (подарок рядом с заказчиком).
    async with h.maker() as s:
        issue = await s.get(Issue, 1)
        content = build_issue_workbook(issue, await order_service.issue_orders(s, issue))
    ws = load_workbook(BytesIO(content))["Производство"]
    headers = [ws.cell(row=1, column=c).value.split("\n")[0] for c in range(5, 8)]
    assert headers == ["Ева", "Ева → Маша 🎁", "Иван"]
    assert ws.cell(row=2, column=6).value == 3  # Маше: 2 + дозаказ 1

    # Объявление выдачи — одно сообщение Еве с обоими заказами.
    await h.click(OWNER, IssueCb(action="close_yes", issue_id=1))
    await h.click(OWNER, IssueCb(action="announce", issue_id=1))
    await h.send(OWNER, "Развожу во вторник")
    await h.send(OWNER, "Забрать в среду")
    await h.click(OWNER, IssueCb(action="announce_yes", issue_id=1))
    to_eva = [c.text for c in h.fake.calls if getattr(c, "chat_id", None) == CLIENT and "Развожу во вторник" in (getattr(c, "text", "") or "")]
    assert len(to_eva) == 1
    assert "Ваш заказ" in to_eva[0] and "Заказ получателю: Маша" in to_eva[0] and "Оплачивает получатель" in to_eva[0]
    assert "К оплате вами: 10 €" in to_eva[0]


async def test_stock_flow(harness):
    """
    «🧺 Блюда вне лавки»: шеф выставляет капусту (2 банки), клиент бронирует,
    оформляет отдельно; второй клиент добавляет к заказу в лавке; шеф сообщает
    всем и отмечает «все отданы».
    """
    from culinary_bot.bot.callbacks import AdmCb, StockCb
    from culinary_bot.db.models import IssueItem, IssueKind

    h = harness
    await h.click(OWNER, DishCb(action="new"))
    await h.send(OWNER, "Капуста")
    for _ in range(4):
        await h.click(OWNER, DishCb(action="skip"))
    await h.send(OWNER, "6")
    await h.click(OWNER, DishCb(action="skip"))
    await h.send(OWNER, "/connect", chat_id=GROUP)

    # Пусто — «всё разобрали» (старая подпись кнопки тоже работает).
    await h.send(CLIENT, "🧺 В наличии")
    assert "всё разобрали" in h.fake.texts()[-1]

    await h.click(OWNER, AdmCb(section="stock"))
    async with h.maker() as s:
        stock = await s.scalar(select(Issue).where(Issue.kind == IssueKind.STOCK))
    await h.click(OWNER, IssueCb(action="stock_add", issue_id=stock.id))
    await h.click(OWNER, IssueCb(action="stock_dish", issue_id=stock.id, dish_id=1))
    await h.send(OWNER, "2")
    await h.click(OWNER, IssueCb(action="stock_post", issue_id=stock.id))
    assert any("Осталось: 2" in t for t in h.fake.texts())
    async with h.maker() as s:
        item = await s.scalar(select(IssueItem).where(IssueItem.issue_id == stock.id))

    # Клиент: 2 банки, третья не даётся.
    await h.send(CLIENT, "/start stock")
    await h.click(CLIENT, StockCb(action="plus", item_id=item.id))
    await h.click(CLIENT, StockCb(action="plus", item_id=item.id))
    await h.click(3, StockCb(action="plus", item_id=item.id))
    assert any("всё разобрали" in (getattr(c, "text", "") or "") for c in h.fake.calls[-3:])
    await h.click(CLIENT, StockCb(action="minus", item_id=item.id))  # одну вернул

    # Отдельный заказ (лавки нет): данные → комментарий.
    await h.click(CLIENT, StockCb(action="checkout"))
    await h.click(CLIENT, CheckoutCb(action="privacy"))
    await h.send(CLIENT, "Ева")
    await h.send(CLIENT, "557692377")
    await h.click(CLIENT, CheckoutCb(action="pickup"))
    await h.click(CLIENT, CheckoutCb(action="skip"))
    assert "Шеф напишет, когда будет готово" in h.fake.texts()[-1]
    async with h.maker() as s:
        order = await s.scalar(select(Order).where(Order.issue_id == stock.id))
        assert order.total_cents == 600
        assert (await s.get(IssueItem, item.id)).stock_left == 1

    # Второй клиент: лавка с его заказом → «Куда добавить?» → к заказу в лавке.
    await h.click(OWNER, IssueCb(action="new"))
    await h.send(OWNER, "Лавка")
    async with h.maker() as s:
        lavka = await s.scalar(select(Issue).where(Issue.kind == IssueKind.LAVKA))
    await h.click(OWNER, IssueCb(action="view", issue_id=lavka.id))
    await h.click(OWNER, IssueCb(action="add_dish", issue_id=lavka.id, dish_id=1))
    await h.click(OWNER, IssueCb(action="publish_yes", issue_id=lavka.id))
    async with h.maker() as s:
        lavka_item = (await s.get(Issue, lavka.id)).items[0]
    await h.click(3, CardCb(action="plus", index=0, item_id=lavka_item.id))
    await h.click(3, CheckoutCb(action="list"))
    await h.click(3, ListCb(action="next"))
    await h.click(3, CheckoutCb(action="confirm"))
    await h.click(3, CheckoutCb(action="privacy"))
    await h.send(3, "Иван")
    await h.send(3, "555123456")
    await h.click(3, CheckoutCb(action="pickup"))
    await h.click(3, CheckoutCb(action="skip"))
    await h.click(3, StockCb(action="plus", item_id=item.id))
    await h.click(3, StockCb(action="checkout"))
    assert "Куда добавить" in h.fake.texts()[-1] and "вместе с доставкой лавки" in h.fake.texts()[-1]
    async with h.maker() as s:
        ivan_order = await s.scalar(select(Order).where(Order.issue_id == lavka.id))
    await h.click(3, StockCb(action="pick", order_id=ivan_order.id))
    assert "Уже заказано" in h.fake.texts()[-1]
    await h.click(3, StockCb(action="to", order_id=ivan_order.id))
    await h.click(3, CheckoutCb(action="skip"))
    assert "Добавлено к заказу" in h.fake.texts()[-1]
    # Чужой заказ выбрать нельзя.
    await h.click(CLIENT, StockCb(action="pick", order_id=ivan_order.id))
    assert any("уже нельзя добавить" in (getattr(c, "text", "") or "") for c in h.fake.calls[-2:])
    async with h.maker() as s:
        parts = list(await s.scalars(select(Order).where(Order.issue_id == lavka.id)))
        assert len(parts) == 2 and (await s.get(IssueItem, item.id)).stock_left == 0

    # Главный экран админки напоминает о неотданных заказах вне лавки.
    await h.send(OWNER, "/admin")
    assert any("Вне лавки: 1 заказ" in t for t in h.fake.texts()[-3:])

    # Объявление выдачи по лавке → напоминание о заказах вне лавки.
    await h.click(OWNER, IssueCb(action="close_yes", issue_id=lavka.id))
    await h.click(OWNER, IssueCb(action="announce", issue_id=lavka.id))
    await h.send(OWNER, "Забрать в среду")
    await h.click(OWNER, IssueCb(action="announce_yes", issue_id=lavka.id))
    assert any("заказ(ов) вне лавки — это сообщение им не пришло" in t for t in h.fake.texts())

    # Шеф: сообщить всем с заказами вне лавки и отметить отданными.
    await h.click(OWNER, IssueCb(action="stock_notify", issue_id=stock.id))
    await h.send(OWNER, "Капуста готова, завтра развожу")
    assert any("Капуста готова" in t and "К оплате: 6 €" in t for t in h.fake.texts())
    await h.click(OWNER, IssueCb(action="excel", issue_id=stock.id))
    await h.click(OWNER, IssueCb(action="stock_done_yes", issue_id=stock.id))
    async with h.maker() as s:
        assert (await s.get(Order, order.id)).status == OrderStatus.DELIVERED



async def test_stock_gift_goes_with_lavka(harness):
    """«🎁 Другому получателю» из «Вне лавки» при заказе в лавке — новый заказ получателю в лавке."""
    from culinary_bot.bot.callbacks import StockCb
    from culinary_bot.db.models import IssueKind
    from culinary_bot.services import cart as cart_service
    from culinary_bot.services import catalog as catalog_service
    from culinary_bot.services import customers as customer_service
    from culinary_bot.services import issues as issue_service
    from culinary_bot.services import orders as order_service

    h = harness
    async with h.maker() as s:
        soup = await catalog_service.create_dish(s, name="Суп", price_cents=1000)
        jam = await catalog_service.create_dish(s, name="Варенье", price_cents=500)
        lavka = await issue_service.create_draft(s, "Лавка")
        await issue_service.add_dish_to_issue(s, lavka, soup)
        await issue_service.open_issue(s, lavka)
        stock = await issue_service.get_stock_issue(s)
        jam_item = await issue_service.add_stock_dish(s, stock, jam, 5)
        eva = await customer_service.get_or_create(s, CLIENT, "eva")
        customer_service.save_contact(eva, name="Ева", phone="555123456", delivery_type="delivery", address="Будва 1")
        eva.privacy_accepted = True
        await cart_service.change_quantity(s, eva, lavka.items[0], +1)
        await order_service.place_order(s, eva, lavka, comment=None)
        await s.commit()
        jam_id, lavka_id = jam_item.id, lavka.id

    # В меню лавки есть переход «🧺 Ещё вне лавки».
    await h.send(CLIENT, "🍽 Меню лавки")
    labels = [b.text for row in h.fake.calls[-1].reply_markup.inline_keyboard for b in row]
    assert "🧺 Ещё вне лавки →" in labels

    await h.click(CLIENT, StockCb(action="open"))
    assert "приедет вместе с ним" in h.fake.texts()[-1]
    await h.click(CLIENT, StockCb(action="plus", item_id=jam_id))
    await h.click(CLIENT, StockCb(action="checkout"))
    await h.click(CLIENT, StockCb(action="gift"))
    await h.send(CLIENT, "Маша")
    await h.send(CLIENT, "+382 67 111 222")
    await h.send(CLIENT, "Котор 5")
    await h.click(CLIENT, CheckoutCb(action="skip_point"))
    await h.click(CLIENT, CheckoutCb(action="payer_me"))
    await h.click(CLIENT, CheckoutCb(action="skip"))
    assert "Приедет вместе с доставкой лавки" in h.fake.texts()[-1]
    async with h.maker() as s:
        gift = (await s.scalars(select(Order).order_by(Order.id.desc()))).first()
        assert gift.issue_id == lavka_id and gift.recipient_id is not None
        assert gift.issue.kind == IssueKind.LAVKA



async def test_long_lists(harness):
    """
    Длинные списки (решение от 26.11): 30 блюд в лавке, 40 в каталоге, 40 заказов —
    ни один экран не превышает лимит Telegram в 100 кнопок; есть страницы,
    «📋 Весь список», недавние, поиск по составу и фильтр заказов.
    """
    from culinary_bot.bot.callbacks import AdmCb, PickCb
    from culinary_bot.db.models import DeliveryType
    from culinary_bot.services import cart as cart_service
    from culinary_bot.services import catalog as catalog_service
    from culinary_bot.services import customers as customer_service
    from culinary_bot.services import issues as issue_service
    from culinary_bot.services import orders as order_service

    h = harness
    async with h.maker() as s:
        dishes = []
        for i in range(40):
            composition = "курица, орехи, специи" if i == 7 else None
            dishes.append(await catalog_service.create_dish(s, name=f"Блюдо {i:02d}", price_cents=1000, composition=composition))
        lavka = await issue_service.create_draft(s, "Большая лавка")
        for d in dishes[:30]:
            await issue_service.add_dish_to_issue(s, lavka, d)
        await issue_service.open_issue(s, lavka)
        for n in range(40):
            c = await customer_service.get_or_create(s, 1000 + n, None)
            customer_service.save_contact(c, name=f"К{n}", phone="555123456", delivery_type=DeliveryType.PICKUP, address=None)
            await cart_service.change_quantity(s, c, lavka.items[0], +1)
            o = await order_service.place_order(s, c, lavka, comment=None)
            if n < 10:
                order_service.mark_delivered(o)
        await s.commit()
        lavka_id = lavka.id

    # Клиент: карусель с «📋 Весь список», список страницами.
    await h.send(CLIENT, "🍽 Меню лавки")
    labels = [b.text for row in h.fake.calls[-1].reply_markup.inline_keyboard for b in row]
    assert "📋 Весь список (30)" in labels
    await h.click(CLIENT, CheckoutCb(action="list"))
    labels = [b.text for row in h.fake.calls[-2].reply_markup.inline_keyboard for b in row]
    assert "1/3" in labels
    await h.click(CLIENT, ListCb(action="page", page=2))
    await h.click(CLIENT, ListCb(action="plus", item_id=30, page=2))

    # Шеф: каталог страницами, недавние, поиск по составу.
    await h.click(OWNER, AdmCb(section="catalog"))
    catalog_call = [c for c in h.fake.calls if "Каталог блюд" in (getattr(c, "text", "") or "")][-1]
    labels = [b.text for row in catalog_call.reply_markup.inline_keyboard for b in row]
    assert any(t.startswith("🕘") for t in labels) and "🔍 Найти" in labels
    await h.click(OWNER, PickCb(mode="cat", action="search"))
    await h.send(OWNER, "Курица")
    result = h.fake.calls[-1]
    labels = [b.text for row in result.reply_markup.inline_keyboard for b in row]
    assert any("Блюдо 07" in t and "состав" in t for t in labels)
    await h.click(OWNER, PickCb(mode="lavka", issue_id=lavka_id, page=1))

    # Заказы: по умолчанию неотданные (30), страницами; «Показать все» — 40.
    await h.click(OWNER, IssueCb(action="orders", issue_id=lavka_id))
    assert "Неотданные: 30 из 40" in h.fake.texts()[-1]
    await h.click(OWNER, IssueCb(action="orders", issue_id=lavka_id, page=1))
    await h.click(OWNER, IssueCb(action="orders", issue_id=lavka_id, show_all=1))
    assert "Все: 40" in h.fake.texts()[-1]

    # Лишние порции при 30 блюдах — страницами.
    await h.click(OWNER, IssueCb(action="leftovers", issue_id=lavka_id))
    from culinary_bot.bot.callbacks import LeftCb

    await h.click(OWNER, LeftCb(action="page", page=2))
