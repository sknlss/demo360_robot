"""Собирает tests/_preview.html: index.html с подставленными данными вместо fetch.

Нужен, чтобы посмотреть вёрстку через file:// без сервера. Данные — из preview_server.seed(), через app/api.py.
Запросы страницы разбираются по пути и параметрам; неизвестный адрес отвечает 404.
Админ (номера целиком): REVEAL=1 python3 tests/build_preview.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import preview_server as ps  # noqa: E402
from app import api, stats  # noqa: E402

reveal = os.environ.get("REVEAL") == "1"
data = {"api/funnels": api.funnels_payload(ps.db.conn, ps.cfg), "api/users": api.users_payload(ps.db.conn, ps.cfg, reveal=reveal)}
for fk in ps.cfg.funnels:
    data["api/overview?funnel=" + fk] = api.overview_payload(ps.db.conn, ps.cfg, fk)
    for lv in stats.LEVELS:
        data["api/users?level=%s&funnel=%s" % (lv, fk)] = api.users_payload(ps.db.conn, ps.cfg, lv, fk, reveal=reveal)
mock = ("<script>var __D=%s;window.__hdrs=[];window.fetch=function(p,o){__hdrs.push(o&&o.headers?o.headers['X-Init-Data']:null);var v=__D[p];"
        "return Promise.resolve({ok:!!v,status:v?200:404,json:function(){return Promise.resolve(v)},"
        "blob:function(){return Promise.resolve(null)}})};</script>" % json.dumps(data))
html = open(os.path.join(ROOT, "app/static/index.html"), encoding="utf-8").read()
open(os.path.join(ROOT, "tests/_preview.html"), "w", encoding="utf-8").write(html.replace("<script>", mock + "<script>", 1))
