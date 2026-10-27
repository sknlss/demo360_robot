"""Тела ответов дашборда (app/api.py): уровни по воронкам, структура шагов, список всех пользователей."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from app import api, stats  # noqa: E402
from app.config import ConfigError, funnel_to_dict, parse_config  # noqa: E402
from app.service import has_tag  # noqa: E402
from test_core import Base, RAW  # noqa: E402


def levels(conn, cfg, funnel=None):
    return {l["key"]: l["count"] for l in stats.overview(conn, cfg, funnel)["levels"]}


class TestLevels(Base):
    async def test_three_levels_from_events_nested(self):
        await self.begin()
        self.assertEqual(levels(self.db.conn, self.cfg), {"started": 1, "requested": 1, "phone": 0})
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        self.assertEqual(levels(self.db.conn, self.cfg), {"started": 1, "requested": 1, "phone": 1})

    async def test_started_but_not_yet_requested(self):
        """Запустил, но запрос номера ещё не получил: приветствие ушло, шаг с запросом ждёт в очереди."""
        await self.svc.start(self.u)
        self.assertEqual(levels(self.db.conn, self.cfg), {"started": 1, "requested": 0, "phone": 0})

    async def test_funnel_without_phone_request_has_zero_second_and_third_level(self):
        await self.begin(funnel="plain")
        await self.drain(advance=6)
        self.assertEqual(levels(self.db.conn, self.cfg), {"started": 1, "requested": 0, "phone": 0})
        self.assertEqual(api.users_payload(self.db.conn, self.cfg, "requested")["users"], [])

    async def test_levels_are_counted_per_funnel(self):
        await self.begin()                                   # 111: signup, запрос получил
        await self.begin(user=type(self.u)(222, "Второй"), funnel="plain")
        self.assertEqual(levels(self.db.conn, self.cfg, "signup"), {"started": 1, "requested": 1, "phone": 0})
        self.assertEqual(levels(self.db.conn, self.cfg, "plain"), {"started": 1, "requested": 0, "phone": 0})
        self.assertEqual(levels(self.db.conn, self.cfg), {"started": 2, "requested": 1, "phone": 0})
        self.assertEqual([u["tg_id"] for u in api.users_payload(self.db.conn, self.cfg, "started",
                                                                funnel="plain")["users"]], [222])
        self.assertEqual([s["step"] for s in stats.overview(self.db.conn, self.cfg, "plain")["steps"]], ["a", "wb", "b"])

    async def test_request_delivered_inside_cancelled_run_still_counts(self):
        await self.svc.start(self.u)
        await self.drain()                                   # welcome и ask
        await self.svc.start(self.u)                         # повторный старт: новый прогон, старый отменён
        # шаг запроса старого прогона был в работе: доводим отправку до конца уже в отменённом прогоне
        self.db.conn.execute("UPDATE runs SET status='cancelled' WHERE id=1")
        self.db.conn.execute("DELETE FROM events WHERE kind='phone_requested'")
        row = {"id": 0, "run_id": 1, "user_id": 111, "funnel": "signup", "step_key": "ask"}
        self.db.conn.execute("INSERT INTO queue(id,run_id,user_id,funnel,step_key,due_at,status) "
                             "VALUES (99,1,111,'signup','ask',0,'processing')")
        row["id"] = 99
        f = self.cfg.funnel('signup')
        self.worker._mark_sent(row, f, f.step('ask'))
        ev = self.q("SELECT run_id FROM events WHERE kind='phone_requested'")
        self.assertEqual(ev, [{"run_id": 1}])
        self.assertEqual(levels(self.db.conn, self.cfg)["requested"], 1)

    async def test_overview_unknown_funnel_is_none(self):
        self.assertIsNone(api.overview_payload(self.db.conn, self.cfg, "nope"))
        self.assertIsNone(api.users_payload(self.db.conn, self.cfg, None, "nope"))
        self.assertIsNone(api.users_payload(self.db.conn, self.cfg, "bad-level"))


class TestFunnelsPayload(Base):
    def test_structure_has_every_step_field_and_is_json(self):
        p = api.funnels_payload(self.db.conn, self.cfg)
        json.dumps(p, ensure_ascii=False)
        sig = next(f for f in p["funnels"] if f["key"] == "signup")
        self.assertTrue(sig["is_default"])
        self.assertEqual([s["key"] for s in sig["steps"]], ["has_lead", "welcome", "ask", "d1", "m1", "d2", "m2", "already", "thanks", "set_lead", "declined"])
        ask = sig["steps"][2]
        self.assertEqual(ask["request_phone"], {"button": "дать", "decline_button": "нет"})
        self.assertEqual(ask["on_reply"], {"contact": "thanks", "decline": "declined"})
        self.assertEqual(sig["steps"][3]["duration"], 10.0)
        self.assertEqual(sig["steps"][3]["duration_text"], "10s")
        cond = sig["steps"][0]
        self.assertEqual((cond["check"], cond["on_true"], cond["on_false"]), ({"kind": "tag", "tag": "signup_lead"}, "already", "next"))
        self.assertEqual(cond["derived"]["exits"], {"on_true": "already", "on_false": "welcome"})   # «next» разрешён в ключ
        self.assertNotIn("entry_conditions", sig)
        self.assertEqual([l["key"] for l in sig["levels"]], ["started", "requested", "phone"])

    def test_derived_is_separate_from_source_fields(self):
        f = funnel_to_dict(self.cfg.funnel("signup"))
        thanks = next(s for s in f["steps"] if s["key"] == "thanks")
        self.assertEqual(thanks["derived"]["reached_by"], [{"step": "ask", "reply": "contact"}])
        self.assertEqual(thanks["derived"]["next_resolved"], "set_lead")   # дальше шаг меток, за ним конец
        d1 = next(s for s in f["steps"] if s["key"] == "d1")
        self.assertEqual(d1["derived"]["next_resolved"], "m1")           # пауза ведёт на следующий по списку
        for src in ("key", "kind", "duration", "duration_text"):
            self.assertIn(src, d1)
        self.assertNotIn("next_resolved", d1)                # вычисленное только внутри derived
        already = next(s for s in f["steps"] if s["key"] == "already")
        self.assertEqual(already["derived"]["reached_by"], [{"step": "has_lead", "reply": "on_true"}])

    def test_payload_has_no_phone_numbers(self):
        self.db.conn.execute("INSERT INTO users(tg_id,name,phone,created_at,tags) VALUES (5,'x','+37499111222',1,'[]')")
        body = json.dumps(api.funnels_payload(self.db.conn, self.cfg))
        self.assertNotIn("37499111222", body)


class TestUsersAll(Base):
    def setUp(self):
        super().setUp()
        for i in range(1, 6):
            self.db.conn.execute("INSERT INTO users(tg_id,name,phone,created_at,tags) VALUES (?,?,?,?,'[]')",
                                 (i, f"u{i}", "+3749911122%d" % i if i != 3 else None, 1000.0 + i))

    def test_all_users_without_filters_masked_for_non_admin(self):
        p = api.users_payload(self.db.conn, self.cfg)
        self.assertEqual(p["total"], 5)
        self.assertEqual([u["tg_id"] for u in p["users"]], [5, 4, 3, 2, 1])      # новые сверху
        body = json.dumps(p, ensure_ascii=False)
        for i in (1, 2, 4, 5):
            self.assertNotIn("3749911122%d" % i, body)
            self.assertNotIn("374991112", body)
        self.assertEqual(next(u for u in p["users"] if u["tg_id"] == 1)["phone"], "•••••••1221")
        self.assertIsNone(next(u for u in p["users"] if u["tg_id"] == 3)["phone"])   # нет телефона — null, а не маска

    def test_admin_sees_full(self):
        p = api.users_payload(self.db.conn, self.cfg, reveal=True)
        self.assertIn("+37499111221", [u["phone"] for u in p["users"]])

    def test_limit_is_reported_not_hidden(self):
        old = api.USERS_LIMIT
        api.USERS_LIMIT = 2
        try:
            p = api.users_payload(self.db.conn, self.cfg)
        finally:
            api.USERS_LIMIT = old
        self.assertEqual((len(p["users"]), p["total"], p["limit"]), (2, 5, 2))


class TestOldFormatRejected(unittest.TestCase):
    """Конфиг прежнего формата (у Олега на сервере может лежать он) не принимается молча: ошибка говорит, что делать."""

    def test_entry_conditions_removed(self):
        raw = json.loads(json.dumps(RAW))
        raw["funnels"]["signup"]["entry_conditions"] = [{"kind": "phone_given", "reply": "x"}]
        with self.assertRaises(ConfigError) as cm:
            parse_config(raw)
        self.assertIn("entry_conditions: раздел убран", str(cm.exception))
        self.assertIn("kind: condition", str(cm.exception))

    def test_message_delay_removed(self):
        raw = json.loads(json.dumps(RAW))
        raw["funnels"]["signup"]["steps"][1]["delay"] = "10s"
        with self.assertRaises(ConfigError) as cm:
            parse_config(raw)
        self.assertIn("kind: delay", str(cm.exception))


class TestPageSource(unittest.TestCase):
    """Статические свойства страницы. Поведение в браузере проверено руками (см. отчёт): JS в контейнере запустить нечем."""
    with open(os.path.join(ROOT, "app", "static", "index.html"), encoding="utf-8") as f:
        HTML = f.read()

    def test_step_is_rendered_by_one_function_with_per_kind_bodies(self):
        self.assertEqual(self.HTML.count("function renderStep("), 1)
        self.assertEqual(self.HTML.count('"card sc'), 1)           # разметка шага собирается в одном месте
        self.assertIn("STEP_BODY", self.HTML)
        self.assertIn("opts.onOpen", self.HTML)                    # нажимается только если передан onOpen

    def test_navigation_does_not_touch_address(self):
        # хеш несёт подпись Telegram: перейти в другой хеш или pushState — потерять её при обновлении страницы
        for bad in ("location.hash =", "location.hash=", "pushState", "replaceState", "location.href =", "location.assign"):
            self.assertNotIn(bad, self.HTML)

    def test_every_request_carries_signature_via_one_function(self):
        self.assertEqual(self.HTML.count("fetch("), 1)
        self.assertIn('"X-Init-Data": initData', self.HTML)

    def test_new_event_has_a_label(self):
        self.assertIn("phone_requested:", self.HTML)


if __name__ == "__main__":
    unittest.main()
