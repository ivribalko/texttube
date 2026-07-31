"""Cron timing, singleton locking, and application subprocess adapter."""

from __future__ import annotations

import fcntl
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter

LOCK_PATH = Path("/data/var/texttube.lock")
APPLICATION_COMMAND = (sys.executable, "/app/texttube_app.py")
APPLICATION_WORKING_DIRECTORY = "/app"


def scheduler_log(message: str) -> None:
    """Write one scheduler message to the container log."""
    print(f"[scheduler] {message}", file=sys.stderr, flush=True)


class CronScheduler:
    """Waits for cron occurrences and runs one isolated application process."""

    def __init__(
        self,
        expression: str,
        *,
        stop_requested: threading.Event | None = None,
    ):
        self.expression = expression
        self.stop_requested = stop_requested or threading.Event()
        self.child: subprocess.Popen[bytes] | None = None

    @staticmethod
    def validate_expression(expression: str) -> str:
        """Validate and return one standard five-field cron expression."""
        fields = expression.split()
        if "\n" in expression or "\r" in expression:
            raise ValueError("CRON must be a single line.")
        if expression.startswith("@") or len(fields) != 5:
            raise ValueError("CRON must contain exactly five standard cron fields.")
        if not croniter.is_valid(expression):
            raise ValueError("CRON is not a valid cron expression.")
        return expression

    def handle_signal(self, signum: int) -> None:
        """Stop future runs and forward shutdown to an active child."""
        self.stop_requested.set()
        if self.child is None or self.child.poll() is not None:
            return
        try:
            self.child.send_signal(signum)
        except ProcessLookupError:
            pass

    def next_run(self) -> datetime:
        """Calculate the next occurrence in UTC."""
        return croniter(
            self.expression,
            datetime.now(timezone.utc),
        ).get_next(datetime)

    def run(self) -> int:
        """Wait and invoke application subprocesses until shutdown."""
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
        """Run the application while holding the non-blocking singleton lock."""
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
