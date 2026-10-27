"""Воронка из блоков: сообщение, задержка, условие. Python stdlib, время — подставные часы."""
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from app import plan, stats  # noqa: E402
from app.checks import CHECKS  # noqa: E402
from app.config import CHECK_KINDS, ConfigError, clean_tags, parse_config  # noqa: E402
from app.service import FunnelService, UserInfo, add_tag  # noqa: E402
from app.worker import Worker  # noqa: E402
from test_core import Base, RAW  # noqa: E402


def msg(key, text=None, **kw):
    return {"key": key, "kind": "message", "text": text or key.upper(), **kw}


def cond(key, on_true, on_false, tag="vip"):
    return {"key": key, "kind": "condition", "check": {"kind": "tag", "tag": tag}, "on_true": on_true, "on_false": on_false}


def delay(key, duration):
    return {"key": key, "kind": "delay", "duration": duration}


def tags(key, *names):
    return {"key": key, "kind": "tags", "tags": list(names)}


def raw_with(**funnels):
    return {"settings": {"default_funnel": next(iter(funnels)), "stale_after": "5m", "send_pause": 0},
            "funnels": {k: {"steps": v} for k, v in funnels.items()}}


class Blocks(Base):
    def use(self, steps, key="f"):
        cfg = parse_config(raw_with(**{key: steps}))
        self.cfg = cfg
        self.svc.cfg = cfg
        self.worker.cfg = cfg
        return cfg

    def open_rows(self):
        return self.q("SELECT step_key FROM queue WHERE status IN ('pending','processing')")


class TestShippedFunnel(Blocks):
    """Поставляемая воронка в блоках ведёт себя снаружи как прежняя."""

    async def test_without_phone_welcome_request_then_reminders_at_10s_and_1m(self):
        await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.sender.texts(), ["привет", "телефон?"])
        t0 = self.q("SELECT sent_at FROM queue WHERE step_key='ask'")[0]["sent_at"]
        await self.drain(advance=9)
        self.assertEqual(self.sender.texts(), ["привет", "телефон?"])
        await self.drain(advance=2)                                           # 11 с от запроса
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "первое"])
        t1 = self.q("SELECT sent_at FROM queue WHERE step_key='m1'")[0]["sent_at"]
        await self.drain(advance=59)
        self.assertEqual(len(self.sender.texts()), 3)
        await self.drain(advance=2)
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "первое", "второе"])
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        self.assertEqual(self.open_rows(), [])
        self.assertGreaterEqual(t1 - t0, 10)
        self.assertLess(t1 - t0, 12)

    async def test_with_phone_one_message_and_end(self):
        await self.svc.start(self.u)
        await self.drain()
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        n = len(self.sender.sent)
        await self.svc.start(self.u)
        await self.drain(advance=100)
        self.assertEqual(self.sender.texts()[n:], ["ЗАЯВКА У МЕНЕДЖЕРА"])
        self.assertEqual(self.q("SELECT COUNT(*) n FROM queue WHERE run_id=2 AND step_key='has_lead'")[0]["n"], 0)

    async def test_delay_step_gives_same_due_as_old_field(self):
        await self.svc.start(self.u)
        await self.drain()
        sent = self.q("SELECT sent_at FROM queue WHERE step_key='ask'")[0]["sent_at"]
        d1 = self.q("SELECT due_at FROM queue WHERE step_key='d1'")[0]["due_at"]
        self.assertEqual(d1, sent + 10)
        await self.drain(advance=10)
        m1 = self.q("SELECT due_at FROM queue WHERE step_key='m1'")[0]["due_at"]
        self.assertEqual(m1, d1)                              # сообщение после паузы — в срок паузы
        # ни пауза, ни условие ничего не отправили
        self.assertEqual(self.sender.texts(), ["привет", "телефон?", "первое"])
        kinds = [e["kind"] for e in self.q("SELECT kind FROM events")]
        self.assertEqual(kinds.count("message_sent"), 3)

    async def test_delay_expires_by_stale_rule_after_downtime(self):
        await self.svc.start(self.u)
        await self.drain()
        await self.drain(advance=10 + 301)                    # простой дольше stale_after
        self.assertEqual(self.sender.texts(), ["привет", "телефон?"])
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='d1'")[0]["status"], "expired")


