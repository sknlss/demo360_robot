"""Тесты ядра на stdlib: python3 -m unittest discover -s tests -v"""
import asyncio
import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import stats  # noqa: E402
from app.auth import authorize, verify_init_data, AuthError  # noqa: E402
from app.config import ConfigError, parse_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import Blocked, RetryAfter, Transient  # noqa: E402
from app.lock import AlreadyRunning, ProcessLock  # noqa: E402
from app.service import FunnelService, UserInfo, has_tag  # noqa: E402
from app.worker import Worker  # noqa: E402

def _funnel_raw():
    """Воронка из задания в новой модели: приветствие -> запрос номера -> напоминания (10 с, 1 мин);
    ответ уводит на «спасибо»/«отказ». Тексты те же, что у старых тестов, где это возможно."""
    return {
        "entry_conditions": [{"kind": "phone_given", "reply": "ЗАЯВКА У МЕНЕДЖЕРА"}],
        "steps": [
            {"key": "welcome", "kind": "message", "text": "привет"},
            {"key": "ask", "kind": "message", "text": "телефон?",
             "request_phone": {"button": "дать", "decline_button": "нет"},
             "on_reply": {"contact": "thanks", "decline": "declined"}},
            {"key": "m1", "kind": "message", "delay": "10s", "text": "первое"},
            {"key": "m2", "kind": "message", "delay": "1m", "text": "второе"},
            {"key": "thanks", "kind": "message", "text": "спасибо"},
            {"key": "declined", "kind": "message", "text": "ладно"},
        ],
    }


RAW = {
    "settings": {"default_funnel": "signup", "stale_after": "5m", "send_pause": 0, "max_attempts": 3,
                 "retry_delay": "5s"},
    "funnels": {
        "signup": _funnel_raw(),
        "promo": {"steps": [{"key": "only", "kind": "message", "delay": 0, "text": "акция"}]},
        # воронка вообще без запроса номера
        "plain": {"steps": [{"key": "a", "kind": "message", "text": "A"},
                            {"key": "b", "kind": "message", "delay": "5s", "text": "B"}]},
        # два запроса номера подряд: второй идёт после «спасибо» первого
        "double": {"steps": [
            {"key": "ask1", "kind": "message", "text": "номер1?", "request_phone": {"button": "дать", "decline_button": "нет"},
             "on_reply": {"contact": "ok1", "decline": "no1"}},
            {"key": "ok1", "kind": "message", "text": "ок1", "next": "ask2"},
            {"key": "no1", "kind": "message", "text": "нет1", "next": "ask2"},
            {"key": "ask2", "kind": "message", "text": "номер2?", "request_phone": {"button": "дать", "decline_button": "нет"},
             "on_reply": {"contact": "ok2", "decline": "no2"}},
            {"key": "ok2", "kind": "message", "text": "ок2"},
            {"key": "no2", "kind": "message", "text": "нет2"},
        ]},
    },
}
TOKEN = "123456:TEST-TOKEN"


class FakeSender:
    """Заглушка отправки: пишет всё в список; может ломаться по сценарию."""

    def __init__(self):
        self.sent = []
        self.errors = []      # очередь исключений, которые надо бросить на следующих отправках
        self.gate = None      # asyncio.Event: отправка висит, пока его не откроют
        self.entered = asyncio.Event()

    async def send_text(self, chat_id, text, **kw):
        if self.gate is not None and text == "первое":  # висим только на отправке шага
            self.entered.set()
            await self.gate.wait()
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, text, kw))

    def texts(self):
        return [t for _, t, _ in self.sent]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.cfg = parse_config(RAW)
        self.sender = FakeSender()
        self.clock = Clock()
        self.sleeps = []

        async def fake_sleep(s):
            self.sleeps.append(s)

        self.svc = FunnelService(self.db, self.cfg, self.sender, self.clock, fake_sleep)
        self.worker = Worker(self.db, self.cfg, self.sender, self.clock, fake_sleep)
        self.u = UserInfo(111, "Тест Тестов", "tester")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def q(self, sql, *a):
        return [dict(r) for r in self.db.conn.execute(sql, a).fetchall()]

    async def begin(self, user=None, funnel=None):
        """/start и отправка приветствия и запроса номера; напоминание m1 ждёт в очереди."""
        await self.svc.start(user or self.u, funnel)
        await self.drain()

    async def drain(self, advance=0):
        self.clock.t += advance
        while await self.worker.tick():
            pass

    def assert_one_open(self):
        """Инвариант: у прогона не больше одного неотправленного шага (ждёт или в отправке)."""
        bad = self.q("SELECT run_id, COUNT(*) n FROM queue WHERE status IN ('pending','processing') "
                     "GROUP BY run_id HAVING n > 1")
        self.assertEqual(bad, [])


