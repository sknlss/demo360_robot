"""Постановка шагов в очередь. Общее для service (ответы людей) и worker (цепочка после отправки).

Всё вызывается внутри транзакции вызывающего. Планируется один следующий шаг; второго
неотправленного шага у прогона не бывает (частичный уникальный индекс queue_one_open).
"""


SILENT_KINDS = ("condition", "tags")  # виды шагов, которые ничего не отправляют и проходятся насквозь
MAX_SILENT_STEPS = 20  # шагов без отправки (условий и меток), которые бот проходит за один раз; страховка от зацикленного конфига


def enqueue_step(conn, run_id: int, user_id: int, funnel, step, now: float):
    """Поставить в очередь сообщение или задержку. Срок задержки — now + duration, сообщения — now."""
    due = now + (step.duration if step.kind == "delay" else 0.0)
    conn.execute(
        "INSERT INTO queue(run_id, user_id, funnel, step_key, due_at, status) "
        "VALUES (?,?,?,?,?, 'pending')",
        (run_id, user_id, funnel.key, step.key, due))


def finish_run(conn, run_id: int, user_id: int, funnel_key: str, now: float, log_event):
    """Цепочка кончилась. Закрывает только идущий прогон: у отменённого и уже завершённого ничего не меняется."""
    cur = conn.execute("UPDATE runs SET status='completed', finished_at=? WHERE id=? AND status='active'",
                       (now, run_id))
    if cur.rowcount:
        from .service import add_tag
        add_tag(conn, user_id, f"done:{funnel_key}")
        log_event(now, "run_completed", user_id, run_id, funnel_key)


def enter(conn, run_id: int, user_id: int, funnel, step, now: float, log_event, limit: int = None) -> str:
    """Перейти на `step`. Условия и шаги меток выполняются сразу и ничего не ставят в очередь; идём дальше, пока
    не упрёмся в сообщение или задержку (ставим в очередь) или в конец (прогон завершается).

    Сверх `limit` таких шагов за один раз прогон закрывается с причиной в журнале (`abort_run`).
    Возвращает 'queued' | 'ended' | 'aborted'. Вызывать, когда у прогона нет открытой записи."""
    from .checks import evaluate
    limit = MAX_SILENT_STEPS if limit is None else limit
    passed = 0
    while step is not None and step.kind in SILENT_KINDS:
        passed += 1
        if passed > limit:
            abort_run(conn, run_id, user_id, now,
                      f"предел шагов без отправки ({limit}): шаг '{step.key}' — конфиг зациклен?")
            return "aborted"
        if step.kind == "tags":
            from .service import add_tag
            for t in step.tags:
                add_tag(conn, user_id, t)           # повтор метки ничего не меняет
            log_event(now, "tags_set", user_id, run_id, f"{step.key}: {', '.join(step.tags)}")
            step = funnel.next_step(step)
            continue
        result = evaluate(conn, user_id, funnel.key, step.check)
        target = funnel.exit_step(step, result)
        log_event(now, "condition", user_id, run_id,
                  f"{step.key}: {'да' if result else 'нет'} → {target.key if target else 'конец'}")
        step = target
    if step is None:
        finish_run(conn, run_id, user_id, funnel.key, now, log_event)
        return "ended"
    enqueue_step(conn, run_id, user_id, funnel, step, now)
    return "queued"


def has_open_row(conn, run_id: int) -> bool:
    """Есть ли у прогона запись «ждёт» или «взято в работу»."""
    return conn.execute("SELECT 1 FROM queue WHERE run_id=? AND status IN ('pending','processing')",
                        (run_id,)).fetchone() is not None


ABORT_STATUS = "cancelled"  # в какой статус уходит прогон, у которого из конфига исчез нужный шаг (одна строка)


def abort_run(conn, run_id: int, user_id: int, now: float, reason: str):
    """Прогон нельзя продолжить (шаг или воронка исчезли из конфига после правки): закрываем аккуратно.

    Ждущие записи отменяем, запись «взято в работу» не трогаем (доедет сама), в журнал — причина.
    Уже завершённый прогон остаётся завершённым: снимаем только ожидание ответа."""
    conn.execute("UPDATE queue SET status='cancelled' WHERE run_id=? AND status='pending'", (run_id,))
    cur = conn.execute("UPDATE runs SET status=?, finished_at=?, awaiting_step=NULL, jump_to=NULL "
                       "WHERE id=? AND status='active'", (ABORT_STATUS, now, run_id))
    conn.execute("UPDATE runs SET awaiting_step=NULL, jump_to=NULL WHERE id=?", (run_id,))
    if cur.rowcount:
        conn.execute("INSERT INTO events(ts, user_id, run_id, kind, details) VALUES (?,?,?,?,?)",
                     (now, user_id, run_id, "run_cancelled", f"прогон закрыт: {reason}"))


def jump(conn, run_id: int, user_id: int, funnel, target_key: str, now: float, log_event) -> str:
    """Ответ человека: обрываем расписание и переходим на целевой шаг. Общий механизм ветвления.

    Ждущие записи отменяем. Запись «взято в работу» не трогаем (отправка доводится до конца):
    пока она в полёте, ставить второй шаг нельзя, поэтому цель запоминаем в runs.jump_to,
    и её поставит `settle`, когда та запись закончится. Если целевой шаг исчез из конфига — прогон
    закрывается (`abort_run`). Возвращает 'queued' | 'ended' | 'deferred' | 'aborted'."""
    conn.execute("UPDATE queue SET status='cancelled' WHERE run_id=? AND status='pending'", (run_id,))
    conn.execute("UPDATE runs SET awaiting_step=NULL WHERE id=?", (run_id,))
    target = funnel.step(target_key) if funnel else None
    if target is None:
        abort_run(conn, run_id, user_id, now, f"шага '{target_key}' нет в конфиге")
        return "aborted"
    if has_open_row(conn, run_id):
        conn.execute("UPDATE runs SET jump_to=? WHERE id=?", (target_key, run_id))
        return "deferred"
    return enter(conn, run_id, user_id, funnel, target, now, log_event)


def settle(conn, run_id: int, user_id: int, funnel, now: float, log_event) -> bool:
    """Запись прогона вышла из «взято в работу» (отправлена, ошибка, замена). Если ответ человека
    ждал этого момента — ставим целевой шаг. True, если поставили."""
    run = conn.execute("SELECT status, jump_to FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None or run["jump_to"] is None:
        return False
    conn.execute("UPDATE runs SET jump_to=NULL WHERE id=?", (run_id,))
    if run["status"] == "cancelled" or has_open_row(conn, run_id):
        return False
    step = funnel.step(run["jump_to"]) if funnel else None
    if step is None:
        abort_run(conn, run_id, user_id, now, f"шага '{run['jump_to']}' нет в конфиге")
        return False
    return enter(conn, run_id, user_id, funnel, step, now, log_event) != "aborted"
