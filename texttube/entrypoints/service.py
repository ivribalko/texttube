"""Unified container command dispatch and dependency composition."""

from __future__ import annotations

import os
import signal
import sys
import threading
from typing import Sequence

from texttube.adapters.google_auth import AuthorizationError
from texttube.adapters.scheduler import CronScheduler, scheduler_log
from texttube.adapters.service import StackService
from texttube.entrypoints.app import main as app_main
from texttube.entrypoints.auth import build_service as build_authorization_service
from texttube.entrypoints.auth import main as auth_main
from texttube.entrypoints.scheduler import main as scheduler_main


def main(arguments: Sequence[str] | None = None) -> int:
    """Dispatch app, auth, scheduler, healthcheck, or the combined service."""
    parsed = list(arguments) if arguments is not None else sys.argv[1:]
    command = parsed[0] if parsed else "serve"
    command_arguments = parsed[1:]
    if command == "app":
        return app_main(command_arguments)
    if command == "auth":
        return auth_main(command_arguments)
    if command == "scheduler":
        return scheduler_main(command_arguments)
    if command == "healthcheck":
        if command_arguments:
            _print_usage()
            return 2
        return auth_main(["--healthcheck"])
    if command != "serve" or command_arguments:
        _print_usage()
        return 2
    try:
        stop_requested = threading.Event()
        authorization_ready = threading.Event()
        authorization = build_authorization_service(
            stop_requested=stop_requested,
            startup_ready=authorization_ready,
        )
        expression = CronScheduler.validate_expression(os.environ.get("CRON", ""))
        scheduler = CronScheduler(expression, stop_requested=stop_requested)
        service = StackService(
            authorization,
            scheduler,
            stop_requested,
            authorization_ready,
        )
    except (AuthorizationError, ValueError) as exc:
        scheduler_log(str(exc))
        return 2
    signal.signal(signal.SIGINT, service.handle_signal)
    signal.signal(signal.SIGTERM, service.handle_signal)
    return service.run()


def _print_usage() -> None:
    """Print the unified container command interface."""
    print(
        "Usage: python -m texttube.entrypoints.service "
        "[serve | app [OPTIONS] | auth [--once] | scheduler | healthcheck]",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
