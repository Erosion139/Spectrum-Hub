"""MCP server at /mcp (streamable HTTP, Bearer API_TOKEN).

The same actions the voice pipeline uses, as tools, so any MCP client (a bigger local model,
Claude Desktop, scripts) can drive the hub and the phone.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import events, jellyfin
from .intents import Command
from .phone import hub
from .pipeline import pipeline

mcp = MCPServer(
    name="spectrum-hub",
    instructions=(
        "Controls a car music player (Spectrum Player on Android) and a music library. "
        "Songs not in the Jellyfin library are fetched from Soulseek automatically; that can take minutes, "
        "so request_song returns a request id you can check with get_request."
    ),
)

activity: dict[str, Any] = {"calls": 0, "last_call": None, "last_tool": None}


def _note(tool: str) -> None:
    activity["calls"] += 1
    activity["last_call"] = time.time()
    activity["last_tool"] = tool
    events.log("mcp", f"Tool call: {tool}")


@mcp.tool()
async def voice_command(text: str) -> dict[str, Any]:
    """Runs a natural-language command exactly as if it was spoken on the phone ("play dreams by fleetwood mac", "skip")."""
    _note("voice_command")
    r = await pipeline.handle_text(text, source="mcp")
    return {"request_id": r.id, "status": r.status}


@mcp.tool()
async def request_song(title: str, artist: str = "", action: Literal["play", "queue", "download"] = "play") -> dict[str, Any]:
    """Plays, queues or downloads a song. Uses the library copy if there is one, otherwise downloads it from Soulseek first."""
    _note("request_song")
    r = await pipeline.handle_command(Command(action=action, kind="song", title=title, artist=artist, source="mcp"))
    return {"request_id": r.id, "status": r.status}


@mcp.tool()
async def play_collection(kind: Literal["album", "artist"], name: str, artist: str = "", action: Literal["play", "queue"] = "play") -> dict[str, Any]:
    """Plays an album or an artist that's already in the library."""
    _note("play_collection")
    cmd = Command(action=action, kind=kind, album=name if kind == "album" else "", artist=artist if kind == "album" else name, source="mcp")
    r = await pipeline.handle_command(cmd)
    return {"request_id": r.id, "status": r.status}


@mcp.tool()
async def get_request(request_id: str) -> dict[str, Any]:
    """Progress of a request (stage, message, download progress)."""
    _note("get_request")
    r = pipeline.requests.get(request_id)
    if not r:
        return {"error": "No such request"}
    v = r.view()
    return {k: v[k] for k in ("id", "text", "stage", "status", "message", "progress", "jellyfin_id", "file")}


@mcp.tool()
async def list_requests(limit: int = 10) -> list[dict[str, Any]]:
    """The most recent requests, newest first."""
    _note("list_requests")
    return [{k: r[k] for k in ("id", "text", "stage", "status", "message")} for r in pipeline.list()[:limit]]


@mcp.tool()
async def search_library(query: str) -> list[dict[str, Any]]:
    """Searches songs in the Jellyfin library."""
    _note("search_library")
    return [t.as_dict() for t in await jellyfin.search_tracks(query, limit=15)]


@mcp.tool()
async def phone_control(
    action: Literal["skip", "previous", "pause", "resume", "navigate", "add_stop", "stop_navigation"],
    destination: str = "",
) -> dict[str, Any]:
    """Sends a playback or navigation command straight to the phone."""
    _note("phone_control")
    return await hub.command(action, {"destination": destination} if destination else {})


@mcp.tool()
async def phone_status() -> dict[str, Any]:
    """Whether the phone is connected and what it's playing."""
    _note("phone_status")
    return hub.status()