class TestStartCases(Base):
    async def test_first_start_welcome_and_phone_request(self):
        r = await self.svc.start(self.u)
        self.assertEqual(r, "new")
        self.assertEqual(self.sender.texts(), [])        # шаги идут через очередь, а не из обработчика
        await self.drain()
        self.assertEqual(self.sender.texts(), ["привет", "телефон?"])
        self.assertTrue(self.sender.sent[1][2]["phone_button"])
        self.assertNotIn("phone_button", self.sender.sent[0][2])
        self.assertEqual(len(self.q("SELECT * FROM runs WHERE status='active'")), 1)
        # запланирован только следующий шаг
        self.assertEqual([r["step_key"] for r in self.q("SELECT step_key FROM queue WHERE status='pending'")], ["m1"])

    async def test_restart_while_running_cancels_old_run_and_pending(self):
        await self.begin()
        old = self.q("SELECT id FROM runs")[0]["id"]
        self.assertEqual(len(self.q("SELECT * FROM queue WHERE status='pending'")), 1)
        r = await self.svc.start(self.u)
        self.assertEqual(r, "restarted")
        runs = self.q("SELECT id, status FROM runs ORDER BY id")
        self.assertEqual([x["status"] for x in runs], ["cancelled", "active"])
        self.assertNotEqual(runs[1]["id"], old)
        self.assertEqual([x["status"] for x in self.q("SELECT status FROM queue WHERE run_id=? ORDER BY id", old)],
                         ["sent", "sent", "cancelled"])
        # новый прогон начат с начала: ждёт приветствие
        self.assertEqual([(x["step_key"], x["status"]) for x in
                          self.q("SELECT step_key, status FROM queue WHERE run_id=?", runs[1]["id"])],
                         [("welcome", "pending")])
        self.assertIn("run_cancelled", [e["kind"] for e in self.q("SELECT kind FROM events")])
        n = len(self.sender.sent)
        await self.drain()
        self.assertEqual(self.sender.texts()[n:], ["привет", "телефон?"])
        self.assert_one_open()

    async def test_restart_after_phone_left_is_blocked_by_entry_condition(self):
        await self.begin()
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        self.assertTrue(has_tag(self.db.conn, 111, "phone:signup"))
        await self.drain(advance=100)
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        n_sent = len(self.sender.sent)
        r = await self.svc.start(self.u)
        self.assertEqual(r, "blocked")
        self.assertEqual(self.sender.sent[n_sent:][0][1], "ЗАЯВКА У МЕНЕДЖЕРА")
        self.assertEqual(len(self.sender.sent), n_sent + 1)
        self.assertEqual(len(self.q("SELECT * FROM runs")), 1)
        await self.drain(advance=100)
        self.assertEqual(len(self.sender.sent), n_sent + 1)    # и в очередь ничего не попало

    async def test_phone_left_blocks_even_while_thanks_run_is_active(self):
        """Контакт пришёл, прогон ещё идёт (спасибо не ушло): /start уже не начинает заново."""
        await self.begin()
        await self.svc.contact(111, "+37400000000", 111)
        self.assertEqual(await self.svc.start(self.u), "blocked")
        self.assertEqual(len(self.q("SELECT * FROM runs")), 1)

    async def test_restart_after_reminders_ran_out_without_answer_starts_over(self):
        await self.begin()
        await self.drain(advance=11)
        await self.drain(advance=70)
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        self.assertTrue(has_tag(self.db.conn, 111, "done:signup"))     # воронку «прошёл» до конца, но номера нет
        self.assertFalse(has_tag(self.db.conn, 111, "phone:signup"))
        n_sent = len(self.sender.sent)
        self.assertEqual(await self.svc.start(self.u), "new")
        await self.drain()
        self.assertEqual(self.sender.texts()[n_sent:], ["привет", "телефон?"])
        self.assertNotIn("ЗАЯВКА У МЕНЕДЖЕРА", self.sender.texts())
        self.assertEqual([r["status"] for r in self.q("SELECT status FROM runs ORDER BY id")], ["completed", "active"])

    async def test_restart_after_decline_starts_over(self):
        await self.begin()
        await self.svc.decline(111)
        await self.drain(advance=100)
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        n_sent = len(self.sender.sent)
        self.assertEqual(await self.svc.start(self.u), "new")
        await self.drain()
        self.assertEqual(self.sender.texts()[n_sent:], ["привет", "телефон?"])

    async def test_phone_in_other_funnel_does_not_block_this_one(self):
        """Условие по воронке: номер, оставленный в «double», не закрывает вход в «signup»."""
        await self.begin(funnel="double")
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        self.assertTrue(has_tag(self.db.conn, 111, "phone:double"))
        self.assertNotEqual(await self.svc.start(self.u, "signup"), "blocked")
        self.assertEqual(self.q("SELECT funnel FROM runs WHERE status='active'"), [{"funnel": "signup"}])
        self.assertNotIn("ЗАЯВКА У МЕНЕДЖЕРА", self.sender.texts())

    async def test_two_rapid_starts_leave_one_active_run_and_no_old_pending(self):
        await self.begin()
        await asyncio.gather(self.svc.start(self.u), self.svc.start(self.u))
        runs = self.q("SELECT id, status FROM runs ORDER BY id")
        self.assertEqual([r["status"] for r in runs], ["cancelled", "cancelled", "active"])
        # ждущая запись одна, и она у последнего прогона: от прежних записей не осталось
        pend = self.q("SELECT run_id FROM queue WHERE status='pending'")
        self.assertEqual([p["run_id"] for p in pend], [runs[2]["id"]])
        self.assert_one_open()

    async def test_unique_index_forbids_two_active_runs(self):
        await self.svc.start(self.u)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("INSERT INTO runs(user_id,funnel,status,started_at) VALUES (111,'signup','active',1)")

    async def test_funnel_chosen_by_start_param_and_default_fallback(self):
        await self.svc.start(self.u, "promo")
        self.assertEqual(self.q("SELECT funnel FROM runs")[0]["funnel"], "promo")
        await self.svc.start(UserInfo(222, "B"), "нет-такой")
        self.assertEqual(self.q("SELECT funnel FROM runs WHERE user_id=222")[0]["funnel"], "signup")


