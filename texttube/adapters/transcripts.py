"""Native-caption selection and dormant audio-fallback adapter."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from texttube.domain import NativeTranscriptUnavailable, Transcript, Video, VideoFailure
from texttube.ports import AudioTranscription, Log


class NativeTranscriptFetcher:
    """Fetches YouTube transcripts and ranks preferred language matches."""

    def __init__(self, language_preferences: tuple[str, ...], log: Log):
        self.language_preferences = language_preferences
        self.log = log

    @staticmethod
    def transcript_language_match_rank(
        candidate_language_code: str,
        preferred_languages: tuple[str, ...],
    ) -> tuple[int, int] | None:
        """Rank an exact or primary-subtag language match."""
        normalized_candidate = candidate_language_code.strip().lower()
        if not normalized_candidate:
            return None
        candidate_primary = normalized_candidate.split("-", 1)[0]
        for index, preferred_language in enumerate(preferred_languages):
            if normalized_candidate == preferred_language:
                return (index, 0)
            if candidate_primary == preferred_language or normalized_candidate.startswith(
                f"{preferred_language}-"
            ):
                return (index, 1)
        return None

    @classmethod
    def select_original_audio_language_rank(
        cls,
        candidates: list[Any],
        preferred_languages: tuple[str, ...],
        original_audio_language: str = "",
    ) -> tuple[int, int] | None:
        """Find the configured rank for YouTube's original audio language."""
        normalized_original_language = original_audio_language.strip().lower()
        if normalized_original_language:
            return cls.transcript_language_match_rank(
                normalized_original_language,
                preferred_languages,
            )
        ranks: list[tuple[int, int]] = []
        for transcript in candidates:
            if not bool(getattr(transcript, "is_generated", False)):
                continue
            rank = cls.transcript_language_match_rank(
                str(getattr(transcript, "language_code", "")).strip(),
                preferred_languages,
            )
            if rank is not None:
                ranks.append(rank)
        return min(ranks) if ranks else None

    @classmethod
    def order_candidates(
        cls,
        candidates: list[Any],
        preferred_languages: tuple[str, ...],
        original_audio_language: str = "",
    ) -> list[Any]:
        """Prefer configured original-audio captions, then stable fallbacks."""
        if not preferred_languages:
            return candidates
        original_audio_rank = cls.select_original_audio_language_rank(
            candidates,
            preferred_languages,
            original_audio_language,
        )
        ranked: list[tuple[tuple[int, int], int, Any]] = []
        fallback: list[tuple[int, Any]] = []
        for original_index, transcript in enumerate(candidates):
            rank = cls.transcript_language_match_rank(
                str(getattr(transcript, "language_code", "")).strip(),
                preferred_languages,
            )
            if rank is None:
                fallback.append((original_index, transcript))
                continue
            if original_audio_rank is not None and rank[0] == original_audio_rank[0]:
                rank = (-1, rank[1])
            ranked.append((rank, original_index, transcript))
        if not ranked:
            return candidates
        ranked.sort(key=lambda item: (item[0], item[1]))
        ordered = [transcript for _, _, transcript in ranked]
        ordered.extend(transcript for _, transcript in fallback)
        return ordered

    def fetch(
        self,
        video_id: str,
        *,
        original_audio_language: str = "",
    ) -> Transcript:
        """Fetch the first nonempty transcript in preference order."""
        self.log.write(f"transcript native: list {video_id}")
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import (
                CouldNotRetrieveTranscript,
                NoTranscriptFound,
                TranscriptsDisabled,
                VideoUnavailable,
            )
        except ModuleNotFoundError as exc:
            raise VideoFailure(
                "transcript unavailable: missing Python dependency youtube-transcript-api"
            ) from exc
        transcript_errors = (
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
            CouldNotRetrieveTranscript,
        )
        try:
            transcript_api = YouTubeTranscriptApi()
            if hasattr(transcript_api, "list"):
                transcript_list = transcript_api.list(video_id)
            else:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        except transcript_errors as exc:
            raise VideoFailure(
                f"transcript unavailable: {self.log.exception(exc)}"
            ) from exc
        except Exception as exc:
            raise VideoFailure(
                f"transcript unavailable: unexpected error: {self.log.exception(exc)}"
            ) from exc
        candidates = list(transcript_list)
        if not candidates:
            raise VideoFailure("transcript unavailable: no transcripts found")
        self.log.write(f"transcript native: candidates {video_id}: {len(candidates)}")
        if self.language_preferences:
            self.log.write(
                "transcript native: preferred languages "
                f"{video_id}: {', '.join(self.language_preferences)}"
            )
            original_audio_rank = self.select_original_audio_language_rank(
                candidates,
                self.language_preferences,
                original_audio_language,
            )
            if original_audio_rank is not None:
                self.log.write(
                    "transcript native: original audio language matched preference "
                    f"{video_id}: {self.language_preferences[original_audio_rank[0]]}"
                )
        candidates = self.order_candidates(
            candidates,
            self.language_preferences,
            original_audio_language,
        )
        last_error: Exception | None = None
        for transcript in candidates:
            try:
                language_code = (
                    str(getattr(transcript, "language_code", "")).strip() or "unknown"
                )
                generated = bool(getattr(transcript, "is_generated", False))
                self.log.write(
                    f"transcript native: fetch {video_id}: "
                    f"lang={language_code} generated={'yes' if generated else 'no'}"
                )
                fetched = transcript.fetch()
                lines: list[str] = []
                for item in fetched:
                    text = (
                        str(item.get("text", "")).strip()
                        if isinstance(item, dict)
                        else str(getattr(item, "text", "")).strip()
                    )
                    if text:
                        lines.append(text)
                text = "\n".join(lines).strip()
                if text:
                    self.log.write(
                        f"transcript native: ok {video_id}: lang={language_code} "
                        f"chars={len(text)} lines={len(lines)}"
                    )
                    return Transcript(text=text, language_code=language_code)
            except transcript_errors as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
        if last_error:
            raise VideoFailure(
                f"transcript unavailable: {self.log.exception(last_error)}"
            ) from last_error
        raise VideoFailure("transcript unavailable: transcript was empty")

class TranscriptResolver:
    """Resolves native-caption transcripts with dormant audio code."""

    def __init__(
        self,
        native: NativeTranscriptFetcher,
        audio: AudioTranscription,
        log: Log,
    ):
        self.native = native
        self.audio = audio
        self.log = log

    def fetch(
        self,
        video: Video,
        *,
        allow_audio: bool,
    ) -> Transcript:
        """Resolve a transcript while honoring the core's audio decision."""
        try:
            self.log.write(
                f"process {video.video_id}: native transcript", essential=True
            )
            result = self.native.fetch(
                video.video_id,
                original_audio_language=video.default_audio_language,
            )
        except VideoFailure as transcript_exc:
            self.log.write(f"transcript fallback {video.video_id}: {transcript_exc}")
            if not allow_audio:
                raise NativeTranscriptUnavailable(
                    f"{transcript_exc}; audio transcription skipped because "
                    "it is disabled"
                ) from transcript_exc
            try:
                result = self.audio.fetch(video.video_id)
                if not result.language_code and video.default_audio_language:
                    result = replace(
                        result,
                        language_code=video.default_audio_language,
                    )
                    self.log.write(
                        "transcript audio: YouTube default language "
                        f"{video.video_id}: {video.default_audio_language}"
                    )
            except VideoFailure as audio_exc:
                raise VideoFailure(f"{transcript_exc}; {audio_exc}") from audio_exc
        return result
