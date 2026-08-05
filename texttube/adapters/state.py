"""Filesystem state, logging, and process-lifecycle adapters."""

from __future__ import annotations

import json
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from texttube.config import (
    LAST_SUBSCRIPTION_WINDOW_END_FILE,
    LOG_FILE_PREFIX,
    LOG_RETENTION_DAYS,
    MAX_VIDEO_PROCESSING_ATTEMPTS,
    PENDING_VIDEO_FAILURES_FILE,
    SUBSCRIPTION_STATE_DIR_NAME,
    ValueParser,
)
from texttube.domain import FatalError, PendingVideoFailure


class ConsoleLog:
    """Writes concise operator logs to stderr and an optional run file."""

    def __init__(self, verbose: bool, log_dir: Path | None = None):
        self.verbose = verbose
        self.path: Path | None = None
        self._file = None
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            self.path = log_dir / f"{LOG_FILE_PREFIX}{timestamp}.log"
            self._file = self.path.open("x", encoding="utf-8")
            self._remove_expired_logs(log_dir)

    def write(self, message: str, *, essential: bool = False) -> None:
        """Write a timestamped message when its configured level is visible."""
        if not self.verbose and not essential:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        print(line, file=sys.stderr, flush=True)
        if self._file is not None:
            try:
                print(line, file=self._file, flush=True)
            except (OSError, ValueError) as exc:
                self._disable_file_logging(exc)

    def close(self) -> None:
        """Close the optional application-run log file."""
        if self._file is not None:
            log_file = self._file
            self._file = None
            log_file.close()

    def exception(self, error: Exception) -> str:
        """Return safe exception detail for the active verbosity level."""
        if self.verbose:
            return str(error) or error.__class__.__name__
        return "error details hidden; run with --verbose to show the full exception"

    def _remove_expired_logs(self, log_dir: Path) -> None:
        """Remove application logs that reached the retention boundary."""
        cutoff = datetime.now(timezone.utc).timestamp() - timedelta(
            days=LOG_RETENTION_DAYS
        ).total_seconds()
        for path in log_dir.glob(f"{LOG_FILE_PREFIX}*.log"):
            try:
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                self.write(
                    f"run log retention failed for {path.name}: {exc}",
                    essential=True,
                )

    def _disable_file_logging(self, error: Exception) -> None:
        """Keep stderr logging active after a run-file write failure."""
        log_file = self._file
        self._file = None
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass
        print(
            f"TextTube run-file logging stopped: {error}",
            file=sys.stderr,
            flush=True,
        )


