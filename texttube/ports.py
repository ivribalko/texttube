"""Dependency-inversion ports implemented by TextTube infrastructure adapters."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Protocol

from texttube.domain import Summary, Transcript, Video


class VideoDiscovery(Protocol):
    """Provides selected and subscription videos to the application core."""

    def ensure_authorized(self) -> None: ...

    def fetch_video(self, video_id: str) -> Video: ...

    def iter_recent_videos(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> Iterable[Video]: ...


class Transcription(Protocol):
    """Resolves a video transcript using allowed external sources."""

    def fetch(
        self,
        video: Video,
        *,
        allow_audio: bool,
        audio_cache_path: Path | None,
        transcript_cache_path: Path | None,
    ) -> Transcript: ...


class AudioTranscription(Protocol):
    """Downloads and transcribes one video's audio when the core permits it."""

    def fetch(
        self,
        video_id: str,
        *,
        audio_cache_path: Path | None,
    ) -> Transcript: ...


class Summarization(Protocol):
    """Creates summaries from transcript or description content."""

    def summarize_transcript(self, video: Video, transcript: Transcript) -> str: ...

    def summarize_description(self, video: Video) -> str: ...


class Delivery(Protocol):
    """Delivers video summaries and run-level notices."""

    def deliver(self, video: Video, summary: Summary) -> None: ...

    def send_notice(self, message: str) -> None: ...


class State(Protocol):
    """Reads and advances the completed subscription window."""

    def subscription_window(self) -> tuple[datetime, datetime]: ...

    def complete_window(self, window_end: datetime) -> None: ...


class CachePaths(Protocol):
    """Selects optional per-video cache paths for one invocation."""

    def audio(self, video_id: str) -> Path | None: ...

    def transcript(self, video_id: str) -> Path | None: ...


class Log(Protocol):
    """Writes operator logs while controlling sensitive exception detail."""

    def write(self, message: str, *, essential: bool = False) -> None: ...

    def exception(self, error: Exception) -> str: ...