class TestCondition(Blocks):
    EXITS = ("next", "z", "end")

    async def run_matrix(self, result: bool):
        seen = {}
        for ex in self.EXITS:
            # шаги: c, a (дальше z), z. Проверяемый выход — на нужной ветке; на другой — такой, чтобы все шаги были достижимы
            other = "z" if ex == "next" else "next"
            on_true, on_false = (ex, other) if result else (other, ex)
            self.use([cond("c", on_true, on_false), msg("a", next="z"), msg("z", next="end")])
            u = UserInfo(1000 + len(seen), "U")
            if result:
                await self.svc.start(u)       # создаст пользователя
                with self.db.tx() as c:
                    add_tag(c, u.tg_id, "vip")
            before = len(self.sender.sent)
            await self.svc.start(u)
            await self.drain()
            seen[ex] = [t for c, t, _ in self.sender.sent[before:] if c == u.tg_id]
            runs = self.q("SELECT status FROM runs WHERE user_id=? ORDER BY id DESC LIMIT 1", u.tg_id)
            self.assertEqual(runs[0]["status"], "completed", ex)
        return seen

    async def test_true_exit_all_three(self):
        seen = await self.run_matrix(True)
        self.assertEqual(seen["next"], ["A", "Z"])  # следующий по списку: a, а у него next: z
        self.assertEqual(seen["z"], ["Z"])
        self.assertEqual(seen["end"], [])

    async def test_false_exit_all_three(self):
        seen = await self.run_matrix(False)
        self.assertEqual(seen["next"], ["A", "Z"])
        self.assertEqual(seen["z"], ["Z"])
        self.assertEqual(seen["end"], [])

    async def test_condition_chain_passes_without_sending_or_queue_rows(self):
        self.use([cond("c1", "next", "next"), cond("c2", "next", "next"), cond("c3", "end", "end")])
        await self.svc.start(self.u)
        self.assertEqual(self.q("SELECT * FROM queue"), [])
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        self.assertEqual([e["details"] for e in self.q("SELECT details FROM events WHERE kind='condition' ORDER BY id")],
                         ["c1: нет → c2", "c2: нет → c3", "c3: нет → конец"])

    async def test_condition_after_message_and_delay_is_evaluated_at_its_turn(self):
        self.use([msg("a", next="d"), delay("d", "5s"), cond("c", "end", "b"), msg("b", next="end")])
        await self.svc.start(self.u)
        await self.drain()
        with self.db.tx() as c:
            add_tag(c, 111, "vip")
        await self.drain(advance=6)
        self.assertEqual(self.sender.texts(), ["A"])           # условие на момент прохода: тег есть -> конец
        self.assertEqual(self.open_rows(), [])


class TestCycles(Blocks):
    def test_cycle_without_delay_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "цикл без задержки"):
            parse_config(raw_with(f=[cond("c1", "c2", "end"), cond("c2", "c1", "end")]))
        with self.assertRaisesRegex(ConfigError, "цикл без задержки"):
            parse_config(raw_with(f=[msg("a", next="b"), msg("b", next="a")]))
        with self.assertRaisesRegex(ConfigError, "цикл без задержки"):    # цикл через условие и сообщение
            parse_config(raw_with(f=[cond("c", "a", "end"), msg("a", next="c")]))

    def test_cycle_with_delay_is_accepted(self):
        parse_config(raw_with(f=[msg("a", next="d"), delay("d", "1m"), cond("c", "a", "end")]))

    async def test_cycle_with_delay_runs_laps_with_one_open_row(self):
        # a -> пауза 30 с -> условие: метки нет — снова a; метка есть — конец
        self.use([msg("a", next="d"), delay("d", "30s"), cond("c", "end", "a")])
        await self.svc.start(self.u)
        for lap in range(3):
            await self.drain()
            self.assertLessEqual(len(self.open_rows()), 1)
            await self.drain(advance=31)
            self.assertLessEqual(len(self.open_rows()), 1)
        self.assertEqual(self.sender.texts(), ["A", "A", "A", "A"])
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "active")
        with self.db.tx() as c:
            add_tag(c, 111, "vip")
        await self.drain(advance=31)
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        self.assertEqual(self.open_rows(), [])


