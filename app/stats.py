"""Запросы для дашборда. Только чтение."""

STATUSES = ("pending", "processing", "sent", "cancelled", "expired", "error")

def _event(kind: str) -> str:
    """Условие «у пользователя есть событие kind»; `?` — воронка (NULL = любая)."""
    return (f"EXISTS (SELECT 1 FROM events e LEFT JOIN runs r ON r.id=e.run_id "
            f"WHERE e.user_id=u.tg_id AND e.kind='{kind}' AND (? IS NULL OR r.funnel=?))")


# Уровни выводятся из журнала событий, а не из устройства воронки: им всё равно, сколько в ней шагов
# и в каком порядке. Воронка без запроса номера просто даёт нули на втором и третьем уровне.
# Уровни вложены: кто дал телефон, тот получил запрос номера (phone_given возможен только после него).
LEVELS = {
    "started": ("Запустил бота", _event("run_started")),
    "requested": ("Получил запрос номера", _event("phone_requested")),
    "phone": ("Дал телефон", _event("phone_given")),
}


def levels(conn, funnel=None):
    out = []
    for key, (title, cond) in LEVELS.items():
        n = conn.execute(f"SELECT COUNT(*) FROM users u WHERE {cond}", (funnel, funnel)).fetchone()[0]
        out.append({"key": key, "title": title, "count": n})
    return out


def mask_phone(phone, reveal: bool):
    """Телефон для ответа API. Не-админам — только последние 4 цифры, остальное точками.

    Маска строится здесь, на сервере: полный номер в ответ не попадает вовсе."""
    if not phone or reveal:
        return phone
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) <= 4:
        return "•" * len(digits)
    return "•" * (len(digits) - 4) + digits[-4:]


def count_users(conn, level=None, funnel=None):
    """Сколько всего пользователей в выборке (чтобы страница показала «N из M», а не молча обрезала)."""
    if not level:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if level not in LEVELS:
        return None
    return conn.execute(f"SELECT COUNT(*) FROM users u WHERE {LEVELS[level][1]}", (funnel, funnel)).fetchone()[0]


def level_users(conn, level, reveal: bool = False, limit: int = 200, funnel=None):
    """Пользователи уровня (level=None или '' — все пользователи, без фильтра).

    По умолчанию номера замазаны: забытый флаг скрывает, а не открывает."""
    if level:
        if level not in LEVELS:
            return None
        where, params = LEVELS[level][1], (funnel, funnel, limit)
    else:
        where, params = "1", (limit,)
    rows = conn.execute(
        f"SELECT tg_id, name, username, phone, created_at, photo_file_id IS NOT NULL AS has_photo "
        f"FROM users u WHERE {where} ORDER BY created_at DESC, tg_id DESC LIMIT ?", params).fetchall()
    out = [dict(r) for r in rows]
    for u in out:
        u["phone"] = mask_phone(u["phone"], reveal)
    return out


def steps(conn, cfg, funnel=None):
    """По каждому шагу: сколько в каком состоянии. Отдельно — отправлено в отменённых прогонах."""
    counts = {}
    for r in conn.execute("SELECT funnel, step_key, status, COUNT(*) n FROM queue GROUP BY 1,2,3"):
        counts.setdefault((r["funnel"], r["step_key"]), {})[r["status"]] = r["n"]
    late = {}
    for r in conn.execute(
            "SELECT q.funnel, q.step_key, COUNT(*) n FROM queue q JOIN runs r ON r.id=q.run_id "
            "WHERE q.status='sent' AND r.status='cancelled' GROUP BY 1,2"):
        late[(r["funnel"], r["step_key"])] = r["n"]
    out = []
    order = [(f.key, s.key) for f in cfg.funnels.values() for s in f.steps if funnel in (None, f.key)]
    order += [k for k in counts if k not in order and funnel in (None, k[0])]  # шаги, которых уже нет в конфиге
    for fkey, step in order:
        c = counts.get((fkey, step), {})
        out.append({"funnel": fkey, "step": step,
                    **{st: c.get(st, 0) for st in STATUSES},
                    "sent_in_cancelled": late.get((fkey, step), 0)})
    return out


def runs_summary(conn, funnel=None):
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM runs WHERE (? IS NULL OR funnel=?) GROUP BY status", (funnel, funnel))}


def recent_events(conn, limit: int = 50, funnel=None):
    """События воронки — по прогону."""
    rows = conn.execute(
        "SELECT e.id, e.ts, e.kind, e.details, e.user_id, e.run_id, u.name, u.username "
        "FROM events e LEFT JOIN users u ON u.tg_id=e.user_id LEFT JOIN runs r ON r.id=e.run_id "
        "WHERE (? IS NULL OR r.funnel=?) "
        "ORDER BY e.id DESC LIMIT ?", (funnel, funnel, limit)).fetchall()
    return [dict(r) for r in rows]


def overview(conn, cfg, funnel=None):
    return {"funnel": funnel, "levels": levels(conn, funnel), "steps": steps(conn, cfg, funnel),
            "runs": runs_summary(conn, funnel), "events": recent_events(conn, funnel=funnel)}
