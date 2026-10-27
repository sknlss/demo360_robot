"""Проверяем саму заглушку: отвечает ли она в формате Bot API (через urllib)."""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_telegram as ft  # noqa: E402


def call(base, path, payload=None):
    req = urllib.request.Request(base + path, data=json.dumps(payload or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestStub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ft.serve(0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_flow_and_error_modes(self):
        self.assertTrue(call(self.base, "/bot1:T/getMe")[1]["result"]["is_bot"])
        call(self.base, "/_test/update", ft.start_update())
        s, r = call(self.base, "/bot1:T/getUpdates", {"offset": 0, "timeout": 0})
        self.assertEqual(r["result"][0]["message"]["text"], "/start")
        self.assertEqual(call(self.base, "/bot1:T/sendMessage", {"chat_id": 1, "text": "hi"})[0], 200)
        call(self.base, "/_test/mode", {"mode": "429", "retry_after": 3, "count": 1})
        s, r = call(self.base, "/bot1:T/sendMessage", {"chat_id": 1, "text": "x"})
        self.assertEqual((s, r["parameters"]["retry_after"]), (429, 3))
        self.assertEqual(call(self.base, "/bot1:T/sendMessage", {"chat_id": 1, "text": "x"})[0], 200)
        call(self.base, "/_test/mode", {"mode": "403", "count": 1})
        self.assertEqual(call(self.base, "/bot1:T/sendMessage", {"chat_id": 1, "text": "x"})[0], 403)


if __name__ == "__main__":
    unittest.main()
