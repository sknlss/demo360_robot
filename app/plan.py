"""Постановка шагов в очередь. Общее для service (ответы людей) и worker (цепочка после отправки).

Всё вызывается внутри транзакции вызывающего. Планируется один следующий шаг; второго
неотправленного шага у прогона не бывает (частичный уникальный индекс queue_one_open).
"""


def enqueue_step(conn, run_id: int, user_id: int, funnel, step, now: float):
    conn.execute(
        "INSERT INTO queue(run_id, user_id, funnel, step_key, due_at, status) "
        "VALUES (?,?,?,?,?, 'pending')",
        (run_id, user_id, funnel.key, step.key, now + step.delay))


def has_open_row(conn, run_id: int) -> bool:
    """Есть ли у прогона запись «ждёт» или «взято в работу»."""
    return conn.execute("SELECT 1 FROM queue WHERE run_id=? AND status IN ('pending','processing')",
                        (run_id,)).fetchone() is not None


def jump(conn, run_id: int, user_id: int, funnel, target_key: str, now: float) -> str:
    """Ответ человека: обрываем расписание и переходим на целевой шаг. Общий механизм ветвления.

    Ждущие записи отменяем. Запись «взято в работу» не трогаем (отправка доводится до конца):
    пока она в полёте, ставить второй шаг нельзя, поэтому цель запоминаем в runs.jump_to,
    и её поставит `settle`, когда та запись закончится. Возвращает 'queued' | 'deferred'."""
    conn.execute("UPDATE queue SET status='cancelled' WHERE run_id=? AND status='pending'", (run_id,))
    conn.execute("UPDATE runs SET awaiting_step=NULL WHERE id=?", (run_id,))
    if has_open_row(conn, run_id):
        conn.execute("UPDATE runs SET jump_to=? WHERE id=?", (target_key, run_id))
        return "deferred"
    enqueue_step(conn, run_id, user_id, funnel, funnel.step(target_key), now)
    return "queued"


def settle(conn, run_id: int, user_id: int, funnel, now: float) -> bool:
    """Запись прогона вышла из «взято в работу» (отправлена, ошибка, замена). Если ответ человека
    ждал этого момента — ставим целевой шаг. True, если поставили."""
    run = conn.execute("SELECT status, jump_to FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run["jump_to"] is None:
        return False
    conn.execute("UPDATE runs SET jump_to=NULL WHERE id=?", (run_id,))
    step = funnel.step(run["jump_to"]) if funnel else None
    if step is None or run["status"] == "cancelled" or has_open_row(conn, run_id):
        return False
    enqueue_step(conn, run_id, user_id, funnel, step, now)
    return True
