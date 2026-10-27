"""Проверки для шагов-условий. Набор видов — тот же, что CHECK_KINDS в config.py (тест сверяет).

Новая проверка = функция здесь + имя в config.CHECK_KINDS + разбор параметров в config._parse_check."""
import json


def _tags(conn, user_id: int) -> list:
    row = conn.execute("SELECT tags FROM users WHERE tg_id=?", (user_id,)).fetchone()
    return json.loads(row["tags"]) if row else []


def phone_given(conn, user_id: int, funnel_key: str, check) -> bool:
    """Человек оставил номер в этой воронке (метка phone:<воронка>). Напоминания без ответа и отказ её не ставят."""
    return f"phone:{funnel_key}" in _tags(conn, user_id)


def tag(conn, user_id: int, funnel_key: str, check) -> bool:
    return check.tag in _tags(conn, user_id)


CHECKS = {"phone_given": phone_given, "tag": tag}


def evaluate(conn, user_id: int, funnel_key: str, check) -> bool:
    return CHECKS[check.kind](conn, user_id, funnel_key, check)
