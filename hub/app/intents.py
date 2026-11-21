"""Turning what you said into a command.

Simple phrases ("skip", "pause", "play X by Y") are matched by rules, instantly, with no LLM.
Anything else goes to the LLM, which must answer with JSON matching SCHEMA (llama.cpp
enforces it with a grammar, so even small models can't return broken output). When no
model is running, the rules are all there is.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import events
from .llm import llm

ACTIONS = [
    "play",          # play a song/album/artist now (download it if needed)
    "queue",         # add to the queue after the current song
    "download",      # just get it into the library
    "skip", "previous", "pause", "resume",
    "navigate",      # start navigation to a place
    "add_stop",      # add a stop on the way
    "stop_navigation",
    "chat",          # not a command: answer in [reply]
]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ACTIONS},
        "kind": {"type": "string", "enum": ["song", "album", "artist"]},
        "artist": {"type": "string"},
        "title": {"type": "string"},
        "album": {"type": "string"},
        "destination": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["action"],
}

SYSTEM_PROMPT = """You turn voice commands from a driver into JSON for a car music and navigation app.
Speech-to-text output may have errors; fix obvious misspellings of well-known artists and songs.

Actions:
- play: play music now. Set kind (song, album or artist), artist, and title (song) or album.
- queue: like play, but add it after the current song ("play X next", "add X to the queue").
- download: only fetch the music to the library, don't play it.
- skip, previous, pause, resume: playback controls.
- navigate: start directions. destination = the place or address.
- add_stop: add a stop on the current route. destination = the place.
- stop_navigation: end directions.
- chat: anything else. Put a one-sentence answer in reply.

Examples:
"play dreams by fleetwood mac" -> {"action":"play","kind":"song","artist":"Fleetwood Mac","title":"Dreams"}
"put on some radiohead" -> {"action":"play","kind":"artist","artist":"Radiohead"}
"play the album rumours" -> {"action":"play","kind":"album","album":"Rumours","artist":"Fleetwood Mac"}
"queue up bohemian rhapsody" -> {"action":"queue","kind":"song","artist":"Queen","title":"Bohemian Rhapsody"}
"next song" -> {"action":"skip"}
"take me to the nearest gas station" -> {"action":"navigate","destination":"gas station"}
"stop at a starbucks on the way" -> {"action":"add_stop","destination":"Starbucks"}
Answer with JSON only."""


@dataclass
class Command:
    action: str
    kind: str = "song"
    artist: str = ""
    title: str = ""
    album: str = ""
    destination: str = ""
    reply: str = ""
    source: str = "rules"  # rules or llm
    raw: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_music(self) -> bool:
        return self.action in {"play", "queue", "download"}

    def describe(self) -> str:
        if self.is_music:
            if self.kind == "song":
                what = f"“{self.title}”" + (f" by {self.artist}" if self.artist else "")
            elif self.kind == "album":
                what = f"album “{self.album}”" + (f" by {self.artist}" if self.artist else "")
            else:
                what = f"artist {self.artist}"
            return f"{self.action} {what}"
        if self.destination:
            return f"{self.action.replace('_', ' ')} → {self.destination}"
        return self.action.replace("_", " ")


_SIMPLE = [
    (r"^(skip|next|next (song|track)|skip (this|it|song|track)( song| track)?)$", "skip"),
    (r"^(previous|back|go back|previous (song|track)|last (song|track))$", "previous"),
    (r"^(pause|stop|stop (the )?music|pause (the )?music)$", "pause"),
    (r"^(resume|play|continue|unpause|resume (the )?music)$", "resume"),
    (r"^(stop|end|cancel) (the )?(navigation|directions|route)$", "stop_navigation"),
]


def _clean(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"^(hey|ok|okay|please|can you|could you|would you)\b[ ,]*", "", t)
    t = re.sub(r"[.!?]+$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_rules(text: str) -> Command | None:
    t = _clean(text)
    for pat, action in _SIMPLE:
        if re.match(pat, t):
            return Command(action=action, raw=text)
    m = re.match(r"^(take me to|navigate to|directions to|drive to|go to)\s+(.+)$", t)
    if m:
        return Command(action="navigate", destination=m.group(2), raw=text)
    m = re.match(r"^(add a stop at|add stop at|stop at|add a stop for)\s+(.+?)( on the way)?$", t)
    if m:
        return Command(action="add_stop", destination=m.group(2), raw=text)
    m = re.match(r"^(put on|play)\s+(some|something by|some music by|music by|songs by)\s+(.+)$", t)
    if m:
        return Command(action="play", kind="artist", artist=m.group(3).title(), raw=text)
    m = re.match(r"^(play|queue|queue up)\s+the album\s+(.+?)(\s+by\s+(.+))?$", t)
    if m:
        return Command(action="queue" if m.group(1).startswith("queue") else "play", kind="album",
                       album=m.group(2).title(), artist=(m.group(4) or "").title(), raw=text)
    m = re.match(r"^(play|queue|queue up|download|get)\s+(the song\s+|song\s+)?(.+?)\s+by\s+(.+)$", t)
    if m:
        verb = m.group(1)
        action = "queue" if verb.startswith("queue") else "download" if verb in {"download", "get"} else "play"
        return Command(action=action, kind="song", title=m.group(3).title(), artist=m.group(4).title(), raw=text)
    return None


def parse_fallback(text: str) -> Command:
    """Used when no LLM is running and the rules didn't match: treat 'play X' as a song title."""
    t = _clean(text)
    m = re.match(r"^(play|queue|queue up|download)\s+(.+)$", t)
    if m:
        action = "queue" if m.group(1).startswith("queue") else m.group(1) if m.group(1) == "download" else "play"
        return Command(action=action, kind="song", title=m.group(2).title(), raw=text)
    return Command(action="chat", reply="I couldn't work that out, and no language model is running.", raw=text)


async def parse(text: str) -> Command:
    cmd = parse_rules(text)
    # With a model running, only the instant controls (skip, pause...) skip it: phrases like
    # "play something chill by bonobo" need the model, not a regex.
    if cmd and llm.state == "ready" and cmd.action not in {"skip", "previous", "pause", "resume", "stop_navigation"}:
        cmd = None
    if cmd:
        events.log("llm", f"Matched by rules (no LLM needed): {cmd.describe()}")
        return cmd
    if llm.state != "ready":
        cmd = parse_fallback(text)
        events.log("llm", f"No model running; best guess: {cmd.describe()}", "warn")
        return cmd
    out = await llm.chat_json(SYSTEM_PROMPT, text, SCHEMA)
    action = out.get("action") if out.get("action") in ACTIONS else "chat"
    cmd = Command(
        action=action,
        kind=out.get("kind") or ("artist" if out.get("artist") and not out.get("title") and not out.get("album") else "song"),
        artist=(out.get("artist") or "").strip(),
        title=(out.get("title") or "").strip(),
        album=(out.get("album") or "").strip(),
        destination=(out.get("destination") or "").strip(),
        reply=(out.get("reply") or "").strip(),
        source="llm",
        raw=text,
    )
    if cmd.is_music and cmd.kind == "song" and not cmd.title:
        cmd.kind = "album" if cmd.album else "artist"
    events.log("llm", f"Understood: {cmd.describe()}")
    return cmd