class TestSilentLimit(Blocks):
    async def test_limit_closes_run_and_logs_reason(self):
        chain = [cond(f"c{i}", "next", "next") for i in range(6)] + [msg("m", next="end")]
        self.use(chain)                                        # конфиг законный: цикла нет
        with mock.patch.object(plan, "MAX_SILENT_STEPS", 3):
            await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "cancelled")
        self.assertEqual(self.q("SELECT * FROM queue"), [])
        self.assertEqual(self.sender.sent, [])
        ev = self.q("SELECT details FROM events WHERE kind='run_cancelled'")
        self.assertEqual(len(ev), 1)
        self.assertIn("предел шагов без отправки (3)", ev[0]["details"])
        self.assertIn("c3", ev[0]["details"])

    async def test_default_limit_is_not_hit_by_normal_funnels(self):
        await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "active")


class TestDelayInFlight(Blocks):
    async def test_contact_while_delay_row_is_processing_is_not_lost(self):
        await self.svc.start(self.u)
        await self.drain()
        self.clock.t += 11
        row = self.worker.claim()
        self.assertEqual(row["step_key"], "d1")               # пауза взята в работу
        self.assertEqual(await self.svc.contact(111, "+37400000000", 111), "accepted")
        self.assertEqual(self.q("SELECT step_key FROM queue WHERE step_key='thanks'"), [])   # ждёт, пока пауза закончится
        funnel = self.cfg.funnel("signup")
        self.worker._mark_sent(row, funnel, funnel.step("d1"))
        await self.drain()
        self.assertEqual(self.sender.texts()[-1], "спасибо")
        self.assertEqual(self.sender.texts().count("первое"), 0)
        self.assertEqual(self.open_rows(), [])


class TestTagsStep(Blocks):
    """Шаг «метки»: ставит метки, ничего не отправляет, проходится насквозь."""

    def user_tags(self):
        return json.loads(self.q("SELECT tags FROM users WHERE tg_id=111")[0]["tags"])

    async def test_tag_set_and_seen_by_condition(self):
        self.use([tags("t", "vip"), cond("c", "yes", "no"), msg("no", next="end"), msg("yes", next="end")])
        await self.svc.start(self.u)
        await self.drain()
        self.assertIn("vip", self.user_tags())
        self.assertEqual(self.sender.texts(), ["YES"])         # условие по метке увидело то, что поставил шаг

    async def test_several_tags_at_once(self):
        self.use([tags("t", "lead", "funnel_msg_1_sent", "Ключ2"), msg("m", next="end")])
        await self.svc.start(self.u)
        for t in ("lead", "funnel_msg_1_sent", "Ключ2"):
            self.assertIn(t, self.user_tags())

    async def test_repeat_does_not_duplicate(self):
        self.use([tags("t1", "vip"), tags("t2", "vip", "vip"), msg("m", next="end")])
        await self.svc.start(self.u)
        await self.svc.start(self.u)                           # второй проход тем же пользователем
        await self.drain()
        self.assertEqual(self.user_tags().count("vip"), 1)

    async def test_no_queue_rows_and_no_sends(self):
        self.use([tags("t1", "a"), tags("t2", "b"), cond("c", "end", "end", tag="a")])
        await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.q("SELECT * FROM queue"), [])
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "completed")
        ev = self.q("SELECT details FROM events WHERE kind='tags_set' ORDER BY id")
        self.assertEqual([e["details"] for e in ev], ["t1: a", "t2: b"])

    async def test_tags_after_delay_and_message(self):
        self.use([msg("m", next="d"), delay("d", "10s"), tags("t", "reminded"), msg("z", next="end")])
        await self.svc.start(self.u)
        await self.drain()
        self.assertNotIn("reminded", self.user_tags())
        await self.drain(advance=11)
        self.assertIn("reminded", self.user_tags())
        self.assertEqual(self.sender.texts(), ["M", "Z"])

    async def test_counts_towards_silent_limit(self):
        self.use([tags(f"t{i}", f"x{i}") for i in range(5)] + [msg("m", next="end")])
        with mock.patch.object(plan, "MAX_SILENT_STEPS", 3):
            await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.q("SELECT status FROM runs")[0]["status"], "cancelled")
        self.assertIn("предел шагов без отправки (3)", self.q("SELECT details FROM events WHERE kind='run_cancelled'")[0]["details"])

    def test_cycle_of_tag_steps_without_delay_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "цикл без задержки"):
            parse_config(raw_with(f=[tags("a", "x"), tags("b", "y"), cond("c", "a", "end")]))
        parse_config(raw_with(f=[tags("a", "x"), delay("d", "1s"), cond("c", "a", "end")]))   # с задержкой законно

    def test_config_rejects_bad_tags(self):
        for bad in ([], ["ok", "две метки"], ["a-b"], ["a,b"], [""], "vip"):
            with self.assertRaisesRegex(ConfigError, "tags"):
                parse_config(raw_with(f=[{"key": "t", "kind": "tags", "tags": bad}]))
        with self.assertRaisesRegex(ConfigError, "tags"):
            parse_config(raw_with(f=[{"key": "t", "kind": "tags"}]))

    def test_clean_tags_drops_garbage_keeps_good(self):
        self.assertEqual(clean_tags("lead, funnel_msg_1_sent, bad tag, a-b, , x,y, lead, !!"),
                         ["lead", "funnel_msg_1_sent", "x", "y"])
        self.assertEqual(clean_tags(""), [])
        self.assertEqual(clean_tags(None), [])


