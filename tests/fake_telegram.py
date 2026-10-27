"""Заглушка Telegram Bot API на stdlib: python3 tests/fake_telegram.py [порт]

Бот подключается через TELEGRAM_API_BASE=http://127.0.0.1:<порт>. Умеет: getMe, deleteWebhook,
getUpdates (длинный опрос), sendMessage, getUserProfilePhotos, getFile и отдачу файла.
Управление тестом:
  POST /_test/update          тело — JSON Update (см. start_update/contact_update ниже)
  POST /_test/mode            {"mode": "429"|"403"|null, "retry_after": 3, "count": 1} — следующие N sendMessage
  GET  /_test/sent            все отправленные sendMessage (с метками времени)
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"updates": [], "sent": [], "next_id": 1, "mode": None, "mode_left": 0, "retry_after": 1}
LOCK = threading.Lock()
FILE_BYTES = b"\xff\xd8\xff\xe0FAKEJPEG\xff\xd9"


def user(uid=111, name="Тест"):
    return {"id": uid, "is_bot": False, "first_name": name, "username": f"user{uid}"}


def start_update(uid=111, text="/start", name="Тест"):
    return {"message": {"message_id": 1, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                        "from": user(uid, name), "text": text,
                        "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}


def contact_update(uid=111, phone="+37400000000", contact_uid=None):
    return {"message": {"message_id": 2, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                        "from": user(uid),
                        "contact": {"phone_number": phone, "first_name": "Тест",
                                    "user_id": contact_uid or uid}}}


def text_update(uid=111, text="нет"):
    return {"message": {"message_id": 3, "date": int(time.time()), "chat": {"id": uid, "type": "private"},
                        "from": user(uid), "text": text}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        if not raw:
            return {}
        if "json" in (self.headers.get("Content-Type") or ""):
            return json.loads(raw)
        from urllib.parse import parse_qsl
        return dict(parse_qsl(raw))

    def do_GET(self):
        if self.path == "/_test/sent":
            with LOCK:
                return self.reply(STATE["sent"])
        if self.path.startswith("/file/bot"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(FILE_BYTES)))
            self.end_headers()
            self.wfile.write(FILE_BYTES)
            return
        self.do_POST()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/_test/update":
            with LOCK:
                u = self.body()
                u["update_id"] = STATE["next_id"]
                STATE["next_id"] += 1
                STATE["updates"].append(u)
            return self.reply({"ok": True})
        if path == "/_test/mode":
            b = self.body()
            with LOCK:
                STATE.update(mode=b.get("mode"), mode_left=int(b.get("count", 1)),
                             retry_after=int(b.get("retry_after", 1)))
            return self.reply({"ok": True})
        method = path.rsplit("/", 1)[-1]
        data = self.body()
        if method == "getMe":
            return self.reply({"ok": True, "result": {"id": 123456, "is_bot": True, "first_name": "Stub",
                                                      "username": "stub_bot"}})
        if method == "deleteWebhook":
            return self.reply({"ok": True, "result": True})
        if method == "getUpdates":
            offset, timeout = int(data.get("offset", 0) or 0), int(data.get("timeout", 0) or 0)
            end = time.time() + min(timeout, 5)
            while True:
                with LOCK:
                    out = [u for u in STATE["updates"] if u["update_id"] >= offset]
                if out or time.time() >= end:
                    return self.reply({"ok": True, "result": out})
                time.sleep(0.1)
        if method == "sendMessage":
            with LOCK:
                mode = STATE["mode"] if STATE["mode_left"] > 0 else None
                if mode:
                    STATE["mode_left"] -= 1
                STATE["sent"].append({"t": time.time(), "mode": mode, **data})
                n = len(STATE["sent"])
            if mode == "429":
                return self.reply({"ok": False, "error_code": 429, "description": "Too Many Requests: retry after %d" % STATE["retry_after"],
                                   "parameters": {"retry_after": STATE["retry_after"]}}, 429)
            if mode == "403":
                return self.reply({"ok": False, "error_code": 403,
                                   "description": "Forbidden: bot was blocked by the user"}, 403)
            return self.reply({"ok": True, "result": {"message_id": n, "date": int(time.time()),
                                                      "chat": {"id": int(data.get("chat_id", 0)), "type": "private"},
                                                      "text": data.get("text", "")}})
        if method == "getUserProfilePhotos":
            return self.reply({"ok": True, "result": {"total_count": 1, "photos": [[
                {"file_id": "PHOTO_SMALL", "file_unique_id": "s", "width": 160, "height": 160}]]}})
        if method == "getFile":
            return self.reply({"ok": True, "result": {"file_id": data.get("file_id"), "file_unique_id": "s",
                                                      "file_path": "photos/a.jpg"}})
        return self.reply({"ok": False, "error_code": 404, "description": "unknown method " + method}, 404)


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8081
    print(f"fake telegram on http://127.0.0.1:{port}", flush=True)
    serve(port).serve_forever()
