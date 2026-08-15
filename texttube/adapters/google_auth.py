"""Explicit Google device authorization, token validation, and storage adapter."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REQUEST_TIMEOUT_SECONDS = 30
SLOW_DOWN_INCREMENT_SECONDS = 5


class AuthorizationError(Exception):
    """Authorization failure safe to show to the operator."""


class DeviceAuthorizationExpired(AuthorizationError):
    """Google expired one device code before the operator approved it."""

    def __init__(self, expires_in: int):
        self.expires_in = expires_in
        super().__init__(
            f"Google OAuth device code expired after {expires_in} seconds"
        )


def post_form(url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """Send one form request and return its status and JSON object."""
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise AuthorizationError("Missing Python dependency: requests") from exc
    try:
        response = requests.post(url, data=data, timeout=REQUEST_TIMEOUT_SECONDS)
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
        {"client_id": client_id, "scope": YOUTUBE_READONLY_SCOPE},
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
    stop_requested: threading.Event,
) -> str | None:
    """Poll at Google's interval until approval, shutdown, or expiration."""
    interval = int(authorization["interval"])
    deadline = time.monotonic() + int(authorization["expires_in"])
    device_code = str(authorization["device_code"])
    while time.monotonic() < deadline:
        remaining_seconds = deadline - time.monotonic()
        if stop_requested.wait(min(interval, max(remaining_seconds, 0))):
            return None
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
    raise DeviceAuthorizationExpired(int(authorization["expires_in"]))


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


def authorize(
    client_id: str,
    client_secret: str,
    token_path: Path,
    stop_requested: threading.Event,
    present_instructions: Callable[[str, str, int], None],
) -> Path | None:
    """Complete device authorization and persist its refresh token."""
    authorization = request_device_authorization(client_id)
    present_instructions(
        str(authorization["verification_url"]),
        str(authorization["user_code"]),
        int(authorization["expires_in"]),
    )
    print("Waiting for approval...", file=sys.stderr, flush=True)
    refresh_token = poll_for_refresh_token(
        client_id,
        client_secret,
        authorization,
        stop_requested,
    )
    if refresh_token is None:
        return None
    store_refresh_token(token_path, refresh_token)
    return token_path
