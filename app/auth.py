"""Проверка подписи initData мини-аппа (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class AuthError(Exception):
    pass


def verify_init_data(init_data: str, bot_token: str, max_age: int, now=None) -> dict:
    """Возвращает словарь пользователя из подписанных данных или бросает AuthError.

    Пускает любого с настоящей подписью Telegram: подпись и отсекает посторонних."""
    if not init_data:
        raise AuthError("нет данных Telegram")
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received = params.pop("hash", None)
    if not received:
        raise AuthError("нет подписи")
    # в строку проверки входят все поля, кроме hash, по алфавиту
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("подпись не сошлась")
    try:
        auth_date = int(params.get("auth_date", "0"))
    except ValueError:
        raise AuthError("плохая auth_date")
    now = time.time() if now is None else now
    if max_age and now - auth_date > max_age:
        raise AuthError("данные устарели")
    try:
        user = json.loads(params.get("user", ""))
        uid = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise AuthError("нет пользователя")
    return user


@dataclass(frozen=True)
class Access:
    ok: bool
    reveal: bool = False    # видит телефоны целиком
    can_edit: bool = False  # может менять воронки
    why: str = ""


def access(init_data, *, disabled: bool, token: str, admin_ids, max_age: int, now=None) -> Access:
    """Решение для API дашборда. Единственное место, где читается флаг отключения проверки и
    решается, кто что может: админы из ADMIN_IDS и режим DASHBOARD_AUTH_DISABLED видят номера целиком и
    правят воронки (без этого в локальном запуске редактор нельзя показать). Остальным с подписью
    номер замазывает сервер, а запись в конфиг закрыта."""
    if disabled:
        return Access(True, True, True, "проверка отключена (DASHBOARD_AUTH_DISABLED)")
    try:
        user = verify_init_data(init_data, token, max_age, now)
    except AuthError as e:
        return Access(False, False, False, str(e))
    admin = int(user["id"]) in admin_ids
    return Access(True, admin, admin, f"user {user['id']}" + (" (админ)" if admin else ""))


def authorize(init_data, **kw):
    """(пустить?, показывать ли полные телефоны?, причина) — короткая форма `access`."""
    a = access(init_data, **kw)
    return a.ok, a.reveal, a.why
