"""Сборка и запуск: опрос Telegram, петля очереди и дашборд — в одном процессе."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web

from .bot import AiogramSender, build_router
from .config import ConfigError, load_config
from .db import Database
from .photos import PhotoCache
from .service import FunnelService
from .web import make_app
from .worker import Worker

log = logging.getLogger("funnel")


async def amain(settings) -> int:
    try:
        cfg = load_config(settings.config_path)
    except ConfigError as e:
        log.error("конфиг воронок: %s", e)
        return 2
    if settings.auth_disabled:
        log.warning("!!! DASHBOARD_AUTH_DISABLED=1: дашборд открыт всем и показывает полные телефоны, только для локального запуска !!!")
    elif not settings.admin_ids:
        log.warning("ADMIN_IDS пуст: дашборд открыт всем с подписью Telegram, но полных телефонов не увидит никто")

    db = Database(settings.db_path)
    session = (AiohttpSession(api=TelegramAPIServer.from_base(settings.api_base))
               if settings.api_base else None)
    bot = Bot(settings.bot_token, session=session)
    sender = AiogramSender(bot)
    worker = Worker(db, cfg, sender)
    service = FunnelService(db, cfg, sender, notify=worker.wake)

    runner = web.AppRunner(make_app(db, cfg, settings, PhotoCache(db, bot, settings.photo_cache_dir)))
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    log.info("дашборд: http://%s:%s/", settings.web_host, settings.web_port)

    stop = asyncio.Event()
    worker_task = asyncio.create_task(worker.run_forever(stop))
    dp = Dispatcher()
    dp.include_router(build_router(service, settings))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        log.info("бот @%s запущен", me.username)
        await dp.start_polling(bot)
    finally:
        stop.set()
        await worker_task
        await runner.cleanup()
        await bot.session.close()
        db.close()
    return 0


def run(settings) -> int:
    try:
        return asyncio.run(amain(settings))
    except KeyboardInterrupt:
        return 0
