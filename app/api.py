"""Тела ответов дашборда. Без aiohttp: их собирают и web.py, и tests/preview_server.py, и тесты —
значит, JSON, который видит страница, проверяется здесь же, а не только в браузере."""
import logging

from . import stats
from .config import ConfigError, funnel_to_dict
from .config_edit import EditError, save_funnel_steps

log = logging.getLogger("funnel.api")

USERS_LIMIT = 200


def funnels_payload(conn, cfg, can_edit=False) -> dict:
    """Список воронок: структура шагов и сводка по каждой (три уровня, прогоны)."""
    out = []
    for f in cfg.funnels.values():
        d = funnel_to_dict(f)
        d["is_default"] = f.key == cfg.settings.default_funnel
        d["levels"] = stats.levels(conn, f.key)
        d["runs"] = stats.runs_summary(conn, f.key)
        out.append(d)
    # can_edit — только подсказка странице, показывать ли «Редактировать»; запись всё равно проверяется сервером
    return {"funnels": out, "can_edit": bool(can_edit)}


def overview_payload(conn, cfg, funnel=None):
    """None, если такой воронки нет в конфиге."""
    if funnel is not None and cfg.funnel(funnel) is None:
        return None
    return stats.overview(conn, cfg, funnel)


def users_payload(conn, cfg, level=None, funnel=None, reveal=False):
    """Пользователи: все (level пуст) или одного уровня. Телефоны замазываются здесь, при сборке ответа.
    None — неизвестный уровень или воронка."""
    if funnel is not None and cfg.funnel(funnel) is None:
        return None
    rows = stats.level_users(conn, level, reveal=reveal, limit=USERS_LIMIT, funnel=funnel)
    if rows is None:
        return None
    return {"users": rows, "total": stats.count_users(conn, level, funnel), "limit": USERS_LIMIT}


def save_funnel_payload(store, funnel_key, body, can_edit, codec=None):
    """Запись шагов воронки из редактора. Возвращает (код HTTP, тело ответа).

    Право проверяется здесь, на сервере (а не только скрытой кнопкой): без can_edit — 403 и файл не тронут.
    Любая ошибка проверки — 400 с текстом, что не так; файл остаётся как был."""
    if not can_edit:
        return 403, {"error": "forbidden"}
    if store.funnel(funnel_key) is None:
        return 404, {"error": "unknown funnel"}
    if not isinstance(body, dict) or "steps" not in body:
        return 400, {"error": "ожидался объект с полем steps"}
    try:
        save_funnel_steps(store.path, funnel_key, body["steps"], codec=codec)
    except (EditError, ConfigError) as e:
        return 400, {"error": str(e)}
    except Exception as e:  # не молчим и не отдаём трассировку наружу
        log.exception("запись конфига не удалась")
        return 500, {"error": f"не удалось записать конфиг: {type(e).__name__}"}
    store.reload()  # бот подхватит и сам по изменению файла, но ответ страницы должен быть уже новым
    f = store.funnel(funnel_key)
    return 200, {"ok": True, "funnel": funnel_to_dict(f)}
