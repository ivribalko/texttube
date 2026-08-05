"""SDK-free orchestration for TextTube application and per-video flows."""

from __future__ import annotations

from dataclasses import dataclass

from texttube.domain import (
    ChannelDiscoveryFailure,
    DeliveryFailure,
    NativeTranscriptUnavailable,
    RunOutcome,
    Summary,
    SummarySource,
    Video,
    VideoFailure,
    VideoOutcome,
    VideoStatus,
)
from texttube.ports import (
    Delivery,
    Log,
    State,
    Summarization,
    Transcription,
    VideoDiscovery,
)

SUMMARY_UNAVAILABLE_MESSAGE = "summary unavailable"


@dataclass(frozen=True)
class ProcessingPolicy:
    """Fixed business boundaries used by the orchestration layer."""

    max_short_duration_seconds: int
    max_audio_duration_seconds: int
    default_video_limit: int
    max_native_caption_attempts: int


class VideoPipeline:
    """Orchestrates transcript resolution, fallback summary, and delivery."""

    def __init__(
        self,
        transcription: Transcription,
        summarization: Summarization,
        delivery: Delivery,
        policy: ProcessingPolicy,
        log: Log,
    ):
        self.transcription = transcription
        self.summarization = summarization
        self.delivery = delivery
        self.policy = policy
        self.log = log

    def is_probable_short(self, video: Video) -> bool:
        """Return whether the known duration falls within the Shorts boundary."""
        return (
            video.duration_seconds is not None
            and video.duration_seconds <= self.policy.max_short_duration_seconds
        )

    def is_audio_allowed(self, video: Video) -> bool:
        """Keep the retained audio-transcription path disabled."""
        return False

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
        return self._deliver(video, summary)

    def process_description_fallback(self, video: Video) -> VideoOutcome:
        """Summarize and deliver description content after caption retries expire."""
        return self._deliver(video, self._summarize_description(video))

    def _deliver(self, video: Video, summary: Summary) -> VideoOutcome:
        """Deliver one prepared summary and return its successful outcome."""
        self.log.write(f"process {video.video_id}: format message")
        self.log.write(f"process {video.video_id}: send telegram", essential=True)
        self.delivery.deliver(video, summary)
        result_label = {
            SummarySource.TRANSCRIPT: "summarized",
            SummarySource.DESCRIPTION: "description fallback",
            SummarySource.UNAVAILABLE: "summary unavailable",
        }[summary.source]
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
            )
        except NativeTranscriptUnavailable as exc:
            self.log.write(
                f"process {video.video_id}: native transcript unavailable: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
            raise
        except VideoFailure as exc:
            self.log.write(
                f"process {video.video_id}: transcript retrieval error, "
                f"using description: {self.log.exception(exc)}",
                essential=True,
            )
            return self._summarize_description(video)
        except Exception as exc:
            self.log.write(
                f"process {video.video_id}: unexpected transcript error, "
                f"using description: {self.log.exception(exc)}",
                essential=True,
            )
            return self._summarize_description(video)

        try:
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
                f"process {video.video_id}: transcript summary unavailable, "
                f"using description: {self.log.exception(exc)}",
                essential=True,
            )
        except Exception as exc:
            self.log.write(
                f"process {video.video_id}: unexpected summary error, using description: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
        return self._summarize_description(video)

    def _summarize_description(self, video: Video) -> Summary:
        """Create a labeled description summary or an unavailable result."""
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
        except Exception as exc:
            self.log.write(
                f"process {video.video_id}: unexpected description summary error: "
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
        outcome = self._attempt_video(video)
        delivered_count = int(outcome is not None and outcome.delivered)
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

        sent_count, attempted_video_ids, stopped_by_limit = self._retry_pending_captions(
            limit
        )
        self.log.write("subscriptions mode: iterate recent videos", essential=True)
        items = (
            ()
            if stopped_by_limit
            else self.discovery.iter_recent_videos(window_start, window_end)
        )
        for item in items:
            if isinstance(item, ChannelDiscoveryFailure):
                self._report_channel_failure(item)
                continue
            video = item
            if video.video_id in attempted_video_ids:
                self.log.write(
                    f"skip {video.video_id}: already attempted this run"
                )
                continue
            if limit > 0 and sent_count >= limit:
                stopped_by_limit = True
                break
            attempted_video_ids.add(video.video_id)
            outcome = self._attempt_video(video)
            if outcome is not None and outcome.delivered:
                sent_count += 1
                if limit > 0 and sent_count >= limit:
                    stopped_by_limit = True
                    break

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

    def _retry_pending_captions(
        self,
        limit: int,
    ) -> tuple[int, set[str], bool]:
        """Retry videos whose native captions failed in earlier runs."""
        sent_count = 0
        attempted_video_ids: set[str] = set()
        pending_failures = self.state.pending_caption_failures()
        if pending_failures:
            self.log.write(
                f"pending native captions: retry {len(pending_failures)}",
                essential=True,
            )
        for failure in pending_failures:
            if limit > 0 and sent_count >= limit:
                return sent_count, attempted_video_ids, True
            video_id = failure.video_id
            attempted_video_ids.add(video_id)
            video = self.discovery.fetch_video(video_id)
            outcome = self._attempt_video(video)
            if outcome is not None and outcome.delivered:
                sent_count += 1
        return sent_count, attempted_video_ids, False

    def _attempt_video(self, video: Video) -> VideoOutcome | None:
        """Process one video and persist only native-caption failure."""
        try:
            outcome = self.videos.process(video)
        except NativeTranscriptUnavailable as exc:
            self.log.write(
                f"native captions failed {video.video_id}: {self.log.exception(exc)}",
                essential=True,
            )
            return self._record_caption_failure(video)
        except DeliveryFailure as exc:
            self.log.write(
                f"telegram delivery failed {video.video_id}: {self.log.exception(exc)}",
                essential=True,
            )
            self.state.complete_caption_retry(video.video_id)
            return None
        except VideoFailure as exc:
            self.log.write(
                f"failed to process {video.video_id}: {self.log.exception(exc)}",
                essential=True,
            )
            self.state.complete_caption_retry(video.video_id)
            return None
        except Exception as exc:
            self.log.write(
                f"failed to process {video.video_id}: unexpected error: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
            self.state.complete_caption_retry(video.video_id)
            return None
        self.state.complete_caption_retry(video.video_id)
        return outcome

    def _record_caption_failure(self, video: Video) -> VideoOutcome | None:
        """Record a caption failure and use description after attempt three."""
        attempts = self.state.record_caption_failure(video.video_id)
        self.log.write(
            f"pending native captions {video.video_id}: failed attempt {attempts}",
            essential=True,
        )
        if attempts < self.policy.max_native_caption_attempts:
            return None
        self.log.write(
            f"native captions exhausted {video.video_id}: use description",
            essential=True,
        )
        try:
            return self.videos.process_description_fallback(video)
        except DeliveryFailure as exc:
            self.log.write(
                f"telegram delivery failed {video.video_id}: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
            return None
        except Exception as exc:
            self.log.write(
                f"description fallback failed {video.video_id}: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
            return None

    def _report_channel_failure(self, failure: ChannelDiscoveryFailure) -> None:
        """Log and notify about one skipped subscription channel."""
        self.log.write(
            f"channel {failure.channel_title}: uploads unavailable: {failure.detail}",
            essential=True,
        )
        try:
            self.delivery.send_notice(
                f'TextTube skipped channel "{failure.channel_title}" because YouTube '
                "could not read its uploads playlist. The run will continue.\n\n"
                f"https://www.youtube.com/channel/{failure.channel_id}"
            )
        except DeliveryFailure as exc:
            self.log.write(
                f"channel {failure.channel_title}: telegram notice failed: "
                f"{self.log.exception(exc)}",
                essential=True,
            )
