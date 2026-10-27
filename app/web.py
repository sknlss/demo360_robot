"""Дашборд: aiohttp. Страница открыта, данные (/api/*) — только с подписью Telegram; полные телефоны — только админам."""
import logging
import os

from aiohttp import web

from . import api
from .auth import authorize

log = logging.getLogger("funnel.web")
STATIC = os.path.join(os.path.dirname(__file__), "static")


@web.middleware
async def auth_middleware(request, handler):
    if request.path.startswith("/api/"):
        s = request.app["settings"]
        ok, reveal, why = authorize(request.headers.get("X-Init-Data", ""), disabled=s.auth_disabled,
                                    token=s.bot_token, admin_ids=s.admin_ids,
                                    max_age=s.init_data_max_age)
        if not ok:
            log.info("доступ закрыт: %s", why)
            return web.json_response({"error": "forbidden"}, status=403)
        request["reveal"] = reveal
    return await handler(request)


async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"),
                            headers={"Cache-Control": "no-cache"})


def _state(request):
    a = request.app
    return a["db"].conn, a["cfg"]


async def api_funnels(request):
    return web.json_response(api.funnels_payload(*_state(request)))


async def api_overview(request):
    body = api.overview_payload(*_state(request), funnel=request.query.get("funnel"))
    if body is None:
        return web.json_response({"error": "unknown funnel"}, status=404)
    return web.json_response(body)


async def api_users(request):
    # без level — все пользователи; номера замазаны, пока middleware не поставил reveal (по умолчанию False)
    body = api.users_payload(*_state(request), level=request.query.get("level") or None,
                             funnel=request.query.get("funnel"), reveal=request.get("reveal", False))
    if body is None:
        return web.json_response({"error": "unknown level or funnel"}, status=400)
    return web.json_response(body)


async def api_photo(request):
    try:
        uid = int(request.match_info["user_id"])
    except ValueError:
        raise web.HTTPNotFound()
    photos = request.app.get("photos")
    path = None
    if photos:
        try:
            path = await photos.get_path(uid)
        except Exception:
            log.exception("не удалось получить фото %s", uid)
    if not path:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


def make_app(db, cfg, settings, photos=None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["db"], app["cfg"], app["settings"], app["photos"] = db, cfg, settings, photos
    app.router.add_get("/", index)
    app.router.add_get("/api/funnels", api_funnels)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/users", api_users)
    app.router.add_get("/api/photo/{user_id}", api_photo)
    return app
