"""Jellyfin: find songs, and tell the server about newly imported files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings
from .matching import song_score


@dataclass
class Track:
    id: str
    name: str
    artist: str
    album: str
    album_id: str
    path: str
    duration_s: int

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class JellyfinError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    url = settings["jellyfin_url"].rstrip("/")
    key = settings["jellyfin_api_key"]
    if not url or not key:
        raise JellyfinError("Jellyfin address or API key not set")
    return httpx.AsyncClient(
        base_url=url,
        headers={"X-Emby-Token": key, "Accept": "application/json"},
        timeout=httpx.Timeout(15.0, connect=5.0),
    )


async def ping() -> dict[str, Any]:
    async with _client() as c:
        r = await c.get("/System/Info")
        if r.status_code == 401:
            raise JellyfinError("API key rejected (401)")
        r.raise_for_status()
        info = r.json()
        return {"name": info.get("ServerName"), "version": info.get("Version")}


def _track(o: dict[str, Any]) -> Track:
    artists = o.get("Artists") or []
    return Track(
        id=o["Id"],
        name=o.get("Name", ""),
        artist=o.get("AlbumArtist") or (artists[0] if artists else ""),
        album=o.get("Album", ""),
        album_id=o.get("AlbumId", ""),
        path=o.get("Path", ""),
        duration_s=int((o.get("RunTimeTicks") or 0) / 10_000_000),
    )


async def search_tracks(term: str, limit: int = 25) -> list[Track]:
    async with _client() as c:
        r = await c.get(
            "/Items",
            params={
                "searchTerm": term,
                "IncludeItemTypes": "Audio",
                "Recursive": "true",
                "Limit": limit,
                "Fields": "Path",
            },
        )
        r.raise_for_status()
        return [_track(o) for o in r.json().get("Items", [])]


async def search_folders(term: str, kind: str, limit: int = 10) -> list[dict[str, Any]]:
    """kind: album or artist."""
    types = {"album": "MusicAlbum", "artist": "MusicArtist"}[kind]
    async with _client() as c:
        r = await c.get(
            "/Items",
            params={"searchTerm": term, "IncludeItemTypes": types, "Recursive": "true", "Limit": limit},
        )
        r.raise_for_status()
        return [
            {"id": o["Id"], "name": o.get("Name", ""), "artist": o.get("AlbumArtist", ""), "type": kind}
            for o in r.json().get("Items", [])
        ]


async def find_song(artist: str, title: str, threshold: float = 82) -> Track | None:
    """Best library match for artist + title, or None if nothing is close enough."""
    best: tuple[float, Track] | None = None
    # Title alone first (Jellyfin's search is per-field); then artist + title as a fallback.
    for term in dict.fromkeys(filter(None, [title, f"{artist} {title}".strip()])):
        for t in await search_tracks(term):
            s = song_score(artist, title, t.artist, t.name)
            if best is None or s > best[0]:
                best = (s, t)
        if best and best[0] >= 95:
            break
    if best and best[0] >= threshold:
        return best[1]
    return None


async def notify_new_file(hub_path: str) -> bool:
    """Tells Jellyfin a file appeared. Returns False if only a full library scan was possible."""
    async with _client() as c:
        jf_root = settings["jellyfin_import_path"].rstrip("/")
        if jf_root:
            rel = hub_path[len(settings["import_dir"].rstrip("/")):].lstrip("/")
            path = f"{jf_root}/{rel}"
            r = await c.post(
                "/Library/Media/Updated",
                json={"Updates": [{"Path": path, "UpdateType": "Created"}]},
            )
            r.raise_for_status()
            return True
        r = await c.post("/Library/Refresh")
        r.raise_for_status()
        return False


async def full_refresh() -> None:
    async with _client() as c:
        (await c.post("/Library/Refresh")).raise_for_status()
