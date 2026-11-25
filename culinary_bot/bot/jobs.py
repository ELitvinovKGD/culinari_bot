"""
Фоновые задачи: то, что бот делает сам, без нажатия кнопки.

Сейчас задача одна — «подтверждение» заказов после окна правки.
Клиент оформил заказ → 10 минут может нажать «✏️ Изменить» → потом заказ
«сформирован»: пост в группу и уведомление шефу.

Почему не таймер на каждый заказ (asyncio.sleep(600)): таймеры живут в памяти
и пропадут при перезапуске бота — заказ так и не дошёл бы до шефа.
Вместо этого раз в 30 секунд смотрим в базу: «есть заказы, у которых окно
истекло, а подтверждения нет?». База переживает перезапуск, значит и задача
ничего не потеряет: после рестарта просто доделает пропущенное.
"""

import asyncio
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from culinary_bot import texts
from culinary_bot.bot import keyboards, publisher
from culinary_bot.db.models import Order
from culinary_bot.services import orders as order_service

log = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 30


async def finalize_order(bot: Bot, session: AsyncSession, order: Order) -> None:
    """
    Подтвердить заказ: отметить, опубликовать анонимный пост, уведомить шефа.

    Вызывается фоновой задачей (окно истекло) и при закрытии приёма
    (тогда ждать конца окна уже не нужно).
    """
    order_service.mark_confirmed(order)
    # Таблицу уже скачивали — значит, она не учитывает этот заказ.
    excel_outdated = order.issue.excel_exported_at is not None
    await publisher.publish_order(bot, session, order)

    if await order_service.is_reorder(session, order):
        # Клиент добавил блюда к своему заказу — шефу «✏️ Изменение заказа»
        # с новым общим итогом по всем частям.
        parts = await order_service.customer_issue_orders(session, order.customer, order.issue)
        summaries = order_service.summarize_by_customer(parts)
        new_total = summaries[0].total_cents if summaries else order.total_cents
        text = texts.order_change_notification(order, new_total, excel_outdated=excel_outdated)
    else:
        text = texts.new_order_notification(order, excel_outdated=excel_outdated)
    await publisher.notify_admins(bot, session, text, keyboards.new_order_notification(order.id), orders=True)


async def confirm_due_orders(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], minutes: int) -> int:
    """Один проход: подтвердить все заказы с истёкшим окном. Возвращает, сколько подтверждено."""
    async with sessionmaker() as session:
        due = await order_service.orders_to_confirm(session, minutes)
        for order in due:
            await finalize_order(bot, session, order)
            # commit после КАЖДОГО заказа: если на следующем что-то упадёт,
            # уже отправленные не будут подтверждены и разосланы повторно.
            await session.commit()
        return len(due)


async def run_confirmations(bot: Bot, sessionmaker: async_sessionmaker[AsyncSession], minutes: int) -> None:
    """Бесконечный цикл фоновой задачи. Запускается из __main__.py."""
    while True:
        try:
            await confirm_due_orders(bot, sessionmaker, minutes)
        except Exception:
            # Любая ошибка не должна убить задачу навсегда: пишем в журнал
            # и пробуем снова через 30 секунд.
            log.exception("Ошибка при подтверждении заказов")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
