"""Tests for the networked publishers: youtube_upload and instagram_reels.

All HTTP is replaced by a fake ``requests`` module, so nothing leaves the
machine. Covers the contract, the confirm_publish gate (draft without network),
request building and validation, and the full happy/failed upload flows.
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from schemas.artifacts import validate_artifact
from tools.base_tool import ToolStatus, ToolTier
from tools.publishers import instagram_reels as ig_mod
from tools.publishers import youtube_upload as yt_mod
from tools.publishers.instagram_reels import InstagramReels
from tools.publishers.youtube_upload import YouTubeUpload
from tools.tool_registry import ToolRegistry


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeRequests:
    """Records calls and replies from a queue of (method, url-substring, response)."""

    Response = FakeResponse

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, method, url, **kwargs):
        data = kwargs.get("data")
        if hasattr(data, "read"):
            kwargs["data"] = data.read()
        self.calls.append((method, url, kwargs))
        assert self.script, f"unexpected {method} {url}"
        exp_method, exp_url, resp = self.script.pop(0)
        assert method == exp_method and exp_url in url, f"expected {exp_method} {exp_url}, got {method} {url}"
        return resp

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def put(self, url, **kw):
        return self._next("PUT", url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)


def _video(tmp_path, size=1024):
    path = tmp_path / "final.mp4"
    path.write_bytes(b"\x00" * size)
    return path


@pytest.fixture
def yt_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "refresh")


@pytest.fixture
def ig_env(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "igtoken")
    monkeypatch.setenv("INSTAGRAM_USER_ID", "17841400000000000")


# ---- Contract ----


@pytest.mark.parametrize("cls,name,provider", [
    (YouTubeUpload, "youtube_upload", "youtube"),
    (InstagramReels, "instagram_reels", "instagram"),
])
def test_contract_and_discovery(cls, name, provider, monkeypatch):
    for var in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
                "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    info = cls().get_info()
    assert info["name"] == name
    assert info["capability"] == "publish"
    assert info["tier"] == ToolTier.PUBLISH.value
    assert info["provider"] == provider
    assert info["resource_profile"]["network_required"] is True
    assert cls().get_status() == ToolStatus.UNAVAILABLE

    registry = ToolRegistry()
    registry.discover("tools.publishers")
    assert registry.get(name) is not None


def test_status_available_with_credentials(yt_env, ig_env):
    assert YouTubeUpload().get_status() == ToolStatus.AVAILABLE
    assert InstagramReels().get_status() == ToolStatus.AVAILABLE


# ---- YouTube ----


def test_youtube_draft_without_confirm_makes_no_requests(tmp_path, monkeypatch):
    fake = FakeRequests([])
    monkeypatch.setattr(yt_mod, "requests", fake)
    result = YouTubeUpload().execute({
        "video_path": str(_video(tmp_path)),
        "title": "Vector DBs in 60s",
        "description": "Quick explainer.",
        "hashtags": ["ai", "#vector db"],
        "chapters": [{"start_seconds": 0, "title": "Intro"}, {"start_seconds": 75, "title": "Search"}],
        "publish_at": "2026-11-01T15:00:00Z",
        "visibility": "public",
    })
    assert result.success, result.error
    assert fake.calls == []
    entry = result.data["publish_log"]["entries"][0]
    assert entry["status"] == "draft"
    validate_artifact("publish_log", result.data["publish_log"])
    req = result.data["request"]
    # Scheduling forces private until publishAt.
    assert req["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                             "publishAt": "2026-11-01T15:00:00Z"}
    assert req["snippet"]["description"] == "Quick explainer.\n\n0:00 - Intro\n1:15 - Search\n\n#ai #vectordb"


@pytest.mark.parametrize("override,msg", [
    ({"title": "x" * 101}, "100"),
    ({"title": "a <b>"}, "'<' or '>'"),
    ({"tags": ["t" * 300, "u" * 300]}, "500"),
    ({"thumbnail_path": "/nope.png"}, "thumbnail_path"),
])
def test_youtube_validation(tmp_path, override, msg):
    inputs = {"video_path": str(_video(tmp_path)), "title": "ok", **override}
    result = YouTubeUpload().execute(inputs)
    assert result.success is False
    assert msg in result.error


def test_youtube_missing_video(tmp_path):
    result = YouTubeUpload().execute({"video_path": str(tmp_path / "x.mp4"), "title": "t"})
    assert result.success is False and "not found" in result.error


def test_youtube_confirmed_without_credentials_fails(tmp_path, monkeypatch):
    for var in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    result = YouTubeUpload().execute(
        {"video_path": str(_video(tmp_path)), "title": "t", "confirm_publish": True}
    )
    assert result.success is False and "credentials" in result.error


def test_youtube_resumable_upload_with_resume_and_thumbnail(tmp_path, monkeypatch, yt_env):
    monkeypatch.setattr(yt_mod, "CHUNK_SIZE", 256 * 1024)
    video = _video(tmp_path, size=600 * 1024)
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"\x89PNG")
    session = "https://upload.example/session/abc"
    fake = FakeRequests([
        ("POST", "oauth2.googleapis.com/token", FakeResponse(200, {"access_token": "AT"})),
        ("POST", "/upload/youtube/v3/videos", FakeResponse(200, {}, {"Location": session})),
        ("PUT", session, FakeResponse(308, {}, {"Range": "bytes=0-262143"})),
        # Server only persisted part of the second chunk; client must resume from its Range.
        ("PUT", session, FakeResponse(308, {}, {"Range": "bytes=0-399999"})),
        ("PUT", session, FakeResponse(200, {"id": "VID123"})),
        ("POST", "/thumbnails/set", FakeResponse(403, {"error": "not verified"})),
    ])
    monkeypatch.setattr(yt_mod, "requests", fake)

    result = YouTubeUpload().execute({
        "video_path": str(video),
        "title": "Hello",
        "tags": ["a", "b"],
        "visibility": "unlisted",
        "thumbnail_path": str(thumb),
        "confirm_publish": True,
    })
    assert result.success, result.error
    assert result.data["url"] == "https://youtu.be/VID123"
    entry = result.data["publish_log"]["entries"][0]
    assert entry["status"] == "published"
    assert entry["video_id"] == "VID123"
    assert entry["visibility"] == "unlisted"
    assert result.data["warnings"] and "thumbnail not set" in result.data["warnings"][0]

    init = fake.calls[1][2]
    assert init["headers"]["Authorization"] == "Bearer AT"
    assert json.loads(init["data"])["status"]["privacyStatus"] == "unlisted"
    ranges = [c[2]["headers"]["Content-Range"] for c in fake.calls if c[0] == "PUT"]
    total = 600 * 1024
    assert ranges == [f"bytes 0-262143/{total}", f"bytes 262144-524287/{total}",
                      f"bytes 400000-{total - 1}/{total}"]
    assert all(c[2]["timeout"] for c in fake.calls)


def test_youtube_upload_error_returns_failed_log(tmp_path, monkeypatch, yt_env):
    fake = FakeRequests([
        ("POST", "oauth2.googleapis.com/token", FakeResponse(400, {"error": "invalid_grant"})),
    ])
    monkeypatch.setattr(yt_mod, "requests", fake)
    result = YouTubeUpload().execute(
        {"video_path": str(_video(tmp_path)), "title": "t", "confirm_publish": True}
    )
    assert result.success is False
    assert "invalid_grant" in result.error
    entry = result.data["publish_log"]["entries"][0]
    assert entry["status"] == "failed"
    validate_artifact("publish_log", result.data["publish_log"])


# ---- Instagram ----


def test_instagram_draft_builds_caption_without_network(tmp_path, monkeypatch):
    fake = FakeRequests([])
    monkeypatch.setattr(ig_mod, "requests", fake)
    result = InstagramReels().execute({
        "video_path": str(_video(tmp_path)),
        "title": "Vector DBs in 60s",
        "description": "Quick explainer.",
        "hashtags": ["ai", "#vectordb"],
        "thumb_offset_ms": 1500,
    })
    assert result.success, result.error
    assert fake.calls == []
    assert result.data["request"] == {
        "media_type": "REELS",
        "caption": "Vector DBs in 60s\n\nQuick explainer.\n\n#ai #vectordb",
        "share_to_feed": "true",
        "upload_type": "resumable",
        "thumb_offset": "1500",
    }
    entry = result.data["publish_log"]["entries"][0]
    assert entry["status"] == "draft" and entry["visibility"] == "public"


@pytest.mark.parametrize("override,msg", [
    ({"video_path": None}, "video_path or video_url"),
    ({"caption": "x" * 2201}, "2200"),
    ({"caption": " ".join(f"#t{i}" for i in range(31))}, "31 hashtags"),
    ({"collaborators": ["a", "b", "c", "d"]}, "3 collaborators"),
])
def test_instagram_validation(tmp_path, override, msg):
    inputs = {"video_path": str(_video(tmp_path)), "caption": "hi", **override}
    inputs = {k: v for k, v in inputs.items() if v is not None}
    result = InstagramReels().execute(inputs)
    assert result.success is False
    assert msg in result.error


def test_instagram_resumable_publish_flow(tmp_path, monkeypatch, ig_env):
    monkeypatch.setattr(ig_mod.time, "sleep", lambda s: None)
    video = _video(tmp_path, size=2048)
    fake = FakeRequests([
        ("POST", "/v23.0/17841400000000000/media", FakeResponse(200, {"id": "C1", "uri": "x"})),
        ("POST", "rupload.facebook.com/ig-api-upload/v23.0/C1", FakeResponse(200, {"success": True})),
        ("GET", "/v23.0/C1", FakeResponse(200, {"status_code": "IN_PROGRESS"})),
        ("GET", "/v23.0/C1", FakeResponse(200, {"status_code": "FINISHED"})),
        ("POST", "/17841400000000000/media_publish", FakeResponse(200, {"id": "M9"})),
        ("GET", "/v23.0/M9", FakeResponse(200, {"permalink": "https://www.instagram.com/reel/abc/"})),
    ])
    monkeypatch.setattr(ig_mod, "requests", fake)

    result = InstagramReels().execute({
        "video_path": str(video),
        "caption": "My reel #ai",
        "hashtags": ["ai"],
        "confirm_publish": True,
    })
    assert result.success, result.error
    assert result.data["url"] == "https://www.instagram.com/reel/abc/"
    entry = result.data["publish_log"]["entries"][0]
    assert entry["status"] == "published" and entry["video_id"] == "M9"

    create = fake.calls[0][2]["data"]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable"
    assert create["access_token"] == "igtoken"
    upload = fake.calls[1][2]
    assert upload["headers"] == {"Authorization": "OAuth igtoken", "offset": "0", "file_size": "2048"}
    assert upload["data"] == b"\x00" * 2048
    assert fake.calls[4][2]["data"]["creation_id"] == "C1"


def test_instagram_video_url_skips_upload(tmp_path, monkeypatch, ig_env):
    fake = FakeRequests([
        ("POST", "/media", FakeResponse(200, {"id": "C2"})),
        ("GET", "/C2", FakeResponse(200, {"status_code": "FINISHED"})),
        ("POST", "/media_publish", FakeResponse(200, {"id": "M2"})),
        ("GET", "/M2", FakeResponse(200, {"permalink": "https://www.instagram.com/reel/m2/"})),
    ])
    monkeypatch.setattr(ig_mod, "requests", fake)
    result = InstagramReels().execute({
        "video_url": "https://cdn.example.com/final.mp4",
        "caption": "hi",
        "confirm_publish": True,
    })
    assert result.success, result.error
    assert fake.calls[0][2]["data"]["video_url"] == "https://cdn.example.com/final.mp4"
    assert "upload_type" not in fake.calls[0][2]["data"]


def test_instagram_processing_error_returns_failed_log(tmp_path, monkeypatch, ig_env):
    fake = FakeRequests([
        ("POST", "/media", FakeResponse(200, {"id": "C3"})),
        ("POST", "rupload.facebook.com", FakeResponse(200, {"success": True})),
        ("GET", "/C3", FakeResponse(200, {"status_code": "ERROR", "status": "Unsupported aspect ratio"})),
    ])
    monkeypatch.setattr(ig_mod, "requests", fake)
    result = InstagramReels().execute(
        {"video_path": str(_video(tmp_path)), "caption": "hi", "confirm_publish": True}
    )
    assert result.success is False
    assert "Unsupported aspect ratio" in result.error
    assert result.data["publish_log"]["entries"][0]["status"] == "failed"


def test_instagram_graph_error_message_surfaces(tmp_path, monkeypatch, ig_env):
    fake = FakeRequests([
        ("POST", "/media", FakeResponse(400, {"error": {"message": "Invalid OAuth access token"}})),
    ])
    monkeypatch.setattr(ig_mod, "requests", fake)
    result = InstagramReels().execute(
        {"video_path": str(_video(tmp_path)), "caption": "hi", "confirm_publish": True}
    )
    assert result.success is False
    assert "Invalid OAuth access token" in result.error


# ---- CLI ----


def test_publish_video_cli_preview(tmp_path, monkeypatch, capsys):
    from tools.publishers.export_bundle import ExportBundle
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import publish_video

    video = tmp_path / "projects" / "demo" / "renders" / "final.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"\x00" * 64)
    export = ExportBundle().execute({
        "video_path": str(video), "title": "Demo", "description": "D",
        "hashtags": ["#x"], "export_dir": str(tmp_path / "exports"),
    })
    assert export.success
    fake = FakeRequests([])
    monkeypatch.setattr(yt_mod, "requests", fake)
    monkeypatch.setattr(ig_mod, "requests", fake)

    code = publish_video.main([str(tmp_path / "exports"), "--youtube", "--instagram"])
    assert code == 0
    assert fake.calls == []
    out = capsys.readouterr().out
    assert "[youtube] preview" in out and "[instagram] preview" in out
    log = json.loads((tmp_path / "exports" / "publish_log.instagram.json").read_text())
    assert log["entries"][0]["status"] == "draft"