class TestQueue(Base):
    async def test_steps_sent_on_schedule_then_run_completed(self):
        await self.begin()
        await self.drain(advance=5)
        self.assertNotIn("первое", self.sender.texts())
        await self.drain(advance=6)   # 11 с после запроса номера
        self.assertIn("первое", self.sender.texts())
        self.assertNotIn("второе", self.sender.texts())
        await self.drain(advance=50)  # до второго ещё 60 с после первого
        self.assertNotIn("второе", self.sender.texts())
        await self.drain(advance=11)
        self.assertIn("второе", self.sender.texts())
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "первое", "второе"])
        # напоминания не просят клавиатуру заново
        self.assertFalse(any("phone_button" in kw for _, t, kw in self.sender.sent if t in ("первое", "второе")))

    async def test_claimed_item_is_finished_and_marked_sent_inside_cancelled_run(self):
        await self.begin()
        old = self.q("SELECT id FROM runs")[0]["id"]
        self.clock.t += 11
        self.sender.gate = asyncio.Event()
        task = asyncio.create_task(self.worker.tick())
        await self.sender.entered.wait()           # шаг взят в работу и висит в отправке
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m1'")[0]["status"], "processing")
        await self.svc.start(self.u)               # повторный /start отменяет прогон
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m1'")[0]["status"], "processing")
        self.sender.gate.set()
        await task
        row = self.q("SELECT q.status qs, r.status rs FROM queue q JOIN runs r ON r.id=q.run_id "
                     "WHERE q.step_key='m1'")[0]
        self.assertEqual((row["qs"], row["rs"]), ("sent", "cancelled"))
        # у отменённого прогона ничего не запланировано дальше
        self.assertEqual([x["step_key"] for x in self.q("SELECT step_key FROM queue WHERE run_id=? ORDER BY id", old)],
                         ["welcome", "ask", "m1"])
        st = [s for s in stats.steps(self.db.conn, self.cfg) if s["step"] == "m1"][0]
        self.assertEqual((st["sent"], st["sent_in_cancelled"]), (1, 1))
        # отменённый прогон не стал «завершённым» и метка done не поставлена
        self.assertFalse(has_tag(self.db.conn, 111, "done:signup"))

    async def test_stale_item_expired_not_sent_and_nothing_scheduled_after(self):
        await self.begin()
        n = len(self.sender.sent)
        await self.drain(advance=10 + 300 + 1)    # m1 опоздал больше чем на stale_after
        self.assertEqual(len(self.sender.sent), n)
        self.assertEqual([r["status"] for r in self.q("SELECT status FROM queue ORDER BY id")],
                         ["sent", "sent", "expired"])
        self.assertIn("step_expired", [e["kind"] for e in self.q("SELECT kind FROM events")])
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "active")  # не «завершён»

    async def test_claim_is_atomic_one_row_per_claim(self):
        await self.svc.start(self.u)
        await self.svc.start(UserInfo(222, "B"))
        a, b = self.worker.claim(), self.worker.claim()
        self.assertNotEqual(a["id"], b["id"])
        self.assertIsNone(self.worker.claim())

    async def test_retry_after_waits_exactly_that_long(self):
        await self.begin()
        self.clock.t += 11
        self.sender.errors = [RetryAfter(7)]
        self.sleeps.clear()
        await self.worker.tick()
        self.assertEqual(self.sleeps, [7])
        self.assertEqual(self.q("SELECT status, attempts FROM queue WHERE step_key='m1'")[0],
                         {"status": "pending", "attempts": 0})
        await self.worker.tick()
        self.assertIn("первое", self.sender.texts())

    async def test_blocked_user_is_recorded_and_not_retried(self):
        await self.begin()
        self.sender.errors = [Blocked("Forbidden: bot was blocked by the user")]
        await self.drain(advance=11)
        r = self.q("SELECT status, attempts, error FROM queue WHERE step_key='m1'")[0]
        self.assertEqual((r["status"], r["attempts"]), ("error", 1))
        self.assertIn("blocked", r["error"])
        self.assertIn("send_failed", [e["kind"] for e in self.q("SELECT kind FROM events")])
        self.assertEqual(self.q("SELECT * FROM queue WHERE status IN ('pending','processing')"), [])

    async def test_transient_errors_retried_limited_times(self):
        await self.begin()
        self.sender.errors = [Transient("сеть")] * 5
        for _ in range(6):
            await self.drain(advance=11)
        r = self.q("SELECT status, attempts FROM queue WHERE step_key='m1'")[0]
        self.assertEqual((r["status"], r["attempts"]), ("error", 3))

    async def test_recover_marks_processing_as_error_without_resend(self):
        await self.begin()
        self.clock.t += 11
        self.worker.claim()
        self.assertEqual(self.worker.recover(), 1)
        n = len(self.sender.sent)
        await self.drain(advance=1)
        self.assertEqual(len(self.sender.sent), n)
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m1'")[0]["status"], "error")


