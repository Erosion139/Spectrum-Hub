"""Spectrum Hub: web UI, REST API, phone WebSocket and MCP server in one process."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import platform
import time
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings

from . import events, jellyfin, metrics, models, slskd
from .config import MODELS_DIR, WEB_DIR, settings
from .llm import llm
from .mcp_tools import activity as mcp_activity
from .mcp_tools import mcp
from .phone import hub
from .pipeline import downloaded_library, pipeline

VERSION = "0.1.0"
services: dict[str, dict[str, Any]] = {
    "jellyfin": {"ok": None, "detail": "Not checked yet"},
    "soulseek": {"ok": None, "detail": "Not checked yet"},
}


async def check_services() -> None:
    try:
        info = await jellyfin.ping()
        services["jellyfin"] = {"ok": True, "detail": f"{info['name']} (Jellyfin {info['version']})"}
    except Exception as e:  # noqa: BLE001
        services["jellyfin"] = {"ok": False, "detail": str(e) or type(e).__name__}
    try:
        st = await slskd.server_state()
        logged = bool(st.get("isLoggedIn"))
        services["soulseek"] = {
            "ok": logged,
            "detail": f"{st.get('state', '?')}" + (f" as {st.get('username')}" if st.get("username") else ""),
            "connected": bool(st.get("isConnected")),
        }
    except Exception as e:  # noqa: BLE001
        services["soulseek"] = {"ok": False, "detail": f"slskd unreachable: {e}" if "API key" not in str(e) else str(e)}
    for v in services.values():
        v["checked"] = time.time()


def status() -> dict[str, Any]:
    return {
        "version": VERSION,
        "services": services,
        "phone": hub.status(),
        "llm": llm.status(),
        "mcp": {**mcp_activity, "active": bool(mcp_activity["last_call"] and time.time() - mcp_activity["last_call"] < 300)},
        "requests": pipeline.stats(),
        "host": platform.node(),
    }


async def _metrics_loop() -> None:
    metrics.sample()
    while True:
        await asyncio.sleep(2)
        s = await asyncio.to_thread(metrics.sample)
        events.emit("metrics", keep=False, metrics=s, llm=llm.status())


async def _services_loop() -> None:
    while True:
        before = {k: v.get("ok") for k, v in services.items()}
        await check_services()
        for k, v in services.items():
            if v.get("ok") != before.get(k):
                events.log("system", f"{k.capitalize()}: {'OK' if v['ok'] else 'problem'}, {v['detail']}", "info" if v["ok"] else "warn")
        events.emit("services", keep=False, services=services)
        await asyncio.sleep(20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    events.bind_loop(asyncio.get_running_loop())
    hub.on_voice = lambda text, src: pipeline.handle_text(text, src)
    hub.on_connect = pipeline.phone_connected
    events.log("system", f"Spectrum Hub {VERSION} starting")
    tasks = [asyncio.create_task(_metrics_loop()), asyncio.create_task(_services_loop())]
    if settings["llm_autostart"] and settings["llm_model"]:
        asyncio.create_task(llm.start())
    async with mcp.session_manager.run():
        yield
    for t in tasks:
        t.cancel()
    await llm.stop()


app = FastAPI(title="Spectrum Hub", version=VERSION, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)

# MCP over streamable HTTP at /mcp. Host checks are off: it's reached through the tunnel's
# hostname as well as the LAN address, and the Bearer token does the protecting.
_mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
app.router.routes.extend(_mcp_app.routes)


# ---- access control -------------------------------------------------------------------------

def _ui_cookie_value() -> str:
    return hmac.new(settings["api_token"].encode(), ("ui:" + settings["ui_password"]).encode(), hashlib.sha256).hexdigest()


def _token_ok(given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(given, settings["api_token"])


class AccessMiddleware:
    """Phone and MCP use the API token; the web UI uses a password cookie (if a password is set)."""

    OPEN = ("/health", "/login", "/static/login.css")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path: str = scope["path"]
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if path.startswith(self.OPEN) or path == "/ws/phone":
            return await self.app(scope, receive, send)  # /ws/phone checks its token itself
        if path.startswith("/mcp"):
            auth = headers.get("authorization", "")
            tok = auth[7:] if auth.lower().startswith("bearer ") else parse_qs(scope.get("query_string", b"").decode()).get("token", [None])[0]
            if _token_ok(tok):
                return await self.app(scope, receive, send)
            return await JSONResponse({"error": "Bearer token required"}, status_code=401)(scope, receive, send)
        if not settings["ui_password"]:
            return await self.app(scope, receive, send)
        cookie = SimpleCookie(headers.get("cookie", ""))
        val = cookie.get("hub_auth")
        if val and hmac.compare_digest(val.value, _ui_cookie_value()):
            return await self.app(scope, receive, send)
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and _token_ok(auth[7:]):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        if path.startswith("/api"):
            return await JSONResponse({"error": "Sign in first"}, status_code=401)(scope, receive, send)
        return await RedirectResponse("/login")(scope, receive, send)


app.add_middleware(AccessMiddleware)


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "login.html").read_text("utf-8"))


@app.post("/login")
async def login(request: Request):
    form = parse_qs((await request.body()).decode())
    pw = form.get("password", [""])[0]
    if settings["ui_password"] and hmac.compare_digest(pw, settings["ui_password"]):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("hub_auth", _ui_cookie_value(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 60)
        return resp
    return RedirectResponse("/login?bad=1", status_code=303)


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("hub_auth")
    return resp


# ---- pages ----------------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ---- status, settings, activity ----------------------------------------------------------------

@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    return status()


@app.post("/api/status/check")
async def api_check() -> dict[str, Any]:
    await check_services()
    return status()


@app.get("/api/metrics")
async def api_metrics() -> dict[str, Any]:
    return {"history": list(metrics.history)}


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return settings.public()


@app.put("/api/settings")
async def put_settings(body: dict[str, Any]) -> dict[str, Any]:
    try:
        settings.update(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Bad value: {e}") from e
    events.log("system", "Settings saved")
    asyncio.create_task(check_services())
    return settings.public()


@app.get("/api/settings/token")
async def get_token() -> dict[str, str]:
    """The phone/MCP token, shown on request in the UI."""
    return {"api_token": settings["api_token"]}


@app.post("/api/settings/token/rotate")
async def rotate_token() -> dict[str, str]:
    import secrets

    settings.update({"api_token": secrets.token_urlsafe(24)})
    events.log("system", "API token changed: update the phone app", "warn")
    return {"api_token": settings["api_token"]}


@app.get("/api/events")
async def get_events() -> list[dict[str, Any]]:
    return events.backlog()


@app.websocket("/ws/ui")
async def ui_socket(ws: WebSocket) -> None:
    await ws.accept()
    q = events.subscribe()
    try:
        await ws.send_json({"kind": "hello", "status": status(), "backlog": events.backlog()[-400:], "requests": pipeline.list()[:100]})
        while True:
            ev = await q.get()
            await ws.send_json(ev)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        events.unsubscribe(q)


# ---- requests (the voice pipeline) ------------------------------------------------------------

@app.get("/api/requests")
async def list_requests() -> list[dict[str, Any]]:
    return pipeline.list()


@app.post("/api/requests")
async def new_request(body: dict[str, Any]) -> dict[str, Any]:
    text = str(body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Say something")
    r = await pipeline.handle_text(text, source="ui")
    return r.view()


@app.post("/api/requests/{rid}/cancel")
async def cancel_request(rid: str) -> dict[str, bool]:
    return {"cancelled": pipeline.cancel(rid)}


@app.post("/api/requests/{rid}/retry")
async def retry_request(rid: str) -> dict[str, Any]:
    r = await pipeline.retry(rid)
    if not r:
        raise HTTPException(404, "No such request")
    return r.view()


@app.delete("/api/requests/{rid}")
async def delete_request(rid: str) -> dict[str, bool]:
    pipeline.delete(rid)
    return {"deleted": True}


@app.get("/api/library")
async def library() -> list[dict[str, Any]]:
    return await asyncio.to_thread(downloaded_library)


# ---- phone ------------------------------------------------------------------------------------

@app.websocket("/ws/phone")
async def phone_socket(ws: WebSocket) -> None:
    auth = ws.headers.get("authorization", "")
    tok = auth[7:] if auth.lower().startswith("bearer ") else ws.query_params.get("token")
    if not _token_ok(tok):
        events.log("phone", "Rejected a phone connection with a wrong token", "warn")
        await ws.close(code=4401)
        return
    await ws.accept()
    await hub.serve(ws)


@app.post("/api/phone/command")
async def phone_command(body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action") or "")
    if not action:
        raise HTTPException(400, "action required")
    return await hub.command(action, body.get("args") or {})


# ---- Soulseek ---------------------------------------------------------------------------------

@app.get("/api/soulseek")
async def soulseek_activity() -> dict[str, Any]:
    try:
        searches = await slskd.list_searches()
        transfers = await slskd.downloads()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "searches": [], "downloads": []}
    searches.sort(key=lambda s: s.get("startedAt", ""), reverse=True)
    transfers.sort(key=lambda t: t.get("requestedAt", "") or t.get("enqueuedAt", ""), reverse=True)
    return {"searches": searches[:40], "downloads": transfers[:60]}


# ---- models and the LLM -------------------------------------------------------------------------

@app.get("/api/models")
async def get_models() -> dict[str, Any]:
    import shutil as _sh

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    du = _sh.disk_usage(MODELS_DIR)
    return {
        "models": models.list_models(),
        "downloads": list(models.downloads.values()),
        "disk_free": du.free,
        "llm": llm.status(),
        "params": {k: settings[k] for k in ("llm_gpu_layers", "llm_ctx", "llm_threads", "llm_extra_args", "llm_autostart")},
    }


@app.post("/api/models/hf/files")
async def hf_files(body: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return await models.hf_files(str(body.get("repo", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/models/hf/download")
async def hf_download(body: dict[str, Any]) -> dict[str, str]:
    try:
        name = await models.hf_download(str(body.get("repo", "")), str(body.get("path", "")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"name": name}


@app.put("/api/models/upload/{name}")
async def upload_model(name: str, request: Request) -> dict[str, Any]:
    try:
        n = await models.save_upload(name, request.stream())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"name": name, "size": n}


@app.delete("/api/models/{name}")
async def delete_model(name: str) -> dict[str, bool]:
    if name == llm.model and llm.state in ("loading", "ready"):
        await llm.stop()
    try:
        models.delete_model(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if settings["llm_model"] == name:
        settings.update({"llm_model": ""})
    return {"deleted": True}


@app.post("/api/llm/start")
async def llm_start(body: dict[str, Any]) -> dict[str, Any]:
    model = str(body.get("model") or settings["llm_model"])
    params = {k: body[k] for k in ("llm_gpu_layers", "llm_ctx", "llm_threads", "llm_extra_args", "llm_autostart") if k in body}
    settings.update({"llm_model": model, **params})
    await llm.start(model)
    return llm.status()


@app.post("/api/llm/stop")
async def llm_stop() -> dict[str, Any]:
    await llm.stop()
    return llm.status()


@app.post("/api/llm/test")
async def llm_test(body: dict[str, Any]) -> dict[str, Any]:
    """Parses a phrase without acting on it (for trying models)."""
    from . import intents

    text = str(body.get("text") or "")
    t0 = time.perf_counter()
    cmd = await intents.parse(text)
    return {"command": cmd.as_dict(), "seconds": round(time.perf_counter() - t0, 2), "llm": llm.status()["last"]}
