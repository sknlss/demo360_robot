"""Настройки из переменных окружения (описание каждой — в .env.example)."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _dir_url(url: str) -> str:
    """Страница берёт данные по относительным путям (api/...), поэтому адрес дашборда должен кончаться на «/»."""
    url = url.strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.path.endswith("/"):
        return url
    return urlunsplit(parts._replace(path=parts.path + "/"))


@dataclass
class EnvSettings:
    bot_token: str
    db_path: str
    config_path: str
    web_host: str
    web_port: int
    admin_ids: frozenset
    auth_disabled: bool
    init_data_max_age: int
    api_base: str
    miniapp_url: str
    photo_cache_dir: str

    @classmethod
    def from_env(cls):
        env = os.environ.get
        db_path = env("DB_PATH", "data/bot.db")
        ids = [x for x in env("ADMIN_IDS", "").replace(" ", "").split(",") if x]
        return cls(
            bot_token=env("BOT_TOKEN", ""),
            db_path=db_path,
            config_path=env("CONFIG_PATH", "funnels.yml"),
            web_host=env("WEB_HOST", "127.0.0.1"),
            web_port=int(env("WEB_PORT", "8080")),
            admin_ids=frozenset(int(x) for x in ids),
            auth_disabled=_bool(env("DASHBOARD_AUTH_DISABLED", "0")),
            init_data_max_age=int(env("INIT_DATA_MAX_AGE", "86400")),
            api_base=env("TELEGRAM_API_BASE", ""),
            miniapp_url=_dir_url(env("MINIAPP_URL", "")),
            photo_cache_dir=env("PHOTO_CACHE_DIR", os.path.join(os.path.dirname(db_path) or ".", "photos")),
        )
