"""Deciding which file is the song you asked for.

Plain code, not the LLM: it's fast, the same every time, and easy to tune. Every candidate
gets a score with the reasons, which the UI shows so you can see why a file was picked.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from rapidfuzz import fuzz

# Words that mean "a different version" unless you asked for them.
VARIANT_WORDS = [
    "remix", "live", "instrumental", "karaoke", "cover", "acoustic", "edit", "mix",
    "remaster", "demo", "8bit", "8-bit", "nightcore", "slowed", "reverb", "sped up", "tribute",
]
LOSSLESS = {"flac", "wav", "alac", "aiff", "ape"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[_\-.]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def song_score(want_artist: str, want_title: str, artist: str, title: str) -> float:
    """0-100: how well a library song matches what was asked for."""
    t = fuzz.ratio(norm(want_title), norm(title))
    # "Dreams" vs "Dreams (2004 Remaster)": partial match on the title is fine.
    t = max(t, fuzz.partial_ratio(norm(want_title), norm(title)) - 5)
    if not want_artist:
        return t
    a = fuzz.token_set_ratio(norm(want_artist), norm(artist))
    return 0.65 * t + 0.35 * a


def search_text(artist: str, title: str) -> str:
    """What to type into Soulseek: short and plain works best there."""
    s = norm(f"{artist} {title}")
    # Soulseek treats some words as noise; drop very short tokens except numbers.
    words = [w for w in s.split() if len(w) > 1 or w.isdigit()]
    return " ".join(words)


def score_file(f: dict[str, Any], resp: dict[str, Any], artist: str, title: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Scores one Soulseek search result file. None = not usable at all."""
    filename: str = f.get("filename", "")
    ext = (f.get("extension") or filename.rsplit(".", 1)[-1]).lower().lstrip(".")
    if ext not in cfg["formats"]:
        return None
    if f.get("isLocked"):
        return None
    size_mb = (f.get("size") or 0) / 1_048_576
    if size_mb <= 0.3 or size_mb > cfg["max_file_mb"]:
        return None
    bitrate = f.get("bitRate") or 0
    lossless = ext in LOSSLESS
    if not lossless and bitrate and bitrate < cfg["min_bitrate"]:
        return None

    parts = re.split(r"[\\/]", filename)
    base = parts[-1].rsplit(".", 1)[0]
    folder = parts[-2] if len(parts) > 1 else ""
    nb, nf = norm(base), norm(folder)

    reasons: list[str] = []
    title_s = max(fuzz.partial_ratio(norm(title), nb), fuzz.token_set_ratio(norm(title), nb))
    artist_s = fuzz.token_set_ratio(norm(artist), f"{nb} {nf}") if artist else 100
    match = 0.65 * title_s + 0.35 * artist_s
    if match < 70:
        return None
    score = match
    reasons.append(f"name match {match:.0f}")

    asked = norm(f"{artist} {title}")
    for w in VARIANT_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", nb) and w not in asked:
            score -= 25
            reasons.append(f"'{w}' version −25")
            break

    if lossless:
        bonus = 12 if cfg["prefer_lossless"] else 4
        score += bonus
        reasons.append(f"lossless +{bonus}")
    elif bitrate >= 320:
        score += 8
        reasons.append("320 kbps +8")
    elif bitrate >= 256:
        score += 4
        reasons.append(f"{bitrate} kbps +4")

    if resp.get("hasFreeUploadSlot"):
        score += 10
        reasons.append("free slot +10")
    queue = resp.get("queueLength") or 0
    if queue:
        pen = min(20, queue)
        score -= pen
        reasons.append(f"queue {queue} −{pen}")
    speed = (resp.get("uploadSpeed") or 0) / 1_048_576
    if speed >= 1:
        score += 5
        reasons.append(f"{speed:.1f} MB/s +5")

    length = f.get("length")
    return {
        "username": resp.get("username"),
        "filename": filename,
        "size": f.get("size"),
        "ext": ext,
        "bitrate": bitrate,
        "bit_depth": f.get("bitDepth"),
        "length_s": length,
        "score": round(score, 1),
        "reasons": reasons,
    }


def rank(responses: list[dict[str, Any]], artist: str, title: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for r in responses:
        for f in r.get("files", []):
            s = score_file(f, r, artist, title, cfg)
            if s:
                out.append(s)
    out.sort(key=lambda c: c["score"], reverse=True)
    # One file per user in the top picks, so a retry goes to someone else.
    seen, ranked, rest = set(), [], []
    for c in out:
        (rest if c["username"] in seen else ranked).append(c)
        seen.add(c["username"])
    return ranked + rest
