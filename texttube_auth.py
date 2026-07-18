"""Authorize TextTube through Google's limited-input device OAuth flow.

This file owns the one-shot container flow that shows a verification code,
polls for approval, and stores the refresh token in the managed data volume.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REQUEST_TIMEOUT_SECONDS = 30
SLOW_DOWN_INCREMENT_SECONDS = 5
GOOGLE_OAUTH_REFRESH_TOKEN_FILE = "google_oauth_refresh_token"


class AuthorizationError(Exception):
    """Represents a device authorization failure safe to show to the operator."""


def require_environment(name: str) -> str:
    """Return one required environment variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise AuthorizationError(f"Missing required configuration: {name}")
    return value


def refresh_token_path() -> Path:
    """Resolve the managed-volume path used by the application."""
    texttube_home = Path(os.environ.get("TEXTTUBE_HOME", "/data")).expanduser()
    return texttube_home / "var" / "state" / GOOGLE_OAUTH_REFRESH_TOKEN_FILE


def post_form(url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Send one form request and return its status and JSON object."""
    try:
        response = requests.post(
            url,
            data=data,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise AuthorizationError(f"Google OAuth request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise AuthorizationError(
            f"Google OAuth returned invalid JSON with HTTP {response.status_code}"
        ) from exc
    finally:
        response.close()

    if not isinstance(payload, dict):
        raise AuthorizationError("Google OAuth returned unexpected JSON")
    return response.status_code, payload


def response_error(payload: dict[str, Any], status_code: int) -> str:
    """Build an operator-safe OAuth error without exposing credentials."""
    error = str(payload.get("error", "")).strip() or f"HTTP {status_code}"
    description = str(payload.get("error_description", "")).strip()
    if error == "invalid_client":
        return (
            "invalid_client: create Google OAuth credentials with application type "
            "'TVs and Limited Input devices'"
        )
    return f"{error}: {description}" if description else error


def request_device_authorization(client_id: str) -> dict[str, Any]:
    """Request the verification URL, user code, and polling parameters."""
    status_code, payload = post_form(
        DEVICE_CODE_URL,
        {
            "client_id": client_id,
            "scope": YOUTUBE_READONLY_SCOPE,
        },
    )
    if status_code != 200:
        raise AuthorizationError(response_error(payload, status_code))

    required_values = {
        "device_code": str(payload.get("device_code", "")).strip(),
        "user_code": str(payload.get("user_code", "")).strip(),
        "verification_url": str(
            payload.get("verification_url")
            or payload.get("verification_uri")
            or ""
        ).strip(),
    }
    missing = [key for key, value in required_values.items() if not value]
    if missing:
        raise AuthorizationError(
            "Google OAuth device response omitted: " + ", ".join(missing)
        )

    try:
        expires_in = int(payload.get("expires_in", 0))
        interval = int(payload.get("interval", 5))
    except (TypeError, ValueError) as exc:
        raise AuthorizationError(
            "Google OAuth returned invalid polling parameters"
        ) from exc
    if expires_in <= 0 or interval <= 0:
        raise AuthorizationError("Google OAuth returned invalid polling parameters")

    return {
        **required_values,
        "expires_in": expires_in,
        "interval": interval,
    }


def poll_for_refresh_token(
    client_id: str,
    client_secret: str,
    authorization: dict[str, Any],
) -> str:
    """Poll at Google's requested interval until approval or expiration."""
    interval = int(authorization["interval"])
    deadline = time.monotonic() + int(authorization["expires_in"])
    device_code = str(authorization["device_code"])

    while time.monotonic() < deadline:
        remaining_seconds = deadline - time.monotonic()
        time.sleep(min(interval, max(remaining_seconds, 0)))
        if time.monotonic() >= deadline:
            break
        status_code, payload = post_form(
            TOKEN_URL,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device_code,
                "grant_type": DEVICE_GRANT_TYPE,
            },
        )
        if status_code == 200:
            refresh_token = str(payload.get("refresh_token", "")).strip()
            if not refresh_token:
                raise AuthorizationError(
                    "Google OAuth approval did not return a refresh token"
                )
            return refresh_token

        error = str(payload.get("error", "")).strip()
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_INCREMENT_SECONDS
            continue
        raise AuthorizationError(response_error(payload, status_code))

    raise AuthorizationError("Google OAuth device code expired before approval")


def store_refresh_token(path: Path, refresh_token: str) -> None:
    """Atomically store the refresh token with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".google-oauth-refresh-token.",
        dir=str(path.parent),
        text=True,
    )
    try:
        os.fchmod(file_descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(f"{refresh_token}\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def authorize() -> Path:
    """Complete device authorization and persist its refresh token."""
    client_id = require_environment("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = require_environment("GOOGLE_OAUTH_CLIENT_SECRET")
    authorization = request_device_authorization(client_id)

    print("", file=sys.stderr)
    print("Authorize TextTube with Google:", file=sys.stderr)
    print(f"  Open: {authorization['verification_url']}", file=sys.stderr)
    print(f"  Enter code: {authorization['user_code']}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Waiting for approval...", file=sys.stderr, flush=True)

    refresh_token = poll_for_refresh_token(
        client_id,
        client_secret,
        authorization,
    )
    destination = refresh_token_path()
    store_refresh_token(destination, refresh_token)
    return destination


def main() -> int:
    """Run one interactive device authorization session."""
    try:
        destination = authorize()
    except KeyboardInterrupt:
        print("Google OAuth authorization cancelled.", file=sys.stderr)
        return 130
    except (AuthorizationError, OSError) as exc:
        print(f"Google OAuth authorization failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"Google OAuth authorization stored securely at {destination}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
