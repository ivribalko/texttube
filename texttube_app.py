"""Application entrypoint for TextTube.

This file owns configuration, YouTube API access, transcript fetching, OpenAI
summarization and transcription, Telegram delivery, command-line behavior,
run-level failure notifications, and subscription run state.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time as time_module
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
OPENAI_SUMMARY_MODEL = "gpt-5.6-luna"
OPENAI_TRANSCRIPTION_MODEL = "gpt-transcribe"
REQUEST_TIMEOUT_SECONDS = 30
OPENAI_SUMMARY_TIMEOUT_SECONDS = 180
OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS = 1800
MAX_AUDIO_TRANSCRIPTION_DURATION_SECONDS = 60 * 60
YOUTUBE_PAGE_SIZE = 50
CACHE_DIR_NAME = "cache"
AUDIO_CACHE_EXTENSION = ".m4a"
TRANSCRIPT_CACHE_EXTENSION = ".txt"
VERBOSE_LOGGING = False
DEFAULT_VIDEO_LIMIT = 100
SUBSCRIPTION_STATE_DIR_NAME = "state"
LAST_SUBSCRIPTION_WINDOW_END_FILE = "last_subscription_window_end_utc.txt"
GOOGLE_OAUTH_REFRESH_TOKEN_FILE = "google_oauth_refresh_token"
TRANSCRIPT_LANGUAGE_SEPARATOR = ","
GOOGLE_OAUTH_REAUTHORIZATION_ERROR_CODE = "invalid_grant"
GOOGLE_OAUTH_AUTH_COMMAND = (
    "docker compose --file compose.yaml --file compose.local.yaml "
    "--profile auth run --build --rm auth"
)
GENERIC_RUN_FAILURE_MESSAGE = "TextTube run failed."
GOOGLE_OAUTH_REAUTHORIZATION_MESSAGE = (
    "TextTube could not access YouTube because Google authorization expired or was revoked. "
    "Run {auth_command} to reconnect YouTube. The next run will process the preserved "
    "subscription window."
)
DESCRIPTION_SUMMARIZER_PROMPT = """Summarize a YouTube video from its title and description.

