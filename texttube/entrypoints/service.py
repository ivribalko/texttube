"""Unified container command dispatch for scheduled and manual operations."""

from __future__ import annotations

import sys
from typing import Sequence

from texttube.entrypoints.app import check_startup_authorization, main as app_main
from texttube.entrypoints.scheduler import main as scheduler_main


def main(arguments: Sequence[str] | None = None) -> int:
    """Dispatch the scheduler and explicit application or authorization runs."""
    parsed = list(arguments) if arguments is not None else sys.argv[1:]
    command = parsed[0] if parsed else "serve"
    command_arguments = parsed[1:]
    if command == "app":
        return app_main(command_arguments)
    if command == "scheduler":
        return scheduler_main(command_arguments)
    if command == "serve" and not command_arguments:
        authorization_status = check_startup_authorization()
        if authorization_status != 0:
            return authorization_status
        return scheduler_main([])
    _print_usage()
    return 2


def _print_usage() -> None:
    """Print the unified container command interface."""
    print(
        "Usage: python -m texttube.entrypoints.service "
        "[serve | app [OPTIONS] | scheduler]",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
