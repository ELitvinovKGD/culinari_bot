"""
Команды в группе: /connect — подключить группу к боту (сценарий А13).

Бот запоминает ID группы и дальше публикует туда посты выпусков,
анонимные посты о заказах и лишние порции.
"""

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from culinary_bot import texts
from culinary_bot.bot import keyboards, publisher
from culinary_bot.bot.callbacks import SetupCb
from culinary_bot.services import settings as settings_service

router = Router(name="group")
# Этот роутер работает только в группах и супергруппах.
router.message.filter(F.chat.type.in_({"group", "supergroup"}))


@router.message(Command("connect"))
async def cmd_connect(message: Message, session: AsyncSession, is_admin: bool, bot: Bot) -> None:
    """
    /connect в группе.

    БЕЗОПАСНОСТЬ: команду принимаем только от админа бота — иначе любой
    участник мог бы «перетянуть» публикации в свою группу.
    Не-админу ничего не отвечаем, чтобы не засорять группу.
    """
    if not is_admin:
        return

    # Проверяем, что бот в группе — администратор с нужными правами.
    me = await bot.get_chat_member(message.chat.id, bot.id)
    if me.status != ChatMemberStatus.ADMINISTRATOR:
        await message.answer("Сделайте бота администратором группы (права: публиковать и удалять сообщения) и повторите /connect.")
        return
    if not getattr(me, "can_delete_messages", False):
        await message.answer("Боту нужно право «Удалять сообщения» — без него не получится убирать посты отменённых заказов.")
        return

    await settings_service.set_value(session, settings_service.GROUP_CHAT_ID, str(message.chat.id))
    await message.answer("Группа подключена ✅ Сюда будут публиковаться выпуски и заказы.")
    # Пишем админу в личку: если он шёл по мастеру настройки — следующий шаг
    # под рукой, возвращаться в бота и искать кнопку не нужно.
    await publisher.send_safe(
        bot,
        message.from_user.id,
        f"Группа «{texts.esc(message.chat.title)}» подключена ✅",
        keyboards.setup_buttons(("Дальше: контакт для клиентов →", SetupCb(action="step", value="contact"))),
    )