Return one compact plain-text paragraph of 1 to 3 short sentences in the
description's dominant language. Keep only facts that describe the video's
actual subject. Remove every URL, domain, social handle, sponsor or affiliate
message, discount, merchandise pitch, subscription request, channel boilerplate,
contact detail, and other unrelated information. Do not mention that the source
was a title or description. If no relevant facts remain, return exactly:
No essential facts."""


def log(message: str, *, essential: bool = False) -> None:
    if not VERBOSE_LOGGING and not essential:
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def format_duration(seconds: float) -> str:
    return f"{seconds:.1f}s"


def exception_log_message(exc: Exception) -> str:
    if VERBOSE_LOGGING:
        return str(exc) or exc.__class__.__name__
    return "error details hidden; run with --verbose to show the full exception"


class TextTubeCli:
    """Parses CLI arguments and launches the main TextTube application flow."""

    @staticmethod
    def parse_args() -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            description="Summarize YouTube subscriptions or one selected video."
        )
        target_group = parser.add_mutually_exclusive_group()
        target_group.add_argument(
            "--limit",
            type=int,
            default=None,
            metavar="N",
            help="maximum videos to process; 0 means unlimited",
        )
        target_group.add_argument(
            "--video",
            default="",
            metavar="URL_OR_ID",
            help="process one YouTube video instead of subscriptions",
        )
        parser.add_argument(
            "--cache",
            action="store_true",
            help="reuse and update transcript and audio caches",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="show detailed progress and errors",
        )
        return parser.parse_args()

    @classmethod
    def main(cls) -> int:
        global VERBOSE_LOGGING
        lifecycle = ApplicationLifecycle()
        lifecycle.install_signal_handlers()
        app: TextTubeApp | None = None
        try:
            args = cls.parse_args()
            ConfigLoader.apply_runtime_defaults(args)
            VERBOSE_LOGGING = args.verbose
            log("startup: parse args")
            if args.limit < 0:
                raise FatalError("--limit must be 0 or greater")
            log("startup: load config")
            app = TextTubeApp(args, lifecycle=lifecycle)
            return app.run()
        except KeyboardInterrupt:
            log("interrupt: shutting down", essential=True)
            return 130
        except FatalError as exc:
            log(f"fatal: {exc}", essential=True)
            cls.notify_run_failure(app=app, error=exc)
            return 1
        except Exception as exc:
            log(f"fatal: unexpected error: {exception_log_message(exc)}", essential=True)
            cls.notify_run_failure(app=app, error=exc)
            return 1
        finally:
            lifecycle.cleanup()
            lifecycle.restore_signal_handlers()

    @staticmethod
    def notify_run_failure(*, app: "TextTubeApp | None", error: Exception) -> None:
        if app is not None:
            app.notify_run_failure(error)

class TextTubeApp:
    """Coordinates one full TextTube invocation across setup, fetching, and delivery."""

    def __init__(self, args: argparse.Namespace, *, lifecycle: "ApplicationLifecycle"):
        self.args = args
        self.lifecycle = lifecycle
        self.paths = RuntimePaths.discover()
        self.session = AppEnvironment.import_requests().Session()
        self.lifecycle.add_cleanup(self.session.close)
        self.config = ConfigLoader.load_config(
            self.paths.google_refresh_token_path(),
        )
        prompt_path = self.paths.prompt_path()
        if not prompt_path.exists():
            raise FatalError(f"Missing summarizer prompt file: {prompt_path}")
        self.system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        if not self.system_prompt:
            raise FatalError(f"Summarizer prompt file is empty: {prompt_path}")

        self.transcript_language_preferences = (
            ConfigLoader.resolve_transcript_language_preferences()
        )
        self.youtube = YouTubeClient(self.session, self.config)
        openai_sdk = AppEnvironment.import_openai().OpenAI(
            api_key=self.config.openai_api_key,
            max_retries=0,
        )
        self.lifecycle.add_cleanup(openai_sdk.close)
        self.openai = OpenAIClient(
            openai_sdk,
            self.system_prompt,
        )
        self.telegram = TelegramClient(self.session, self.config)
        self.transcript_summarizer = TranscriptSummarizer(
            self.openai,
            self.telegram,
            self.transcript_language_preferences,
        )

    def run(self) -> int:
        log("startup: load prompt", essential=True)
        log(f"prompt: {self.paths.display_path(self.paths.prompt_path())}")
        log(
            f"openai: summary={OPENAI_SUMMARY_MODEL} "
            f"transcription={OPENAI_TRANSCRIPTION_MODEL}"
        )
        if self.transcript_language_preferences:
            log(
                "transcript languages: "
                f"{', '.join(self.transcript_language_preferences)}"
            )
        video_id = ValueParser.parse_youtube_video_id(self.args.video) if self.args.video else None
        if video_id:
            return self.run_single_video(video_id)
        return self.run_subscriptions()

    def notify_run_failure(self, error: Exception) -> None:
        message = GENERIC_RUN_FAILURE_MESSAGE
        if isinstance(error, GoogleOAuthReauthorizationRequired):
            message = GOOGLE_OAUTH_REAUTHORIZATION_MESSAGE.format(
                auth_command=GOOGLE_OAUTH_AUTH_COMMAND
            )
        try:
            self.telegram.send_run_failure_message(message)
        except Exception:
            log("telegram run failure notification failed", essential=True)

    def run_single_video(self, video_id: str) -> int:
        log(f"single video mode: {video_id}", essential=True)
        log("startup: resolve youtube token", essential=True)
        video = self.youtube.fetch_video(video_id)
        audio_cache_path = self.paths.audio_cache_path(video_id, enabled=self.args.cache)
        transcript_cache_path = self.paths.transcript_cache_path(video_id, enabled=self.args.cache)
        try:
            sent_count = (
                1
                if self.transcript_summarizer.process_video(
                    video,
                    audio_cache_path=audio_cache_path,
                    transcript_cache_path=transcript_cache_path,
                )
                else 0
            )
        except (VideoFailure, TelegramFailure) as exc:
            log(f"failed to send {video.video_id}: {exc}", essential=True)
            sent_count = 0
        log(f"sent messages: {sent_count}", essential=True)
        return 0

    def run_subscriptions(self) -> int:
        log("startup: resolve youtube token", essential=True)
        self.youtube.access_token()
        window_start, window_end = SubscriptionState.subscription_window(self.paths.state_root)
        log(
            f"subscription window utc: {window_start.isoformat()} -> {window_end.isoformat()}",
            essential=True,
        )

        sent_count = 0
        stopped_by_limit = False
        log("subscriptions mode: iterate recent videos", essential=True)
        for video in self.youtube.iter_recent_subscription_videos(window_start, window_end):
            if self.args.limit > 0 and sent_count >= self.args.limit:
                stopped_by_limit = True
                break
            try:
                audio_cache_path = self.paths.audio_cache_path(video.video_id, enabled=self.args.cache)
                transcript_cache_path = self.paths.transcript_cache_path(
                    video.video_id,
                    enabled=self.args.cache,
                )
                if self.transcript_summarizer.process_video(
                    video,
                    audio_cache_path=audio_cache_path,
                    transcript_cache_path=transcript_cache_path,
                ):
                    sent_count += 1
                    if self.args.limit > 0 and sent_count >= self.args.limit:
                        stopped_by_limit = True
                        break
            except (VideoFailure, TelegramFailure) as exc:
                log(f"failed to send {video.video_id}: {exc}", essential=True)
            except Exception as exc:
                log(
                    f"failed to process {video.video_id}: unexpected error: "
                    f"{exception_log_message(exc)}",
                    essential=True,
                )

        self.telegram.maybe_send_limit_reached_message(
            limit=self.args.limit,
            sent_count=sent_count,
            stopped_by_limit=stopped_by_limit,
        )
        state_dir = SubscriptionState.subscription_state_dir(self.paths.state_root)
        state_dir.mkdir(parents=True, exist_ok=True)
        SubscriptionState.last_subscription_window_end_path(self.paths.state_root).write_text(
            window_end.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            encoding="utf-8",
        )
        log(f"sent messages: {sent_count}", essential=True)
        return 0

class TranscriptSummarizer:
    """Coordinates transcript retrieval, fallback audio transcription, summary creation, and delivery."""

    def __init__(
        self,
        openai: "OpenAIClient",
        telegram: TelegramClient,
        transcript_language_preferences: tuple[str, ...],
    ):
        self.openai = openai
        self.telegram = telegram
        self.transcript_language_preferences = transcript_language_preferences

    @staticmethod
    def is_probable_short(video: Video) -> bool:
        return video.duration_seconds is not None and video.duration_seconds <= 180

    @staticmethod
    def exceeds_audio_transcription_limit(video: Video) -> bool:
        """Return whether audio fallback is forbidden by the duration limit."""
        return (
            video.duration_seconds is not None
            and video.duration_seconds > MAX_AUDIO_TRANSCRIPTION_DURATION_SECONDS
        )

    def process_video(
        self,
        video: Video,
        *,
        audio_cache_path: Path | None = None,
        transcript_cache_path: Path | None = None,
    ) -> bool:
        if self.is_probable_short(video):
            log(f"skip {video.video_id}: Short", essential=True)
            return False

        log(f"process {video.video_id}: {video.channel_title}: {video.title}", essential=True)
        summary = ""
        try:
            transcript_result = self.fetch_transcript(
                video,
                audio_cache_path=audio_cache_path,
                transcript_cache_path=transcript_cache_path,
            )
            log(f"process {video.video_id}: summarize", essential=True)
            summary = self.openai.summarize_transcript(
                transcript_result.text,
                language_code=transcript_result.language_code,
            )
        except VideoFailure as exc:
            log(
                f"process {video.video_id}: transcript summary unavailable: "
                f"{exception_log_message(exc)}",
                essential=True,
            )
        except Exception as exc:
            log(
                f"process {video.video_id}: unexpected summary error, using description: "
                f"{exception_log_message(exc)}",
                essential=True,
            )

        used_description = False
        if not summary:
            used_description = True
            log(f"process {video.video_id}: summarize description fallback", essential=True)
            try:
                summary = self.openai.summarize_description(video.title, video.description)
            except VideoFailure as exc:
                log(
                    f"process {video.video_id}: description summary unavailable: "
                    f"{exception_log_message(exc)}",
                    essential=True,
                )
                summary = DescriptionCleaner.clean(video.description)

        log(f"process {video.video_id}: format message")
        message = TelegramClient.format_message(video, summary)
        log(f"process {video.video_id}: send telegram", essential=True)
        self.telegram.send_message(message)
        result_label = "description fallback" if used_description else "summarized"
        log(f"sent {video.video_id}: {result_label}")
        return True

    def fetch_transcript(
        self,
        video: "Video",
        *,
        audio_cache_path: Path | None,
        transcript_cache_path: Path | None,
    ) -> "TranscriptResult":
        """Resolve a cached, native-caption, or OpenAI audio transcript."""
        if transcript_cache_path and transcript_cache_path.exists():
            cached_transcript = transcript_cache_path.read_text(encoding="utf-8").strip()
            if cached_transcript:
                log(f"transcript cache: hit {transcript_cache_path.name}")
                return TranscriptResult(text=cached_transcript)

        try:
            log(f"process {video.video_id}: native transcript", essential=True)
            transcript_result = TranscriptFetcher.fetch_transcript(
                video.video_id,
                preferred_languages=self.transcript_language_preferences,
            )
        except VideoFailure as transcript_exc:
            log(f"transcript fallback {video.video_id}: {transcript_exc}")
            if self.exceeds_audio_transcription_limit(video):
                raise VideoFailure(
                    f"{transcript_exc}; audio transcription skipped because video exceeds "
                    "60 minutes"
                ) from transcript_exc
            try:
                transcript_result = OpenAIAudioTranscriber.fetch_audio_transcript(
                    self.openai.client,
                    video.video_id,
                    audio_cache_path=audio_cache_path,
                )
            except VideoFailure as audio_exc:
                raise VideoFailure(f"{transcript_exc}; {audio_exc}") from audio_exc

        if transcript_cache_path:
            transcript = transcript_result.text.strip()
            if transcript:
                transcript_cache_path.parent.mkdir(parents=True, exist_ok=True)
                transcript_cache_path.write_text(f"{transcript}\n", encoding="utf-8")
                log(f"transcript cache: stored {transcript_cache_path.name}")
        return transcript_result

class YouTubeClient:
    """Resolves YouTube auth and exposes the app's video-fetching workflows."""

    def __init__(self, session: Any, config: Config):
        self.session = session
        self.config = config
        self._access_token: str | None = None

    def access_token(self) -> str:
        if self._access_token is None:
            self._access_token = self._refresh_access_token()
        return self._access_token

    def fetch_video(self, video_id: str) -> Video:
        log(f"video metadata: request {video_id}")
        page = self._get(
            "videos",
            {
                "part": "snippet,contentDetails",
                "id": video_id,
                "maxResults": 1,
            },
        )
        items = page.get("items", [])
        if not isinstance(items, list) or not items:
            raise FatalError(f"YouTube video not found or unavailable: {video_id}")

        item = items[0]
        if not isinstance(item, dict):
            raise FatalError(f"YouTube returned unexpected video metadata for: {video_id}")

        snippet = item.get("snippet") or {}
        content_details = item.get("contentDetails") or {}
        published_raw = str(snippet.get("publishedAt", "")).strip()
        try:
            published_at = (
                ValueParser.parse_rfc3339(published_raw)
                if published_raw
                else datetime.now(timezone.utc)
            )
        except ValueError:
            published_at = datetime.now(timezone.utc)

        tags_raw = snippet.get("tags") or []
        tags = tuple(str(tag) for tag in tags_raw if isinstance(tag, str))
        return Video(
            video_id=video_id,
            title=str(snippet.get("title", "")).strip() or video_id,
            channel_id=str(snippet.get("channelId", "")).strip(),
            channel_title=str(snippet.get("channelTitle", "")).strip() or "YouTube",
            published_at=published_at,
            duration_seconds=ValueParser.parse_iso8601_duration_seconds(
                str(content_details.get("duration", "")).strip()
            ),
            description=str(snippet.get("description", "")).strip(),
            tags=tags,
        )

    def iter_recent_subscription_videos(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> Iterator[Video]:
        seen_video_ids: set[str] = set()
        next_page_token: str | None = None
        subscription_count = 0
        playlist_count = 0

        while True:
            params: dict[str, Any] = {
                "part": "snippet",
                "mine": "true",
                "maxResults": YOUTUBE_PAGE_SIZE,
            }
            if next_page_token:
                params["pageToken"] = next_page_token

            page = self._get("subscriptions", params)
            subscriptions = self._parse_subscription_items(page.get("items", []))
            subscription_count += len(subscriptions)
            log(f"subscription page: {len(subscriptions)} channels")

            playlists = self._fetch_upload_playlists(subscriptions)
            playlist_count += len(playlists)
            log(f"upload playlists so far: {playlist_count}")

            for channel_id, (playlist_id, channel_title) in playlists.items():
                for video in self.iter_playlist_videos(
                    playlist_id,
                    channel_id,
                    channel_title,
                    window_start,
                    window_end,
                ):
                    if video.video_id in seen_video_ids:
                        continue
                    seen_video_ids.add(video.video_id)
                    yield video

            next_page_token = str(page.get("nextPageToken", "")).strip() or None
            if not next_page_token:
                log(f"subscriptions: {subscription_count}")
                log(f"upload playlists: {playlist_count}")
                return

    def iter_playlist_videos(
        self,
        uploads_playlist_id: str,
        channel_id: str,
        channel_title: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Iterator[Video]:
        next_page_token: str | None = None
        while True:
            videos, saw_older_video, next_page_token = self._fetch_playlist_video_page(
                uploads_playlist_id,
                channel_id,
                channel_title,
                window_start,
                window_end,
                next_page_token,
            )
            for video in self.enrich_video_details(videos):
                yield video

            if saw_older_video or not next_page_token:
                return

    def enrich_video_details(self, videos: list[Video]) -> list[Video]:
        by_id = {video.video_id: video for video in videos}
        if not by_id:
            return []

        for video_ids in ValueParser.chunks(list(by_id), 50):
            log(f"videos metadata: request {len(video_ids)}")
            page = self._get(
                "videos",
                {
                    "part": "snippet,contentDetails",
                    "id": ",".join(video_ids),
                    "maxResults": 50,
                },
            )
            for item in page.get("items", []):
                if not isinstance(item, dict):
                    continue
                video_id = str(item.get("id", "")).strip()
                if video_id not in by_id:
                    continue
                snippet = item.get("snippet") or {}
                content_details = item.get("contentDetails") or {}
                duration_seconds = ValueParser.parse_iso8601_duration_seconds(
                    str(content_details.get("duration", "")).strip()
                )
                tags_raw = snippet.get("tags") or []
                tags = tuple(str(tag) for tag in tags_raw if isinstance(tag, str))
                by_id[video_id] = replace(
                    by_id[video_id],
                    title=str(snippet.get("title", "")).strip() or by_id[video_id].title,
                    channel_title=str(snippet.get("channelTitle", "")).strip()
                    or by_id[video_id].channel_title,
                    description=str(snippet.get("description", "")).strip(),
                    duration_seconds=duration_seconds,
                    tags=tags,
                )

        log(f"videos metadata: enriched {len(by_id)}")
        return [by_id[video.video_id] for video in videos if video.video_id in by_id]

    def _refresh_access_token(self) -> str:
        log("oauth refresh: request")
        payload = {
            "client_id": self.config.google_client_id,
            "client_secret": self.config.google_client_secret,
            "refresh_token": self.config.google_refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            data = HttpJsonClient.request_json(
                self.session,
                "POST",
                GOOGLE_TOKEN_URL,
                timeout=REQUEST_TIMEOUT_SECONDS,
                data=payload,
            )
        except FatalError as exc:
            if GOOGLE_OAUTH_REAUTHORIZATION_ERROR_CODE in str(exc):
                raise GoogleOAuthReauthorizationRequired(
                    "Google OAuth authorization expired or was revoked"
                ) from exc
            raise
        token = str(data.get("access_token", "")).strip()
        if not token:
            raise FatalError("Google OAuth refresh response did not include an access token")
        log("oauth refresh: ok")
        return token

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.access_token()}"}
        url = f"{YOUTUBE_API_BASE_URL}/{endpoint}"
        return HttpJsonClient.request_json(
            self.session,
            "GET",
            url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers=headers,
            params=params,
        )

    def _fetch_upload_playlists(
        self,
        subscriptions: list[tuple[str, str]],
    ) -> dict[str, tuple[str, str]]:
        channel_titles = {channel_id: title for channel_id, title in subscriptions}
        playlist_by_channel: dict[str, tuple[str, str]] = {}

        for channel_ids in ValueParser.chunks(list(channel_titles), 50):
            log(f"channels metadata: request {len(channel_ids)}")
            page = self._get(
                "channels",
                {
                    "part": "contentDetails,snippet",
                    "id": ",".join(channel_ids),
                    "maxResults": 50,
                },
            )
            for item in page.get("items", []):
                if not isinstance(item, dict):
                    continue
                channel_id = str(item.get("id", "")).strip()
                snippet = item.get("snippet") or {}
                title = str(snippet.get("title", "")).strip() or channel_titles.get(
                    channel_id,
                    channel_id,
                )
                content_details = item.get("contentDetails") or {}
                related = content_details.get("relatedPlaylists") or {}
                uploads_playlist_id = str(related.get("uploads", "")).strip()
                if channel_id and uploads_playlist_id:
                    playlist_by_channel[channel_id] = (uploads_playlist_id, title)

        log(f"channels metadata: resolved {len(playlist_by_channel)} upload playlists")
        return playlist_by_channel

    @staticmethod
    def _parse_subscription_items(items: Any) -> list[tuple[str, str]]:
        subscriptions: list[tuple[str, str]] = []
        if not isinstance(items, list):
            return subscriptions

        for item in items:
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            resource_id = snippet.get("resourceId") or {}
            channel_id = str(resource_id.get("channelId", "")).strip()
            channel_title = str(snippet.get("title", "")).strip() or channel_id
            if channel_id:
                subscriptions.append((channel_id, channel_title))

        return subscriptions

    def _fetch_playlist_video_page(
        self,
        uploads_playlist_id: str,
        channel_id: str,
        channel_title: str,
        window_start: datetime,
        window_end: datetime,
        page_token: str | None,
    ) -> tuple[list[Video], bool, str | None]:
        videos: list[Video] = []
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": YOUTUBE_PAGE_SIZE,
        }
        if page_token:
            params["pageToken"] = page_token

        log(f"playlist items: request {channel_title} page={page_token or 'first'}")
        page = self._get("playlistItems", params)
        saw_older_video = False

        for item in page.get("items", []):
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            video_id = str(content_details.get("videoId", "")).strip()
            published_raw = str(content_details.get("videoPublishedAt") or snippet.get("publishedAt") or "")
            if not video_id or not published_raw:
                continue

            try:
                published_at = ValueParser.parse_rfc3339(published_raw)
            except ValueError:
                log(f"skip {video_id}: invalid published timestamp")
                continue

            if published_at >= window_end:
                continue

            if published_at < window_start:
                saw_older_video = True
                continue

            videos.append(
                Video(
                    video_id=video_id,
                    title=str(snippet.get("title", "")).strip() or video_id,
                    channel_id=channel_id,
                    channel_title=channel_title,
                    published_at=published_at,
                )
            )

        next_page_token = str(page.get("nextPageToken", "")).strip() or None
        log(
            f"playlist items: {channel_title} yielded {len(videos)} recent, "
            f"older_seen={'yes' if saw_older_video else 'no'}"
        )
        return videos, saw_older_video, next_page_token

class TranscriptFetcher:
    """Fetches native YouTube transcripts and ranks preferred language matches."""

    @staticmethod
    def transcript_language_match_rank(
        candidate_language_code: str,
        preferred_languages: tuple[str, ...],
    ) -> tuple[int, int] | None:
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

    @staticmethod
    def select_original_audio_language_rank(
        candidates: list[Any],
        preferred_languages: tuple[str, ...],
    ) -> tuple[int, int] | None:
        original_audio_ranks: list[tuple[int, int]] = []
        for transcript in candidates:
            if not bool(getattr(transcript, "is_generated", False)):
                continue
            language_code = str(getattr(transcript, "language_code", "")).strip()
            match_rank = TranscriptFetcher.transcript_language_match_rank(
                language_code,
                preferred_languages,
            )
            if match_rank is not None:
                original_audio_ranks.append(match_rank)
        if not original_audio_ranks:
            return None
        return min(original_audio_ranks)

    @staticmethod
    def order_transcript_candidates(
        candidates: list[Any],
        preferred_languages: tuple[str, ...],
    ) -> list[Any]:
        if not preferred_languages:
            return candidates

        original_audio_rank = TranscriptFetcher.select_original_audio_language_rank(
            candidates,
            preferred_languages,
        )
        ranked_candidates: list[tuple[tuple[int, int], int, Any]] = []
        fallback_candidates: list[tuple[int, Any]] = []
        for original_index, transcript in enumerate(candidates):
            language_code = str(getattr(transcript, "language_code", "")).strip()
            match_rank = TranscriptFetcher.transcript_language_match_rank(
                language_code,
                preferred_languages,
            )
            if match_rank is None:
                fallback_candidates.append((original_index, transcript))
                continue
            if original_audio_rank is not None and match_rank == original_audio_rank:
                match_rank = (-1, match_rank[1])
            ranked_candidates.append((match_rank, original_index, transcript))

        if not ranked_candidates:
            return candidates

        ranked_candidates.sort(key=lambda item: (item[0], item[1]))
        ordered = [transcript for _, _, transcript in ranked_candidates]
        ordered.extend(transcript for _, transcript in fallback_candidates)
        return ordered

    @staticmethod
    def fetch_transcript(
        video_id: str,
        preferred_languages: tuple[str, ...] = (),
    ) -> "TranscriptResult":
        log(f"transcript native: list {video_id}")
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
                f"transcript unavailable: {exception_log_message(exc)}"
            ) from exc
        except Exception as exc:
            raise VideoFailure(
                f"transcript unavailable: unexpected error: {exception_log_message(exc)}"
            ) from exc

        candidates = list(transcript_list)
        if not candidates:
            raise VideoFailure("transcript unavailable: no transcripts found")
        log(f"transcript native: candidates {video_id}: {len(candidates)}")
        if preferred_languages:
            log(
                "transcript native: preferred languages "
                f"{video_id}: {', '.join(preferred_languages)}"
            )
            original_audio_rank = TranscriptFetcher.select_original_audio_language_rank(
                candidates,
                preferred_languages,
            )
            if original_audio_rank is not None:
                log(
                    "transcript native: original audio language matched preference "
                    f"{video_id}: {preferred_languages[original_audio_rank[0]]}"
                )
        candidates = TranscriptFetcher.order_transcript_candidates(
            candidates,
            preferred_languages,
        )

        last_error: Exception | None = None

        for transcript in candidates:
            try:
                language_code = str(getattr(transcript, "language_code", "")).strip() or "unknown"
                generated = bool(getattr(transcript, "is_generated", False))
                log(
                    f"transcript native: fetch {video_id}: "
                    f"lang={language_code} generated={'yes' if generated else 'no'}"
                )
                fetched = transcript.fetch()
                lines: list[str] = []
                for item in fetched:
                    if isinstance(item, dict):
                        text = str(item.get("text", "")).strip()
                    else:
                        text = str(getattr(item, "text", "")).strip()
                    if text:
                        lines.append(text)
                text = "\n".join(lines).strip()
                if text:
                    log(f"transcript native: ok {video_id}: {len(lines)} lines")
                    return TranscriptResult(text=text, language_code=language_code)
            except transcript_errors as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc

        if last_error:
            raise VideoFailure(
                f"transcript unavailable: {exception_log_message(last_error)}"
            ) from last_error
        raise VideoFailure("transcript unavailable: transcript was empty")

