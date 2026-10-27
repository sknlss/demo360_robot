"""Дашборд по HTTP: подпись, роли, замазанные телефоны в JSON и в разметке.

Поднимает tests/preview_server.py (stdlib) с включённой проверкой подписи через app.auth.authorize —
той же функцией, что вызывает app/web.py. Сам aiohttp-слой тут не участвует (его в контейнере нет).
"""
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from test_core import make_init_data  # noqa: E402

TOKEN = "123456:TEST-TOKEN"
PORT = "18099"
BASE = "http://127.0.0.1:" + PORT
FULL = ["+37499111222", "+37477000111", "+37455123456"]  # телефоны из preview_server.seed()


def get(path, init=None):
    req = urllib.request.Request(BASE + path, headers={"X-Init-Data": init} if init else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class TestDashboardHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, PREVIEW_TOKEN=TOKEN, PREVIEW_ADMIN_IDS="42")
        cls.proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "tests", "preview_server.py"), PORT],
                                    env=env, cwd=ROOT)
        for _ in range(50):
            try:
                if get("/")[0] == 200:
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("preview_server не поднялся")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)

    def test_without_signature_api_closed(self):
        for path in ("/api/overview", "/api/funnels", "/api/users", "/api/users?level=phone"):
            code, body = get(path)
            self.assertEqual(code, 403)
            self.assertFalse(any(p in body for p in FULL))
        self.assertEqual(get("/api/users?level=phone", "hash=deadbeef&auth_date=1")[0], 403)

    def test_non_admin_with_real_signature_sees_masked_number_only(self):
        init = make_init_data(TOKEN, 7, int(time.time()))
        for lv in ("started", "requested", "phone"):
            code, body = get("/api/users?level=" + lv, init)
            self.assertEqual(code, 200)
            for ph in FULL:
                digits = ph.lstrip("+")
                self.assertNotIn(ph, body)
                self.assertNotIn(digits[:-4], body)
        code, body = get("/api/users?level=phone", init)
        phones = {u["phone"] for u in json.loads(body)["users"]}
        self.assertIn("•••••••1222", phones)
        # все пользователи (без level) и воронки: тоже без полного номера, а разметка страницы — без номеров
        for path in ("/api/users", "/api/funnels", "/api/overview?funnel=signup"):
            code, body = get(path, init)
            self.assertEqual(code, 200, path)
            for ph in FULL:
                self.assertNotIn(ph, body)
                self.assertNotIn(ph.lstrip("+")[:-4], body)
        everyone = json.loads(get("/api/users", init)[1])
        self.assertEqual(everyone["total"], 6)
        self.assertEqual(len(everyone["users"]), 6)
        self.assertEqual({u["phone"] for u in everyone["users"]} - {None}, {"•••••••1222", "•••••••0111", "•••••••3456"})
        self.assertEqual(get("/api/overview", init)[0], 200)
        # разметка страницы тоже без номеров
        page = get("/", init)[1]
        for ph in FULL:
            self.assertNotIn(ph.lstrip("+")[:-4], page)

    def test_admin_sees_full_number(self):
        code, body = get("/api/users?level=phone", make_init_data(TOKEN, 42, int(time.time())))
        self.assertEqual(code, 200)
        phones = {u["phone"] for u in json.loads(body)["users"]}
        self.assertEqual(phones, set(FULL))
        everyone = json.loads(get("/api/users", make_init_data(TOKEN, 42, int(time.time())))[1])["users"]
        self.assertEqual({u["phone"] for u in everyone} - {None}, set(FULL))

    def test_funnel_endpoints_and_unknown_funnel(self):
        init = make_init_data(TOKEN, 7, int(time.time()))
        f = json.loads(get("/api/funnels", init)[1])["funnels"]
        self.assertEqual([x["key"] for x in f], ["signup", "promo", "plain", "double"])
        self.assertEqual([l["count"] for l in f[0]["levels"]], [5, 4, 3])
        self.assertEqual([l["count"] for l in f[2]["levels"]], [1, 0, 0])      # воронка без запроса номера
        self.assertEqual(get("/api/overview?funnel=nope", init)[0], 404)
        self.assertEqual(get("/api/users?level=started&funnel=nope", init)[0], 400)


if __name__ == "__main__":
    unittest.main()
