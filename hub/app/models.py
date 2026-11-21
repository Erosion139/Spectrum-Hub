"""GGUF model files: list, download from Hugging Face, upload from the browser, delete."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from . import events
from .config import MODELS_DIR, settings

SAFE_NAME = re.compile(r"^[\w.\-]+\.gguf$")

# file name -> progress for downloads in flight
downloads: dict[str, dict[str, Any]] = {}


def safe_path(name: str) -> Path:
    if not SAFE_NAME.match(name):
        raise ValueError("Model names must be plain .gguf file names")
    return MODELS_DIR / name


def list_models() -> list[dict[str, Any]]:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(MODELS_DIR.glob("*.gguf")):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "modified": st.st_mtime, "active": p.name == settings["llm_model"]})
    return out


def delete_model(name: str) -> None:
    p = safe_path(name)
    if p.exists():
        p.unlink()
    events.log("llm", f"Deleted model {name}")


def _hf_headers() -> dict[str, str]:
    tok = settings["hf_token"]
    return {"Authorization": f"Bearer {tok}"} if tok else {}


async def hf_files(repo: str) -> list[dict[str, Any]]:
    """GGUF files in a Hugging Face repo, e.g. 'Qwen/Qwen2.5-3B-Instruct-GGUF'."""
    repo = repo.strip().removeprefix("https://huggingface.co/").strip("/")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(f"https://huggingface.co/api/models/{repo}/tree/main", params={"recursive": "true"}, headers=_hf_headers())
        if r.status_code == 404:
            raise ValueError("Repo not found (or it's private and needs a Hugging Face token)")
        r.raise_for_status()
        files = [
            {"path": f["path"], "size": f.get("lfs", {}).get("size") or f.get("size", 0)}
            for f in r.json()
            if f.get("type") == "file" and f["path"].endswith(".gguf")
        ]
        return sorted(files, key=lambda f: f["size"])


async def hf_download(repo: str, path: str) -> str:
    repo = repo.strip().removeprefix("https://huggingface.co/").strip("/")
    name = Path(path).name
    dest = safe_path(name)
    if name in downloads and downloads[name]["state"] == "downloading":
        raise ValueError("Already downloading")
    prog = downloads[name] = {"name": name, "repo": repo, "state": "downloading", "done": 0, "total": 0, "speed": 0, "error": ""}
    asyncio.create_task(_download(repo, path, dest, prog))
    return name


async def _download(repo: str, path: str, dest: Path, prog: dict[str, Any]) -> None:
    tmp = dest.with_suffix(".part")
    url = f"https://huggingface.co/{repo}/resolve/main/{path}"
    events.log("llm", f"Downloading {repo}/{path}")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), follow_redirects=True) as c:
            async with c.stream("GET", url, headers=_hf_headers()) as r:
                r.raise_for_status()
                prog["total"] = int(r.headers.get("content-length") or 0)
                t0, last_emit = time.monotonic(), 0.0
                with tmp.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
                        prog["done"] += len(chunk)
                        now = time.monotonic()
                        prog["speed"] = prog["done"] / max(0.1, now - t0)
                        if now - last_emit > 1:
                            last_emit = now
                            events.emit("model_download", keep=False, **prog)
        tmp.replace(dest)
        prog["state"] = "done"
        events.log("llm", f"Downloaded {dest.name} ({prog['done'] / 1e9:.2f} GB)")
    except Exception as e:  # noqa: BLE001 - report anything to the UI
        prog["state"], prog["error"] = "error", str(e)
        tmp.unlink(missing_ok=True)
        events.log("llm", f"Download of {dest.name} failed: {e}", "error")
    events.emit("model_download", keep=False, **prog)


async def save_upload(name: str, body: AsyncIterator[bytes]) -> int:
    dest = safe_path(name)
    tmp = dest.with_suffix(".part")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in body:
                f.write(chunk)
                n += len(chunk)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    events.log("llm", f"Uploaded model {name} ({n / 1e9:.2f} GB)")
    return n
