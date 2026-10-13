"""Mint a YouTube OAuth refresh token for the `youtube_upload` publisher.

One-time setup, run on a machine with a browser:

    python scripts/youtube_auth.py

It reads YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET (from the environment or
.env), opens Google's consent screen, catches the redirect on a local loopback
port, and prints the YOUTUBE_REFRESH_TOKEN line to paste into .env.

The OAuth client must be of type "Desktop app" in Google Cloud Console, with
the YouTube Data API v3 enabled. Sign in with the account that owns the
channel you want to publish to.
"""
from __future__ import annotations

import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Needed for thumbnails.set.
    "https://www.googleapis.com/auth/youtube",
]


def main() -> int:
    from tools.tool_registry import ToolRegistry

    ToolRegistry._load_dotenv()
    client_id = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.", file=sys.stderr)
        return 1

    result: dict[str, str] = {}
    state = secrets.token_urlsafe(16)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OpenMontage: authorization received. You can close this tab.")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    print(f"Opening the Google consent screen. If no browser opens, visit:\n\n{url}\n")
    threading.Thread(target=server.handle_request, daemon=True).start()
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        threading.Event().wait(0.2)
    server.server_close()

    if result.get("error"):
        print(f"Authorization failed: {result['error']}", file=sys.stderr)
        return 1
    if result.get("state") != state:
        print("Authorization failed: state mismatch.", file=sys.stderr)
        return 1

    resp = requests.post(
        TOKEN_URL,
        data={
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=60,
    )
    if resp.status_code != 200 or "refresh_token" not in resp.json():
        print(f"Token exchange failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        return 1

    print("Add this line to .env:\n")
    print(f"YOUTUBE_REFRESH_TOKEN={resp.json()['refresh_token']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
