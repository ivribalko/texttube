"""Scheduler configuration and dependency construction."""

from __future__ import annotations

import os
import fcntl
import signal
import sys
from types import FrameType
from typing import Sequence

from texttube.adapters.scheduler import CronScheduler, scheduler_log
from texttube.adapters.telegram import TelegramCommands
from texttube.adapters.state import ConsoleLog
from texttube.config import ConfigLoader, RuntimePaths

INVALID_CONFIGURATION_EXIT_CODE = 2


def build_scheduler() -> CronScheduler:
    """Construct a validated scheduler from CRON and TZ environment values."""
    expression = CronScheduler.validate_expression(os.environ.get("CRON", ""))
    schedule_timezone = CronScheduler.validate_timezone(os.environ.get("TZ", ""))
    return CronScheduler(expression, schedule_timezone=schedule_timezone)


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
    import requests

    paths = RuntimePaths.discover()
    state_dir = paths.google_refresh_token_path().parent
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "telegram_commands.lock").open("a") as listener_lock:
        try:
            fcntl.flock(listener_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            scheduler_log("another Telegram command listener is already running")
            return 1
        with requests.Session() as session:
            commands = TelegramCommands(
                session,
                ConfigLoader.load_app_config(paths.google_refresh_token_path()),
                state_dir / "telegram_update_offset.txt",
                ConsoleLog(),
            )
            commands.register_menu()
            scheduler.poll_restart = commands.poll
            scheduler_log("Telegram command listener ready")
            return scheduler.run()


if __name__ == "__main__":
    raise SystemExit(main())
