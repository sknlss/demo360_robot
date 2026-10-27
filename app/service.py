"""Логика воронки: /start, телефон, постановка шагов в очередь.

Ничего не знает про aiogram и aiohttp: сообщения уходят через `sender`
(объект с методом send_text), поэтому тесты гоняют ту же логику, что и бот.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from .errors import Blocked, RetryAfter, SendError
from .plan import enqueue_step, jump

log = logging.getLogger("funnel.service")


@dataclass
class UserInfo:
    tg_id: int
    name: str = ""
    username: Optional[str] = None
    photo_file_id: Optional[str] = None


def add_tag(conn, user_id: int, tag: str):
    """Добавить метку в users.tags. Вызывать внутри транзакции."""
    row = conn.execute("SELECT tags FROM users WHERE tg_id=?", (user_id,)).fetchone()
    if row is None:
        return
    tags = json.loads(row["tags"])
    if tag not in tags:
        tags.append(tag)
        conn.execute("UPDATE users SET tags=? WHERE tg_id=?", (json.dumps(tags), user_id))


def has_tag(conn, user_id: int, tag: str) -> bool:
    row = conn.execute("SELECT tags FROM users WHERE tg_id=?", (user_id,)).fetchone()
    return bool(row) and tag in json.loads(row["tags"])


class FunnelService:
    def __init__(self, db, cfg, sender, clock=time.time, sleep=asyncio.sleep, notify=None):
        self.db, self.cfg, self.sender = db, cfg, sender
        self.clock, self._sleep = clock, sleep
        self._notify = notify or (lambda: None)  # разбудить петлю очереди: шаг без задержки уходит сразу
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, user_id: int) -> asyncio.Lock:
        # по одному обработчику на пользователя: два быстрых /start идут друг за другом
        return self._locks.setdefault(user_id, asyncio.Lock())

    # ---------- вспомогательное ----------
    def needs_photo(self, tg_id: int) -> bool:
        row = self.db.conn.execute(
            "SELECT photo_file_id FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return row is None or row["photo_file_id"] is None

    def _upsert_user(self, u: UserInfo, now: float):
        with self.db.tx() as c:
            c.execute(
                """INSERT INTO users(tg_id, name, username, photo_file_id, created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(tg_id) DO UPDATE SET
                     name=excluded.name, username=excluded.username,
                     photo_file_id=COALESCE(users.photo_file_id, excluded.photo_file_id)""",
                (u.tg_id, u.name, u.username, u.photo_file_id, now))

    def _active_run(self, user_id: int):
        return self.db.conn.execute(
            "SELECT * FROM runs WHERE user_id=? AND status='active'", (user_id,)).fetchone()

    def _awaiting_run(self, user_id: int):
        """Прогон, ждущий ответа. Берём только последний прогон пользователя и только не отменённый:
        иначе ответ на старую клавиатуру попал бы не в тот прогон. Завершённый тоже годится —
        напоминания закончились, а контакт всё равно может прийти (срока ожидания нет)."""
        run = self.db.conn.execute(
            "SELECT * FROM runs WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if run is None or run["status"] == "cancelled" or run["awaiting_step"] is None:
            return None
        return run

    async def _say(self, user_id: int, run_id, text: str, **kw):
        """Отправка из обработчика. Ждём RetryAfter один раз, остальное — в журнал."""
        for attempt in (1, 2):
            try:
                await self.sender.send_text(user_id, text, **kw)
                return True
            except RetryAfter as e:
                if attempt == 2:
                    break
                await self._sleep(e.seconds)
            except SendError as e:
                self.db.log_event(self.clock(), "send_failed", user_id, run_id, f"{type(e).__name__}: {e}")
                return False
        self.db.log_event(self.clock(), "send_failed", user_id, run_id, "RetryAfter повторно")
        return False

    # ---------- /start ----------
    async def start(self, user: UserInfo, funnel_key: Optional[str] = None) -> str:
        """Возвращает 'blocked' | 'restarted' | 'new'."""
        async with self._lock(user.tg_id):
            now = self.clock()
            funnel = self.cfg.funnel(funnel_key) if funnel_key else None
            if funnel is None:
                if funnel_key:
                    log.info("неизвестная воронка %r, беру по умолчанию", funnel_key)
                funnel = self.cfg.funnel(self.cfg.settings.default_funnel)
            self._upsert_user(user, now)

            # случай 1: условие на входе
            # «оставил номер» — по воронке: метка phone:<воронка>. Напоминания, кончившиеся без ответа,
            # и отказ её не ставят, поэтому такой человек начинает воронку заново.
            left_phone = has_tag(self.db.conn, user.tg_id, f"phone:{funnel.key}")
            for cond in funnel.entry_conditions:
                if cond.kind == "phone_given" and left_phone:
                    self.db.log_event(now, "start_blocked", user.tg_id, None, funnel.key)
                    await self._say(user.tg_id, None, cond.reply)
                    return "blocked"

            # случаи 2 и 3: одна транзакция — отмена прежнего и создание нового
            outcome = "new"
            with self.db.tx() as c:
                old = c.execute("SELECT id FROM runs WHERE user_id=? AND status='active'",
                                (user.tg_id,)).fetchone()
                if old:
                    outcome = "restarted"
                    c.execute("UPDATE runs SET status='cancelled', finished_at=? WHERE id=?",
                              (now, old["id"]))
                    # отменяем только ждущих; уже взятые в работу доедут сами
                    n = c.execute("UPDATE queue SET status='cancelled' "
                                  "WHERE run_id=? AND status='pending'", (old["id"],)).rowcount
                    self.db.log_event(now, "run_cancelled", user.tg_id, old["id"],
                                      f"повторный /start, отменено ждущих: {n}")
                cur = c.execute("INSERT INTO runs(user_id, funnel, status, started_at) "
                                "VALUES (?,?, 'active', ?)", (user.tg_id, funnel.key, now))
                run_id = cur.lastrowid
                self.db.log_event(now, "run_started", user.tg_id, run_id, funnel.key)

                enqueue_step(c, run_id, user.tg_id, funnel, funnel.first, now)
            self._notify()
            return outcome

    # ---------- ответы человека ----------
    async def contact(self, user_id: int, phone: str, contact_user_id: Optional[int]) -> str:
        async with self._lock(user_id):
            run = self._awaiting_run(user_id)
            funnel = self.cfg.funnel(run["funnel"]) if run else None
            step = funnel.step(run["awaiting_step"]) if funnel else None
            if step is None or step.request_phone is None:
                return "ignored"
            if contact_user_id is not None and contact_user_id != user_id:
                await self._say(user_id, run["id"], step.text, **_phone_kw(step))
                return "foreign"
            now = self.clock()
            with self.db.tx() as c:
                c.execute("UPDATE users SET phone=? WHERE tg_id=?", (phone, user_id))
                add_tag(c, user_id, "phone")
                add_tag(c, user_id, f"phone:{funnel.key}")
                self.db.log_event(now, "phone_given", user_id, run["id"], step.key)
                jump(c, run["id"], user_id, funnel, step.reply_target("contact"), now)
            self._notify()
            return "accepted"

    async def decline(self, user_id: int) -> str:
        """Отказ: кнопка «не хочу» или любой текст вместо контакта."""
        async with self._lock(user_id):
            run = self._awaiting_run(user_id)
            funnel = self.cfg.funnel(run["funnel"]) if run else None
            step = funnel.step(run["awaiting_step"]) if funnel else None
            if step is None or step.request_phone is None:
                return "ignored"
            now = self.clock()
            with self.db.tx() as c:
                add_tag(c, user_id, "phone_declined")
                self.db.log_event(now, "phone_declined", user_id, run["id"], step.key)
                jump(c, run["id"], user_id, funnel, step.reply_target("decline"), now)
            self._notify()
            return "declined"


def _phone_kw(step) -> dict:
    return {"phone_button": step.request_phone.button, "decline_button": step.request_phone.decline_button}
