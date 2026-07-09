"""Checkout OAuth helper for renewing TextTube's YouTube refresh token.

This file owns the interactive Google OAuth consent flow used by TextTube auth
commands. It updates only `GOOGLE_OAUTH_REFRESH_TOKEN` in the selected
`.secrets` file and does not print token values.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class OAuthCallbackServer(HTTPServer):
    """Local HTTP server that stores the Google OAuth callback outcome."""

    auth_code = ""
    auth_error = ""


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Receives the local OAuth redirect and records the authorization code."""

    server: OAuthCallbackServer
    server_version = "TextTubeOAuth/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [""])[0]
        self.server.auth_error = params.get("error", [""])[0]
        message = "TextTube authorization received. You can close this tab."
        if self.server.auth_error:
            message = (
                "TextTube authorization failed: "
                f"{html.escape(self.server.auth_error)}"
            )
        body = f"<!doctype html><title>TextTube Auth</title><p>{message}</p>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def update_secret(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    updated = False
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        prefix = "export " if stripped.startswith("export ") else ""
        assignment = stripped[len(prefix) :] if prefix else stripped
        if assignment.startswith(f"{key}="):
            result.append(f"{prefix}{key}={value}")
            updated = True
        else:
            result.append(line)
    if not updated:
        result.append(f"{key}={value}")

    fd, temp_name = tempfile.mkstemp(
        prefix=".secrets.",
        dir=str(path.parent),
        text=True,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        temp_file.write("\n".join(result) + "\n")
    os.chmod(temp_name, path.stat().st_mode & 0o777)
    os.replace(temp_name, path)


def build_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).decode().rstrip("=")[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def build_auth_url(client_id: str, challenge: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def wait_for_auth_code(auth_url: str) -> str | None:
    try:
        server = OAuthCallbackServer((REDIRECT_HOST, REDIRECT_PORT), OAuthCallbackHandler)
    except OSError as exc:
        print(
            f"Cannot listen on {REDIRECT_HOST}:{REDIRECT_PORT} for OAuth callback: {exc}",
            file=sys.stderr,
        )
        return None

    print("Opening browser for Google OAuth consent.", file=sys.stderr)
    print(f"Waiting for callback on {REDIRECT_URI} ...", file=sys.stderr)
    try:
        subprocess.run(["open", auth_url], check=False)
    except OSError:
        print("Open this URL in a browser:", file=sys.stderr)
        print(auth_url, file=sys.stderr)

    while not server.auth_code and not server.auth_error:
        server.handle_request()

    server.server_close()
    if server.auth_error:
        print(f"Google OAuth authorization failed: {server.auth_error}", file=sys.stderr)
        return None
    return server.auth_code


def exchange_auth_code(
    client_id: str,
    client_secret: str,
    auth_code: str,
    verifier: str,
) -> str | None:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
    ).encode()
    try:
        with urllib.request.urlopen(TOKEN_URL, data=payload, timeout=30) as response:
            token_data = json.loads(response.read().decode())
    except Exception as exc:
        print(f"Google OAuth token exchange failed: {exc}", file=sys.stderr)
        return None

    refresh_token = str(token_data.get("refresh_token", "")).strip()
    if not refresh_token:
        print(
            "Google OAuth response did not include a refresh token; rerun auth and approve consent.",
            file=sys.stderr,
        )
        return None
    return refresh_token


def main() -> int:
    configured_secrets_path = os.environ.get("TEXTTUBE_SECRETS_PATH", "").strip()
    if configured_secrets_path:
        secrets_path = Path(configured_secrets_path).expanduser()
    else:
        repo_root = Path(os.environ.get("TEXTTUBE_REPO_ROOT", Path.cwd()))
        secrets_path = repo_root / ".secrets"
    if not secrets_path.exists():
        print(f"Missing {secrets_path}; create it before running auth.", file=sys.stderr)
        return 1

    values = read_dotenv(secrets_path)
    client_id = values.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = values.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print(
            "Missing GOOGLE_OAUTH_CLIENT_ID or GOOGLE_OAUTH_CLIENT_SECRET in .secrets.",
            file=sys.stderr,
        )
        return 1

    verifier, challenge = build_pkce_pair()
    auth_code = wait_for_auth_code(build_auth_url(client_id, challenge))
    if not auth_code:
        return 1
    refresh_token = exchange_auth_code(client_id, client_secret, auth_code, verifier)
    if not refresh_token:
        return 1

    update_secret(secrets_path, "GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token)
    print("Updated GOOGLE_OAUTH_REFRESH_TOKEN in .secrets.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
