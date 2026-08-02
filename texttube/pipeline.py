"""SDK-free orchestration for TextTube application and per-video flows."""

from __future__ import annotations

from dataclasses import dataclass

from texttube.domain import (
    DeliveryFailure,
    RunOutcome,
    Summary,
    SummarySource,
    Video,
    VideoFailure,
    VideoOutcome,
    VideoStatus,
)
from texttube.ports import (
    CachePaths,
    Delivery,
    Log,
    State,
    Summarization,
    Transcription,
    VideoDiscovery,
)

SUMMARY_UNAVAILABLE_MESSAGE = "Summary unavailable."


@dataclass(frozen=True)
class ProcessingPolicy:
    """Fixed business boundaries used by the orchestration layer."""

    max_short_duration_seconds: int
    max_audio_duration_seconds: int
    default_video_limit: int


class VideoPipeline:
    """Orchestrates transcript resolution, fallback summary, and delivery."""

    def __init__(
        self,
        transcription: Transcription,
        summarization: Summarization,
        delivery: Delivery,
        cache_paths: CachePaths,
        policy: ProcessingPolicy,
        log: Log,
    ):
        self.transcription = transcription
        self.summarization = summarization
        self.delivery = delivery
        self.cache_paths = cache_paths
        self.policy = policy
        self.log = log

    def is_probable_short(self, video: Video) -> bool:
        """Return whether the known duration falls within the Shorts boundary."""
        return (
            video.duration_seconds is not None
            and video.duration_seconds <= self.policy.max_short_duration_seconds
        )

    def is_audio_allowed(self, video: Video) -> bool:
        """Return whether audio fallback is permitted for the known duration."""
        return (
            video.duration_seconds is not None
            and video.duration_seconds <= self.policy.max_audio_duration_seconds
        )

    def process(self, video: Video) -> VideoOutcome:
        """Process one video through the complete core use case."""
        if self.is_probable_short(video):
            self.log.write(f"skip {video.video_id}: Short", essential=True)
            return VideoOutcome(video_id=video.video_id, status=VideoStatus.SKIPPED)

        self.log.write(
            f"process {video.video_id}: {video.channel_title}: {video.title}",
            essential=True,
        )
        summary = self._summarize(video)
        self.log.write(f"process {video.video_id}: format message")
        self.log.write(f"process {video.video_id}: send telegram", essential=True)
        self.delivery.deliver(video, summary)
        result_label = (
            "summarized"
            if summary.source is SummarySource.TRANSCRIPT
            else "description fallback"
        )
        self.log.write(f"sent {video.video_id}: {result_label}")
        return VideoOutcome(
            video_id=video.video_id,
            status=VideoStatus.DELIVERED,
            summary=summary,
        )

    def _summarize(self, video: Video) -> Summary:
        """Create a transcript summary or the documented description fallback."""
        try:
            transcript = self.transcription.fetch(
                video,
                allow_audio=self.is_audio_allowed(video),
                audio_cache_path=self.cache_paths.audio(video.video_id),
                transcript_cache_path=self.cache_paths.transcript(video.video_id),
            )
            self.log.write(
                f"process {video.video_id}: summarize transcript "
                f"language={transcript.language_code or 'unknown'} "
                f"chars={len(transcript.text)} lines={len(transcript.text.splitlines())}",
                essential=True,
            )
            return Summary(
                text=self.summarization.summarize_transcript(video, transcript),
                source=SummarySource.TRANSCRIPT,
            )
        except VideoFailure as exc:
            self.log.write(
                f"process {video.video_id}: transcript summary unavailable: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
        except Exception as exc:
            self.log.write(
                f"process {video.video_id}: unexpected summary error, using description: "
                f"{self.log.exception(exc)}",
                essential=True,
            )

        self.log.write(
            f"process {video.video_id}: summarize description fallback",
            essential=True,
        )
        try:
            return Summary(
                text=self.summarization.summarize_description(video),
                source=SummarySource.DESCRIPTION,
            )
        except VideoFailure as exc:
            self.log.write(
                f"process {video.video_id}: description summary unavailable: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
            return Summary(
                text=SUMMARY_UNAVAILABLE_MESSAGE,
                source=SummarySource.UNAVAILABLE,
            )


class ApplicationPipeline:
    """Orchestrates selected-video and subscription-window application runs."""

    def __init__(
        self,
        discovery: VideoDiscovery,
        videos: VideoPipeline,
        delivery: Delivery,
        state: State,
        policy: ProcessingPolicy,
        log: Log,
    ):
        self.discovery = discovery
        self.videos = videos
        self.delivery = delivery
        self.state = state
        self.policy = policy
        self.log = log

    def run_single_video(self, video_id: str) -> RunOutcome:
        """Process one selected video without advancing subscription state."""
        self.log.write(f"single video mode: {video_id}", essential=True)
        self.log.write("startup: resolve youtube token", essential=True)
        video = self.discovery.fetch_video(video_id)
        try:
            outcome = self.videos.process(video)
            delivered_count = int(outcome.delivered)
        except (VideoFailure, DeliveryFailure) as exc:
            self.log.write(f"failed to send {video.video_id}: {exc}", essential=True)
            delivered_count = 0
        self.log.write(f"sent messages: {delivered_count}", essential=True)
        return RunOutcome(delivered_count=delivered_count)

    def run_subscriptions(self, limit: int) -> RunOutcome:
        """Process the current subscription window and persist its completion."""
        self.log.write("startup: resolve youtube token", essential=True)
        self.discovery.ensure_authorized()
        window_start, window_end = self.state.subscription_window()
        self.log.write(
            f"subscription window utc: {window_start.isoformat()} -> {window_end.isoformat()}",
            essential=True,
        )

        sent_count = 0
        stopped_by_limit = False
        self.log.write("subscriptions mode: iterate recent videos", essential=True)
        for video in self.discovery.iter_recent_videos(window_start, window_end):
            if limit > 0 and sent_count >= limit:
                stopped_by_limit = True
                break
            try:
                outcome = self.videos.process(video)
                if outcome.delivered:
                    sent_count += 1
                    if limit > 0 and sent_count >= limit:
                        stopped_by_limit = True
                        break
            except (VideoFailure, DeliveryFailure) as exc:
                self.log.write(f"failed to send {video.video_id}: {exc}", essential=True)
            except Exception as exc:
                self.log.write(
                    f"failed to process {video.video_id}: unexpected error: "
                    f"{self.log.exception(exc)}",
                    essential=True,
                )

        if (
            stopped_by_limit
            and limit == self.policy.default_video_limit
            and sent_count >= self.policy.default_video_limit
        ):
            self.log.write(
                f"limit reached: send telegram for {sent_count} messages",
                essential=True,
            )
            self.delivery.send_notice(
                "TextTube stopped after reaching the "
                f"{self.policy.default_video_limit}-video limit for this run."
            )
        self.state.complete_window(window_end)
        self.log.write(f"sent messages: {sent_count}", essential=True)
        return RunOutcome(
            delivered_count=sent_count,
            stopped_by_limit=stopped_by_limit,
        )
