"""Точка входа: python -m app. Замок берём ДО импорта aiogram/aiohttp."""
import logging
import os
import sys

from .envfile import load_env
from .lock import AlreadyRunning, ProcessLock
from .settings import EnvSettings


def main() -> int:
    load_env()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = EnvSettings.from_env()
    except ValueError as e:
        print(f"Ошибка в переменных окружения: {e}", file=sys.stderr)
        return 2
    data_dir = os.path.dirname(settings.db_path) or "."
    os.makedirs(data_dir, exist_ok=True)
    lock = ProcessLock(os.path.join(data_dir, "bot.lock"))
    try:
        lock.acquire()
    except AlreadyRunning as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1
    if not settings.bot_token:
        print("ОШИБКА: не задан BOT_TOKEN (см. .env.example)", file=sys.stderr)
        return 2
    from .runner import run  # здесь подтягиваются aiogram и aiohttp

    return run(settings)
