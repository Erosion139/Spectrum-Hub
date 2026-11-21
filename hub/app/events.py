"""Activity log and live updates for the web UI.

Everything interesting (phone messages, LLM prompts and output, Soulseek searches, downloads)
goes through log(). The UI gets a backlog on connect and then every new event over a WebSocket.
Safe to call from threads (the llama-server output reader runs in one).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

_backlog: deque[dict[str, Any]] = deque(maxlen=1500)
_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None
_seq = 0


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def _publish(event: dict[str, Any]) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow browser tab: drop rather than stall the server


def emit(kind: str, keep: bool = True, **data: Any) -> None:
    """Sends an event to the UI. [keep] adds it to the backlog new tabs see."""
    global _seq
    _seq += 1
    event = {"seq": _seq, "t": time.time(), "kind": kind, **data}
    if keep:
        _backlog.append(event)
    if _loop is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        _publish(event)
    else:
        _loop.call_soon_threadsafe(_publish, event)


def log(category: str, message: str, level: str = "info", **data: Any) -> None:
    """One line in the activity log. category: phone, llm, soulseek, jellyfin, pipeline, system, mcp."""
    emit("log", category=category, level=level, message=message, **({"data": data} if data else {}))


def backlog() -> list[dict[str, Any]]:
    return list(_backlog)


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)
