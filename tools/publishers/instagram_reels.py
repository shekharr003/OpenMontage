"""Instagram Reels publisher — a networked ``publish`` provider.

Publishes a finished render as a Reel through the Instagram Graph API content
publishing flow: create a REELS media container, upload the file (resumable
upload to ``rupload.facebook.com``, or let Instagram fetch a public
``video_url``), wait for processing, then ``media_publish``.

Requirements: an Instagram Business or Creator account linked to a Facebook
Page, a Meta app with ``instagram_content_publish`` (plus ``instagram_basic``
and ``pages_read_engagement``), and a long-lived access token for it.

Safety: Instagram has no private or unlisted state, so anything published is
public immediately. Nothing leaves the machine unless ``confirm_publish`` is
true; without it the tool returns a ``draft`` publish_log with the exact caption
for the user to approve. The Graph API cannot schedule Reels; schedule by
running ``scripts/publish_video.py`` from cron or a task scheduler.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

import requests

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.publishers._common import build_publish_log, normalize_hashtags

DEFAULT_GRAPH_HOST = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"
RUPLOAD_HOST = "https://rupload.facebook.com"
REQUEST_TIMEOUT = 120

MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30

ENV_VARS = ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID")


class InstagramReels(BaseTool):
    name = "instagram_reels"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "instagram"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = [f"env:{v}" for v in ENV_VARS]
    install_instructions = (
        "1. Convert the Instagram account to Business or Creator and link it to a Facebook Page.\n"
        "2. Create a Meta app (developers.facebook.com) with the Instagram Graph API and\n"
        "   permissions instagram_basic, instagram_content_publish, pages_read_engagement.\n"
        "3. Generate a long-lived user access token and find the Instagram user id\n"
        "   (GET /me/accounts -> page -> ?fields=instagram_business_account).\n"
        "4. Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID in .env."
    )
    fallback = "export_bundle"
    fallback_tools = ["export_bundle"]
    agent_skills = []

    capabilities = ["upload_video", "publish_reel", "write_publish_log"]
    supports = {
        "uploads": True,
        "scheduling": False,
        "private_visibility": False,
        "free": True,
        "local_offline": False,
    }
    best_for = [
        "publishing a vertical (9:16) render as an Instagram Reel",
    ]
    not_good_for = [
        "posting without the user's explicit approval (requires confirm_publish)",
        "private or unlisted posts (Instagram posts are always public)",
        "scheduled posts (no scheduling in the Graph API; use cron with scripts/publish_video.py)",
        "videos shorter than 3 seconds or longer than 15 minutes",
    ]

    input_schema = {
        "type": "object",
        "properties": {
            "video_path": {"type": "string", "description": "Final vertical render (MP4/MOV, H.264, AAC)."},
            "video_url": {
                "type": "string",
                "description": "Public HTTPS URL Instagram can fetch, instead of uploading video_path.",
            },
            "caption": {
                "type": "string",
                "description": "Full caption. Built from title + description + hashtags when omitted.",
            },
            "title": {"type": "string"},
            "description": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "share_to_feed": {"type": "boolean", "default": True},
            "cover_url": {"type": "string", "description": "Public URL of a cover image."},
            "thumb_offset_ms": {"type": "integer", "description": "Frame to use as cover when no cover_url."},
            "collaborators": {"type": "array", "items": {"type": "string"}, "description": "Up to 3 usernames."},
            "poll_interval_seconds": {"type": "number", "default": 5},
            "poll_timeout_seconds": {"type": "number", "default": 600},
            "confirm_publish": {
                "type": "boolean",
                "default": False,
                "description": "Must be true to actually post. Only set after the user approves.",
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "publish_log": {"type": "object"},
            "media_id": {"type": "string"},
            "url": {"type": "string"},
            "request": {"type": "object"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=0, network_required=True
    )
    side_effects = ["publishes a public Reel on the authenticated Instagram account"]
    user_visible_verification = [
        "Open the returned Instagram permalink and check the Reel, caption and cover",
    ]

    def get_status(self) -> ToolStatus:
        if all(os.environ.get(v) for v in ENV_VARS):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    # ---- Request building ----

    @staticmethod
    def build_caption(inputs: dict[str, Any]) -> str:
        if inputs.get("caption"):
            return inputs["caption"].strip()
        parts = [p.strip() for p in (inputs.get("title"), inputs.get("description")) if p]
        hashtags = normalize_hashtags(inputs.get("hashtags") or [])
        if hashtags:
            parts.append(" ".join(hashtags))
        return "\n\n".join(parts)

    def build_request(self, inputs: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "media_type": "REELS",
            "caption": self.build_caption(inputs),
            "share_to_feed": "true" if inputs.get("share_to_feed", True) else "false",
        }
        if inputs.get("video_url"):
            params["video_url"] = inputs["video_url"]
        else:
            params["upload_type"] = "resumable"
        if inputs.get("cover_url"):
            params["cover_url"] = inputs["cover_url"]
        elif inputs.get("thumb_offset_ms") is not None:
            params["thumb_offset"] = str(int(inputs["thumb_offset_ms"]))
        if inputs.get("collaborators"):
            params["collaborators"] = ",".join(inputs["collaborators"])
        return params

    @staticmethod
    def validate(inputs: dict[str, Any], params: dict[str, Any]) -> Optional[str]:
        if not inputs.get("video_path") and not inputs.get("video_url"):
            return "provide video_path or video_url"
        if inputs.get("video_path") and not inputs.get("video_url"):
            video = Path(inputs["video_path"]).expanduser()
            if not video.is_file():
                return f"video_path not found: {video}"
        caption = params["caption"]
        if len(caption) > MAX_CAPTION_CHARS:
            return f"caption is {len(caption)} chars; Instagram allows {MAX_CAPTION_CHARS}"
        n_tags = sum(1 for word in caption.split() if word.startswith("#"))
        if n_tags > MAX_HASHTAGS:
            return f"caption has {n_tags} hashtags; Instagram allows {MAX_HASHTAGS}"
        if len(inputs.get("collaborators") or []) > 3:
            return "Instagram allows at most 3 collaborators"
        return None

    # ---- Network ----

    @staticmethod
    def _graph_base() -> str:
        host = os.environ.get("INSTAGRAM_GRAPH_HOST") or DEFAULT_GRAPH_HOST
        version = os.environ.get("INSTAGRAM_GRAPH_API_VERSION") or DEFAULT_API_VERSION
        return f"{host.rstrip('/')}/{version}"

    @staticmethod
    def _check(resp: requests.Response, step: str) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if resp.status_code != 200 or "error" in payload:
            detail = payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else None
            raise RuntimeError(f"Instagram {step} failed ({resp.status_code}): {detail or resp.text[:500]}")
        return payload

    def _create_container(self, token: str, user_id: str, params: dict[str, Any]) -> str:
        resp = requests.post(
            f"{self._graph_base()}/{user_id}/media",
            data={**params, "access_token": token},
            timeout=REQUEST_TIMEOUT,
        )
        return self._check(resp, "container creation")["id"]

    def _upload_file(self, token: str, container_id: str, video: Path) -> None:
        version = os.environ.get("INSTAGRAM_GRAPH_API_VERSION") or DEFAULT_API_VERSION
        with open(video, "rb") as fh:
            resp = requests.post(
                f"{RUPLOAD_HOST}/ig-api-upload/{version}/{container_id}",
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(video.stat().st_size),
                },
                data=fh,
                timeout=REQUEST_TIMEOUT * 5,
            )
        payload = self._check(resp, "video upload")
        if payload.get("success") is False:
            raise RuntimeError(f"Instagram video upload failed: {payload}")

    def _wait_until_ready(self, token: str, container_id: str, interval: float, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            resp = requests.get(
                f"{self._graph_base()}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
                timeout=REQUEST_TIMEOUT,
            )
            payload = self._check(resp, "status check")
            code = payload.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram rejected the video ({code}): {payload.get('status', '')}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Instagram still processing after {timeout:.0f}s (last status {code})")
            time.sleep(interval)

    def _publish(self, token: str, user_id: str, container_id: str) -> str:
        resp = requests.post(
            f"{self._graph_base()}/{user_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=REQUEST_TIMEOUT,
        )
        return self._check(resp, "publish")["id"]

    def _permalink(self, token: str, media_id: str) -> Optional[str]:
        resp = requests.get(
            f"{self._graph_base()}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=REQUEST_TIMEOUT,
        )
        try:
            return self._check(resp, "permalink lookup").get("permalink")
        except RuntimeError:
            return None

    # ---- Execution ----

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        params = self.build_request(inputs)
        problem = self.validate(inputs, params)
        if problem:
            return ToolResult(success=False, error=problem)

        log_kwargs = dict(
            platform="instagram",
            title=inputs.get("title", ""),
            description=params["caption"],
            hashtags=normalize_hashtags(inputs.get("hashtags") or []),
            visibility="public",
        )

        if not inputs.get("confirm_publish"):
            return ToolResult(
                success=True,
                data={
                    "publish_log": build_publish_log(status="draft", **log_kwargs),
                    "request": params,
                    "message": "Not posted: Instagram Reels are public immediately. "
                    "Set confirm_publish=true once the user approves.",
                },
            )

        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error="Instagram credentials missing. " + self.install_instructions,
            )

        token = os.environ["INSTAGRAM_ACCESS_TOKEN"]
        user_id = os.environ["INSTAGRAM_USER_ID"]
        try:
            container_id = self._create_container(token, user_id, params)
            if not inputs.get("video_url"):
                self._upload_file(token, container_id, Path(inputs["video_path"]).expanduser())
            self._wait_until_ready(
                token,
                container_id,
                float(inputs.get("poll_interval_seconds", 5)),
                float(inputs.get("poll_timeout_seconds", 600)),
            )
            media_id = self._publish(token, user_id, container_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent verbatim
            return ToolResult(
                success=False,
                error=str(exc),
                data={"publish_log": build_publish_log(status="failed", error=str(exc), **log_kwargs)},
            )

        url = self._permalink(token, media_id)
        return ToolResult(
            success=True,
            data={
                "publish_log": build_publish_log(
                    status="published", url=url, video_id=media_id, **log_kwargs
                ),
                "media_id": media_id,
                "url": url,
            },
        )