class TestPhone(Base):
    async def test_decline_does_not_break_funnel(self):
        await self.begin()
        self.assertEqual(await self.svc.decline(111), "declined")
        self.assertEqual(await self.svc.decline(111), "ignored")   # повтор не зацикливает
        await self.drain()
        self.assertEqual(self.sender.sent[-1][1], "ладно")
        self.assertTrue(self.sender.sent[-1][2]["remove_keyboard"])
        self.assertIn("phone_declined", [e["kind"] for e in self.q("SELECT kind FROM events")])
        self.assertTrue(has_tag(self.db.conn, 111, "phone_declined"))
        await self.drain(advance=500)
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "ладно"])   # напоминаний нет
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        self.assertIsNone(self.q("SELECT phone FROM users")[0]["phone"])

    async def test_contact_saves_phone_and_tags(self):
        await self.begin()
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        u = self.q("SELECT phone, tags FROM users")[0]
        self.assertEqual(u["phone"], "+37400000000")
        self.assertIn("phone", json.loads(u["tags"]))
        self.assertEqual(self.sender.sent[-1][1], "спасибо")
        self.assertTrue(self.sender.sent[-1][2]["remove_keyboard"])

    async def test_foreign_contact_rejected(self):
        await self.begin()
        self.assertEqual(await self.svc.contact(111, "+1", 999), "foreign")
        self.assertIsNone(self.q("SELECT phone FROM users")[0]["phone"])
        self.assertEqual(self.sender.sent[-1][1], "телефон?")     # просим ещё раз
        self.assertEqual([r["step_key"] for r in self.q("SELECT step_key FROM queue WHERE status='pending'")], ["m1"])

    async def test_reply_without_request_is_ignored(self):
        await self.svc.start(self.u)                   # приветствие ещё не ушло, запрос тем более
        self.assertEqual(await self.svc.decline(111), "ignored")
        self.assertEqual(await self.svc.contact(111, "+1", 111), "ignored")
        self.assertEqual(await self.svc.contact(555, "+1", 555), "ignored")   # незнакомый пользователь


