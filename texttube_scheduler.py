"""Cron-driven container scheduler for TextTube.

This file owns cron expression validation, interruptible waiting, singleton
locking, application process execution, and shutdown signal forwarding.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType

from croniter import croniter

CRON_ENVIRONMENT_VARIABLE = "CRON"
LOCK_PATH = Path("/data/var/texttube.lock")
APPLICATION_COMMAND = (sys.executable, "/app/texttube_app.py")
APPLICATION_WORKING_DIRECTORY = "/app"
INVALID_CONFIGURATION_EXIT_CODE = 2


def scheduler_log(message: str) -> None:
    print(f"[scheduler] {message}", file=sys.stderr, flush=True)


class TextTubeScheduler:
    """Waits for cron occurrences and runs one isolated TextTube process at a time."""

    def __init__(self, expression: str):
        self.expression = expression
        self.stop_requested = threading.Event()
        self.child: subprocess.Popen[bytes] | None = None

    @classmethod
    def from_environment(cls) -> "TextTubeScheduler":
        expression = os.environ.get(CRON_ENVIRONMENT_VARIABLE, "")
        fields = expression.split()
        if "\n" in expression or "\r" in expression:
            raise ValueError("CRON must be a single line.")
        if expression.startswith("@") or len(fields) != 5:
            raise ValueError("CRON must contain exactly five standard cron fields.")
        if not croniter.is_valid(expression):
            raise ValueError("CRON is not a valid cron expression.")
        return cls(expression)

    def handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        self.stop_requested.set()
        if self.child is None or self.child.poll() is not None:
            return
        try:
            self.child.send_signal(signum)
        except ProcessLookupError:
            pass

    def next_run(self) -> datetime:
        return croniter(
            self.expression,
            datetime.now(timezone.utc),
        ).get_next(datetime)

    def run(self) -> int:
        while not self.stop_requested.is_set():
            next_run = self.next_run()
            scheduler_log(f"next run utc: {next_run.isoformat(timespec='seconds')}")
            wait_seconds = max(
                0.0,
                (next_run - datetime.now(timezone.utc)).total_seconds(),
            )
            if self.stop_requested.wait(wait_seconds):
                break
            self.run_application()
        return 0

    def run_application(self) -> None:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_PATH.open("a", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                scheduler_log("run skipped: scheduler lock is already held")
                return
            try:
                if self.stop_requested.is_set():
                    return
                self.child = subprocess.Popen(
                    APPLICATION_COMMAND,
                    cwd=APPLICATION_WORKING_DIRECTORY,
                )
                return_code = self.child.wait()
                if return_code not in (0, 130):
                    scheduler_log(f"TextTube exited with status {return_code}")
            finally:
                self.child = None
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def main() -> int:
    try:
        scheduler = TextTubeScheduler.from_environment()
    except ValueError as exc:
        scheduler_log(str(exc))
        return INVALID_CONFIGURATION_EXIT_CODE
    signal.signal(signal.SIGINT, scheduler.handle_signal)
    signal.signal(signal.SIGTERM, scheduler.handle_signal)
    return scheduler.run()


if __name__ == "__main__":
    raise SystemExit(main())
