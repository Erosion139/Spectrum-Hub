"""Settings: environment variables give the first values, the web UI can change them later.

Changes made in the UI are saved to /data/settings.json and win over the environment from
then on (so you can set things up once in the TrueNAS YAML, then tweak them in the browser).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/models"))
LLAMA_SERVER_BIN = os.environ.get("LLAMA_SERVER_BIN", "/usr/local/bin/llama-server")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Values the UI shows as "••••" and only replaces when you type a new one.
SECRET_KEYS = {"jellyfin_api_key", "slskd_api_key", "api_token", "ui_password", "hf_token"}


def _env(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, list):
        return [p.strip() for p in raw.split(",") if p.strip()]
    return raw


def _defaults() -> dict[str, Any]:
    return {
        # Jellyfin
        "jellyfin_url": _env("JELLYFIN_URL", ""),
        "jellyfin_api_key": _env("JELLYFIN_API_KEY", ""),
        # Where the hub's music folder is as *Jellyfin* sees it (its container path). Lets the
        # hub tell Jellyfin about one new file instead of rescanning everything.
        "jellyfin_import_path": _env("JELLYFIN_IMPORT_PATH", ""),
        # slskd (Soulseek)
        "slskd_url": _env("SLSKD_URL", "http://slskd:5030"),
        "slskd_api_key": _env("SLSKD_API_KEY", ""),
        "slskd_downloads_dir": _env("SLSKD_DOWNLOADS_DIR", "/downloads"),
        # Finished songs are moved here: Artist/Album/NN - Title.ext. Must be inside a
        # Jellyfin music library.
        "import_dir": _env("IMPORT_DIR", "/music/Soulseek"),
        # Search and download choices
        "formats": _env("FORMATS", ["flac", "mp3", "m4a", "ogg", "opus"]),
        "prefer_lossless": _env("PREFER_LOSSLESS", False),
        "min_bitrate": _env("MIN_BITRATE", 192),
        "max_file_mb": _env("MAX_FILE_MB", 150),
        "search_timeout_s": _env("SEARCH_TIMEOUT_S", 15),
        "download_timeout_s": _env("DOWNLOAD_TIMEOUT_S", 900),
        "max_attempts": _env("MAX_ATTEMPTS", 3),
        "parallel_downloads": _env("PARALLEL_DOWNLOADS", 2),
        # Access
        "api_token": _env("API_TOKEN", ""),
        "ui_password": _env("UI_PASSWORD", ""),
        # LLM
        "llm_model": _env("LLM_MODEL", ""),
        "llm_autostart": _env("LLM_AUTOSTART", True),
        "llm_gpu_layers": _env("LLM_GPU_LAYERS", 99),
        "llm_ctx": _env("LLM_CTX", 4096),
        "llm_threads": _env("LLM_THREADS", 0),
        "llm_extra_args": _env("LLM_EXTRA_ARGS", ""),
        "hf_token": _env("HF_TOKEN", ""),
    }


class Settings:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = DATA_DIR / "settings.json"
        self._values = _defaults()
        if self._path.exists():
            try:
                saved = json.loads(self._path.read_text("utf-8"))
                self._values.update({k: v for k, v in saved.items() if k in self._values})
            except (OSError, json.JSONDecodeError):
                pass
        if not self._values["api_token"]:
            # The phone and MCP clients need a token; make one on first run.
            self._values["api_token"] = secrets.token_urlsafe(24)
            self._save()

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def public(self) -> dict[str, Any]:
        """Settings for the UI, with secrets masked."""
        out = {}
        for k, v in self._values.items():
            if k in SECRET_KEYS:
                out[k] = {"set": bool(v)}
            else:
                out[k] = v
        return out

    def update(self, changes: dict[str, Any]) -> None:
        with self._lock:
            defaults = _defaults()
            for k, v in changes.items():
                if k not in self._values:
                    continue
                if k in SECRET_KEYS and (v is None or v == ""):
                    continue  # blank secret field = keep the current one
                d = defaults[k]
                if isinstance(d, bool):
                    v = bool(v)
                elif isinstance(d, int) and not isinstance(d, bool):
                    v = int(v)
                elif isinstance(d, list) and isinstance(v, str):
                    v = [p.strip() for p in v.split(",") if p.strip()]
                self._values[k] = v
            self._save()

    def clear_secret(self, key: str) -> None:
        if key in SECRET_KEYS and key != "api_token":
            with self._lock:
                self._values[key] = ""
                self._save()

    def _save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._values, indent=2), "utf-8")
        tmp.replace(self._path)


settings = Settings()
