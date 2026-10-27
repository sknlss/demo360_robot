"""Петля отправки отложенных шагов из очереди в базе."""
import asyncio
import logging
import time

from .errors import Permanent, RetryAfter, SendError, Transient
from .plan import enqueue_step, has_open_row, settle
from .service import add_tag

log = logging.getLogger("funnel.worker")

CLAIM_SQL = """
UPDATE queue SET status='processing', attempts=attempts+1
WHERE id = (SELECT id FROM queue WHERE status='pending' AND due_at <= :now
            ORDER BY due_at, id LIMIT 1)
RETURNING id, run_id, user_id, funnel, step_key, attempts
"""


class Worker:
    def __init__(self, db, cfg, sender, clock=time.time, sleep=asyncio.sleep):
        self.db, self.cfg, self.sender = db, cfg, sender
        self.clock, self._sleep = clock, sleep
        self._wake = asyncio.Event()

    def wake(self):
        """Не ждать секунду до следующего прохода: пришёл шаг без задержки."""
        self._wake.set()

    def recover(self) -> int:
        """После перезапуска «взято в работу» не доказано отправленным: не шлём второй раз.

        Лучше потерять одно сообщение, чем продублировать: помечаем ошибкой.
        """
        now = self.clock()
        with self.db.tx() as c:
            rows = c.execute(
                "UPDATE queue SET status='error', error='прервано перезапуском, отправка не подтверждена' "
                "WHERE status='processing' RETURNING id, run_id, user_id").fetchall()
            for r in rows:
                self.db.log_event(now, "send_failed", r["user_id"], r["run_id"],
                                  "прервано перезапуском, отправка не подтверждена")
                f = self._funnel_of_run(c, r["run_id"])
                settle(c, r["run_id"], r["user_id"], f, now)
        return len(rows)

    def _funnel_of_run(self, conn, run_id):
        r = conn.execute("SELECT funnel FROM runs WHERE id=?", (run_id,)).fetchone()
        return self.cfg.funnel(r["funnel"]) if r else None

    def expire(self) -> int:
        """Помечаем просроченное до забора, чтобы после простоя не было залпа."""
        now = self.clock()
        border = now - self.cfg.settings.stale_after
        with self.db.tx() as c:
            rows = c.execute(
                "UPDATE queue SET status='expired', error='просрочено' "
                "WHERE status='pending' AND due_at < ? RETURNING id, run_id, user_id, step_key",
                (border,)).fetchall()
            for r in rows:
                self.db.log_event(now, "step_expired", r["user_id"], r["run_id"], r["step_key"])
        return len(rows)

    def claim(self):
        """Забор одним запросом: выбрали и пометили «взято в работу» атомарно."""
        with self.db.tx() as c:
            return c.execute(CLAIM_SQL, {"now": self.clock()}).fetchone()

    async def tick(self) -> bool:
        """Один проход. True — что-то обработали (можно сразу идти за следующим)."""
        self.expire()
        row = self.claim()
        if row is None:
            return False
        funnel = self.cfg.funnel(row["funnel"])
        step = funnel.step(row["step_key"]) if funnel else None
        if step is None:
            self._fail(row, "шаг удалён из конфига")
            return True
        try:
            await self.sender.send_text(row["user_id"], step.text, **self._send_kw(funnel, step))
        except RetryAfter as e:
            # ждём ровно столько, сколько сказал Telegram; попытка не засчитывается
            self._requeue(row, attempts_back=1)
            log.warning("Telegram просит подождать %s с", e.seconds)
            await self._sleep(e.seconds)
            return True
        except Permanent as e:
            self._fail(row, f"{type(e).__name__}: {e}")
        except SendError as e:  # Transient и прочее
            self._retry_or_fail(row, f"{type(e).__name__}: {e}")
        except Exception as e:  # незнакомая ошибка не должна валить петлю
            log.exception("неожиданная ошибка отправки")
            self._retry_or_fail(row, f"{type(e).__name__}: {e}")
        else:
            self._mark_sent(row)
        await self._sleep(self.cfg.settings.send_pause)
        return True

    @staticmethod
    def _send_kw(funnel, step) -> dict:
        if step.request_phone:
            return {"phone_button": step.request_phone.button,
                    "decline_button": step.request_phone.decline_button}
        # шаг, куда ведёт ответ человека, заодно убирает клавиатуру запроса (она сама не исчезает)
        return {"remove_keyboard": True} if step.key in funnel.branch_targets() else {}

    def _requeue(self, row, due=None, error=None, attempts_back=0):
        """Вернуть запись в «ждёт». Если за время отправки пришёл ответ человека, повтор не нужен:
        запись заменяется, а на её месте ставится целевой шаг ответа."""
        now = self.clock()
        with self.db.tx() as c:
            run = c.execute("SELECT jump_to FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            if run["jump_to"] is not None:
                c.execute("UPDATE queue SET status='cancelled', error='заменён ответом человека' WHERE id=?",
                          (row["id"],))
                settle(c, row["run_id"], row["user_id"], self.cfg.funnel(row["funnel"]), now)
                return
            c.execute("UPDATE queue SET status='pending', attempts=attempts-?, due_at=COALESCE(?, due_at), "
                      "error=COALESCE(?, error) WHERE id=?", (attempts_back, due, error, row["id"]))

    def _mark_sent(self, row):
        now = self.clock()
        funnel = self.cfg.funnel(row["funnel"])
        step = funnel.step(row["step_key"])
        with self.db.tx() as c:
            c.execute("UPDATE queue SET status='sent', sent_at=?, error=NULL WHERE id=?",
                      (now, row["id"]))
            run = c.execute("SELECT status, jump_to FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            add_tag(c, row["user_id"], f"step:{row['funnel']}:{row['step_key']}")
            note = row["step_key"] + (" (прогон уже отменён)" if run["status"] == "cancelled" else "")
            self.db.log_event(now, "message_sent", row["user_id"], row["run_id"], note)
            if step.request_phone:
                # человек получил запрос номера (даже если прогон успели отменить: письмо уже ушло)
                self.db.log_event(now, "phone_requested", row["user_id"], row["run_id"], row["step_key"])
            if run["status"] == "cancelled":
                return  # доставили начатое, дальше цепочку не продолжаем
            if run["jump_to"] is not None:
                # пока шло письмо, человек ответил: его ответ главнее цепочки
                settle(c, row["run_id"], row["user_id"], funnel, now)
                return
            if step.request_phone:
                c.execute("UPDATE runs SET awaiting_step=? WHERE id=?", (step.key, row["run_id"]))
            nxt = funnel.next_step(step)
            if nxt is not None:
                enqueue_step(c, row["run_id"], row["user_id"], funnel, nxt, now)
            elif run["status"] == "active" and not has_open_row(c, row["run_id"]):
                c.execute("UPDATE runs SET status='completed', finished_at=? WHERE id=?",
                          (now, row["run_id"]))
                add_tag(c, row["user_id"], f"done:{row['funnel']}")
                self.db.log_event(now, "run_completed", row["user_id"], row["run_id"], row["funnel"])

    def _fail(self, row, error: str):
        now = self.clock()
        with self.db.tx() as c:
            c.execute("UPDATE queue SET status='error', error=? WHERE id=?", (error, row["id"]))
            self.db.log_event(now, "send_failed", row["user_id"], row["run_id"],
                              f"{row['step_key']}: {error}")
            settle(c, row["run_id"], row["user_id"], self.cfg.funnel(row["funnel"]), now)

    def _retry_or_fail(self, row, error: str):
        s = self.cfg.settings
        if row["attempts"] >= s.max_attempts:
            self._fail(row, f"{error} (попыток: {row['attempts']})")
            return
        self._requeue(row, due=self.clock() + s.retry_delay, error=error)

    async def run_forever(self, stop: asyncio.Event):
        n = self.recover()
        if n:
            log.warning("после перезапуска помечено ошибкой записей: %d", n)
        while not stop.is_set():
            try:
                busy = await self.tick()
            except Exception:
                log.exception("сбой в петле очереди")
                busy = False
            if not busy:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake_or_stop(stop), timeout=1.0)  # раз в секунду
                except asyncio.TimeoutError:
                    pass

    async def _wake_or_stop(self, stop: asyncio.Event):
        waiters = [asyncio.ensure_future(self._wake.wait()), asyncio.ensure_future(stop.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
