"""Google device authorization, token validation, storage, and health adapter."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT_TYPE = "refresh_token"
REQUEST_TIMEOUT_SECONDS = 30
SLOW_DOWN_INCREMENT_SECONDS = 5
TOKEN_VALIDATION_INTERVAL_SECONDS = 60 * 60
VALIDATION_RETRY_SECONDS = 60
HEALTH_MAX_AGE_SECONDS = TOKEN_VALIDATION_INTERVAL_SECONDS + 5 * 60
AUTHORIZATION_READY_PATH = Path("/run/texttube-auth.ready")


class AuthorizationError(Exception):
    """Authorization failure safe to show to the operator."""


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


def validate_refresh_token(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> bool:
    """Exchange a refresh token to prove that Google still accepts it."""
    status_code, payload = post_form(
        TOKEN_URL,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": REFRESH_GRANT_TYPE,
        },
    )
    if status_code == 200:
        if not str(payload.get("access_token", "")).strip():
            raise AuthorizationError(
                "Google OAuth refresh response omitted the access token"
            )
        return True
    if str(payload.get("error", "")).strip() == "invalid_grant":
        return False
    raise AuthorizationError(response_error(payload, status_code))


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


def authorize(
    client_id: str,
    client_secret: str,
    token_path: Path,
    stop_requested: threading.Event,
) -> Path | None:
    """Complete device authorization and persist its refresh token."""
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
        stop_requested,
    )
    if refresh_token is None:
        return None
    store_refresh_token(token_path, refresh_token)
    return token_path


def read_refresh_token(path: Path) -> str | None:
    """Read the stored refresh token without printing it."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return token or None


def mark_healthy() -> None:
    """Record that the service recently validated the stored refresh token."""
    AUTHORIZATION_READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTHORIZATION_READY_PATH.touch()


def mark_unhealthy() -> None:
    """Remove authorization readiness without changing the stored token."""
    AUTHORIZATION_READY_PATH.unlink(missing_ok=True)


def healthcheck(token_path: Path) -> int:
    """Report healthy only after recent validation of a nonempty stored token."""
    try:
        token = read_refresh_token(token_path)
        validation_age = time.time() - AUTHORIZATION_READY_PATH.stat().st_mtime
    except (OSError, ValueError):
        return 1
    if not token or validation_age < 0:
        return 1
    return 0 if validation_age <= HEALTH_MAX_AGE_SECONDS else 1


class AuthorizationService:
    """Maintains Google authorization and publishes container health readiness."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: Path,
        *,
        stop_requested: threading.Event | None = None,
        startup_ready: threading.Event | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self.stop_requested = stop_requested or threading.Event()
        self.startup_ready = startup_ready

    def request_stop(self) -> None:
        """Interrupt authorization polling or periodic validation."""
        self.stop_requested.set()

    def run(self) -> int:
        """Validate forever and authorize again whenever required."""
        mark_unhealthy()
        was_healthy = False
        try:
            while not self.stop_requested.is_set():
                try:
                    refresh_token = read_refresh_token(self.token_path)
                    if refresh_token and validate_refresh_token(
                        self.client_id,
                        self.client_secret,
                        refresh_token,
                    ):
                        mark_healthy()
                        if self.startup_ready is not None:
                            self.startup_ready.set()
                        if not was_healthy:
                            print(
                                "Google OAuth refresh token is valid; "
                                "authorization service is healthy.",
                                file=sys.stderr,
                                flush=True,
                            )
                        was_healthy = True
                        if self.stop_requested.wait(TOKEN_VALIDATION_INTERVAL_SECONDS):
                            break
                        continue
                    mark_unhealthy()
                    print(
                        "Google OAuth refresh token "
                        + (
                            "expired or was revoked; authorization is required."
                            if refresh_token
                            else "is missing; authorization is required."
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    was_healthy = False
                    destination = authorize(
                        self.client_id,
                        self.client_secret,
                        self.token_path,
                        self.stop_requested,
                    )
                    if destination is None:
                        break
                    print(
                        f"Google OAuth authorization stored securely at {destination}.",
                        file=sys.stderr,
                        flush=True,
                    )
                except (AuthorizationError, OSError) as exc:
                    mark_unhealthy()
                    was_healthy = False
                    print(
                        "Google OAuth authorization unavailable: "
                        f"{exc}; retrying in {VALIDATION_RETRY_SECONDS} seconds.",
                        file=sys.stderr,
                        flush=True,
                    )
                    if self.stop_requested.wait(VALIDATION_RETRY_SECONDS):
                        break
        finally:
            mark_unhealthy()
        return 0

    def run_once(self) -> int:
        """Validate or replace the stored refresh token, then exit."""
        refresh_token = read_refresh_token(self.token_path)
        if refresh_token and validate_refresh_token(
            self.client_id,
            self.client_secret,
            refresh_token,
        ):
            print("Google OAuth refresh token is valid.", file=sys.stderr, flush=True)
            return 0
        if refresh_token:
            print(
                "Google OAuth refresh token expired or was revoked; "
                "authorization is required.",
                file=sys.stderr,
                flush=True,
            )
        destination = authorize(
            self.client_id,
            self.client_secret,
            self.token_path,
            self.stop_requested,
        )
        if destination is None:
            return 130
        print(
            f"Google OAuth authorization stored securely at {destination}.",
            file=sys.stderr,
            flush=True,
        )
        return 0
