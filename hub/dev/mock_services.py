"""Fake Jellyfin + slskd for trying the hub on a PC without TrueNAS.

    python dev/mock_services.py --downloads ./scratch/downloads --music ./scratch/music

Then point the hub at it: JELLYFIN_URL=http://127.0.0.1:8790/jf, SLSKD_URL=http://127.0.0.1:8790/slskd,
any non-empty API keys. Jellyfin "knows" a few songs; anything else is found on fake Soulseek
users, "downloaded" (a silent MP3) into --downloads, and appears in Jellyfin once the hub has
moved it into --music.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI()
ARGS: argparse.Namespace

LIBRARY = [
    {"Id": "jf-dreams", "Name": "Dreams", "AlbumArtist": "Fleetwood Mac", "Album": "Rumours", "Path": "/media/music/Fleetwood Mac/Rumours/03 - Dreams.flac"},
    {"Id": "jf-karma", "Name": "Karma Police", "AlbumArtist": "Radiohead", "Album": "OK Computer", "Path": "/media/music/Radiohead/OK Computer/06 - Karma Police.flac"},
]
ALBUMS = [{"Id": "jf-rumours", "Name": "Rumours", "AlbumArtist": "Fleetwood Mac", "Type": "MusicAlbum"}]
ARTISTS = [{"Id": "jf-radiohead", "Name": "Radiohead", "Type": "MusicArtist"}]
searches: dict[str, dict] = {}
transfers: list[dict] = []
indexed: list[dict] = []


def silent_mp3(frames: int = 1600) -> bytes:
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz: 417-byte frames of silence. Valid enough for mutagen.
    frame = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)
    return frame * frames


# ---- Jellyfin ---------------------------------------------------------------------------

@app.get("/jf/System/Info")
async def jf_info():
    return {"ServerName": "Mock Jellyfin", "Version": "10.11.0"}


def _scan():
    root = Path(ARGS.music)
    for p in root.rglob("*.mp3"):
        if any(i["Path"].endswith(p.name) for i in indexed):
            continue
        artist = p.parent.parent.name
        indexed.append({"Id": "jf-" + uuid.uuid4().hex[:8], "Name": p.stem.split(" - ", 1)[-1], "AlbumArtist": artist, "Album": p.parent.name, "Path": str(p)})


@app.get("/jf/Items")
async def jf_items(searchTerm: str = "", IncludeItemTypes: str = "Audio"):
    term = searchTerm.lower()
    pool = {"Audio": LIBRARY + indexed, "MusicAlbum": ALBUMS, "MusicArtist": ARTISTS}.get(IncludeItemTypes, [])
    hits = [i for i in pool if any(w in (i["Name"] + " " + i.get("AlbumArtist", "")).lower() for w in term.split())]
    return {"Items": hits}


@app.post("/jf/Library/Media/Updated")
async def jf_updated(request: Request):
    asyncio.get_running_loop().call_later(4, _scan)
    return {}


@app.post("/jf/Library/Refresh")
async def jf_refresh():
    asyncio.get_running_loop().call_later(6, _scan)
    return {}


# ---- slskd ------------------------------------------------------------------------------

@app.get("/slskd/api/v0/server")
async def sl_server():
    return {"state": "Connected, LoggedIn", "isConnected": True, "isLoggedIn": True, "username": "mockuser"}


@app.post("/slskd/api/v0/searches")
async def sl_search(body: dict):
    sid = body.get("id") or str(uuid.uuid4())
    text = body["searchText"]
    words = " ".join(w.capitalize() for w in text.split())
    searches[sid] = {
        "id": sid, "searchText": text, "state": "InProgress", "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "responseCount": 3, "fileCount": 4, "ends": time.time() + 4,
        "responses": [
            {"username": "slowpoke", "hasFreeUploadSlot": False, "queueLength": 14, "uploadSpeed": 90000,
             "files": [{"filename": f"@@music\\Various\\{words} (Live).mp3", "size": 7_000_000, "bitRate": 320, "extension": "mp3"}]},
            {"username": "goodsource", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 3_000_000,
             "files": [{"filename": f"@@share\\Music\\{words}\\01 - {words}.mp3", "size": 9_200_000, "bitRate": 320, "extension": "mp3"},
                       {"filename": f"@@share\\Music\\{words}\\cover.jpg", "size": 90_000, "extension": "jpg"}]},
            {"username": "lowbit", "hasFreeUploadSlot": True, "queueLength": 0, "uploadSpeed": 500000,
             "files": [{"filename": f"music\\{words}.mp3", "size": 2_000_000, "bitRate": 96, "extension": "mp3"}]},
        ],
    }
    return {k: v for k, v in searches[sid].items() if k != "responses"}


@app.get("/slskd/api/v0/searches")
async def sl_searches():
    return [{k: v for k, v in s.items() if k not in ("responses", "ends")} for s in searches.values()]


@app.get("/slskd/api/v0/searches/{sid}")
async def sl_search_get(sid: str):
    s = searches[sid]
    if time.time() > s["ends"]:
        s["state"] = "Completed, TimedOut"
    return {k: v for k, v in s.items() if k not in ("responses", "ends")}


@app.get("/slskd/api/v0/searches/{sid}/responses")
async def sl_responses(sid: str):
    return searches[sid]["responses"]


async def _run_transfer(t: dict):
    t["state"] = "Queued, Remote"
    await asyncio.sleep(2)
    t["state"] = "InProgress"
    for p in range(0, 101, 20):
        t["percentComplete"] = p
        t["averageSpeed"] = 2_500_000
        await asyncio.sleep(0.6)
    parts = t["filename"].replace("/", "\\").split("\\")
    dest = Path(ARGS.downloads) / parts[-2] / parts[-1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(silent_mp3())
    t["state"] = "Completed, Succeeded"


@app.post("/slskd/api/v0/transfers/downloads/{username}")
async def sl_enqueue(username: str, body: list[dict]):
    for f in body:
        t = {"id": uuid.uuid4().hex, "username": username, "filename": f["filename"], "size": f["size"], "state": "Requested", "percentComplete": 0}
        transfers.append(t)
        asyncio.create_task(_run_transfer(t))
    return {}


@app.get("/slskd/api/v0/transfers/downloads")
async def sl_downloads():
    users: dict[str, list] = {}
    for t in transfers:
        users.setdefault(t["username"], []).append({k: v for k, v in t.items() if k != "username"})
    return [{"username": u, "directories": [{"directory": "x", "files": fs}]} for u, fs in users.items()]


@app.delete("/slskd/api/v0/transfers/downloads/{username}/{tid}")
async def sl_cancel(username: str, tid: str):
    for t in transfers:
        if t["id"] == tid:
            t["state"] = "Completed, Cancelled"
    return {}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--downloads", required=True)
    ap.add_argument("--music", required=True)
    ap.add_argument("--port", type=int, default=8790)
    ARGS = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=ARGS.port)
