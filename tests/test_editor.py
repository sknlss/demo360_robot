"""Редактор: проверка и запись конфига, подхват без перезапуска, исчезнувший шаг, права на запись.

Кодек — JsonCodec (JSON — подмножество YAML): ruamel/PyYAML в этом окружении нет, а логика правки одна и та же.
Сохранение комментариев проверяет tests/test_yaml_roundtrip.py (нужен ruamel.yaml)."""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from app import api  # noqa: E402
from app.config import ConfigError, parse_config  # noqa: E402
from app.config_edit import EditError, JsonCodec, save_funnel_steps  # noqa: E402
from app.configstore import ConfigStore  # noqa: E402
from app.service import UserInfo  # noqa: E402
from test_core import RAW, Base, FakeSender  # noqa: E402

CODEC = JsonCodec()

# воронка для правок без условия и «заявки у менеджера»: welcome, ask, d1(10s), m1, d2(1m), m2, thanks, declined.
# Условие и пауза проверяются отдельно (TestBlocksEditing)
E_RAW = copy.deepcopy(RAW)
E_RAW["funnels"]["signup"]["steps"] = [x for x in RAW["funnels"]["signup"]["steps"] if x["key"] not in ("has_phone", "already")]


def loader(p):
    with open(p, encoding="utf-8") as f:
        return parse_config(json.load(f))


def write_json(path, raw):
    with open(path, "w", encoding="utf-8") as f:
        f.write(CODEC.dump(raw))


def steps_of(raw, funnel="signup"):
    return copy.deepcopy(raw["funnels"][funnel]["steps"])


class EditBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "funnels.json")
        write_json(self.path, E_RAW)
        self.store = ConfigStore(self.path, loader=loader)

    def tearDown(self):
        self.tmp.cleanup()

    def text(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def save(self, steps, funnel="signup"):
        return save_funnel_steps(self.path, funnel, steps, codec=CODEC)


class TestSave(EditBase):
    def test_roundtrip_without_changes_does_not_touch_file(self):
        before, st = self.text(), os.stat(self.path)
        self.save(steps_of(E_RAW))
        self.assertEqual(self.text(), before)
        self.assertEqual(os.stat(self.path).st_ino, st.st_ino)   # даже не подменялся

    def test_edit_text_add_move_delete(self):
        s = steps_of(E_RAW)
        s[0]["text"] = "новый привет"
        s.insert(4, {"key": "w_extra", "kind": "delay", "duration": "30s"})
        s.insert(5, {"key": "extra", "kind": "message", "text": "ещё"})
        s = [x for x in s if x["key"] not in ("d2", "m2")]
        self.save(s)
        keys = [x["key"] for x in json.loads(self.text())["funnels"]["signup"]["steps"]]
        self.assertEqual(keys, ["welcome", "ask", "d1", "m1", "w_extra", "extra", "thanks", "declined"])
        cfg = loader(self.path)
        self.assertEqual(cfg.funnel("signup").step("welcome").text, "новый привет")
        self.assertEqual(cfg.funnel("signup").step("w_extra").duration, 30)
        self.assertEqual(json.loads(self.text())["funnels"]["promo"], E_RAW["funnels"]["promo"])  # чужое не тронуто

    def test_reorder_keeps_other_fields(self):
        s = steps_of(E_RAW)
        s[6], s[7] = s[7], s[6]            # «declined» перед «thanks»
        self.save(s)
        keys = [x["key"] for x in json.loads(self.text())["funnels"]["signup"]["steps"]]
        self.assertEqual(keys[6:8], ["declined", "thanks"])
        self.assertEqual(loader(self.path).funnel("signup").step("d1").duration, 10)

    def test_same_delay_keeps_original_spelling(self):
        s = steps_of(E_RAW)
        s[2]["duration"] = 10                 # то же значение, другая запись: «10s» остаётся как было
        self.save(s)
        self.assertEqual(json.loads(self.text())["funnels"]["signup"]["steps"][2]["duration"], "10s")

    def test_delay_duration_saved(self):
        s = steps_of(E_RAW)
        s[2]["duration"] = "45s"
        self.save(s)
        self.assertEqual(loader(self.path).funnel("signup").step("d1").duration, 45)

    def test_phone_request_and_branching_edited(self):
        s = steps_of(E_RAW)
        s[1]["request_phone"]["button"] = "Поделиться"
        s[1]["on_reply"]["decline"] = "thanks"
        s = [x for x in s if x["key"] != "declined"]       # иначе в «declined» было бы не попасть
        self.save(s)
        st = loader(self.path).funnel("signup").step("ask")
        self.assertEqual(st.request_phone.button, "Поделиться")
        self.assertEqual(st.reply_target("decline"), "thanks")


class TestValidationRefusals(EditBase):
    """Каждая проверка срабатывает на своём неверном случае; файл при этом не меняется."""

    def refuse(self, mutate, expect, funnel="signup", exc=(EditError, ConfigError)):
        s = steps_of(E_RAW, funnel)
        mutate(s)
        before = self.text()
        with self.assertRaises(exc) as cm:
            self.save(s, funnel)
        self.assertIn(expect, str(cm.exception))
        self.assertEqual(self.text(), before, "файл изменился при отказе")
        self.assertEqual([f for f in os.listdir(self.tmp.name) if f.startswith(".funnels-")], [])

    def test_missing_text(self):
        self.refuse(lambda s: s[0].update(text=""), "text")

    def test_missing_key(self):
        self.refuse(lambda s: s[0].update(key=""), "нет ключа")

    def test_bad_delay(self):
        self.refuse(lambda s: s[2].update(duration="скоро"), "длительность")

    def test_negative_delay(self):
        self.refuse(lambda s: s[2].update(duration=-5), "отрицательной")
        self.refuse(lambda s: s[2].update(duration=""), "длительность")        # пустая длительность

    def test_next_to_missing_step(self):
        self.refuse(lambda s: s[3].update(next="nowhere"), "nowhere")

    def test_branch_to_missing_step(self):
        self.refuse(lambda s: s[1]["on_reply"].update(contact="nowhere"), "nowhere")

    def test_branch_target_missing_outcome(self):
        self.refuse(lambda s: s[1]["on_reply"].pop("decline"), "decline")

    def test_deleting_referenced_step(self):
        self.refuse(lambda s: s.__delitem__(6), "thanks")        # на него ведёт on_reply шага ask

    def test_duplicate_keys(self):
        self.refuse(lambda s: s[4].update(key="m1"), "m1")

    def test_unreachable_step(self):
        # d2 и m2 остаются в списке, но m1 теперь заканчивает прогон (next: end) -> в них не попасть
        self.refuse(lambda s: s[3].update(next="end"), "нельзя попасть")

    def test_unknown_kind(self):
        self.refuse(lambda s: s[0].update(kind="voice"), "неизвестный вид")

    def test_empty_steps(self):
        self.refuse(lambda s: s.clear(), "хотя бы один шаг")

    def test_phone_request_without_branching(self):
        self.refuse(lambda s: s[1].pop("on_reply"), "ask")

    def test_unknown_funnel(self):
        before = self.text()
        with self.assertRaises(EditError):
            self.save(steps_of(E_RAW), "nope")
        self.assertEqual(self.text(), before)


class TestAtomic(EditBase):
    def test_failed_replace_leaves_original_and_no_temp(self):
        s = steps_of(E_RAW)
        s[0]["text"] = "другой"
        before = self.text()
        with mock.patch("app.config_edit.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                self.save(s)
        self.assertEqual(self.text(), before)
        self.assertEqual(os.listdir(self.tmp.name), ["funnels.json"])

    def test_written_through_temp_file_in_same_dir(self):
        s = steps_of(E_RAW)
        s[0]["text"] = "другой"
        seen = []
        real = os.replace
        with mock.patch("app.config_edit.os.replace", side_effect=lambda a, b: (seen.append((a, b)), real(a, b))):
            self.save(s)
        (src, dst), = seen
        self.assertEqual(os.path.dirname(src), os.path.dirname(dst))
        self.assertNotEqual(src, dst)


class TestStore(EditBase):
    def test_picks_up_new_file_without_restart(self):
        self.assertFalse(self.store.maybe_reload())
        s = steps_of(E_RAW)
        s[0]["text"] = "после правки"
        self.save(s)
        self.assertTrue(self.store.maybe_reload())
        self.assertEqual(self.store.funnel("signup").step("welcome").text, "после правки")

    def test_two_quick_saves_both_seen(self):
        for i in range(3):
            s = steps_of(E_RAW)
            s[0]["text"] = f"v{i}"
            self.save(s)
            self.assertTrue(self.store.maybe_reload(), i)
            self.assertEqual(self.store.funnel("signup").step("welcome").text, f"v{i}")

    def test_broken_file_keeps_previous_config(self):
        raw = copy.deepcopy(E_RAW)
        raw["funnels"]["signup"]["steps"][1]["on_reply"]["contact"] = "gone"
        write_json(self.path, raw)
        self.assertFalse(self.store.maybe_reload())
        self.assertIn("gone", self.store.last_error)
        self.assertEqual(self.store.funnel("signup").step("ask").reply_target("contact"), "thanks")
        write_json(self.path, E_RAW)             # починили -> подхватили, ошибка сброшена
        self.assertTrue(self.store.maybe_reload())
        self.assertIsNone(self.store.last_error)

    def test_snapshot_is_stable(self):
        snap = self.store.snapshot()
        s = steps_of(E_RAW)
        s[0]["text"] = "иначе"
        self.save(s)
        self.store.maybe_reload()
        self.assertEqual(snap.funnel("signup").step("welcome").text, "привет")


class TestBlocksEditing(EditBase):
    """Условие и задержка в редакторе: сохраняются, читаются обратно, проверки срабатывают."""

    def cond_step(self, **kw):
        return {"key": "vip_check", "kind": "condition", "check": {"kind": "tag", "tag": "vip"},
                "on_true": "thanks", "on_false": "next", **kw}

    def test_condition_with_two_exits_saved_and_read_back(self):
        s = steps_of(E_RAW)
        s.insert(0, self.cond_step())
        self.save(s)
        c = loader(self.path).funnel("signup").step("vip_check")
        self.assertEqual((c.kind, c.check.kind, c.check.tag, c.on_true, c.on_false), ("condition", "tag", "vip", "thanks", "next"))
        raw = json.loads(self.text())["funnels"]["signup"]["steps"][0]
        self.assertEqual(raw, self.cond_step())                 # в файл ушли ровно поля условия
        s = steps_of(json.loads(self.text()))
        s[0]["on_false"], s[0]["on_true"] = "end", "welcome"     # оба выхода меняются
        self.save(s)
        c = loader(self.path).funnel("signup").step("vip_check")
        self.assertEqual((c.on_true, c.on_false), ("welcome", "end"))

    def test_delay_added_and_kind_change_drops_foreign_fields(self):
        s = steps_of(E_RAW)
        s.insert(1, {"key": "pause", "kind": "delay", "duration": "2m", "text": "лишнее", "next": "end", "on_true": "x"})
        self.save(s)
        self.assertEqual(json.loads(self.text())["funnels"]["signup"]["steps"][1], {"key": "pause", "kind": "delay", "duration": "2m"})
        self.assertEqual(loader(self.path).funnel("signup").step("pause").duration, 120)
        s = steps_of(json.loads(self.text()))
        s[1] = {"key": "pause", "kind": "message", "text": "теперь сообщение"}      # вид сменили: duration уходит
        self.save(s)
        self.assertEqual(json.loads(self.text())["funnels"]["signup"]["steps"][1],
                         {"key": "pause", "kind": "message", "text": "теперь сообщение"})

    def refuse(self, mutate, pattern):
        s = steps_of(E_RAW)
        s.insert(0, self.cond_step())
        mutate(s)
        before = self.text()
        with self.assertRaisesRegex((EditError, ConfigError), pattern):
            self.save(s)
        self.assertEqual(self.text(), before)

    def test_condition_refusals(self):
        self.refuse(lambda s: s[0].update(on_true="nowhere"), "nowhere")
        self.refuse(lambda s: s[0].update(on_false=None), "on_false")
        self.refuse(lambda s: s[0].update(check={"kind": "weather"}), "неизвестная проверка")
        self.refuse(lambda s: s[0].update(check={"kind": "tag", "tag": ""}), "tag")
        self.refuse(lambda s: s.__delitem__(7), "thanks")           # на «thanks» ведёт выход условия и on_reply

    def test_deleting_condition_target_is_refused(self):
        s = steps_of(E_RAW)
        s.insert(0, self.cond_step(on_true="declined"))
        s = [x for x in s if x["key"] != "thanks"]                   # on_reply шага ask тоже ведёт на thanks
        s[2]["on_reply"]["contact"] = "declined"
        self.save(s)                                                 # цели на месте — сохраняется
        s = [x for x in steps_of(json.loads(self.text())) if x["key"] != "declined"]
        with self.assertRaisesRegex((EditError, ConfigError), "declined"):
            self.save(s)

    def test_cycle_without_delay_refused_with_delay_allowed(self):
        s = steps_of(E_RAW)
        s.insert(0, self.cond_step(on_true="back", on_false="next"))
        s.append({"key": "back", "kind": "message", "text": "вернулись", "next": "vip_check"})
        with self.assertRaisesRegex((EditError, ConfigError), "цикл без задержки"):
            self.save(s)
        s[-1] = {"key": "back", "kind": "delay", "duration": "30s"}      # та же петля, но через паузу
        self.save(s)

    def test_unknown_kind_refused(self):
        self.refuse(lambda s: s[0].update(kind="poll"), "неизвестный вид")


class TestWriteRights(EditBase):
    def test_non_admin_refused_and_file_untouched(self):
        before = self.text()
        s = steps_of(E_RAW)
        s[0]["text"] = "взлом"
        code, body = api.save_funnel_payload(self.store, "signup", {"steps": s}, can_edit=False, codec=CODEC)
        self.assertEqual((code, body), (403, {"error": "forbidden"}))
        self.assertEqual(self.text(), before)

    def test_admin_saves_and_response_has_new_funnel(self):
        s = steps_of(E_RAW)
        s[0]["text"] = "ок"
        code, body = api.save_funnel_payload(self.store, "signup", {"steps": s}, can_edit=True, codec=CODEC)
        self.assertEqual(code, 200)
        self.assertEqual(body["funnel"]["steps"][0]["text"], "ок")
        self.assertEqual(self.store.funnel("signup").step("welcome").text, "ок")

    def test_invalid_is_400_with_reason(self):
        s = steps_of(E_RAW)
        s[3]["next"] = "nowhere"
        code, body = api.save_funnel_payload(self.store, "signup", {"steps": s}, can_edit=True, codec=CODEC)
        self.assertEqual(code, 400)
        self.assertIn("nowhere", body["error"])

    def test_bad_body_and_unknown_funnel(self):
        self.assertEqual(api.save_funnel_payload(self.store, "signup", None, True, CODEC)[0], 400)
        self.assertEqual(api.save_funnel_payload(self.store, "nope", {"steps": []}, True, CODEC)[0], 404)


class StoreBase(Base):
    """Бот работает на ConfigStore, файл правят между шагами прогона."""

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.tmp.name, "funnels.json")
        write_json(self.path, E_RAW)
        self.store = ConfigStore(self.path, loader=loader)
        self.svc.cfg = self.worker.cfg = self.store

    def edit(self, mutate, funnel="signup"):
        raw = copy.deepcopy(E_RAW)
        mutate(raw)
        write_json(self.path, raw)
        self.assertTrue(self.store.maybe_reload(), self.store.last_error)

    def run_row(self):
        return self.q("SELECT status, awaiting_step, jump_to FROM runs WHERE user_id=111 ORDER BY id DESC")[0]

    def events(self, kind):
        return self.q("SELECT details FROM events WHERE kind=?", kind)


class TestStepDisappears(StoreBase):
    async def test_queued_step_removed_run_closed_not_crash(self):
        await self.begin()                       # welcome, ask отправлены; пауза d1 ждёт
        self.edit(lambda r: r["funnels"]["signup"]["steps"].pop(2))     # убрали d1
        await self.drain(advance=11)
        self.assertEqual(self.run_row()["status"], "cancelled")
        self.assertEqual(self.q("SELECT status, error FROM queue WHERE step_key='d1'"),
                         [{"status": "error", "error": "шаг удалён из конфига"}])
        self.assertIn("d1", self.events("run_cancelled")[0]["details"])
        self.assert_one_open()
        # человек может начать заново — и получит воронку по новому конфигу
        await self.svc.start(self.u)
        await self.drain()
        self.assertEqual(self.run_row()["status"], "active")

    async def test_funnel_removed_run_closed(self):
        await self.begin()
        self.edit(lambda r: r["funnels"].pop("plain"))                   # другая воронка: не мешает
        self.edit(lambda r: (r["funnels"].pop("signup"), r["settings"].update(default_funnel="promo")))
        await self.drain(advance=11)
        self.assertEqual(self.run_row()["status"], "cancelled")
        self.assertEqual(self.q("SELECT COUNT(*) n FROM queue WHERE status='pending'")[0]["n"], 0)

    async def test_jump_target_removed_run_closed(self):
        await self.begin()
        # ответ пришёл, пока шла отправка: цель запомнена в jump_to; потом цель убрали из конфига
        self.db.conn.execute("UPDATE queue SET status='processing' WHERE step_key='d1'")
        self.db.conn.execute("UPDATE runs SET jump_to='thanks', awaiting_step=NULL WHERE user_id=111")
        self.edit(lambda r: r["funnels"]["signup"].update(
            steps=[s for s in r["funnels"]["signup"]["steps"] if s["key"] not in ("thanks",)]
        ) or r["funnels"]["signup"]["steps"][1]["on_reply"].update(contact="declined"))
        row = self.q("SELECT * FROM queue WHERE step_key='d1'")[0]
        f = self.store.snapshot().funnel("signup")
        self.worker._mark_sent(row, f, f.step("d1"))
        self.assertEqual(self.run_row()["status"], "cancelled")
        self.assertIn("thanks", self.events("run_cancelled")[0]["details"])

    async def test_awaiting_step_removed_contact_still_saved(self):
        await self.begin()
        def drop_ask(r):
            st = r["funnels"]["signup"]["steps"]
            r["funnels"]["signup"]["steps"] = [s for s in st if s["key"] not in ("ask", "thanks", "declined")]
        self.edit(drop_ask)
        res = await self.svc.contact(111, "+37400000001", 111)
        self.assertEqual(res, "accepted")
        self.assertEqual(self.q("SELECT phone FROM users WHERE tg_id=111")[0]["phone"], "+37400000001")
        self.assertEqual(len(self.events("phone_given")), 1)
        self.assertEqual(self.run_row()["status"], "cancelled")
        self.assertEqual(self.q("SELECT COUNT(*) n FROM queue WHERE status='pending'")[0]["n"], 0)

    async def test_awaiting_step_removed_decline_closes_run(self):
        await self.begin()
        self.edit(lambda r: r["funnels"]["signup"].update(
            steps=[s for s in r["funnels"]["signup"]["steps"] if s["key"] not in ("ask", "thanks", "declined")]))
        self.assertEqual(await self.svc.decline(111), "ignored")
        self.assertEqual(self.run_row()["status"], "cancelled")

    async def test_config_reloaded_during_send_does_not_crash(self):
        """Правка файла, пока сообщение в полёте: отправленное доводится до конца по снимку."""
        await self.begin()
        store, edit = self.store, self.edit

        async def send_and_edit(chat_id, text, **kw):
            if text == "первое":
                edit(lambda r: r["funnels"]["signup"]["steps"].pop(3))   # m1 исчез из конфига на лету
            self.sender.sent.append((chat_id, text, kw))

        self.sender.send_text = send_and_edit
        await self.drain(advance=11)
        self.assertEqual(self.q("SELECT status FROM queue WHERE step_key='m1'"), [{"status": "sent"}])
        self.assertEqual(self.run_row()["status"], "active")             # цепочка идёт: пауза d2 поставлена
        self.assert_one_open()
        self.assertEqual(self.q("SELECT step_key FROM queue WHERE status='pending'"), [{"step_key": "d2"}])
        await self.drain(advance=70)                                      # d2 и m2 в новом конфиге есть
        self.assertEqual(self.run_row()["status"], "completed")


class TestAuthFlags(unittest.TestCase):
    def test_can_edit_matrix(self):
        from app.auth import access
        from test_core import make_init_data
        import time
        tok, now = "123456:TEST-TOKEN", int(time.time())
        kw = dict(token=tok, admin_ids=frozenset({42}), max_age=86400, disabled=False)
        self.assertTrue(access(make_init_data(tok, 42, now), **kw).can_edit)
        a = access(make_init_data(tok, 7, now), **kw)
        self.assertTrue(a.ok)
        self.assertFalse(a.can_edit)
        self.assertFalse(a.reveal)
        self.assertFalse(access("", **kw).ok)
        self.assertFalse(access("", **kw).can_edit)
        d = access("", **dict(kw, disabled=True))        # локальный запуск без проверки: редактор доступен
        self.assertTrue(d.ok and d.can_edit and d.reveal)

    def test_funnels_payload_flag(self):
        import sqlite3
        from app.db import Database
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "x.db"))
            cfg = parse_config(E_RAW)
            self.assertFalse(api.funnels_payload(db.conn, cfg)["can_edit"])
            self.assertTrue(api.funnels_payload(db.conn, cfg, can_edit=True)["can_edit"])
            db.close()


if __name__ == "__main__":
    unittest.main()