class OpenAIClient:
    """Calls OpenAI's low-cost text model for transcript and description summaries."""

    def __init__(self, client: Any, system_prompt: str):
        self.client = client
        self.system_prompt = system_prompt

    def generate(self, prompt: str, *, instructions: str) -> str:
        """Generate one plain-text response through the official Responses client."""
        openai_module = AppEnvironment.import_openai()
        try:
            response = self.client.with_options(
                timeout=OPENAI_SUMMARY_TIMEOUT_SECONDS,
            ).responses.create(
                model=OPENAI_SUMMARY_MODEL,
                instructions=instructions,
                input=prompt,
                max_output_tokens=500,
                store=False,
            )
        except openai_module.OpenAIError as exc:
            raise VideoFailure(
                f"OpenAI request failed for {OPENAI_SUMMARY_MODEL}: "
                f"{exception_log_message(exc)}"
            ) from exc

        generated = str(response.output_text or "").strip()
        if not generated:
            raise VideoFailure(f"OpenAI returned no text for {OPENAI_SUMMARY_MODEL}")
        return generated

    def summarize_transcript(self, transcript: str, *, language_code: str = "") -> str:
        """Summarize transcript text using the canonical repository prompt."""
        prompt = self.build_summary_prompt(transcript, language_code=language_code)
        if not prompt:
            raise VideoFailure("summary unavailable: transcript was empty")
        return self.summarize(prompt, instructions=self.system_prompt)

    def summarize_description(self, title: str, description: str) -> str:
        """Summarize and clean video metadata when transcript summarization fails."""
        cleaned_description = DescriptionCleaner.prepare_for_model(description)
        if not cleaned_description:
            raise VideoFailure("description summary unavailable: description was empty")
        prompt = f"Title: {title.strip()}\n\nDescription:\n{cleaned_description}"
        summary = self.summarize(prompt, instructions=DESCRIPTION_SUMMARIZER_PROMPT)
        link_free_summary = DescriptionCleaner.prepare_for_model(summary)
        if not link_free_summary:
            raise VideoFailure("description summary unavailable: only links remained")
        return re.sub(r"\s+", " ", link_free_summary).strip()

    def summarize(self, prompt: str, *, instructions: str) -> str:
        """Run one timed summary request and normalize its output."""
        started_at = time_module.monotonic()
        try:
            log(f"summary: request model={OPENAI_SUMMARY_MODEL}")
            summary = self.generate(prompt, instructions=instructions).strip()
            if not summary:
                raise VideoFailure("summary unavailable: model returned an empty response")
            log(f"summary: ok model={OPENAI_SUMMARY_MODEL}")
            log(f"summary result model={OPENAI_SUMMARY_MODEL}:\n{summary}")
            return summary
        except VideoFailure as exc:
            log(
                f"summary: failed model={OPENAI_SUMMARY_MODEL}: "
                f"{exception_log_message(exc)}"
            )
            raise VideoFailure(f"summary unavailable: {exc}") from exc
        finally:
            log(
                "summary: duration "
                f"model={OPENAI_SUMMARY_MODEL}: elapsed="
                f"{format_duration(time_module.monotonic() - started_at)}",
                essential=True,
            )

    @staticmethod
    def build_summary_prompt(transcript: str, *, language_code: str = "") -> str:
        """Attach a language hint to nonempty transcript text."""
        cleaned_transcript = transcript.strip()
        if not cleaned_transcript:
            return ""
        normalized_language_code = language_code.strip().lower()
        if not normalized_language_code:
            return cleaned_transcript
        return f"Summary language code: {normalized_language_code}.\n\n{cleaned_transcript}"