class TestFunnelModel(Base):
    """Однородные шаги, ветвление по ответу, планирование по одному."""

    async def test_funnel_without_phone_request_runs_to_the_end_and_dashboard_works(self):
        await self.begin(funnel="plain")
        self.assertEqual(self.sender.texts(), ["A"])
        self.assert_one_open()
        await self.drain(advance=6)
        self.assertEqual(self.sender.texts(), ["A", "B"])
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        ov = stats.overview(self.db.conn, self.cfg)
        self.assertEqual({l["key"]: l["count"] for l in ov["levels"]}, {"started": 1, "requested": 0, "phone": 0})
        self.assertEqual(stats.level_users(self.db.conn, "phone"), [])
        self.assertEqual(stats.level_users(self.db.conn, "requested"), [])
        json.dumps(ov)

    async def test_contact_between_reminders_cancels_pending_and_sends_thanks_once(self):
        await self.begin()
        await self.drain(advance=11)                          # m1 ушёл, m2 ждёт
        self.assertEqual([r["step_key"] for r in self.q("SELECT step_key FROM queue WHERE status='pending'")], ["m2"])
        self.assertEqual(await self.svc.contact(111, "+37400000000", 111), "accepted")
        self.assert_one_open()
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m2'")[0]["status"], "cancelled")
        await self.drain()
        await self.drain(advance=1000)
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "первое", "спасибо"])
        self.assertEqual(len(self.q("SELECT * FROM queue WHERE step_key='thanks'")), 1)
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")

    async def test_contact_after_all_reminders_still_sends_thanks(self):
        await self.begin()
        await self.drain(advance=11)
        await self.drain(advance=61)
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")   # без ответа прогон кончился
        self.assertEqual(await self.svc.contact(111, "+37400000000", 111), "accepted")
        await self.drain()
        self.assertEqual(self.sender.texts()[-1], "спасибо")
        self.assertEqual(self.sender.texts().count("спасибо"), 1)
        self.assertEqual(self.q("SELECT phone FROM users")[0]["phone"], "+37400000000")
        self.assertEqual(len(self.q("SELECT * FROM runs")), 1)          # новый прогон не создан
        self.assertEqual(await self.svc.contact(111, "+3", 111), "ignored")   # второй раз на тот же запрос — нет
        self.assert_one_open()

    async def test_reply_during_reminder_in_flight_is_deferred_not_doubled(self):
        await self.begin()
        self.clock.t += 11
        self.sender.gate = asyncio.Event()
        task = asyncio.create_task(self.worker.tick())
        await self.sender.entered.wait()                      # m1 в отправке
        await self.svc.contact(111, "+37400000000", 111)      # ответ пришёл в этот момент
        self.assert_one_open()                                # благодарность ждёт, пока m1 не закончит
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m1'")[0]["status"], "processing")
        self.assertEqual(self.q("SELECT step_key FROM queue WHERE step_key='thanks'"), [])
        self.sender.gate.set()
        await task
        self.assert_one_open()
        await self.drain()
        await self.drain(advance=1000)
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "первое", "спасибо"])   # m2 не пришёл

    async def test_reply_while_reminder_fails_transiently_replaces_it(self):
        await self.begin()
        self.clock.t += 11
        self.sender.gate = asyncio.Event()
        self.sender.errors = [Transient("сеть")]
        task = asyncio.create_task(self.worker.tick())
        await self.sender.entered.wait()
        await self.svc.decline(111)
        self.sender.gate.set()
        await task
        await self.drain()
        await self.drain(advance=1000)
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m1'")[0]["status"], "cancelled")
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "ладно"])

    async def test_decline_by_button_and_by_arbitrary_text_lead_to_declined_step(self):
        # в боте обе дороги (кнопка «нет» — это обычный текст) приходят в service.decline
        for uid in (1, 2):
            u = UserInfo(uid, f"u{uid}")
            await self.begin(u)
            self.assertEqual(await self.svc.decline(uid), "declined")
            await self.drain()
        self.assertEqual(sum(1 for _, t, _ in self.sender.sent if t == "ладно"), 2)
        self.assertEqual(self.sender.texts().count("первое"), 0)

    async def test_two_phone_requests_in_one_funnel(self):
        await self.begin(funnel="double")
        self.assertEqual(self.sender.texts(), ["номер1?"])
        self.assertEqual(await self.svc.contact(111, "+111", 111), "accepted")
        await self.drain()
        self.assertEqual(self.sender.texts(), ["номер1?", "ок1", "номер2?"])
        self.assertEqual(self.q("SELECT awaiting_step FROM runs")[0]["awaiting_step"], "ask2")
        self.assertEqual(await self.svc.decline(111), "declined")        # на второй — отказ
        await self.drain()
        self.assertEqual(self.sender.texts(), ["номер1?", "ок1", "номер2?", "нет2"])
        self.assertEqual(self.q("SELECT phone FROM users")[0]["phone"], "+111")
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        self.assert_one_open()

    async def test_stale_rule_applies_to_steps_after_branching(self):
        await self.begin()
        await self.svc.contact(111, "+37400000000", 111)
        n = len(self.sender.sent)
        await self.drain(advance=301)                          # простой дольше stale_after
        self.assertEqual(len(self.sender.sent), n)
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='thanks'")[0]["status"], "expired")
        self.assertEqual(self.q("SELECT * FROM queue WHERE status IN ('pending','processing')"), [])

    async def test_restart_mid_funnel_starts_new_run_from_scratch(self):
        await self.begin()
        await self.drain(advance=11)
        await self.svc.start(self.u)
        runs = self.q("SELECT status FROM runs ORDER BY id")
        self.assertEqual([r["status"] for r in runs], ["cancelled", "active"])
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m2'")[0]["status"], "cancelled")
        n = len(self.sender.sent)
        await self.drain()
        self.assertEqual(self.sender.texts()[n:], ["привет", "телефон?"])
        # ответ на старый запрос уходит в новый прогон: старый уже отменён и не получит «спасибо»
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        self.assertEqual(self.q("SELECT run_id FROM queue WHERE step_key='thanks'")[0]["run_id"],
                         self.q("SELECT id FROM runs WHERE status != 'cancelled'")[0]["id"])

    async def test_worker_loop_sends_greeting_promptly_after_start(self):
        """Настоящая петля run_forever: /start будит её, приветствие уходит не через секунду."""
        real_sleep = asyncio.sleep
        svc = FunnelService(self.db, self.cfg, self.sender, time.time, real_sleep, notify=None)
        worker = Worker(self.db, self.cfg, self.sender, time.time, real_sleep)
        svc._notify = worker.wake
        stop = asyncio.Event()
        loop_task = asyncio.create_task(worker.run_forever(stop))
        await real_sleep(0.05)                       # петля ушла спать
        t0 = time.time()
        await svc.start(self.u)
        while self.sender.texts()[:2] != ["привет", "телефон?"] and time.time() - t0 < 0.9:
            await real_sleep(0.01)
        stop.set()
        await loop_task
        self.assertEqual(self.sender.texts()[:2], ["привет", "телефон?"])
        self.assertLess(time.time() - t0, 0.9)

    async def test_db_forbids_two_open_rows_per_run(self):
        await self.begin()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("INSERT INTO queue(run_id,user_id,funnel,step_key,due_at,status) "
                                 "VALUES (1,111,'signup','m2',1,'pending')")