class TestChecks(Blocks):
    """Проверок ровно две: «метка» читает метки, «номер» читает поле телефона. Друг от друга не зависят."""

    def pcond(self, kind):
        check = {"kind": "tag", "tag": "signup_lead"} if kind == "tag" else {"kind": "phone"}
        return [{"key": "c", "kind": "condition", "check": check, "on_true": "yes", "on_false": "no"},
                msg("no", next="end"), msg("yes", next="end")]

    async def go(self, kind, phone=None, tag=None):
        self.use(self.pcond(kind))
        u = UserInfo(5000 + len(self.sender.sent), "U")
        await self.svc.start(u)                                          # заводит пользователя
        with self.db.tx() as c:
            if phone:
                c.execute("UPDATE users SET phone=? WHERE tg_id=?", (phone, u.tg_id))
            if tag:
                add_tag(c, u.tg_id, tag)
        before = len(self.sender.sent)
        await self.svc.start(u)
        await self.drain()
        return [t for c, t, _ in self.sender.sent[before:] if c == u.tg_id]

    async def test_phone_check_true_when_phone_in_db_without_any_tag(self):
        self.assertEqual(await self.go("phone", phone="+37400000001"), ["YES"])

    async def test_phone_check_false_without_phone(self):
        self.assertEqual(await self.go("phone"), ["NO"])

    async def test_phone_check_ignores_tags(self):
        # метка «phone»/«phone:f» (старый механизм) есть, поля нет — проверка по номеру не срабатывает
        self.assertEqual(await self.go("phone", tag="phone"), ["NO"])

    async def test_tag_check_ignores_phone_field(self):
        self.assertEqual(await self.go("tag", phone="+37400000002"), ["NO"])

    async def test_tag_check_true_with_tag_and_no_phone(self):
        self.assertEqual(await self.go("tag", tag="signup_lead"), ["YES"])

    def test_old_check_name_rejected_with_explanation(self):
        raw = raw_with(f=[{"key": "c", "kind": "condition", "check": {"kind": "phone_given"},
                           "on_true": "end", "on_false": "end"}])
        with self.assertRaises(ConfigError) as cm:
            parse_config(raw)
        text = str(cm.exception)
        self.assertIn("phone_given", text)
        self.assertIn("убрана", text)
        self.assertIn("'phone'", text)
        self.assertIn("'tag'", text)

    def test_unknown_check_lists_the_two(self):
        raw = raw_with(f=[{"key": "c", "kind": "condition", "check": {"kind": "zzz"},
                           "on_true": "end", "on_false": "end"}])
        with self.assertRaisesRegex(ConfigError, r"есть: tag, phone"):
            parse_config(raw)

    def test_tag_check_needs_tag_and_phone_check_needs_nothing(self):
        raw = raw_with(f=[{"key": "c", "kind": "condition", "check": {"kind": "tag"}, "on_true": "end", "on_false": "end"}])
        with self.assertRaises(ConfigError):
            parse_config(raw)
        parse_config(raw_with(f=[{"key": "c", "kind": "condition", "check": {"kind": "phone"},
                                  "on_true": "end", "on_false": "end"}]))


