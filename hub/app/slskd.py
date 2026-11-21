"""slskd (headless Soulseek client) over its REST API, using the primary API key."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from .config import settings


class SlskdError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    key = settings["slskd_api_key"]
    if not key:
        raise SlskdError("slskd API key not set")
    return httpx.AsyncClient(
        base_url=settings["slskd_url"].rstrip("/") + "/api/v0",
        headers={"X-API-Key": key, "Accept": "application/json"},
        timeout=httpx.Timeout(20.0, connect=5.0),
    )


async def server_state() -> dict[str, Any]:
    """{'state': 'Connected, LoggedIn', 'isConnected': ..., 'isLoggedIn': ..., 'username': ...}"""
    async with _client() as c:
        r = await c.get("/server")
        if r.status_code == 401:
            raise SlskdError("API key rejected (401)")
        r.raise_for_status()
        return r.json()


async def search(text: str, timeout_s: int) -> tuple[str, list[dict[str, Any]]]:
    """Runs a search and waits for it to finish. Returns (search id, responses)."""
    sid = str(uuid.uuid4())
    async with _client() as c:
        r = await c.post(
            "/searches",
            json={
                "id": sid,
                "searchText": text,
                "searchTimeout": timeout_s * 1000,
                "responseLimit": 200,
                "fileLimit": 5000,
                "filterResponses": True,
                "minimumResponseFileCount": 1,
            },
        )
        r.raise_for_status()
        deadline = time.monotonic() + timeout_s + 10
        while time.monotonic() < deadline:
            await asyncio.sleep(1.5)
            s = (await c.get(f"/searches/{sid}")).json()
            if "Completed" in str(s.get("state", "")):
                break
        responses = (await c.get(f"/searches/{sid}/responses")).json()
        return sid, responses


async def list_searches() -> list[dict[str, Any]]:
    async with _client() as c:
        r = await c.get("/searches")
        r.raise_for_status()
        return r.json()


async def enqueue(username: str, filename: str, size: int) -> None:
    async with _client() as c:
        r = await c.post(f"/transfers/downloads/{quote(username, safe='')}", json=[{"filename": filename, "size": size}])
        if r.status_code >= 400:
            raise SlskdError(f"slskd refused the download ({r.status_code}): {r.text[:200]}")


async def downloads() -> list[dict[str, Any]]:
    """All transfers, flattened to one dict per file."""
    async with _client() as c:
        r = await c.get("/transfers/downloads")
        r.raise_for_status()
        out = []
        for user in r.json():
            for d in user.get("directories", []):
                for f in d.get("files", []):
                    out.append({**f, "username": user.get("username")})
        return out


async def find_transfer(username: str, filename: str) -> dict[str, Any] | None:
    for f in await downloads():
        if f.get("username") == username and f.get("filename") == filename:
            return f
    return None


async def cancel(username: str, transfer_id: str) -> None:
    async with _client() as c:
        await c.delete(f"/transfers/downloads/{quote(username, safe='')}/{transfer_id}", params={"remove": "true"})
