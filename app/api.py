"""Тела ответов дашборда. Без aiohttp: их собирают и web.py, и tests/preview_server.py, и тесты —
значит, JSON, который видит страница, проверяется здесь же, а не только в браузере."""
from . import stats
from .config import funnel_to_dict

USERS_LIMIT = 200


def funnels_payload(conn, cfg) -> dict:
    """Список воронок: структура шагов и сводка по каждой (три уровня, прогоны)."""
    out = []
    for f in cfg.funnels.values():
        d = funnel_to_dict(f)
        d["is_default"] = f.key == cfg.settings.default_funnel
        d["levels"] = stats.levels(conn, f.key)
        d["runs"] = stats.runs_summary(conn, f.key)
        out.append(d)
    return {"funnels": out}


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
