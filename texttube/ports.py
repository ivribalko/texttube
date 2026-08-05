"""Dependency-inversion ports implemented by TextTube infrastructure adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Protocol

from texttube.domain import (
    ChannelDiscoveryFailure,
    PendingCaptionFailure,
    Summary,
    Transcript,
    Video,
)


class VideoDiscovery(Protocol):
    """Provides selected and subscription videos to the application core."""

    def ensure_authorized(self) -> None: ...

    def fetch_video(self, video_id: str) -> Video: ...

    def iter_recent_videos(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> Iterable[Video | ChannelDiscoveryFailure]: ...


class Transcription(Protocol):
    """Resolves a video transcript using allowed external sources."""

    def fetch(
        self,
        video: Video,
        *,
        allow_audio: bool,
    ) -> Transcript: ...


class AudioTranscription(Protocol):
    """Downloads and transcribes one video's audio when the core permits it."""

    def fetch(self, video_id: str) -> Transcript: ...


class Summarization(Protocol):
    """Creates summaries from transcript or description content."""

    def summarize_transcript(self, video: Video, transcript: Transcript) -> str: ...

    def summarize_description(self, video: Video) -> str: ...


class Delivery(Protocol):
    """Delivers video summaries and run-level notices."""

    def deliver(self, video: Video, summary: Summary) -> None: ...

    def send_notice(self, message: str) -> None: ...


class State(Protocol):
    """Persists subscription progress and native-caption retry state."""

    def subscription_window(self) -> tuple[datetime, datetime]: ...

    def complete_window(self, window_end: datetime) -> None: ...

    def pending_caption_failures(self) -> tuple[PendingCaptionFailure, ...]: ...

    def record_caption_failure(self, video_id: str) -> int: ...

    def complete_caption_retry(self, video_id: str) -> None: ...


class Log(Protocol):
    """Writes operator logs while controlling sensitive exception detail."""

    def write(self, message: str, *, essential: bool = False) -> None: ...

    def exception(self, error: Exception) -> str: ...