class TestShippedLead(Blocks):
    """Поставляемая воронка сама ставит signup_lead и сама её проверяет."""

    async def test_tags_step_sets_signup_lead_and_entry_condition_sees_it(self):
        await self.svc.start(self.u)
        await self.drain()
        tags_before = json.loads(self.q("SELECT tags FROM users WHERE tg_id=111")[0]["tags"])
        self.assertNotIn("signup_lead", tags_before)
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        self.assertIn("signup_lead", json.loads(self.q("SELECT tags FROM users WHERE tg_id=111")[0]["tags"]))
        self.assertEqual(self.q("SELECT COUNT(*) n FROM queue WHERE step_key='set_lead'")[0]["n"], 0)  # в очередь не встаёт
        self.assertEqual(self.q("SELECT status FROM runs ORDER BY id DESC")[0]["status"], "completed")
        n = len(self.sender.sent)
        await self.svc.start(self.u)
        await self.drain(advance=100)
        self.assertEqual(self.sender.texts()[n:], ["ЗАЯВКА У МЕНЕДЖЕРА"])

    async def test_decline_and_silence_start_over(self):
        await self.svc.start(self.u)
        await self.drain()
        await self.svc.decline(111)
        await self.drain()
        n = len(self.sender.sent)
        await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.sender.texts()[n:], ["привет", "телефон?"])
        await self.drain(advance=11)
        await self.drain(advance=70)                          # напоминания кончились без ответа
        n = len(self.sender.sent)
        await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.sender.texts()[n:], ["привет", "телефон?"])
        self.assertNotIn("ЗАЯВКА У МЕНЕДЖЕРА", self.sender.texts())

    async def test_funnel_works_with_prefix_changed_or_removed(self):
        """Префикс signup_ — договорённость, не механизм: переименованная метка в обоих местах работает так же."""
        steps = [dict(x) for x in RAW["funnels"]["signup"]["steps"]]
        for x in steps:
            if x["kind"] == "tags":
                x["tags"] = ["lead"]
            if x["kind"] == "condition":
                x["check"] = {"kind": "tag", "tag": "lead"}
        self.use(steps, key="signup")
        await self.svc.start(self.u)
        await self.drain()
        await self.svc.contact(111, "+37400000000", 111)
        await self.drain()
        n = len(self.sender.sent)
        await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.sender.texts()[n:], ["ЗАЯВКА У МЕНЕДЖЕРА"])


class TestRegistries(unittest.TestCase):
    def test_check_registries_match(self):
        self.assertEqual(set(CHECKS), set(CHECK_KINDS))


class TestStatsWithBlocks(Blocks):
    async def test_steps_stats_include_delay_and_condition(self):
        await self.svc.start(self.u)
        await self.drain(advance=0)
        await self.drain(advance=11)
        rows = {s["step"]: s for s in stats.steps(self.db.conn, self.cfg, "signup")}
        self.assertEqual(rows["d1"]["sent"], 1)
        self.assertEqual(rows["has_lead"]["sent"], 0)
        json.dumps(stats.overview(self.db.conn, self.cfg, "signup"))


if __name__ == "__main__":
    unittest.main()
