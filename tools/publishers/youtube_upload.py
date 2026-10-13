"""YouTube uploader — a networked ``publish`` provider.

Uploads a finished render to the creator's own YouTube channel through the
YouTube Data API v3 resumable upload protocol, with title, description
(chapters appended), tags, category, visibility, optional scheduled release
(``publish_at``) and an optional custom thumbnail.

Auth is a user OAuth refresh token (service accounts cannot own a channel).
Run ``python scripts/youtube_auth.py`` once to mint ``YOUTUBE_REFRESH_TOKEN``.

Safety: nothing leaves the machine unless ``confirm_publish`` is true. Without
it the tool returns a ``draft`` publish_log describing exactly what would be
uploaded, so the agent can show it to the user for approval first. Uploads
default to ``private``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import requests

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.publishers._common import build_publish_log, chapter_lines, normalize_hashtags

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"

# Chunk size must be a multiple of 256 KiB for resumable uploads.
CHUNK_SIZE = 8 * 1024 * 1024
REQUEST_TIMEOUT = 120

ENV_VARS = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")


class YouTubeUpload(BaseTool):
    name = "youtube_upload"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "youtube"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = [f"env:{v}" for v in ENV_VARS]
    install_instructions = (
        "1. In Google Cloud Console, enable 'YouTube Data API v3' and create an\n"
        "   OAuth client ID of type 'Desktop app'.\n"
        "2. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env.\n"
        "3. Run: python scripts/youtube_auth.py  (sign in with the channel owner)\n"
        "   and paste the printed YOUTUBE_REFRESH_TOKEN into .env."
    )
    fallback = "export_bundle"
    fallback_tools = ["export_bundle"]
    agent_skills = []

    capabilities = ["upload_video", "schedule_release", "set_thumbnail", "write_publish_log"]
    supports = {
        "uploads": True,
        "scheduling": True,
        "thumbnails": True,
        "free": True,
        "local_offline": False,
    }
    best_for = [
        "uploading a finished render straight to the creator's YouTube channel",
        "scheduling a YouTube release (publish_at)",
        "YouTube Shorts (upload a vertical video under 3 minutes)",
    ]
    not_good_for = [
        "posting without the user's explicit approval (requires confirm_publish)",
        "channels the OAuth token does not own",
    ]

    input_schema = {
        "type": "object",
        "required": ["video_path", "title"],
        "properties": {
            "video_path": {"type": "string", "description": "Final rendered video file."},
            "title": {"type": "string", "description": "Video title (max 100 chars)."},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "hashtags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Appended to the description; YouTube shows the first three above the title.",
            },
            "chapters": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Either {start_seconds, title} or {time, label}; appended to the description.",
            },
            "visibility": {
                "type": "string",
                "enum": ["public", "private", "unlisted"],
                "default": "private",
            },
            "publish_at": {
                "type": "string",
                "description": "ISO-8601 time to go public. Forces the upload to private until then.",
            },
            "category_id": {
                "type": "string",
                "default": "22",
                "description": "YouTube category id (22 People & Blogs, 27 Education, 28 Science & Tech).",
            },
            "made_for_kids": {"type": "boolean", "default": False},
            "default_language": {"type": "string", "description": "BCP-47 code, e.g. 'en'."},
            "thumbnail_path": {"type": "string", "description": "JPEG/PNG under 2 MB."},
            "notify_subscribers": {"type": "boolean", "default": True},
            "confirm_publish": {
                "type": "boolean",
                "default": False,
                "description": "Must be true to actually upload. Only set after the user approves.",
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "publish_log": {"type": "object"},
            "video_id": {"type": "string"},
            "url": {"type": "string"},
            "request": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=0, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=0)
    side_effects = ["uploads a video to the authenticated YouTube channel"]
    user_visible_verification = [
        "Open the returned YouTube Studio link and check title, description, visibility and schedule",
    ]

    def get_status(self) -> ToolStatus:
        if all(os.environ.get(v) for v in ENV_VARS):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    # ---- Request building ----

    @staticmethod
    def build_description(inputs: dict[str, Any]) -> str:
        parts: list[str] = []
        if inputs.get("description"):
            parts.append(inputs["description"].strip())
        lines = chapter_lines(inputs.get("chapters") or [])
        if lines:
            parts.append("\n".join(lines))
        hashtags = normalize_hashtags(inputs.get("hashtags") or [])
        if hashtags:
            parts.append(" ".join(hashtags))
        return "\n\n".join(parts)

    def build_request(self, inputs: dict[str, Any]) -> dict[str, Any]:
        snippet: dict[str, Any] = {
            "title": inputs["title"],
            "description": self.build_description(inputs),
            "categoryId": str(inputs.get("category_id") or "22"),
        }
        if inputs.get("tags"):
            snippet["tags"] = list(inputs["tags"])
        if inputs.get("default_language"):
            snippet["defaultLanguage"] = inputs["default_language"]
            snippet["defaultAudioLanguage"] = inputs["default_language"]

        status: dict[str, Any] = {
            "privacyStatus": inputs.get("visibility") or "private",
            "selfDeclaredMadeForKids": bool(inputs.get("made_for_kids", False)),
        }
        if inputs.get("publish_at"):
            # YouTube only honours publishAt on private videos.
            status["privacyStatus"] = "private"
            status["publishAt"] = inputs["publish_at"]
        return {"snippet": snippet, "status": status}

    @staticmethod
    def validate(inputs: dict[str, Any], body: dict[str, Any]) -> Optional[str]:
        title = body["snippet"]["title"]
        desc = body["snippet"]["description"]
        if not title.strip():
            return "title is empty"
        if len(title) > 100:
            return f"title is {len(title)} chars; YouTube allows 100"
        if len(desc.encode("utf-8")) > 5000:
            return "description exceeds YouTube's 5000-byte limit"
        for field_name, text in (("title", title), ("description", desc)):
            if "<" in text or ">" in text:
                return f"{field_name} contains '<' or '>', which YouTube rejects"
        tags = body["snippet"].get("tags") or []
        # YouTube counts quoted length for tags containing spaces, plus separators.
        tag_chars = sum(len(t) + (2 if " " in t else 0) for t in tags) + max(len(tags) - 1, 0)
        if tag_chars > 500:
            return "tags exceed YouTube's 500-character total"
        thumb = inputs.get("thumbnail_path")
        if thumb and not Path(thumb).expanduser().is_file():
            return f"thumbnail_path provided but not found: {thumb}"
        return None

    # ---- Network ----

    @staticmethod
    def _access_token() -> str:
        resp = requests.post(
            TOKEN_URL,
            data={
                "client_id": os.environ["YOUTUBE_CLIENT_ID"],
                "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
                "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"],
                "grant_type": "refresh_token",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"OAuth token refresh failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()["access_token"]

    def _upload(self, token: str, video: Path, body: dict[str, Any], notify: bool) -> dict[str, Any]:
        size = video.stat().st_size
        mime = "video/quicktime" if video.suffix.lower() == ".mov" else "video/mp4"
        init = requests.post(
            UPLOAD_URL,
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": "true" if notify else "false",
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime,
                "X-Upload-Content-Length": str(size),
            },
            data=json.dumps(body),
            timeout=REQUEST_TIMEOUT,
        )
        if init.status_code != 200 or not init.headers.get("Location"):
            raise RuntimeError(f"Upload session failed ({init.status_code}): {init.text[:500]}")
        session_url = init.headers["Location"]

        offset = 0
        with open(video, "rb") as fh:
            while True:
                fh.seek(offset)
                chunk = fh.read(CHUNK_SIZE)
                end = offset + len(chunk) - 1
                resp = requests.put(
                    session_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    data=chunk,
                    timeout=REQUEST_TIMEOUT * 5,
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code == 308:
                    rng = resp.headers.get("Range")
                    offset = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
                    continue
                raise RuntimeError(f"Upload failed ({resp.status_code}): {resp.text[:500]}")

    @staticmethod
    def _set_thumbnail(token: str, video_id: str, thumb: Path) -> Optional[str]:
        mime = "image/png" if thumb.suffix.lower() == ".png" else "image/jpeg"
        resp = requests.post(
            THUMBNAIL_URL,
            params={"videoId": video_id},
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
            data=thumb.read_bytes(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            # Custom thumbnails need a phone-verified channel; the video is
            # already up, so report rather than fail.
            return f"thumbnail not set ({resp.status_code}): {resp.text[:300]}"
        return None

    # ---- Execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        video = Path(inputs["video_path"]).expanduser()
        if not video.is_file():
            return ToolResult(success=False, error=f"video_path not found: {video}")

        body = self.build_request(inputs)
        problem = self.validate(inputs, body)
        if problem:
            return ToolResult(success=False, error=problem)

        log_kwargs = dict(
            platform="youtube",
            title=body["snippet"]["title"],
            description=body["snippet"]["description"],
            hashtags=normalize_hashtags(inputs.get("hashtags") or []),
            chapters=inputs.get("chapters") or [],
            visibility=body["status"]["privacyStatus"],
        )

        if not inputs.get("confirm_publish"):
            return ToolResult(
                success=True,
                data={
                    "publish_log": build_publish_log(status="draft", **log_kwargs),
                    "request": body,
                    "message": "Not uploaded: set confirm_publish=true once the user approves.",
                },
            )

        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error="YouTube credentials missing. " + self.install_instructions,
            )

        try:
            token = self._access_token()
            result = self._upload(token, video, body, inputs.get("notify_subscribers", True))
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent verbatim
            return ToolResult(
                success=False,
                error=str(exc),
                data={"publish_log": build_publish_log(status="failed", error=str(exc), **log_kwargs)},
            )

        video_id = result["id"]
        url = f"https://youtu.be/{video_id}"
        warnings: list[str] = []
        thumb = inputs.get("thumbnail_path")
        if thumb:
            warning = self._set_thumbnail(token, video_id, Path(thumb).expanduser())
            if warning:
                warnings.append(warning)

        return ToolResult(
            success=True,
            data={
                "publish_log": build_publish_log(
                    status="published", url=url, video_id=video_id, **log_kwargs
                ),
                "video_id": video_id,
                "url": url,
                "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
                "publish_at": body["status"].get("publishAt"),
                "warnings": warnings,
            },
        )
