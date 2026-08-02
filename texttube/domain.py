"""Immutable domain values and application-level failures for TextTube."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class FatalError(Exception):
    """Fatal setup or API failure that stops the whole run."""


class GoogleOAuthReauthorizationRequired(FatalError):
    """Google OAuth failure that requires fresh operator authorization."""


class VideoFailure(Exception):
    """Per-video failure that allows later videos to continue."""


class DeliveryFailure(Exception):
    """Delivery failure for one outbound message."""


class SummarySource(Enum):
    """Identifies the source used to create a delivered summary."""

    TRANSCRIPT = "transcript"
    DESCRIPTION = "description"
    UNAVAILABLE = "unavailable"


class VideoStatus(Enum):
    """Identifies whether processing skipped or delivered one video."""

    SKIPPED = "skipped"
    DELIVERED = "delivered"


@dataclass(frozen=True)
class Video:
    """Normalized YouTube metadata used by the application core."""

    video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: datetime
    duration_seconds: int | None = None
    default_audio_language: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Transcript:
    """Transcript text with the best available language hint."""

    text: str
    language_code: str = ""


@dataclass(frozen=True)
class Summary:
    """Summary text and the content source from which it was produced."""

    text: str
    source: SummarySource


@dataclass(frozen=True)
class VideoOutcome:
    """Immutable result of processing one video."""

    video_id: str
    status: VideoStatus
    summary: Summary | None = None

    @property
    def delivered(self) -> bool:
        """Return whether the video produced an outbound message."""
        return self.status is VideoStatus.DELIVERED


@dataclass(frozen=True)
class RunOutcome:
    """Immutable counts and limit state for one application run."""

    delivered_count: int
    stopped_by_limit: bool = False
