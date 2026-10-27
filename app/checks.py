"""Проверки для шагов-условий. Набор видов — тот же, что CHECK_KINDS в config.py (тест сверяет).

Новая проверка = функция здесь + имя в config.CHECK_KINDS + разбор параметров в config._parse_check."""
import json


def _tags(conn, user_id: int) -> list:
    row = conn.execute("SELECT tags FROM users WHERE tg_id=?", (user_id,)).fetchone()
    return json.loads(row["tags"]) if row else []


def phone(conn, user_id: int, funnel_key: str, check) -> bool:
    """Заполнено ли поле телефона у пользователя в базе. Меток не читает: номер мог прийти из другой воронки
    или быть внесён вручную."""
    row = conn.execute("SELECT phone FROM users WHERE tg_id=?", (user_id,)).fetchone()
    return bool(row and row["phone"])


def tag(conn, user_id: int, funnel_key: str, check) -> bool:
    """Есть ли у пользователя метка с таким названием. Поля телефона не читает."""
    return check.tag in _tags(conn, user_id)


CHECKS = {"tag": tag, "phone": phone}


def evaluate(conn, user_id: int, funnel_key: str, check) -> bool:
    return CHECKS[check.kind](conn, user_id, funnel_key, check)
