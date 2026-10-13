"""Shared helpers for the networked publishers (YouTube, Instagram).

Every publisher returns the same schema-valid ``publish_log`` shape that
``export_bundle`` produces, so the publish stage can persist whichever tool ran
without reshaping its output.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def format_chapter_time(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def chapter_lines(chapters: list[dict[str, Any]]) -> list[str]:
    """Render chapters as ``m:ss - Title`` lines (accepts both chapter shapes)."""
    lines: list[str] = []
    for ch in chapters:
        label = ch.get("title") or ch.get("label") or ""
        if "start_seconds" in ch or "time_seconds" in ch:
            ts = format_chapter_time(ch.get("start_seconds", ch.get("time_seconds", 0)))
        elif "time" in ch:
            ts = str(ch["time"])
        else:
            ts = "0:00"
        lines.append(f"{ts} - {label}".rstrip(" -"))
    return lines


def normalize_hashtags(hashtags: list[str]) -> list[str]:
    """Return hashtags with a single leading ``#`` and no inner whitespace."""
    out: list[str] = []
    for tag in hashtags:
        tag = "".join(str(tag).split()).lstrip("#")
        if tag:
            out.append(f"#{tag}")
    return out


def build_publish_log(
    *,
    platform: str,
    status: str,
    title: str = "",
    description: str = "",
    hashtags: Optional[list[str]] = None,
    chapters: Optional[list[dict[str, Any]]] = None,
    url: Optional[str] = None,
    video_id: Optional[str] = None,
    visibility: Optional[str] = None,
    error: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> dict[str, Any]:
    """Build and validate a single-entry ``publish_log``."""
    entry: dict[str, Any] = {
        "platform": platform,
        "status": status,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "metadata_used": {
            "title": title,
            "description": description,
            "hashtags": hashtags or [],
            "chapters": chapters or [],
        },
    }
    if url:
        entry["url"] = url
    if video_id:
        entry["video_id"] = video_id
    if visibility:
        entry["visibility"] = visibility
    if error:
        entry["error"] = error

    publish_log = {"version": "1.0", "entries": [entry]}

    from schemas.artifacts import validate_artifact

    validate_artifact("publish_log", publish_log)
    return publish_log
