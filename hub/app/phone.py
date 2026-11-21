"""The phone's WebSocket link.

The phone connects out to /ws/phone (at home on the LAN address, away through the Cloudflare
tunnel) and keeps the socket open. Messages are JSON:

phone -> hub
  {"type": "hello", "device": "Pixel 8", "app": "0.1.0"}
  {"type": "voice", "id": "<uuid>", "text": "play dreams by fleetwood mac"}
  {"type": "status", "state": "playing", "title": "...", "artist": "...", "media_id": "jf:s0:..."}
  {"type": "ack", "id": "<command id>", "ok": true, "message": "..."}
  {"type": "ping"}

hub -> phone
  {"type": "welcome", "server": "Spectrum Hub"}
  {"type": "command", "id": "<id>", "action": "play_item", "args": {"item_id": "...", "title": "..."}}
      actions: play_item, queue_item, play_folder, skip, previous, pause, resume,
               navigate, add_stop, stop_navigation
  {"type": "notice", "text": "Downloading Dreams…", "request_id": "...", "stage": "downloading"}
  {"type": "pong"}
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

from fastapi import WebSocket

from . import events


class Phone:
    def __init__(self, ws: WebSocket, remote: str) -> None:
        self.ws = ws
        self.remote = remote
        self.device = "phone"
        self.app = ""
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.now_playing: dict[str, Any] = {}

    def info(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "app": self.app,
            "remote": self.remote,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
            "now_playing": self.now_playing,
        }


class PhoneHub:
    def __init__(self) -> None:
        self.phones: list[Phone] = []
        self.pending: dict[str, asyncio.Future] = {}
        self.on_voice: Callable[[str, str], Awaitable[None]] | None = None
        self.on_connect: Callable[[], Awaitable[None]] | None = None
        self.last_disconnect: float | None = None

    @property
    def connected(self) -> bool:
        return bool(self.phones)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "phones": [p.info() for p in self.phones],
            "last_disconnect": self.last_disconnect,
        }

    def _changed(self) -> None:
        events.emit("phone", keep=False, **self.status())

    async def serve(self, ws: WebSocket) -> None:
        remote = ws.headers.get("cf-connecting-ip") or (ws.client.host if ws.client else "?")
        phone = Phone(ws, remote)
        self.phones.append(phone)
        via = "Cloudflare tunnel" if ws.headers.get("cf-ray") else "local network"
        events.log("phone", f"Phone connected from {remote} ({via})")
        await ws.send_json({"type": "welcome", "server": "Spectrum Hub"})
        self._changed()
        if self.on_connect:
            asyncio.create_task(self.on_connect())
        try:
            while True:
                msg = await ws.receive_json()
                phone.last_seen = time.time()
                await self._handle(phone, msg)
        except Exception as e:  # noqa: BLE001 - any disconnect or bad frame ends this phone
            if type(e).__name__ not in {"WebSocketDisconnect", "ConnectionClosedOK"}:
                events.log("phone", f"Phone link error: {e}", "warn")
        finally:
            if phone in self.phones:
                self.phones.remove(phone)
            self.last_disconnect = time.time()
            events.log("phone", f"Phone disconnected ({phone.device})")
            self._changed()

    async def _handle(self, phone: Phone, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "ping":
            await phone.ws.send_json({"type": "pong"})
        elif t == "hello":
            phone.device = str(msg.get("device") or "phone")[:60]
            phone.app = str(msg.get("app") or "")[:30]
            events.log("phone", f"Phone says hello: {phone.device} (app {phone.app})")
            self._changed()
        elif t == "status":
            phone.now_playing = {k: msg.get(k) for k in ("state", "title", "artist", "media_id")}
            self._changed()
        elif t == "voice":
            text = str(msg.get("text") or "").strip()
            if text:
                events.log("phone", f"🎤 “{text}”")
                if self.on_voice:
                    asyncio.create_task(self.on_voice(text, "phone"))
        elif t == "ack":
            fut = self.pending.pop(str(msg.get("id")), None)
            if fut and not fut.done():
                fut.set_result(msg)
        else:
            events.log("phone", f"Unknown message from phone: {t}", "warn")

    async def command(self, action: str, args: dict[str, Any] | None = None, timeout: float = 10) -> dict[str, Any]:
        """Sends a command to the phone and waits for its answer."""
        if not self.phones:
            return {"ok": False, "message": "Phone not connected"}
        phone = self.phones[-1]  # newest connection
        cid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        events.log("phone", f"→ phone: {action} {args or ''}".rstrip())
        try:
            await phone.ws.send_json({"type": "command", "id": cid, "action": action, "args": args or {}})
            ack = await asyncio.wait_for(fut, timeout)
            ok = bool(ack.get("ok"))
            events.log("phone", f"← phone: {'ok' if ok else 'failed'} {ack.get('message') or ''}".rstrip(), "info" if ok else "warn")
            return {"ok": ok, "message": ack.get("message", "")}
        except asyncio.TimeoutError:
            events.log("phone", f"Phone didn't answer '{action}' in {timeout:.0f} s", "warn")
            return {"ok": False, "message": "No answer from the phone"}
        finally:
            self.pending.pop(cid, None)

    async def notice(self, text: str, **extra: Any) -> None:
        for p in list(self.phones):
            try:
                await p.ws.send_json({"type": "notice", "text": text, **extra})
            except Exception:  # noqa: BLE001
                pass


hub = PhoneHub()
