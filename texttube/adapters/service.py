"""Single-container supervision for authorization and scheduling workers."""

from __future__ import annotations

import signal
import threading
from types import FrameType
from typing import Callable

from texttube.adapters.google_auth import AuthorizationService
from texttube.adapters.scheduler import CronScheduler, scheduler_log


class StackService:
    """Supervises authorization maintenance and scheduling in one container."""

    def __init__(
        self,
        authorization: AuthorizationService,
        scheduler: CronScheduler,
        stop_requested: threading.Event,
        authorization_ready: threading.Event,
    ):
        self.authorization = authorization
        self.scheduler = scheduler
        self.stop_requested = stop_requested
        self.authorization_ready = authorization_ready
        self.authorization_done = threading.Event()
        self.scheduler_done = threading.Event()
        self.worker_errors: list[BaseException] = []

    def handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        """Stop both workers and forward shutdown to an active application."""
        self.stop_requested.set()
        self.scheduler.handle_signal(signum)

    def run(self) -> int:
        """Start authorization, gate scheduling on readiness, and supervise both."""
        authorization_thread = self._start_worker(
            self.authorization.run,
            self.authorization_done,
            "texttube-authorization",
        )
        while not self.authorization_ready.is_set():
            if self.stop_requested.wait(0.5):
                authorization_thread.join()
                return 0
            if self.authorization_done.is_set():
                self.stop_requested.set()
                authorization_thread.join()
                return self._exit_code()
        scheduler_thread = self._start_worker(
            self.scheduler.run,
            self.scheduler_done,
            "texttube-scheduler",
        )
        while not self.stop_requested.wait(0.5):
            if self.authorization_done.is_set() or self.scheduler_done.is_set():
                self.stop_requested.set()
                self.scheduler.handle_signal(signal.SIGTERM)
                break
        authorization_thread.join()
        scheduler_thread.join()
        return self._exit_code()

    def _start_worker(
        self,
        target: Callable[[], int],
        done: threading.Event,
        name: str,
    ) -> threading.Thread:
        """Start one supervised worker that always publishes completion."""
        def run_worker() -> None:
            try:
                return_code = target()
                if not self.stop_requested.is_set():
                    self.worker_errors.append(
                        RuntimeError(
                            f"{name} exited unexpectedly with status {return_code}"
                        )
                    )
            except BaseException as exc:
                self.worker_errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run_worker, name=name)
        thread.start()
        return thread

    def _exit_code(self) -> int:
        """Report worker failures so the container restart policy can recover."""
        if not self.worker_errors:
            return 0
        for error in self.worker_errors:
            scheduler_log(f"service worker failed: {error}")
        return 1