class DescriptionCleaner:
    """Removes links and obvious channel boilerplate for a last-resort fallback."""

    LINK_PATTERN = re.compile(
        r"(?i)(?:https?://|www\.)\S+|"
        r"\b[\w.-]+\.(?:com|org|net|io|co|tv|me|gg|ly)(?:/\S*)?"
    )
    UNRELATED_PATTERN = re.compile(
        r"(?i)\b(?:subscribe|sponsor(?:ed)?|affiliate|discount|coupon|promo code|"
        r"merch|patreon|newsletter|follow (?:me|us)|social media|contact|business inquiries)\b"
    )

    @classmethod
    def prepare_for_model(cls, description: str) -> str:
        """Strip links before sending description text to OpenAI."""
        without_links = cls.LINK_PATTERN.sub("", html.unescape(description))
        return "\n".join(line.strip() for line in without_links.splitlines() if line.strip())

    @classmethod
    def clean(cls, description: str) -> str:
        """Return a concise link-free description if OpenAI is unavailable."""
        prepared = cls.prepare_for_model(description)
        relevant_lines: list[str] = []
        for line in prepared.splitlines():
            if cls.UNRELATED_PATTERN.search(line):
                continue
            if re.fullmatch(r"[\W_]*", line):
                continue
            relevant_lines.append(line)
            if len(" ".join(relevant_lines)) >= 600:
                break

        cleaned = " ".join(relevant_lines)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            return "No essential facts."
        if len(cleaned) > 700:
            cleaned = cleaned[:697].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."
        return cleaned

