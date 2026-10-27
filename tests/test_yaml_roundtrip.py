"""Запись YAML с сохранением комментариев (нужен ruamel.yaml). Без него тесты пропускаются.

В этом окружении ruamel нет, поэтому здесь они НЕ запускались. Запуск:
    docker compose run --rm bot python -m unittest tests.test_yaml_roundtrip -v
или локально:  pip install -r requirements.txt && python -m unittest tests.test_yaml_roundtrip -v
"""
import copy
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()
CONFIG = os.path.join(ROOT, "config", "funnels.yml")

try:
    import ruamel.yaml  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

if HAVE:
    from app.config import format_duration, funnel_to_dict, load_config
    from app.config_edit import RuamelCodec, save_funnel_steps


def comments(text):
    return [l.strip() for l in text.splitlines() if l.strip().startswith("#") or re.search(r"\s#\s", l)]


def draft(path, funnel="signup"):
    """Черновик так, как его шлёт форма: только поля вида, длительность строкой."""
    steps = []
    for s in funnel_to_dict(load_config(path).funnel(funnel))["steps"]:
        s = {k: v for k, v in s.items() if k not in ("derived", "duration_text")}
        if s["kind"] == "delay":
            s["duration"] = format_duration(s["duration"])
        steps.append(s)
    return steps


def by_key(steps, key):
    return next(s for s in steps if s["key"] == key)


@unittest.skipUnless(HAVE, "ruamel.yaml не установлен")
class TestRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "funnels.yml")
        shutil.copy(CONFIG, self.path)
        self.orig = read(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, steps):
        return save_funnel_steps(self.path, "signup", steps, codec=RuamelCodec())

    def test_load_dump_without_edits_is_byte_identical(self):
        c = RuamelCodec()
        self.assertEqual(c.dump(c.load(self.orig)), self.orig)

    def test_save_without_changes_leaves_file_alone(self):
        self.save(draft(self.path))
        self.assertEqual(read(self.path), self.orig)

    def test_edit_keeps_every_comment_and_key_order(self):
        steps = draft(self.path)
        by_key(steps, "welcome")["text"] = "Здравствуйте! Это бот записи."
        self.save(steps)
        out = read(self.path)
        self.assertEqual(comments(out), comments(self.orig))
        self.assertIn("Здравствуйте! Это бот записи.", out)
        # всё остальное — построчно как было
        diff = [l for l in out.splitlines() if l not in self.orig.splitlines()]
        self.assertEqual(len(diff), 1, diff)
        self.assertEqual(load_config(self.path).funnel("signup").step("welcome").text, "Здравствуйте! Это бот записи.")

    def test_reorder_add_delete_keep_comments(self):
        steps = draft(self.path)
        keys = [s["key"] for s in steps]
        i, j = keys.index("wait_1"), keys.index("wait_2")
        steps[i], steps[j] = steps[j], steps[i]          # паузы местами: цепочка остаётся достижимой
        k = keys.index("reminder_1") + 1                 # после первого напоминания (у него нет next)
        steps.insert(k, {"key": "extra_wait", "kind": "delay", "duration": "30s"})
        steps.insert(k + 1, {"key": "extra", "kind": "message", "text": "Ещё одно напоминание."})
        self.save(steps)
        got = [s.key for s in load_config(self.path).funnel("signup").steps]
        self.assertEqual(got, [s["key"] for s in steps])
        self.assertEqual(sorted(comments(read(self.path))), sorted(comments(self.orig)))   # комментарии на месте
        # и после удаления тоже (кроме комментариев внутри самого удалённого шага)
        steps = [s for s in draft(self.path) if s["key"] not in ("extra", "extra_wait")]
        self.save(steps)
        self.assertEqual(sorted(comments(read(self.path))), sorted(comments(self.orig)))

    def test_condition_and_delay_roundtrip(self):
        steps = draft(self.path)
        by_key(steps, "has_phone")["on_false"] = "welcome"
        by_key(steps, "wait_1")["duration"] = "20s"
        self.save(steps)
        f = load_config(self.path).funnel("signup")
        self.assertEqual((f.step("has_phone").on_true, f.step("has_phone").on_false), ("already", "welcome"))
        self.assertEqual(f.step("wait_1").duration, 20.0)
        self.assertEqual(sorted(comments(read(self.path))), sorted(comments(self.orig)))

    def test_refusal_leaves_file_byte_identical(self):
        steps = draft(self.path)
        by_key(steps, "reminder_1")["next"] = "nowhere"
        with self.assertRaises(Exception):
            self.save(steps)
        self.assertEqual(read(self.path), self.orig)

    def test_shipped_config_parses_with_pyyaml(self):
        cfg = load_config(CONFIG)
        self.assertIn("signup", cfg.funnels)


if __name__ == "__main__":
    unittest.main()
