"""From "play X" to X playing on the phone.

  understand ─ rules or LLM → Command
  library    ─ already in Jellyfin? → send its ID to the phone, done
  searching  ─ Soulseek search through slskd, rank files (matching.py)
  downloading─ best file first; next one if it fails or sits in a long queue
  importing  ─ tag it, move it to IMPORT_DIR/Artist/Album/NN - Title.ext
  indexing   ─ tell Jellyfin, wait until the song shows up
  sending    ─ phone plays or queues it (it plays its own downloaded copy if it has one)

Songs that finish while the phone is away are queued on the phone when it reconnects.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import events, intents, jellyfin, matching, slskd
from .config import DATA_DIR, settings
from .intents import Command
from .phone import hub

FAILED_STATES = ("Errored", "Rejected", "TimedOut", "Cancelled", "Failed", "Aborted")


@dataclass
class Request:
    id: str
    text: str
    source: str
    created: float
    stage: str = "understanding"
    status: str = "active"  # active, done, failed, waiting_phone, cancelled
    message: str = ""
    command: dict[str, Any] = field(default_factory=dict)
    jellyfin_id: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    chosen: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    progress: float = 0.0
    file: str = ""
    delivered: bool = False
    timeline: list[dict[str, Any]] = field(default_factory=list)
    updated: float = 0.0

    def view(self) -> dict[str, Any]:
        d = asdict(self)
        d["candidates"] = self.candidates[:15]
        return d


class Pipeline:
    def __init__(self) -> None:
        self.requests: dict[str, Request] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._dl_sem: asyncio.Semaphore | None = None
        self._path = DATA_DIR / "requests.json"
        self._load()

    # ---- bookkeeping -----------------------------------------------------------------------

    def _load(self) -> None:
        try:
            for d in json.loads(self._path.read_text("utf-8")):
                r = Request(**d)
                if r.status == "active":
                    r.status, r.message = "failed", "Interrupted by a restart"
                self.requests[r.id] = r
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    def _save(self) -> None:
        items = sorted(self.requests.values(), key=lambda r: r.created)[-300:]
        self.requests = {r.id: r for r in items}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in items]), "utf-8")
        tmp.replace(self._path)

    def _update(self, r: Request, stage: str | None = None, message: str | None = None, status: str | None = None, save: bool = True, **fields: Any) -> None:
        if stage and stage != r.stage:
            r.stage = stage
        if message is not None:
            r.message = message
            r.timeline.append({"t": time.time(), "stage": r.stage, "message": message})
            events.log("pipeline", f"[{r.command.get('title') or r.text[:40]}] {message}")
        if status:
            r.status = status
        for k, v in fields.items():
            setattr(r, k, v)
        r.updated = time.time()
        events.emit("request", keep=False, request=r.view())
        if save:
            self._save()

    def list(self) -> list[dict[str, Any]]:
        return [r.view() for r in sorted(self.requests.values(), key=lambda r: r.created, reverse=True)]

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.requests.values():
            out[r.status] = out.get(r.status, 0) + 1
        return out

    @property
    def sem(self) -> asyncio.Semaphore:
        if self._dl_sem is None:
            self._dl_sem = asyncio.Semaphore(max(1, int(settings["parallel_downloads"])))
        return self._dl_sem

    # ---- entry points ----------------------------------------------------------------------

    async def handle_text(self, text: str, source: str = "ui") -> Request:
        r = Request(id=uuid.uuid4().hex[:10], text=text, source=source, created=time.time())
        self.requests[r.id] = r
        self._update(r, message=f"Heard “{text}”")
        self.tasks[r.id] = asyncio.create_task(self._run(r))
        return r

    async def handle_command(self, cmd: Command, source: str = "mcp") -> Request:
        """A command that's already structured (MCP tools skip the language step)."""
        r = Request(id=uuid.uuid4().hex[:10], text=cmd.raw or cmd.describe(), source=source, created=time.time())
        self.requests[r.id] = r
        self._update(r, message=f"Command: {cmd.describe()}", command=cmd.as_dict())
        self.tasks[r.id] = asyncio.create_task(self._run(r, cmd))
        return r

    def cancel(self, rid: str) -> bool:
        t = self.tasks.get(rid)
        r = self.requests.get(rid)
        if t and not t.done():
            t.cancel()
            return True
        if r and r.status == "waiting_phone":
            self._update(r, status="cancelled", message="Cancelled")
            return True
        return False

    async def retry(self, rid: str) -> Request | None:
        r = self.requests.get(rid)
        if not r:
            return None
        if r.command:
            return await self.handle_command(Command(**r.command), source=r.source)
        return await self.handle_text(r.text, r.source)

    def delete(self, rid: str) -> None:
        self.cancel(rid)
        self.requests.pop(rid, None)
        self._save()

    async def phone_connected(self) -> None:
        """Queues songs that became ready while the phone was away (last 3 hours)."""
        await asyncio.sleep(2)  # let the phone finish its hello
        waiting = [r for r in self.requests.values() if r.status == "waiting_phone" and time.time() - r.created < 3 * 3600]
        for r in sorted(waiting, key=lambda r: r.created):
            res = await hub.command("queue_item", self._item_args(r))
            if res["ok"]:
                self._update(r, stage="done", status="done", delivered=True, message="Queued on the phone after it reconnected")

    # ---- the work --------------------------------------------------------------------------

    async def _run(self, r: Request, cmd: Command | None = None) -> None:
        try:
            if cmd is None:
                self._update(r, stage="understanding", save=False)
                cmd = await intents.parse(r.text)
                self._update(r, command=cmd.as_dict(), message=f"Understood ({cmd.source}): {cmd.describe()}")
            if not cmd.is_music:
                await self._control(r, cmd)
            elif cmd.kind == "song":
                await self._song(r, cmd)
            else:
                await self._folder(r, cmd)
        except asyncio.CancelledError:
            self._update(r, status="cancelled", message="Cancelled")
            await self._cancel_transfer(r)
        except Exception as e:  # noqa: BLE001 - show any failure on the request
            self._update(r, stage="failed", status="failed", message=f"Failed: {e}")
            await hub.notice(f"Couldn't do that: {e}", request_id=r.id, stage="failed")
        finally:
            self.tasks.pop(r.id, None)

    async def _control(self, r: Request, cmd: Command) -> None:
        if cmd.action == "chat":
            reply = cmd.reply or "I'm not sure what to do with that."
            await hub.notice(reply, request_id=r.id)
            self._update(r, stage="done", status="done", message=f"Reply: {reply}")
            return
        args = {"destination": cmd.destination} if cmd.destination else {}
        self._update(r, stage="sending", message=f"Sending {cmd.action} to the phone")
        res = await hub.command(cmd.action, args)
        if res["ok"]:
            self._update(r, stage="done", status="done", delivered=True, message=res.get("message") or "Done on the phone")
        else:
            self._update(r, stage="failed", status="failed", message=res["message"])

    async def _folder(self, r: Request, cmd: Command) -> None:
        name = cmd.album if cmd.kind == "album" else cmd.artist
        self._update(r, stage="library", message=f"Looking for the {cmd.kind} in Jellyfin")
        found = await jellyfin.search_folders(name, cmd.kind)
        best = None
        for f in found:
            s = matching.fuzz.token_set_ratio(matching.norm(name), matching.norm(f["name"]))
            if cmd.kind == "album" and cmd.artist and f.get("artist"):
                s = 0.7 * s + 0.3 * matching.fuzz.token_set_ratio(matching.norm(cmd.artist), matching.norm(f["artist"]))
            if s >= 80 and (best is None or s > best[0]):
                best = (s, f)
        if not best:
            raise RuntimeError(
                f"The {cmd.kind} “{name}” isn't in the library. Downloading whole {cmd.kind}s from Soulseek isn't supported yet; ask for a song."
            )
        f = best[1]
        r.jellyfin_id = f["id"]
        if cmd.action == "download":
            self._update(r, stage="done", status="done", message=f"Already in the library: {f['name']}")
            return
        await self._deliver(r, cmd, {"item_id": f["id"], "kind": cmd.kind, "title": f["name"], "shuffle": cmd.kind == "artist"}, "play_folder")

    async def _song(self, r: Request, cmd: Command) -> None:
        self._update(r, stage="library", message="Checking the Jellyfin library")
        track = await jellyfin.find_song(cmd.artist, cmd.title)
        if track:
            r.jellyfin_id = track.id
            self._update(r, message=f"In the library: {track.artist} – {track.name}")
            if cmd.action == "download":
                self._update(r, stage="done", status="done", message="Already in the library, nothing to download")
                return
            await self._deliver(r, cmd, {"item_id": track.id, "title": track.name, "artist": track.artist})
            return

        if not cmd.artist and cmd.action != "download":
            # "play radiohead" without the LLM arrives as a song title; try it as an artist or album.
            for kind in ("artist", "album"):
                for f in await jellyfin.search_folders(cmd.title, kind):
                    if matching.fuzz.ratio(matching.norm(cmd.title), matching.norm(f["name"])) >= 90:
                        r.jellyfin_id = f["id"]
                        self._update(r, message=f"No song called that, but there's the {kind} {f['name']}")
                        await self._deliver(r, cmd, {"item_id": f["id"], "kind": kind, "title": f["name"], "shuffle": kind == "artist"}, "play_folder")
                        return

        self._update(r, message="Not in the library: searching Soulseek")
        await hub.notice(f"Finding “{cmd.title}” on Soulseek…", request_id=r.id, stage="searching")
        async with self.sem:
            path = await self._fetch(r, cmd)
        self._update(r, stage="importing", message="Tagging and moving into the music library")
        dest = await asyncio.to_thread(import_file, path, cmd)
        r.file = str(dest)
        self._update(r, message=f"Saved as {dest}")

        self._update(r, stage="indexing", message="Waiting for Jellyfin to pick it up")
        track = await self._wait_for_jellyfin(r, cmd, dest)
        r.jellyfin_id = track.id
        self._update(r, message=f"Jellyfin has it: {track.artist} – {track.name}")
        if cmd.action == "download":
            self._update(r, stage="done", status="done", message="Downloaded to the library")
            return
        await self._deliver(r, cmd, {"item_id": track.id, "title": track.name, "artist": track.artist})

    def _item_args(self, r: Request) -> dict[str, Any]:
        c = r.command
        return {"item_id": r.jellyfin_id, "title": c.get("title", ""), "artist": c.get("artist", "")}

    async def _deliver(self, r: Request, cmd: Command, args: dict[str, Any], action: str | None = None) -> None:
        action = action or ("queue_item" if cmd.action == "queue" else "play_item")
        if not hub.connected:
            self._update(r, stage="ready", status="waiting_phone", message="Ready; will queue it on the phone when it reconnects")
            return
        self._update(r, stage="sending", message=f"Sending to the phone ({action})")
        res = await hub.command(action, args)
        if res["ok"]:
            self._update(r, stage="done", status="done", delivered=True, message=res.get("message") or ("Playing" if action != "queue_item" else "Queued"))
        else:
            self._update(r, stage="ready", status="waiting_phone", message=f"Phone couldn't play it ({res['message']}); will retry when it reconnects")

    # ---- Soulseek --------------------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        return {k: settings[k] for k in ("formats", "prefer_lossless", "min_bitrate", "max_file_mb")}

    async def _fetch(self, r: Request, cmd: Command) -> Path:
        self._update(r, stage="searching")
        timeout = int(settings["search_timeout_s"])
        queries = [matching.search_text(cmd.artist, cmd.title)]
        if cmd.artist:
            queries.append(matching.search_text("", cmd.title))
        ranked: list[dict[str, Any]] = []
        for q in dict.fromkeys(queries):
            self._update(r, message=f"Soulseek search: “{q}” ({timeout} s)")
            events.log("soulseek", f"Searching “{q}”")
            _, responses = await slskd.search(q, timeout)
            nfiles = sum(len(x.get("files", [])) for x in responses)
            ranked = matching.rank(responses, cmd.artist, cmd.title, self._cfg())
            events.log("soulseek", f"“{q}”: {len(responses)} users, {nfiles} files, {len(ranked)} usable")
            if ranked:
                break
        if not ranked:
            raise RuntimeError("No usable files found on Soulseek")
        self._update(r, candidates=ranked, message=f"{len(ranked)} usable files; best: {_short(ranked[0])}")

        tries = ranked[: max(1, int(settings["max_attempts"]))]
        for i, cand in enumerate(tries, 1):
            self._update(r, stage="downloading", chosen=cand, progress=0.0, message=f"Download {i}/{len(tries)} from {cand['username']}: {_short(cand)}")
            ok, why = await self._download(r, cand, last=i == len(tries))
            r.attempts.append({"candidate": _short(cand), "username": cand["username"], "ok": ok, "why": why})
            if ok:
                path = await self._locate(cand["filename"])
                if path:
                    return path
                why = "finished, but the file wasn't found in the downloads folder"
            self._update(r, message=f"Attempt {i} failed: {why}")
        raise RuntimeError("Every download attempt failed")

    async def _download(self, r: Request, cand: dict[str, Any], last: bool) -> tuple[bool, str]:
        await slskd.enqueue(cand["username"], cand["filename"], cand["size"])
        events.log("soulseek", f"Queued {_short(cand)} from {cand['username']}")
        start = time.monotonic()
        queued_since = time.monotonic()
        last_pct = -1.0
        while time.monotonic() - start < int(settings["download_timeout_s"]):
            await asyncio.sleep(2)
            t = await slskd.find_transfer(cand["username"], cand["filename"])
            if not t:
                continue
            state = str(t.get("state", ""))
            r.chosen = {**cand, "transfer_id": t.get("id"), "state": state}
            if "Succeeded" in state:
                self._update(r, progress=100.0, message="Download finished")
                return True, ""
            if any(s in state for s in FAILED_STATES):
                return False, state
            if "Queued" in state:
                # Someone else's long upload queue: move on after 2 minutes unless this is the last option.
                if not last and time.monotonic() - queued_since > 120:
                    await slskd.cancel(cand["username"], t["id"])
                    return False, f"stuck in {cand['username']}'s queue"
            else:
                queued_since = time.monotonic()
            pct = float(t.get("percentComplete") or 0)
            if pct - last_pct >= 2 or ("Queued" in state and last_pct < 0):
                last_pct = pct
                self._update(r, progress=round(pct, 1), save=False)
        await self._cancel_transfer(r)
        return False, "timed out"

    async def _cancel_transfer(self, r: Request) -> None:
        tid = r.chosen.get("transfer_id")
        if tid and "Succeeded" not in str(r.chosen.get("state", "")):
            try:
                await slskd.cancel(r.chosen["username"], tid)
            except Exception:  # noqa: BLE001
                pass

    async def _locate(self, remote_filename: str) -> Path | None:
        """slskd saves to DOWNLOADS/<remote folder>/<file>; find it (allowing for renames)."""
        base = re.split(r"[\\/]", remote_filename)[-1]
        folder = re.split(r"[\\/]", remote_filename)[-2] if re.search(r"[\\/]", remote_filename) else ""
        root = Path(settings["slskd_downloads_dir"])
        stem, ext = os.path.splitext(base)
        for _ in range(10):
            direct = root / folder / base
            if direct.exists():
                return direct
            hits = [p for p in root.rglob(f"*{ext}") if p.name == base or p.name.startswith(stem)]
            if hits:
                return max(hits, key=lambda p: p.stat().st_mtime)
            await asyncio.sleep(1.5)
        return None

    async def _wait_for_jellyfin(self, r: Request, cmd: Command, dest: Path) -> jellyfin.Track:
        targeted = await jellyfin.notify_new_file(str(dest))
        if not targeted:
            self._update(r, message="Asked Jellyfin for a library scan (set JELLYFIN_IMPORT_PATH to make this faster)")
        title = read_tags(dest).get("title") or cmd.title
        artist = read_tags(dest).get("artist") or cmd.artist
        start = time.monotonic()
        refreshed = not targeted
        while time.monotonic() - start < 240:
            await asyncio.sleep(3)
            for t in await jellyfin.search_tracks(title):
                if t.path and Path(t.path).name == dest.name:
                    return t
            t = await jellyfin.find_song(artist, title, threshold=90)
            if t:
                return t
            if not refreshed and time.monotonic() - start > 45:
                refreshed = True
                self._update(r, message="Still not there; asking Jellyfin for a full library scan")
                await jellyfin.full_refresh()
        raise RuntimeError("Jellyfin didn't index the new file within 4 minutes (is IMPORT_DIR inside a Jellyfin music library?)")


