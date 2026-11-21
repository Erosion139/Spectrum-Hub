"""Runs llama-server (llama.cpp) with the model picked in the UI, and talks to it.

llama-server is started as a child process on 127.0.0.1:8081, so switching models is just
stop + start with a different file. Its own log lines go to the activity log.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx

from . import events
from .config import LLAMA_SERVER_BIN, MODELS_DIR, settings

PORT = 8081
BASE = f"http://127.0.0.1:{PORT}"


class LlamaManager:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.state = "stopped"  # stopped, loading, ready, error
        self.model = ""
        self.error = ""
        self.started_at = 0.0
        self.requests = 0
        self.last: dict[str, Any] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=50)
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        avg = None
        gens = [h["gen_tps"] for h in self.history if h.get("gen_tps")]
        if gens:
            avg = round(sum(gens) / len(gens), 1)
        return {
            "state": self.state,
            "model": self.model,
            "error": self.error,
            "uptime_s": int(time.time() - self.started_at) if self.state == "ready" else 0,
            "requests": self.requests,
            "last": self.last,
            "avg_gen_tps": avg,
            "binary_found": Path(LLAMA_SERVER_BIN).exists(),
            "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
        }

    async def start(self, model: str | None = None) -> None:
        async with self._lock:
            await self._stop()
            model = model or settings["llm_model"]
            if not model:
                self.state, self.error = "stopped", "No model selected"
                return
            path = MODELS_DIR / model
            if not path.exists():
                self.state, self.error = "error", f"Model file not found: {model}"
                events.log("llm", self.error, "error")
                return
            if not Path(LLAMA_SERVER_BIN).exists():
                self.state, self.error = "error", f"llama-server not found at {LLAMA_SERVER_BIN}"
                events.log("llm", self.error, "error")
                return
            args = [
                LLAMA_SERVER_BIN, "-m", str(path), "--host", "127.0.0.1", "--port", str(PORT),
                "-ngl", str(settings["llm_gpu_layers"]), "-c", str(settings["llm_ctx"]),
                "--metrics", "--no-webui", "-np", "1",
            ]
            if settings["llm_threads"]:
                args += ["-t", str(settings["llm_threads"])]
            if settings["llm_extra_args"]:
                args += shlex.split(settings["llm_extra_args"])
            events.log("llm", "Starting llama-server: " + " ".join(shlex.quote(a) for a in args[1:]))
            self.model, self.state, self.error = model, "loading", ""
            self.proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                errors="replace",
            )
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        asyncio.create_task(self._wait_ready(self.proc))

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                events.emit("llm_server", keep=True, line=line)
        code = proc.wait()
        if proc is self.proc and self.state != "stopped":
            self.state = "error"
            self.error = f"llama-server exited (code {code}). Check the server log; the model may not fit in VRAM."
            events.log("llm", self.error, "error")

    async def _wait_ready(self, proc: subprocess.Popen) -> None:
        async with httpx.AsyncClient(timeout=3) as c:
            for _ in range(600):  # big models on slow disks take a while
                if proc is not self.proc or proc.poll() is not None:
                    return
                try:
                    r = await c.get(f"{BASE}/health")
                    if r.status_code == 200:
                        self.state, self.started_at = "ready", time.time()
                        events.log("llm", f"Model ready: {self.model}")
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        p, self.proc = self.proc, None
        self.state = "stopped"
        if p and p.poll() is None:
            events.log("llm", "Stopping llama-server")
            p.terminate()
            for _ in range(50):
                if p.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if p.poll() is None:
                p.kill()

    async def chat_json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 200) -> dict[str, Any]:
        """One chat turn whose answer must match [schema]. Streams tokens to the UI."""
        if self.state != "ready":
            raise RuntimeError(f"LLM not ready ({self.state})")
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "command", "schema": schema}},
            # Thinking models: we want the answer, not the reasoning.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        turn = f"t{int(time.time() * 1000)}"
        events.emit("llm_turn", turn=turn, phase="start", user=user)
        text, timings = "", {}
        t0 = time.perf_counter()
        first = None
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as c:
            async with c.stream("POST", f"{BASE}/v1/chat/completions", json=body) as r:
                if r.status_code >= 400:
                    err = (await r.aread()).decode(errors="replace")[:300]
                    raise RuntimeError(f"llama-server error {r.status_code}: {err}")
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("timings"):
                        timings = chunk["timings"]
                    for ch in chunk.get("choices", []):
                        piece = (ch.get("delta") or {}).get("content") or ""
                        if piece:
                            first = first or time.perf_counter()
                            text += piece
                            events.emit("llm_turn", keep=False, turn=turn, phase="token", text=piece)
        total = time.perf_counter() - t0
        stats = {
            "at": time.time(),
            "latency_s": round(total, 2),
            "first_token_s": round((first or time.perf_counter()) - t0, 2),
            "prompt_tokens": timings.get("prompt_n"),
            "gen_tokens": timings.get("predicted_n"),
            "prompt_tps": round(timings.get("prompt_per_second") or 0, 1) or None,
            "gen_tps": round(timings.get("predicted_per_second") or 0, 1) or None,
        }
        self.requests += 1
        self.last = stats
        self.history.append(stats)
        events.emit("llm_turn", turn=turn, phase="end", output=text, stats=stats)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Model output wasn't valid JSON: {text[:200]}") from e


llm = LlamaManager()
