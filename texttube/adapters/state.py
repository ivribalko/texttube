"""Filesystem, cache-path, logging, and process-lifecycle adapters."""

from __future__ import annotations

import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from texttube.config import (
    AUDIO_CACHE_EXTENSION,
    CACHE_DIR_NAME,
    LAST_SUBSCRIPTION_WINDOW_END_FILE,
    SUBSCRIPTION_STATE_DIR_NAME,
    TRANSCRIPT_CACHE_EXTENSION,
    RuntimePaths,
    ValueParser,
)
from texttube.domain import FatalError


class ConsoleLog:
    """Writes concise operator logs and hides exception details by default."""

    def __init__(self, verbose: bool):
        self.verbose = verbose

    def write(self, message: str, *, essential: bool = False) -> None:
        """Write a timestamped message when its configured level is visible."""
        if not self.verbose and not essential:
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)

    def exception(self, error: Exception) -> str:
        """Return safe exception detail for the active verbosity level."""
        if self.verbose:
            return str(error) or error.__class__.__name__
        return "error details hidden; run with --verbose to show the full exception"


class FileCachePaths:
    """Provides cache paths only when cache reuse is enabled for the run."""

    def __init__(self, paths: RuntimePaths, *, enabled: bool):
        self.paths = paths
        self.enabled = enabled

    def audio(self, video_id: str) -> Path | None:
        """Return the optional cached-audio path for one video."""
        if not self.enabled:
            return None
        return (
            self.paths.state_root
            / "var"
            / CACHE_DIR_NAME
            / f"{video_id}{AUDIO_CACHE_EXTENSION}"
        )

    def transcript(self, video_id: str) -> Path | None:
        """Return the optional cached-transcript path for one video."""
        if not self.enabled:
            return None
        return (
            self.paths.state_root
            / "var"
            / CACHE_DIR_NAME
            / f"{video_id}{TRANSCRIPT_CACHE_EXTENSION}"
        )


class FileSubscriptionState:
    """Persists the last completed subscription-window boundary."""

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
