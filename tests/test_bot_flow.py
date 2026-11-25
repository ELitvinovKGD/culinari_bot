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

    # Клиент: «✉️ Написать шефу» ведёт на контакт.
    await h.send(CLIENT, "✉️ Написать шефу")
    assert h.fake.calls[-1].reply_markup.inline_keyboard[0][0].url == "https://t.me/chef_anton"
