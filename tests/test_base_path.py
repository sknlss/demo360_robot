"""Дашборд работает и в корне домена, и по пути с подпапкой (/tg-demo/).

Страница берёт данные по относительным путям (api/...). Здесь:
  1. статически: в index.html нет ни одного строкового литерала с ведущей «/» (кроме https://…);
  2. адреса из вызовов api("…") разрешаются так же, как это делает браузер (urljoin от адреса страницы);
  3. по этим адресам ходим на настоящий preview_server (те же stats.py и index.html, что у приложения):
     напрямую (страница в корне) и через прокси, который срезает префикс и всё остальное отдаёт 404,
     как веб-сервер с публикацией по пути;
  4. негатив: старый вариант api("/api/…") под префиксом уходит в корень и получает 404.
aiohttp-слой (app/web.py) тут не участвует: его в контейнере нет, маршруты у него те же.
"""
import os
import re
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()
from test_core import make_init_data  # noqa: E402
from app.settings import _dir_url  # noqa: E402

TOKEN = "123456:TEST-TOKEN"
APP_PORT, FRONT_PORT = 18097, 18098
PREFIX = "/tg-demo"
INDEX = os.path.join(ROOT, "app", "static", "index.html")
HTML = read(INDEX)


def fetch(url, init=None):
    req = urllib.request.Request(url, headers={"X-Init-Data": init} if init else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def api_literals(html):
    """Все адреса данных, которые страница собирает: ['api/funnels', 'api/overview?funnel=', 'api/photo/', 'api/users']."""
    return re.findall(r'"(api/[^"]*)"', html)


class Front(BaseHTTPRequestHandler):
    """Публикация по пути: /tg-demo/… -> приложение без префикса, всё остальное — 404 (как корень чужого сайта)."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == PREFIX or not self.path.startswith(PREFIX + "/"):
            code, body = 404, "not found"
        else:
            code, body = fetch("http://127.0.0.1:%d%s" % (APP_PORT, self.path[len(PREFIX):]),
                               self.headers.get("X-Init-Data"))
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestBasePath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, PREVIEW_TOKEN=TOKEN, PREVIEW_ADMIN_IDS="42")
        cls.proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "tests", "preview_server.py"),
                                     str(APP_PORT)], env=env, cwd=ROOT)
        cls.front = ThreadingHTTPServer(("127.0.0.1", FRONT_PORT), Front)
        threading.Thread(target=cls.front.serve_forever, daemon=True).start()
        for _ in range(50):
            try:
                if fetch("http://127.0.0.1:%d/" % APP_PORT)[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("preview_server не поднялся")
        cls.init = make_init_data(TOKEN, 7, int(time.time()))

    @classmethod
    def tearDownClass(cls):
        cls.front.shutdown()
        cls.front.server_close()
        cls.proc.terminate()
        cls.proc.wait(timeout=5)

    def resolve(self, page_url, literals=None):
        return [urljoin(page_url, p) for p in (literals or api_literals(HTML))]

    def test_no_absolute_literals_in_page(self):
        # любой строковый литерал, начинающийся с «/» (не «//» и не часть https://…), — потенциальный абсолютный путь
        bad = re.findall(r"""["'`]/(?!/)[^"'`\s]*""", HTML)
        self.assertEqual(bad, [])
        self.assertNotRegex(HTML, r"""url\(\s*["']?/""")
        self.assertNotRegex(HTML, r"""(?:src|href|action)\s*=\s*["']/(?!/)""")

    def test_page_calls_all_three_endpoints_relative(self):
        lits = api_literals(HTML)
        self.assertEqual(sorted(lits), ["api/funnels", "api/funnels/", "api/overview?funnel=", "api/photo/", "api/users"])
        self.assertFalse([p for p in lits if p.startswith("/")])

    def test_resolved_paths_stay_under_page_directory(self):
        names = ["api/funnels", "api/funnels/", "api/overview", "api/photo/", "api/users"]
        self.assertEqual(sorted(urlsplit(u).path for u in self.resolve("https://h.example/")),
                         ["/" + n for n in names])
        self.assertEqual(sorted(urlsplit(u).path for u in self.resolve("https://h.example/tg-demo/")),
                         ["/tg-demo/" + n for n in names])
        # хеш с подписью Telegram на разбор пути не влияет
        self.assertEqual(urlsplit(urljoin("https://h.example/tg-demo/#tgWebAppData=a%3Db", "api/overview")).path,
                         "/tg-demo/api/overview")

    def _check_page_works(self, page_url, base):
        """base — откуда реально отвечает сервер; page_url — адрес страницы, от него считаются пути."""
        code, body = fetch(page_url)
        self.assertEqual(code, 200)
        self.assertIn("api(", body)
        # страница собирает адреса из этих кусков: добавляем то, что она подставляет при ходе по экранам
        urls = ["api/funnels", "api/overview?funnel=signup", "api/users", "api/users?level=phone&funnel=signup"]
        self.assertTrue(all(any(lit.split("?")[0] == u.split("?")[0] for u in urls)
                            for lit in api_literals(body) if not lit.endswith(("photo/", "funnels/"))))
        for lit in urls:  # фото в preview_server нет; путь проверен в test_resolved_paths…
            url = urljoin(page_url, lit)
            self.assertTrue(url.startswith(base), url)
            self.assertEqual(fetch(url, self.init)[0], 200, url)
            self.assertEqual(fetch(url)[0], 403, url + " без подписи")

    def test_page_in_domain_root(self):
        self._check_page_works("http://127.0.0.1:%d/" % APP_PORT, "http://127.0.0.1:%d/" % APP_PORT)

    def test_page_under_subpath(self):
        base = "http://127.0.0.1:%d%s/" % (FRONT_PORT, PREFIX)
        self._check_page_works(base, base)

    def test_photo_request_goes_under_prefix(self):
        url = urljoin("http://127.0.0.1:%d%s/" % (FRONT_PORT, PREFIX), "api/photo/" + "42")
        code, _ = fetch(url, self.init)
        self.assertEqual(code, 404)  # preview_server фото не раздаёт; важно, что запрос дошёл до префикса
        self.assertEqual(urlsplit(url).path, PREFIX + "/api/photo/42")

    def test_old_absolute_paths_fail_under_subpath(self):
        """Негатив: ровно то, что сломалось у Олега. Если этот тест перестанет «падать» — проверка ничего не ловит."""
        base = "http://127.0.0.1:%d%s/" % (FRONT_PORT, PREFIX)
        old = ["/api/funnels", "/api/overview?funnel=signup", "/api/users?level=phone"]
        for lit in old:
            url = urljoin(base, lit)
            self.assertEqual(urlsplit(url).path.split("?")[0], lit.split("?")[0])  # ушёл в корень
            self.assertEqual(fetch(url, self.init)[0], 404, url)

    def test_miniapp_url_gets_trailing_slash(self):
        self.assertEqual(_dir_url("https://h.example/tg-demo"), "https://h.example/tg-demo/")
        self.assertEqual(_dir_url("https://h.example/tg-demo/"), "https://h.example/tg-demo/")
        self.assertEqual(_dir_url("https://h.example"), "https://h.example/")
        self.assertEqual(_dir_url("https://h.example/tg-demo?x=1"), "https://h.example/tg-demo/?x=1")
        self.assertEqual(_dir_url(""), "")


if __name__ == "__main__":
    unittest.main()