class TestConfig(unittest.TestCase):
    def test_errors_are_readable(self):
        bad = json.loads(json.dumps(RAW))
        bad["funnels"]["signup"]["steps"][3]["key"] = "m1"
        with self.assertRaisesRegex(ConfigError, "повторяется"):
            parse_config(bad)
        bad = json.loads(json.dumps(RAW))
        bad["funnels"]["signup"]["steps"][0]["kind"] = "poll"
        with self.assertRaisesRegex(ConfigError, "неизвестный вид"):
            parse_config(bad)

    def _broken(self, mutate, pattern):
        bad = json.loads(json.dumps(RAW))
        mutate(bad["funnels"]["signup"])
        with self.assertRaisesRegex(ConfigError, pattern):
            parse_config(bad)

    def test_links_between_steps_are_validated(self):
        self._broken(lambda f: f["steps"][1]["on_reply"].update(contact="нет-такого"), "шага 'нет-такого' нет")
        self._broken(lambda f: f["steps"][2].update(next="нет-такого"), "next")
        self._broken(lambda f: f["steps"][1]["on_reply"].pop("decline"), "не указан шаг для ответа 'decline'")
        self._broken(lambda f: f["steps"][1]["on_reply"].update(maybe="m1"), "неизвестный ответ")
        self._broken(lambda f: f["steps"][2].update(on_reply={"contact": "thanks", "decline": "declined"}),
                     "request_phone нет")
        self._broken(lambda f: f["steps"][0].update(key="end"), "служебное слово")
        self._broken(lambda f: (f["steps"][2].update(next="m2"), f["steps"][3].update(next="m1")), "зацикливается")

    def test_removed_welcome_and_phone_fields_are_rejected_loudly(self):
        self._broken(lambda f: f.update(welcome="привет"), "welcome: поле убрано")
        self._broken(lambda f: f.update(phone={"prompt": "x"}), "phone: поле убрано")

    def test_chain_order_and_branch_targets(self):
        f = parse_config(RAW).funnel("signup")
        order, cur = [], f.first
        while cur:
            order.append(cur.key)
            cur = f.next_step(cur)
        self.assertEqual(order, ["welcome", "ask", "m1", "m2"])    # «спасибо» и «ладно» по списку не достижимы
        self.assertEqual(f.branch_targets(), {"thanks", "declined"})

    def test_defaults_from_task(self):
        c = parse_config(RAW).funnel("signup")
        self.assertEqual([(s.key, s.delay) for s in c.steps if s.key in ("m1", "m2")], [("m1", 10.0), ("m2", 60.0)])

    def test_real_yaml_file_if_pyyaml_present(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML не установлен в этом окружении")
        from app.config import load_config
        cfg = load_config(os.path.join(ROOT, "funnels.yml"))
        steps = {s.key: s for s in cfg.funnel("signup").steps}
        self.assertEqual((steps["reminder_1"].delay, steps["reminder_2"].delay), (10.0, 60.0))
        self.assertEqual(dict(steps["ask_phone"].on_reply), {"contact": "thanks", "decline": "declined"})


class TestLock(unittest.TestCase):
    def test_second_process_refuses_with_message_and_kill9_releases(self):
        d = tempfile.mkdtemp()
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys,time; sys.path.insert(0, sys.argv[1]);"
             "from app.lock import ProcessLock; ProcessLock(sys.argv[2]).acquire(); print('held', flush=True); time.sleep(60)",
             ROOT, os.path.join(d, "bot.lock")], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            env = dict(os.environ, DB_PATH=os.path.join(d, "bot.db"), BOT_TOKEN=TOKEN)
            r = subprocess.run([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 1)
            self.assertIn("уже запущен", r.stderr)
            self.assertIn(str(holder.pid), r.stderr)
            holder.send_signal(signal.SIGKILL)       # грубая смерть
            holder.wait()
            ProcessLock(os.path.join(d, "bot.lock")).acquire()   # замок свободен сам
        finally:
            holder.kill()
            holder.wait()

    def test_in_process_second_acquire_fails(self):
        p = os.path.join(tempfile.mkdtemp(), "l")
        a = ProcessLock(p).acquire()
        with self.assertRaises(AlreadyRunning):
            ProcessLock(p).acquire()
        a.release()
        ProcessLock(p).acquire()


def make_init_data(token, user_id, auth_date, extra=None):
    fields = {"auth_date": str(auth_date), "query_id": "AAA", "user": json.dumps({"id": user_id, "first_name": "X"})}
    fields.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TestAuth(unittest.TestCase):
    now = 2_000_000

    def auth(self, init, **kw):
        args = dict(disabled=False, token=TOKEN, admin_ids={42}, max_age=3600, now=self.now)
        args.update(kw)
        return authorize(init, **args)[:2]  # (пустить?, показывать полные телефоны?)

    def test_valid_signature_admin_sees_full(self):
        self.assertEqual(self.auth(make_init_data(TOKEN, 42, self.now - 10)), (True, True))

    def test_valid_signature_not_admin_is_let_in_but_masked(self):
        self.assertEqual(self.auth(make_init_data(TOKEN, 43, self.now - 10)), (True, False))
        self.assertEqual(self.auth(make_init_data(TOKEN, 42, self.now - 10), admin_ids=set()), (True, False))

    def test_without_signature_closed(self):
        self.assertEqual(self.auth(""), (False, False))
        self.assertEqual(self.auth("user=%7B%22id%22%3A42%7D&auth_date=1"), (False, False))

    def test_wrong_token_tampered_and_stale_closed(self):
        good = make_init_data(TOKEN, 42, self.now - 10)
        self.assertEqual(self.auth(make_init_data("999:OTHER", 42, self.now - 10)), (False, False))
        self.assertEqual(self.auth(good.replace("auth_date=", "auth_date=1")), (False, False))
        self.assertEqual(self.auth(make_init_data(TOKEN, 42, self.now - 7200)), (False, False))

    def test_signature_field_is_part_of_check_string(self):
        self.assertTrue(self.auth(make_init_data(TOKEN, 42, self.now, {"signature": "abc"}))[0])

    def test_disabled_flag_opens_and_reveals(self):
        self.assertEqual(self.auth("", disabled=True), (True, True))


class TestPhoneMasking(unittest.TestCase):
    PHONES = {1: "+37499111222", 2: "+37477000111", 3: "37455123456", 4: "+1 (555) 12", 5: "1234"}

    def setUp(self):
        self.db = Database(os.path.join(tempfile.mkdtemp(), "m.db"))
        for i, ph in self.PHONES.items():
            self.db.conn.execute("INSERT INTO users(tg_id,name,phone,created_at,tags) VALUES (?,?,?,?,'[]')",
                                 (i, f"u{i}", ph, 1000.0 + i))
        self.db.conn.execute("INSERT INTO users(tg_id,name,created_at,tags) VALUES (6,'nophone',1006.0,'[]')")
        # уровни считаются по событиям: без них уровни пусты, и проверка «номера нет в ответе» ничего не проверяла бы
        for i, ph in self.PHONES.items():
            self.db.log_event(1000.0 + i, "run_started", i, None)
            self.db.log_event(1000.0 + i, "message_sent", i, None, "welcome")
            self.db.log_event(1000.0 + i, "phone_requested", i, None, "ask")
            self.db.log_event(1000.0 + i, "phone_given", i, None, "ask")
        self.db.log_event(1006.0, "run_started", 6, None)

    def test_mask_format(self):
        self.assertEqual(stats.mask_phone("+37499111222", False), "•••••••1222")
        self.assertEqual(stats.mask_phone("+1 (555) 12", False), "••5512")
        self.assertEqual(stats.mask_phone("1234", False), "••••")
        self.assertIsNone(stats.mask_phone(None, False))
        self.assertEqual(stats.mask_phone("+37499111222", True), "+37499111222")

    def test_non_admin_json_has_no_full_number_at_any_level(self):
        for lv in stats.LEVELS:
            rows = stats.level_users(self.db.conn, lv)  # reveal по умолчанию False
            body = json.dumps(rows, ensure_ascii=False)
            for ph in self.PHONES.values():
                digits = "".join(c for c in ph if c.isdigit())
                self.assertNotIn(ph, body)
                if len(digits) > 4:
                    self.assertNotIn(digits[:-4], body)       # префикс без последних 4 цифр
                    self.assertNotIn(digits[-5:], body)        # и пятая с конца цифра
        phone_level = {r["tg_id"]: r["phone"] for r in stats.level_users(self.db.conn, "phone")}
        self.assertEqual(phone_level[1], "•••••••1222")       # «дал телефон» виден, номер нет
        self.assertEqual(len(phone_level), 5)
        self.assertNotIn(6, phone_level)
        # весь обзор (включая журнал) тоже без номеров
        ov = json.dumps(stats.overview(self.db.conn, parse_config(RAW)), ensure_ascii=False)
        self.assertNotIn("37499111222", ov)
        self.assertNotIn("499111222", ov)

    def test_admin_sees_full(self):
        rows = {r["tg_id"]: r["phone"] for r in stats.level_users(self.db.conn, "phone", reveal=True)}
        self.assertEqual(rows[1], "+37499111222")
        self.assertEqual(rows[3], "37455123456")

    def test_static_page_never_contains_a_number(self):
        body = open(os.path.join(ROOT, "app", "static", "index.html"), encoding="utf-8").read()
        for ph in self.PHONES.values():
            self.assertNotIn(ph, body)


class TestEnvPriority(unittest.TestCase):
    """Окружение сильнее .env: в Docker переменные приходят из compose, файл их перебивать не должен."""

    def _settings(self, dotenv, **env):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".env"), "w", encoding="utf-8") as f:
            f.write(dotenv)
        code = ("import sys, json; sys.path.insert(0, sys.argv[1])\n"
                "from app.envfile import load_env\nfrom app.settings import EnvSettings\n"
                "load_env(); s = EnvSettings.from_env()\n"
                "print(json.dumps([s.web_host, s.web_port, sorted(s.admin_ids), s.auth_disabled]))")
        base = {k: v for k, v in os.environ.items()
                if k not in ("WEB_HOST", "WEB_PORT", "ADMIN_IDS", "DASHBOARD_AUTH_DISABLED")}
        r = subprocess.run([sys.executable, "-I", "-c", code, ROOT], cwd=d, env=dict(base, **env),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_env_beats_dotenv_and_reaches_settings(self):
        got = self._settings("WEB_HOST=127.0.0.1\nWEB_PORT=8080\nADMIN_IDS=1\nDASHBOARD_AUTH_DISABLED=1\n",
                             WEB_HOST="0.0.0.0", WEB_PORT="18080", ADMIN_IDS="42", DASHBOARD_AUTH_DISABLED="0")
        self.assertEqual(got, ["0.0.0.0", 18080, [42], False])

    def test_dotenv_fills_only_what_env_lacks(self):
        got = self._settings("WEB_PORT=8081\nADMIN_IDS=5\n", WEB_PORT="18080")
        self.assertEqual(got, ["127.0.0.1", 18080, [5], False])

    def test_duplicate_key_in_file_last_wins(self):
        self.assertEqual(self._settings("WEB_PORT=1111\nWEB_PORT=2222\n")[1], 2222)

    def test_empty_env_value_still_beats_dotenv(self):
        # compose может передать пустое значение; это тоже «заданное окружением»
        self.assertEqual(self._settings("ADMIN_IDS=5\n", ADMIN_IDS="")[2], [])


class TestSecretsAndDb(unittest.TestCase):
    def test_wal_and_busy_timeout(self):
        db = Database(os.path.join(tempfile.mkdtemp(), "w.db"))
        self.assertEqual(db.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_no_token_in_static_pages_or_api_payloads(self):
        static = os.path.join(ROOT, "app", "static")
        for name in os.listdir(static):
            body = open(os.path.join(static, name), encoding="utf-8").read()
            self.assertNotIn(TOKEN, body)
            import re
            self.assertIsNone(re.search(r"\d{6,}:[A-Za-z0-9_-]{20,}", body), name)
            self.assertNotRegex(body, r"https?://(?!t\.me)", name)  # без внешних ресурсов

    def test_api_payload_has_no_token(self):
        db = Database(os.path.join(tempfile.mkdtemp(), "p.db"))
        payload = json.dumps(stats.overview(db.conn, parse_config(RAW)))
        self.assertNotIn(TOKEN, payload)
        self.assertNotIn("api.telegram.org", payload)


if __name__ == "__main__":
    unittest.main()