def _short(c: dict[str, Any]) -> str:
    name = re.split(r"[\\/]", c["filename"])[-1]
    q = "lossless" if c["ext"] in matching.LOSSLESS else f"{c['bitrate']} kbps" if c.get("bitrate") else c["ext"]
    return f"{name} ({q}, {c['size'] / 1_048_576:.1f} MB, score {c['score']})"


# ---- tagging and filing --------------------------------------------------------------------

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(s: str, fallback: str) -> str:
    s = _BAD.sub("_", (s or "").strip()).strip(". ")
    return (s or fallback)[:120]


def read_tags(path: Path) -> dict[str, str]:
    try:
        import mutagen

        f = mutagen.File(str(path), easy=True)
        if not f or not f.tags:
            return {}
        return {k: str(v[0]) for k, v in f.tags.items() if v and k in {"artist", "albumartist", "album", "title", "tracknumber", "date"}}
    except Exception:  # noqa: BLE001 - odd or broken files: just no tags
        return {}


def import_file(src: Path, cmd: Command) -> Path:
    """Fills in missing tags, then moves the file to IMPORT_DIR/Artist/Album/NN - Title.ext."""
    import mutagen

    tags = read_tags(src)
    want = {"artist": cmd.artist, "title": cmd.title}
    try:
        f = mutagen.File(str(src), easy=True)
        if f is not None:
            if f.tags is None and hasattr(f, "add_tags"):
                f.add_tags()
            changed = False
            for k, v in want.items():
                if v and not tags.get(k):
                    f[k] = v
                    tags[k] = v
                    changed = True
            if changed:
                f.save()
    except Exception as e:  # noqa: BLE001
        events.log("pipeline", f"Couldn't write tags to {src.name}: {e}", "warn")

    artist = tags.get("albumartist") or tags.get("artist") or cmd.artist
    album = tags.get("album") or "Singles"
    title = tags.get("title") or cmd.title or src.stem
    num = re.match(r"\d+", tags.get("tracknumber", ""))
    name = (f"{int(num.group()):02d} - " if num else "") + _safe(title, "Track") + src.suffix.lower()
    dest = Path(settings["import_dir"]) / _safe(artist, "Unknown Artist") / _safe(album, "Singles") / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        src.unlink(missing_ok=True)
        return dest
    shutil.move(str(src), str(dest))
    try:
        os.chmod(dest, 0o664)
    except OSError:
        pass
    # Remove the now-empty download folder.
    try:
        src.parent.rmdir()
    except OSError:
        pass
    return dest


_lib_cache: dict[str, tuple[float, dict[str, str]]] = {}


def downloaded_library(limit: int = 500) -> list[dict[str, Any]]:
    """Songs in IMPORT_DIR, newest first."""
    root = Path(settings["import_dir"])
    if not root.exists():
        return []
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in matching.LOSSLESS | {"mp3", "m4a", "ogg", "opus", "aac"}]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:limit]:
        st = p.stat()
        key = str(p)
        cached = _lib_cache.get(key)
        tags = cached[1] if cached and cached[0] == st.st_mtime else read_tags(p)
        _lib_cache[key] = (st.st_mtime, tags)
        out.append({
            "path": str(p.relative_to(root)),
            "title": tags.get("title") or p.stem,
            "artist": tags.get("artist", ""),
            "album": tags.get("album", ""),
            "size": st.st_size,
            "format": p.suffix.lower().lstrip("."),
            "added": st.st_mtime,
        })
    return out


pipeline = Pipeline()
