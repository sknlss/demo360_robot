"""Слой aiogram: обработчики и отправитель. Вся логика — в service.py."""
import logging

from aiogram import F, Router
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramServerError,
                                TelegramUnauthorizedError)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

from .errors import Blocked, Permanent, RetryAfter, Transient
from .service import UserInfo

log = logging.getLogger("funnel.bot")


class AiogramSender:
    def __init__(self, bot):
        self.bot = bot

    async def send_text(self, chat_id, text, *, phone_button=None, decline_button=None,
                        remove_keyboard=False):
        markup = None
        if phone_button:
            rows = [[KeyboardButton(text=phone_button, request_contact=True)]]
            if decline_button:
                rows.append([KeyboardButton(text=decline_button)])
            markup = ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)
        elif remove_keyboard:
            markup = ReplyKeyboardRemove()
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramRetryAfter as e:
            raise RetryAfter(e.retry_after) from e
        except TelegramForbiddenError as e:  # бот заблокирован, чат закрыт
            raise Blocked(str(e)) from e
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                raise Blocked(str(e)) from e
            raise Permanent(str(e)) from e
        except TelegramUnauthorizedError as e:
            raise Permanent(str(e)) from e
        except (TelegramNetworkError, TelegramServerError, TelegramAPIError) as e:
            raise Transient(str(e)) from e


async def profile_photo_id(bot, user_id: int):
    """file_id маленькой версии первой фотографии профиля или None."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except TelegramAPIError:
        return None
    if photos.total_count and photos.photos:
        return photos.photos[0][0].file_id
    return None


def build_router(service, settings) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def on_start(message, command: CommandObject, bot):
        u = message.from_user
        photo = await profile_photo_id(bot, u.id) if service.needs_photo(u.id) else None
        await service.start(UserInfo(u.id, u.full_name, u.username, photo), command.args)

    @router.message(Command("dashboard"))
    async def on_dashboard(message):
        if not settings.miniapp_url:
            await message.answer("Адрес дашборда не задан (MINIAPP_URL).")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть дашборд", web_app=WebAppInfo(url=settings.miniapp_url))]])
        await message.answer("Дашборд:", reply_markup=kb)

    @router.message(F.contact)
    async def on_contact(message):
        c = message.contact
        await service.contact(message.from_user.id, c.phone_number, c.user_id)

    @router.message(F.text, ~F.text.startswith("/"))
    async def on_text(message):
        # любой текст вместо контакта (в т.ч. кнопка отказа) = отказ от телефона;
        # когда телефон не ждём, service.decline ничего не делает
        await service.decline(message.from_user.id)

    return router
