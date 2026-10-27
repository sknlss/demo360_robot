"""Предпросмотр дашборда без aiohttp: stdlib-сервер с теми же stats.py и index.html.

python3 tests/preview_server.py [порт]  — засевает временную базу и отдаёт страницу.
Проверка подписи здесь не включена (это только для вёрстки); боевая проверка — app/web.py.
Телефоны замазаны, как у не-админа; REVEAL=1 python3 tests/preview_server.py — как у админа.
PREVIEW_TOKEN=<токен> [PREVIEW_ADMIN_IDS=1,2] — включает ту же проверку подписи (app.auth.authorize),
что и app/web.py: так HTTP-поведение проверяется без aiohttp (см. tests/test_dashboard_http.py).
"""
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
from app import api  # noqa: E402
from app.db import Database  # noqa: E402
from app.config import parse_config  # noqa: E402


def seed():
    from test_core import RAW
    db = Database(os.path.join(tempfile.mkdtemp(), "p.db"))
    t = 1_700_000_000.0
    names = [("Анна Петрова", "anna_p", "+37499111222"), ("Давид", None, None),
             ("Очень Длинное Имя Пользователя Для Проверки Переноса", "very_long_username_for_wrap_test", "+37477000111"),
             ("Mark", "mark", None), ("Лена", "lena", "+37455123456")]
    for i, (n, un, ph) in enumerate(names, 1):
        db.conn.execute("INSERT INTO users(tg_id,name,username,phone,created_at,tags) VALUES (?,?,?,?,?,'[]')",
                        (i, n, un, ph, t - i * 3600))
    # пользователи 1, 3, 5 прошли signup и оставили номер; 2 — получил приветствие и запрос, молчит;
    # 4 — запустил signup, но запрос ещё не получил; 6 — прошёл воронку plain без запроса номера
    runs = {}
    for i, (n, un, ph) in enumerate(names, 1):
        fn = "signup"
        st = "completed" if ph else ("cancelled" if i == 4 else "active")
        cur = db.conn.execute("INSERT INTO runs(user_id,funnel,status,started_at) VALUES (?,?,?,?)", (i, fn, st, t))
        rid = runs[i] = cur.lastrowid
        db.log_event(t - i * 3600, "run_started", i, rid, fn)
        if i != 4:
            db.log_event(t - i * 3600 + 1, "message_sent", i, rid, "welcome")
            db.log_event(t - i * 3600 + 2, "message_sent", i, rid, "ask")
            db.log_event(t - i * 3600 + 2, "phone_requested", i, rid, "ask")
        if ph:
            db.log_event(t - i * 3600 + 5, "phone_given", i, rid, "ask")
        sent = ("welcome", "ask", "thanks") if ph else (("welcome", "ask") if i != 4 else ())
        for k in sent:
            db.conn.execute("INSERT INTO queue(run_id,user_id,funnel,step_key,due_at,status) VALUES (?,?,?,?,?,'sent')",
                            (rid, i, fn, k, t))
    db.conn.execute("INSERT INTO queue(run_id,user_id,funnel,step_key,due_at,status) VALUES (?,?,?,?,?,'cancelled')",
                    (runs[4], 4, "signup", "welcome", t))
    db.conn.execute("INSERT INTO queue(run_id,user_id,funnel,step_key,due_at,status) VALUES (?,?,?,?,?,'expired')",
                    (runs[2], 2, "signup", "m1", t))
    db.conn.execute("INSERT INTO users(tg_id,name,username,created_at,tags) VALUES (6,'Игорь','igor',?,'[]')", (t - 7 * 3600,))
    cur = db.conn.execute("INSERT INTO runs(user_id,funnel,status,started_at) VALUES (6,'plain','completed',?)", (t,))
    db.log_event(t - 7 * 3600, "run_started", 6, cur.lastrowid, "plain")
    db.log_event(t - 7 * 3600 + 1, "message_sent", 6, cur.lastrowid, "a")
    for k, (kind, d) in enumerate([("step_expired", "m1"), ("send_failed", "m1: Blocked: Forbidden: bot was blocked by the user"),
                                   ("run_cancelled", "повторный /start, отменено ждущих: 1"),
                                   ("phone_declined", None)]):
        db.log_event(t + k * 60, kind, k % 5 + 1, runs[k % 5 + 1], d)
    db.log_event(t + 400, "start_blocked", 1, None, "signup")
    return db, parse_config(RAW)


db, cfg = seed()
REVEAL = os.environ.get("REVEAL") == "1"
TOKEN = os.environ.get("PREVIEW_TOKEN", "")
ADMINS = frozenset(int(x) for x in os.environ.get("PREVIEW_ADMIN_IDS", "").split(",") if x)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        reveal = REVEAL
        q = parse_qs(u.query)
        if TOKEN and u.path.startswith("/api/"):
            from app.auth import authorize
            ok, reveal, _ = authorize(self.headers.get("X-Init-Data", ""), disabled=False, token=TOKEN,
                                      admin_ids=ADMINS, max_age=86400)
            if not ok:
                return self.send(b'{"error": "forbidden"}', "application/json", 403)
        if u.path == "/":
            self.send(open(os.path.join(ROOT, "app/static/index.html"), "rb").read(), "text/html; charset=utf-8")
        elif u.path == "/api/funnels":
            self.send(json.dumps(api.funnels_payload(db.conn, cfg)).encode(), "application/json")
        elif u.path == "/api/overview":
            body = api.overview_payload(db.conn, cfg, q.get("funnel", [None])[0])
            self.send(json.dumps(body).encode(), "application/json", 200 if body is not None else 404)
        elif u.path == "/api/users":
            body = api.users_payload(db.conn, cfg, q.get("level", [""])[0] or None, q.get("funnel", [None])[0],
                                     reveal=reveal)
            self.send(json.dumps(body).encode(), "application/json", 200 if body is not None else 400)
        else:
            self.send(b"{}", "application/json", 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    HTTPServer(("127.0.0.1", port), H).serve_forever()
