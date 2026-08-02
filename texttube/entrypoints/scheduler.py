"""Scheduler configuration and dependency construction."""

from __future__ import annotations

import os
import signal
import sys
from types import FrameType
from typing import Sequence

from texttube.adapters.scheduler import CronScheduler, scheduler_log

INVALID_CONFIGURATION_EXIT_CODE = 2


def build_scheduler() -> CronScheduler:
    """Construct a validated scheduler from the CRON environment value."""
    expression = CronScheduler.validate_expression(os.environ.get("CRON", ""))
    return CronScheduler(expression)


def main(arguments: Sequence[str] | None = None) -> int:
    """Validate configuration and run the cron scheduler."""
    parsed = list(arguments) if arguments is not None else sys.argv[1:]
    if parsed:
        print("Usage: python -m texttube.entrypoints.scheduler", file=sys.stderr)
        return INVALID_CONFIGURATION_EXIT_CODE
    try:
        scheduler = build_scheduler()
    except ValueError as exc:
        scheduler_log(str(exc))
        return INVALID_CONFIGURATION_EXIT_CODE

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        scheduler.handle_signal(signum)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return scheduler.run()


if __name__ == "__main__":
    raise SystemExit(main())