class TelegramClient:
    """Formats and sends Telegram messages, including run-level notices."""

    def __init__(self, session: Any, config: Config):
        self.session = session
        self.config = config

    @staticmethod
    def format_message(video: Video, body: str) -> str:
        link = f"https://youtu.be/{video.video_id}"
        header = f"<i>{html.escape(video.channel_title)}</i>: {html.escape(video.title)}"
        escaped_body = html.escape(body.strip())
        message = f"{header}\n\n{escaped_body}\n\n{link}"
        if len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return message

        fixed_length = len(header) + len(link) + 4
        available_body_length = TELEGRAM_MAX_MESSAGE_LENGTH - fixed_length
        if available_body_length <= 20:
            return message[: TELEGRAM_MAX_MESSAGE_LENGTH - 1]

        kept_lines: list[str] = []
        used = 0
        for line in escaped_body.splitlines():
            addition = len(line) + (1 if kept_lines else 0)
            if used + addition > available_body_length - 15:
                break
            kept_lines.append(line)
            used += addition

        trimmed_body = "\n".join(kept_lines).strip()
        if not trimmed_body:
            trimmed_body = escaped_body[: available_body_length - 15].strip()
        return f"{header}\n\n{trimmed_body}\n\n{link}"

    def send_message(self, text: str) -> None:
        requests_module = AppEnvironment.import_requests()
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
        }

        try:
            log("telegram send: preview=off")
            response = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests_module.RequestException as exc:
            raise TelegramFailure(f"Telegram send failed: {exc}") from exc

        if response.status_code != 200:
            detail = response.text[:300].replace("\n", " ")
            raise TelegramFailure(f"Telegram send failed: HTTP {response.status_code}: {detail}")
        log("telegram send: ok")

    def send_run_failure_message(self, message: str) -> None:
        log("run failure: send telegram", essential=True)
        self.send_message(message)

    def maybe_send_limit_reached_message(
        self,
        *,
        limit: int,
        sent_count: int,
        stopped_by_limit: bool,
    ) -> None:
        if not stopped_by_limit or limit != DEFAULT_VIDEO_LIMIT or sent_count < DEFAULT_VIDEO_LIMIT:
            return

        log(f"limit reached: send telegram for {sent_count} messages", essential=True)
        self.send_message(
            f"TextTube stopped after reaching the {DEFAULT_VIDEO_LIMIT}-video limit for this run."
        )