class FileSubscriptionState:
    """Persists subscription boundaries and failed-video retry state."""

    def __init__(self, state_root: Path):
        self.state_root = state_root

    @property
    def state_dir(self) -> Path:
        """Return the directory containing subscription state."""
        return self.state_root / "var" / SUBSCRIPTION_STATE_DIR_NAME

    @property
    def cutoff_path(self) -> Path:
        """Return the completed-window cutoff path."""
        return self.state_dir / LAST_SUBSCRIPTION_WINDOW_END_FILE

    @property
    def pending_path(self) -> Path:
        """Return the durable failed-video retry-state path."""
        return self.state_dir / PENDING_VIDEO_FAILURES_FILE

    def subscription_window(self) -> tuple[datetime, datetime]:
        """Resolve the next half-open subscription window in UTC."""
        window_end = datetime.now(timezone.utc).replace(microsecond=0)
        if not self.cutoff_path.exists():
            window_start = None
        else:
            value = self.cutoff_path.read_text(encoding="utf-8").strip()
            if not value:
                window_start = None
            else:
                try:
                    window_start = ValueParser.parse_rfc3339(value)
                except ValueError as exc:
                    raise FatalError(
                        f"Invalid subscription state file {self.cutoff_path}: {exc}"
                    ) from exc
        if window_start is None:
            window_start = window_end - timedelta(days=1)
        if window_start >= window_end:
            raise FatalError(
                "Last subscription window end must be earlier than the current run time. "
                f"Delete {self.cutoff_path} to reset the schedule state."
            )
        return window_start, window_end

    def complete_window(self, window_end: datetime) -> None:
        """Persist a successfully completed subscription-window boundary."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cutoff_path.write_text(
            window_end.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            encoding="utf-8",
        )

    def pending_video_failures(self) -> tuple[PendingVideoFailure, ...]:
        """Return videos that remain below the processing-attempt limit."""
        pending, _ = self._read_pending_state()
        return tuple(
            PendingVideoFailure(video_id=video_id, failed_attempts=attempts)
            for video_id, attempts in sorted(pending.items())
        )

    def pending_unavailable_notices(self) -> tuple[str, ...]:
        """Return videos awaiting only their terminal unavailable notice."""
        _, unavailable = self._read_pending_state()
        return tuple(sorted(unavailable))

    def record_video_failure(self, video_id: str) -> int:
        """Record one failed run and move attempt three to terminal delivery."""
        pending, unavailable = self._read_pending_state()
        if video_id in unavailable:
            return MAX_VIDEO_PROCESSING_ATTEMPTS
        attempts = pending.get(video_id, 0) + 1
        if attempts >= MAX_VIDEO_PROCESSING_ATTEMPTS:
            pending.pop(video_id, None)
            unavailable.add(video_id)
        else:
            pending[video_id] = attempts
        self._write_pending_state(pending, unavailable)
        return attempts

    def complete_video(self, video_id: str) -> None:
        """Remove a successfully handled video from every pending collection."""
        pending, unavailable = self._read_pending_state()
        changed = pending.pop(video_id, None) is not None
        if video_id in unavailable:
            unavailable.remove(video_id)
            changed = True
        if changed:
            self._write_pending_state(pending, unavailable)

    def _read_pending_state(self) -> tuple[dict[str, int], set[str]]:
        """Read and validate the durable failed-video state document."""
        if not self.pending_path.exists():
            return {}, set()
        try:
            raw = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FatalError(
                f"Invalid pending video state file {self.pending_path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise FatalError(
                f"Invalid pending video state file {self.pending_path}: expected object"
            )
        raw_pending = raw.get("pending", {})
        raw_unavailable = raw.get("summary_unavailable", [])
        if not isinstance(raw_pending, dict) or not isinstance(raw_unavailable, list):
            raise FatalError(
                f"Invalid pending video state file {self.pending_path}: invalid collections"
            )
        pending: dict[str, int] = {}
        for video_id, attempts in raw_pending.items():
            if (
                not isinstance(video_id, str)
                or not video_id
                or isinstance(attempts, bool)
                or not isinstance(attempts, int)
                or attempts < 1
                or attempts >= MAX_VIDEO_PROCESSING_ATTEMPTS
            ):
                raise FatalError(
                    f"Invalid pending video state file {self.pending_path}: "
                    "invalid pending entry"
                )
            pending[video_id] = attempts
        unavailable: set[str] = set()
        for video_id in raw_unavailable:
            if not isinstance(video_id, str) or not video_id:
                raise FatalError(
                    f"Invalid pending video state file {self.pending_path}: "
                    "invalid summary-unavailable entry"
                )
            unavailable.add(video_id)
        if set(pending).intersection(unavailable):
            raise FatalError(
                f"Invalid pending video state file {self.pending_path}: duplicate video state"
            )
        return pending, unavailable

    def _write_pending_state(
        self,
        pending: dict[str, int],
        unavailable: set[str],
    ) -> None:
        """Atomically replace the durable failed-video state document."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        document = {
            "pending": dict(sorted(pending.items())),
            "summary_unavailable": sorted(unavailable),
        }
        temporary_path = self.pending_path.with_name(
            f".{self.pending_path.name}.{os.getpid()}.tmp"
        )
        try:
            temporary_path.write_text(
                f"{json.dumps(document, indent=2)}\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.pending_path)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise FatalError(f"Could not save pending video state: {exc}") from exc


class ApplicationLifecycle:
    """Owns process signal handling and best-effort LIFO cleanup."""

    def __init__(self, log: ConsoleLog):
        self.log = log
        self._cleanup_callbacks: list[tuple[str, Callable[[], Any]]] = []
        self._installed_handlers: list[tuple[int, Any]] = []

    def install_signal_handlers(self) -> None:
        """Install interrupt handlers for one CLI invocation."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(sig)
            signal.signal(sig, self._handle_signal)
            self._installed_handlers.append((sig, previous))

    def restore_signal_handlers(self) -> None:
        """Restore handlers replaced by this lifecycle owner."""
        while self._installed_handlers:
            sig, previous = self._installed_handlers.pop()
            signal.signal(sig, previous)

    def add_cleanup(self, callback: Callable[[], Any]) -> None:
        """Register one best-effort cleanup callback."""
        name = getattr(callback, "__qualname__", getattr(callback, "__name__", repr(callback)))
        self._cleanup_callbacks.append((name, callback))

    def cleanup(self) -> None:
        """Run registered cleanup callbacks in reverse order."""
        while self._cleanup_callbacks:
            name, callback = self._cleanup_callbacks.pop()
            try:
                callback()
            except Exception as exc:
                self.log.write(
                    f"cleanup: {name}: {self.log.exception(exc)}",
                    essential=True,
                )

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        """Convert termination signals into the CLI interrupt path."""
        signal_name = signal.Signals(signum).name
        self.log.write(f"interrupt: received {signal_name}", essential=True)
        raise KeyboardInterrupt
