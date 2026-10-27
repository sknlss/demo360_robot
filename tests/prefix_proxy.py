"""Ручная проверка в браузере: публикует preview_server по пути /tg-demo/ (префикс срезается, остальное — 404).
Запуск: python3 tests/prefix_proxy.py <порт прокси> <порт приложения>"""
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/tg-demo"
PORT, APP = int(sys.argv[1]), int(sys.argv[2])


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print(self.command, self.path, flush=True)

    def do_GET(self):
        if self.path.startswith(PREFIX + "/"):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d%s" % (APP, self.path[len(PREFIX):])) as r:
                    code, body, ct = r.status, r.read(), r.headers.get("Content-Type", "text/plain")
            except urllib.error.HTTPError as e:
                code, body, ct = e.code, e.read(), "text/plain"
        else:
            code, body, ct = 404, b"not found", "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