class OpenAIAudioTranscriber:
    """Downloads YouTube audio and transcribes resource-sized chunks with OpenAI."""

    @staticmethod
    def video_audio_cache_path(state_root: Path, video_id: str) -> Path:
        return state_root / "var" / CACHE_DIR_NAME / f"{video_id}{AUDIO_CACHE_EXTENSION}"

    @staticmethod
    def fetch_audio_transcript(
        client: Any,
        video_id: str,
        *,
        audio_cache_path: Path | None = None,
    ) -> "TranscriptResult":
        log(f"transcript audio: start {video_id} model={OPENAI_TRANSCRIPTION_MODEL}")
        started_at = time_module.perf_counter()

        try:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            with tempfile.TemporaryDirectory(prefix="texttube-audio-") as temp_dir:
                temp_path = Path(temp_dir)
                if audio_cache_path and audio_cache_path.exists():
                    audio_path = audio_cache_path
                    log(f"transcript audio: cache hit {audio_cache_path.name}")
                else:
                    output_template = str(temp_path / "audio.%(ext)s")
                    command = [
                        sys.executable,
                        "-m",
                        "yt_dlp",
                        "--no-playlist",
                        "--extract-audio",
                        "--audio-format",
                        "m4a",
                        "--output",
                        output_template,
                        video_url,
                    ]
                    try:
                        log(f"transcript audio: yt-dlp {video_id}")
                        completed = subprocess.run(
                            command,
                            check=True,
                            capture_output=True,
                            text=True,
                            timeout=3600,
                        )
                    except subprocess.TimeoutExpired as exc:
                        raise VideoFailure("audio transcript unavailable: audio download timed out") from exc
                    except subprocess.CalledProcessError as exc:
                        detail = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()[-1:]
                        reason = detail[0] if detail else str(exc)
                        raise VideoFailure(
                            f"audio transcript unavailable: yt-dlp failed: {reason[:300]}"
                        ) from exc

                    audio_files = [path for path in temp_path.iterdir() if path.is_file()]
                    if not audio_files:
                        detail = completed.stderr.strip().splitlines()[-1:]
                        reason = detail[0] if detail else "yt-dlp did not create an audio file"
                        raise VideoFailure(f"audio transcript unavailable: {reason[:300]}")

                    downloaded_audio_path = max(audio_files, key=lambda path: path.stat().st_size)
                    log(f"transcript audio: downloaded {downloaded_audio_path.name}")
                    if audio_cache_path:
                        audio_cache_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(downloaded_audio_path, audio_cache_path)
                        audio_path = audio_cache_path
                        log(f"transcript audio: cached {audio_cache_path.name}")
                    else:
                        audio_path = downloaded_audio_path

                chunk_dir = Path(temp_dir) / "chunks"
                chunk_dir.mkdir()
                segment_command = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(audio_path),
                    "-f",
                    "segment",
                    "-segment_time",
                    "300",
                    "-c",
                    "copy",
                    str(chunk_dir / "chunk-%03d.m4a"),
                ]
                try:
                    log(f"transcript audio: ffmpeg segment {video_id}")
                    subprocess.run(
                        segment_command,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=1800,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise VideoFailure("audio transcript unavailable: audio chunking timed out") from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()[-1:]
                    reason = detail[0] if detail else str(exc)
                    raise VideoFailure(
                        f"audio transcript unavailable: ffmpeg failed: {reason[:300]}"
                    ) from exc

                chunk_paths = sorted(chunk_dir.glob("chunk-*.m4a")) or [audio_path]
                text = OpenAIAudioTranscriber.transcribe_chunks(
                    client,
                    chunk_paths,
                )
                log(f"transcript audio: ok {video_id}: openai {len(chunk_paths)} chunks")
                log(f"transcript audio result {video_id}:\n{text}")
                return TranscriptResult(text=text)
        finally:
            duration = format_duration(time_module.perf_counter() - started_at)
            log(f"transcript audio: duration {video_id}: {duration}", essential=True)

    @staticmethod
    def transcribe_chunks(client: Any, chunk_paths: list[Path]) -> str:
        """Transcribe audio chunks sequentially to minimize container resource use."""
        openai_module = AppEnvironment.import_openai()
        results: list[str] = []
        for chunk_path in chunk_paths:
            log(
                f"transcript audio: openai chunk {chunk_path.name} "
                f"model={OPENAI_TRANSCRIPTION_MODEL}"
            )
            try:
                with chunk_path.open("rb") as audio_file:
                    response = client.with_options(
                        timeout=OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS,
                    ).audio.transcriptions.create(
                        model=OPENAI_TRANSCRIPTION_MODEL,
                        file=audio_file,
                        response_format="json",
                    )
            except openai_module.OpenAIError as exc:
                raise VideoFailure(
                    "audio transcript unavailable: OpenAI request failed: "
                    f"{exception_log_message(exc)}"
                ) from exc

            text = str(response.text or "").strip()
            if not text:
                raise VideoFailure("audio transcript unavailable: OpenAI returned no text")
            results.append(text)

        text = "\n".join(part for part in results if part).strip()
        if not text:
            raise VideoFailure("audio transcript unavailable: OpenAI returned no text")
        return text

class AppEnvironment:
    """Resolves repository and runtime home paths for the active process."""

    @staticmethod
    def app_root() -> Path:
        return Path(__file__).resolve().parent

    @staticmethod
    def texttube_home() -> Path:
        configured = os.environ.get("TEXTTUBE_HOME", "").strip()
        return Path(configured).expanduser().resolve() if configured else AppEnvironment.app_root()

    @staticmethod
    def import_requests() -> Any:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise FatalError(
                "Missing Python dependency: requests. Install requirements.txt or use Docker."
            ) from exc
        return requests

    @staticmethod
    def import_openai() -> Any:
        try:
            import openai
        except ModuleNotFoundError as exc:
            raise FatalError(
                "Missing Python dependency: openai. Install requirements.txt or use Docker."
            ) from exc
        return openai

class ValueParser:
    """Parses CLI, timestamps, IDs, and other small normalized value types."""

    @staticmethod
    def parse_rfc3339(value: str) -> datetime:
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def parse_youtube_video_id(value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise FatalError("--video must include a YouTube video URL or ID")

        patterns = [
            r"(?:youtube\.com|youtube-nocookie\.com)/watch\?[^#]*\bv=([^&#]+)",
            r"(?:youtube\.com|youtube-nocookie\.com)/(?:embed|shorts|live)/([^?&#/]+)",
            r"youtu\.be/([^?&#/]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, candidate)
            if match:
                return ValueParser.clean_youtube_video_id(match.group(1))

        return ValueParser.clean_youtube_video_id(candidate)

    @staticmethod
    def clean_youtube_video_id(value: str) -> str:
        video_id = value.strip()
        video_id = video_id.split("?", 1)[0].split("&", 1)[0].split("#", 1)[0].strip("/")
        if not re.fullmatch(r"[\w-]{11}", video_id):
            raise FatalError(f"Invalid YouTube video URL or ID: {value}")
        return video_id

    @staticmethod
    def parse_iso8601_duration_seconds(value: str) -> int | None:
        match = re.fullmatch(
            r"P(?:(?P<days>\d+)D)?"
            r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
            value,
        )
        if not match:
            return None
        days = int(match.group("days") or 0)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = int(match.group("seconds") or 0)
        return days * 86400 + hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def chunks(values: list[str], size: int) -> list[list[str]]:
        return [values[index : index + size] for index in range(0, len(values), size)]

class ConfigLoader:
    """Loads process configuration and the volume-backed Google refresh token."""

    @staticmethod
    def parse_optional_bool(
        values: dict[str, str],
        key: str,
    ) -> bool | None:
        raw_value = values.get(key, "").strip()
        if not raw_value:
            return None
        return raw_value == "true"

    @staticmethod
    def parse_optional_int(
        values: dict[str, str],
        key: str,
    ) -> int | None:
        raw_value = values.get(key, "").strip()
        if not raw_value:
            return None
        try:
            return int(raw_value)
        except ValueError as exc:
            raise FatalError(f"Invalid integer configuration for {key}: {raw_value}") from exc

    @staticmethod
    def apply_runtime_defaults(args: argparse.Namespace) -> None:
        values = dict(os.environ)

        configured_limit = ConfigLoader.parse_optional_int(values, "TEXTTUBE_LIMIT")
        if args.limit is None:
            args.limit = configured_limit if configured_limit is not None else DEFAULT_VIDEO_LIMIT

        configured_verbose = ConfigLoader.parse_optional_bool(values, "TEXTTUBE_VERBOSE")
        args.verbose = args.verbose or (
            configured_verbose if configured_verbose is not None else False
        )

    @staticmethod
    def require_env(values: dict[str, str], key: str) -> str:
        value = values.get(key, "").strip()
        if not value:
            raise FatalError(f"Missing required configuration: {key}")
        return value

    @staticmethod
    def load_config(google_refresh_token_path: Path) -> Config:
        values = dict(os.environ)
        return Config(
            openai_api_key=ConfigLoader.require_env(values, "OPENAI_API_KEY"),
            telegram_bot_token=ConfigLoader.require_env(values, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=ConfigLoader.require_env(values, "TELEGRAM_CHAT_ID"),
            google_client_id=ConfigLoader.require_env(values, "GOOGLE_OAUTH_CLIENT_ID"),
            google_client_secret=ConfigLoader.require_env(
                values,
                "GOOGLE_OAUTH_CLIENT_SECRET",
            ),
            google_refresh_token=ConfigLoader.read_google_refresh_token(
                google_refresh_token_path
            ),
        )

    @staticmethod
    def read_google_refresh_token(path: Path) -> str:
        if not path.exists():
            raise FatalError(
                "Google OAuth authorization is missing. Run "
                f"`{GOOGLE_OAUTH_AUTH_COMMAND}`."
            )
        try:
            refresh_token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise FatalError(f"Cannot read Google OAuth authorization: {exc}") from exc
        if not refresh_token:
            raise FatalError(
                "Google OAuth authorization is empty. Run "
                f"`{GOOGLE_OAUTH_AUTH_COMMAND}`."
            )
        return refresh_token

    @staticmethod
    def parse_transcript_language_preferences(raw_value: str) -> tuple[str, ...]:
        preferences: list[str] = []
        for raw_part in raw_value.split(TRANSCRIPT_LANGUAGE_SEPARATOR):
            language_code = raw_part.strip().lower()
            if not language_code:
                continue
            if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language_code):
                raise FatalError(
                    f"Invalid transcript language preference '{raw_part.strip()}'. "
                    "Use comma-separated language codes such as en,en-us,ru."
                )
            if language_code not in preferences:
                preferences.append(language_code)
        return tuple(preferences)

    @staticmethod
    def resolve_transcript_language_preferences() -> tuple[str, ...]:
        configured = os.environ.get("TRANSCRIPT_LANGUAGES", "").strip()
        if configured:
            return ConfigLoader.parse_transcript_language_preferences(configured)
        return ()

class SubscriptionState:
    """Tracks the persisted subscription window cutoff between runs."""

    @staticmethod
    def subscription_state_dir(state_root: Path) -> Path:
        return state_root / "var" / SUBSCRIPTION_STATE_DIR_NAME

    @staticmethod
    def last_subscription_window_end_path(state_root: Path) -> Path:
        return SubscriptionState.subscription_state_dir(state_root) / (
            LAST_SUBSCRIPTION_WINDOW_END_FILE
        )

    @staticmethod
    def subscription_window(state_root: Path) -> tuple[datetime, datetime]:
        window_end = datetime.now(timezone.utc).replace(microsecond=0)
        state_path = SubscriptionState.last_subscription_window_end_path(state_root)
        if not state_path.exists():
            window_start = None
        else:
            value = state_path.read_text(encoding="utf-8").strip()
            if not value:
                window_start = None
            else:
                try:
                    window_start = ValueParser.parse_rfc3339(value)
                except ValueError as exc:
                    raise FatalError(f"Invalid subscription state file {state_path}: {exc}") from exc
        if window_start is None:
            window_start = window_end - timedelta(days=1)
        if window_start >= window_end:
            raise FatalError(
                "Last subscription window end must be earlier than the current run time. "
                f"Delete {SubscriptionState.last_subscription_window_end_path(state_root)} "
                "to reset the schedule state."
            )
        return window_start, window_end

@dataclass(frozen=True)

class RuntimePaths:
    """Centralizes runtime file locations and optional per-video cache paths."""

    code_root: Path
    state_root: Path

    @classmethod
    def discover(cls) -> "RuntimePaths":
        code_root = AppEnvironment.app_root()
        state_root = AppEnvironment.texttube_home()
        return cls(
            code_root=code_root,
            state_root=state_root,
        )

    def prompt_path(self) -> Path:
        configured = os.environ.get("SUMMARIZER_MD", "").strip()
        if not configured:
            return self.code_root / "SUMMARIZER.md"

        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate
        return self.code_root / candidate

    def display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.code_root))
        except ValueError:
            return str(path)

    def audio_cache_path(self, video_id: str, *, enabled: bool) -> Path | None:
        if not enabled:
            return None
        return OpenAIAudioTranscriber.video_audio_cache_path(self.state_root, video_id)

    def transcript_cache_path(self, video_id: str, *, enabled: bool) -> Path | None:
        if not enabled:
            return None
        return self.state_root / "var" / CACHE_DIR_NAME / f"{video_id}{TRANSCRIPT_CACHE_EXTENSION}"

    def google_refresh_token_path(self) -> Path:
        return (
            self.state_root
            / "var"
            / SUBSCRIPTION_STATE_DIR_NAME
            / GOOGLE_OAUTH_REFRESH_TOKEN_FILE
        )

class HttpJsonClient:
    """Wraps one-attempt JSON HTTP requests with consistent fatal errors."""

    @staticmethod
    def request_json(
        session: Any,
        method: str,
        url: str,
        *,
        timeout: int,
        expected_status: int = 200,
        **kwargs: Any,
    ) -> dict[str, Any]:
        requests_module = AppEnvironment.import_requests()
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except requests_module.RequestException as exc:
            raise FatalError(f"Request failed for {url}: {exc}") from exc

        if response.status_code != expected_status:
            detail = response.text[:500].replace("\n", " ")
            raise FatalError(f"Request failed for {url}: HTTP {response.status_code}: {detail}")

        try:
            parsed = response.json()
        except json.JSONDecodeError as exc:
            raise FatalError(f"Request returned invalid JSON for {url}") from exc
        if not isinstance(parsed, dict):
            raise FatalError(f"Request returned unexpected JSON for {url}")
        return parsed

class ApplicationLifecycle:
    """Owns process signal handling and best-effort LIFO cleanup for one CLI run."""

    def __init__(self) -> None:
        self._cleanup_callbacks: list[tuple[str, Any]] = []
        self._installed_handlers: list[tuple[int, Any]] = []

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(sig)
            signal.signal(sig, self._handle_signal)
            self._installed_handlers.append((sig, previous))

    def restore_signal_handlers(self) -> None:
        while self._installed_handlers:
            sig, previous = self._installed_handlers.pop()
            signal.signal(sig, previous)

    def add_cleanup(self, callback: Any) -> None:
        name = getattr(callback, "__qualname__", getattr(callback, "__name__", repr(callback)))
        self._cleanup_callbacks.append((name, callback))

    def cleanup(self) -> None:
        while self._cleanup_callbacks:
            name, callback = self._cleanup_callbacks.pop()
            try:
                callback()
            except Exception as exc:
                log(f"cleanup: {name}: {exception_log_message(exc)}", essential=True)

    @staticmethod
    def _handle_signal(signum: int, _frame: Any) -> None:
        signal_name = signal.Signals(signum).name
        log(f"interrupt: received {signal_name}", essential=True)
        raise KeyboardInterrupt

class FatalError(Exception):
    """Fatal setup or API failure that should stop the whole run."""

class GoogleOAuthReauthorizationRequired(FatalError):
    """Google OAuth failure that requires the operator to authorize TextTube again."""

class VideoFailure(Exception):
    """Per-video failure that should still allow later videos to continue."""

class TelegramFailure(Exception):
    """Telegram delivery failure for a single outbound message."""

@dataclass(frozen=True)
class Config:
    """Runtime secrets and API credentials for one TextTube invocation."""

    openai_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str

@dataclass(frozen=True)
class Video:
    """Normalized YouTube video metadata used throughout processing."""

    video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: datetime
    duration_seconds: int | None = None
    description: str = ""
    tags: tuple[str, ...] = ()

@dataclass(frozen=True)
class TranscriptResult:
    """Transcript text plus the best available language hint for summarization."""

    text: str
    language_code: str = ""

if __name__ == "__main__":
    raise SystemExit(TextTubeCli.main())
