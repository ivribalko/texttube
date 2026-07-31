"""Authorization CLI parsing and dependency construction."""

from __future__ import annotations

import os
import signal
import sys
import threading
from types import FrameType
from typing import Sequence

from texttube.adapters.google_auth import (
    AuthorizationError,
    AuthorizationService,
    healthcheck,
)
from texttube.config import RuntimePaths


def require_environment(name: str) -> str:
    """Return one required authorization environment variable."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise AuthorizationError(f"Missing required configuration: {name}")
    return value


def build_service(
    *,
    stop_requested: threading.Event | None = None,
    startup_ready: threading.Event | None = None,
) -> AuthorizationService:
    """Construct the authorization adapter from process configuration."""
    return AuthorizationService(
        require_environment("GOOGLE_OAUTH_CLIENT_ID"),
        require_environment("GOOGLE_OAUTH_CLIENT_SECRET"),
        RuntimePaths.discover().google_refresh_token_path(),
        stop_requested=stop_requested,
        startup_ready=startup_ready,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    """Run persistent authorization, one authorization pass, or a health check."""
    parsed = list(arguments) if arguments is not None else sys.argv[1:]
    if parsed == ["--healthcheck"]:
        return healthcheck(RuntimePaths.discover().google_refresh_token_path())
    if parsed not in ([], ["--once"]):
        print("Usage: texttube_auth.py [--healthcheck | --once]", file=sys.stderr)
        return 2
    try:
        service = build_service()
    except AuthorizationError as exc:
        print(f"Google OAuth authorization failed: {exc}", file=sys.stderr)
        return 1

    def handle_signal(_signum: int, _frame: FrameType | None) -> None:
        service.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        return service.run_once() if parsed else service.run()
    except KeyboardInterrupt:
        print("Google OAuth authorization cancelled.", file=sys.stderr)
        return 130
    except (AuthorizationError, OSError) as exc:
        print(f"Google OAuth authorization failed: {exc}", file=sys.stderr)
        return 1

