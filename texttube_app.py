"""Application entrypoint for TextTube.

This file owns configuration, YouTube API access, transcript fetching, the
local mlx-whisper helper server, Ollama calls, Telegram delivery, command-line
behavior, run-level failure notifications, and subscription run state.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time as time_module
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib import error, request
from urllib.parse import urlparse

YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "gemma4:e4b-mlx"
MLX_WHISPER_HOST = "127.0.0.1"
MLX_WHISPER_PORT = 50061
MLX_WHISPER_BASE_URL = f"http://{MLX_WHISPER_HOST}:{MLX_WHISPER_PORT}"
REQUEST_TIMEOUT_SECONDS = 30
MLX_TRANSCRIBE_WORKERS = 4
LOCAL_MODEL_TIMEOUT_SECONDS = 1800
YOUTUBE_PAGE_SIZE = 50
MLX_WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
CACHE_DIR_NAME = "cache"
AUDIO_CACHE_EXTENSION = ".m4a"
TRANSCRIPT_CACHE_EXTENSION = ".txt"
VERBOSE_LOGGING = False
DEFAULT_VIDEO_LIMIT = 100
SUBSCRIPTION_STATE_DIR_NAME = "state"
LAST_SUBSCRIPTION_WINDOW_END_FILE = "last_subscription_window_end_utc.txt"
MLX_WHISPER_LOG_FILE_NAME = "mlx-whisper-run.log"
MLX_READY_TIMEOUT_SECONDS = 120
TRANSCRIPT_LANGUAGE_SEPARATOR = ","


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
        parser = argparse.ArgumentParser(add_help=False)
        target_group = parser.add_mutually_exclusive_group()
        target_group.add_argument("--limit", type=int, default=None)
        target_group.add_argument("--video", default="")
        parser.add_argument("--cache", action="store_true")
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--reset-cutoff", action="store_true")
        return parser.parse_args()

    @classmethod
    def main(cls) -> int:
        global VERBOSE_LOGGING
        lifecycle = ApplicationLifecycle()
        lifecycle.install_signal_handlers()
        app: TextTubeApp | None = None
        try:
            args = cls.parse_args()
            ConfigLoader.apply_runtime_defaults(args, RuntimePaths.discover().secrets_path)
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
            cls.notify_run_failure(app=app)
            return 1
        except Exception as exc:
            log(f"fatal: unexpected error: {exception_log_message(exc)}", essential=True)
            cls.notify_run_failure(app=app)
            return 1
        finally:
            lifecycle.cleanup()
            lifecycle.restore_signal_handlers()

    @staticmethod
    def notify_run_failure(*, app: "TextTubeApp | None") -> None:
        if app is not None:
            app.notify_run_failure()

class TextTubeApp:
    """Coordinates one full TextTube invocation across setup, fetching, and delivery."""

    def __init__(self, args: argparse.Namespace, *, lifecycle: "ApplicationLifecycle"):
        self.args = args
        self.lifecycle = lifecycle
        self.paths = RuntimePaths.discover()
        self.session = AppEnvironment.import_requests().Session()
        self.lifecycle.add_cleanup(self.session.close)
        self.allow_manual_access_token = os.environ.get("TEXTTUBE_MANUAL_RUN", "").strip() == "1"
        if self.args.reset_cutoff and not self.allow_manual_access_token:
            raise FatalError("--reset-cutoff is available only for repo checkout manual runs.")
        if self.args.reset_cutoff and self.args.video:
            raise FatalError("--reset-cutoff applies only to subscription runs, not --video.")
        self.config = ConfigLoader.load_config(
            self.paths.secrets_path,
            allow_manual_access_token=self.allow_manual_access_token,
        )
        prompt_path = self.paths.prompt_path()
        if not prompt_path.exists():
            raise FatalError(f"Missing summarizer prompt file: {prompt_path}")
        self.system_prompt = prompt_path.read_text(encoding="utf-8").strip()
        if not self.system_prompt:
            raise FatalError(f"Summarizer prompt file is empty: {prompt_path}")

        self.model = ConfigLoader.resolve_ollama_model(self.paths.secrets_path)
        self.mlx_whisper_model = ConfigLoader.resolve_mlx_whisper_model(self.paths.secrets_path)
        self.transcript_language_preferences = ConfigLoader.resolve_transcript_language_preferences(
            self.paths.secrets_path
        )
        self.mlx_whisper_manager = LazyMlxWhisperManager(self.paths.state_root)
        self.lifecycle.add_cleanup(self.mlx_whisper_manager.stop)
        self.youtube = YouTubeClient(self.session, self.config)
        self.ollama = OllamaClient(self.session, self.model, self.system_prompt)
        self.telegram = TelegramClient(self.session, self.config)
        self.transcript_summarizer = TranscriptSummarizer(
            self.ollama,
            self.telegram,
            self.mlx_whisper_model,
            self.mlx_whisper_manager,
            self.transcript_language_preferences,
        )

    def run(self) -> int:
        log("startup: load prompt", essential=True)
        log(f"prompt: {self.paths.display_path(self.paths.prompt_path())}")
        log(f"model: {self.model}")
        if self.transcript_language_preferences:
            log(
                "transcript languages: "
                f"{', '.join(self.transcript_language_preferences)}"
            )
        log(
            "mlx whisper: "
            f"{MLX_WHISPER_BASE_URL} model={self.mlx_whisper_model} "
            f"transcribe_workers={MLX_TRANSCRIBE_WORKERS}"
        )
        log("startup: warm ollama model", essential=True)
        self.ollama.preload_model()
        video_id = ValueParser.parse_youtube_video_id(self.args.video) if self.args.video else None
        if video_id:
            return self.run_single_video(video_id)
        return self.run_subscriptions()

    def notify_run_failure(self) -> None:
        try:
            self.telegram.send_run_failure_message()
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
        window_start, window_end = SubscriptionState.subscription_window(
            self.paths.state_root,
            ignore_saved_window=self.args.reset_cutoff,
        )
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
        ollama: OllamaClient,
        telegram: TelegramClient,
        mlx_whisper_model: str,
        mlx_whisper_manager: LazyMlxWhisperManager,
        transcript_language_preferences: tuple[str, ...],
    ):
        self.ollama = ollama
        self.telegram = telegram
        self.mlx_whisper_model = mlx_whisper_model
        self.mlx_whisper_manager = mlx_whisper_manager
        self.transcript_language_preferences = transcript_language_preferences

    @staticmethod
    def is_probable_short(video: Video) -> bool:
        return video.duration_seconds is not None and video.duration_seconds <= 180

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
        try:
            transcript_result = None
            if transcript_cache_path and transcript_cache_path.exists():
                cached_transcript = transcript_cache_path.read_text(encoding="utf-8").strip()
                if cached_transcript:
                    transcript_result = TranscriptResult(text=cached_transcript)
                    log(f"transcript cache: hit {transcript_cache_path.name}")
            if transcript_result is None:
                try:
                    log(f"process {video.video_id}: native transcript", essential=True)
                    transcript_result = TranscriptFetcher.fetch_transcript(
                        video.video_id,
                        preferred_languages=self.transcript_language_preferences,
                    )
                except VideoFailure as transcript_exc:
                    log(f"transcript fallback {video.video_id}: {transcript_exc}")
                    self.mlx_whisper_manager.ensure_started()
                    try:
                        transcript_result = MlxWhisperService.fetch_audio_transcript(
                            video.video_id,
                            self.mlx_whisper_model,
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

            log(f"process {video.video_id}: summarize", essential=True)
            summary = self.ollama.summarize_transcript(
                transcript_result.text,
                language_code=transcript_result.language_code,
            )
            log(f"process {video.video_id}: format message")
            message = TelegramClient.format_message(video, summary)
            log(f"process {video.video_id}: send telegram", essential=True)
            self.telegram.send_message(message)
            log(f"sent {video.video_id}: summarized")
            return True
        except VideoFailure:
            fallback_body = "Summary unavailable."
            log(f"process {video.video_id}: format failure message")
            message = TelegramClient.format_message(video, fallback_body)
            log(f"process {video.video_id}: send failure telegram", essential=True)
            self.telegram.send_message(message)
            log(f"sent {video.video_id}: fallback description")
            return True
        except Exception as exc:
            log(
                f"process {video.video_id}: unexpected error, using fallback: "
                f"{exception_log_message(exc)}",
                essential=True,
            )
            fallback_body = "Summary unavailable."
            log(f"process {video.video_id}: format failure message")
            message = TelegramClient.format_message(video, fallback_body)
            log(f"process {video.video_id}: send failure telegram", essential=True)
            self.telegram.send_message(message)
            log(f"sent {video.video_id}: fallback description")
            return True

class YouTubeClient:
    """Resolves YouTube auth and exposes the app's video-fetching workflows."""

    def __init__(self, session: Any, config: Config):
        self.session = session
        self.config = config
        self._access_token: str | None = None

    def access_token(self) -> str:
        if self._access_token is None:
            self._access_token = self._resolve_access_token()
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
        data = HttpJsonClient.request_json(
            self.session,
            "POST",
            GOOGLE_TOKEN_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            data=payload,
        )
        token = str(data.get("access_token", "")).strip()
        if not token:
            raise FatalError("Google OAuth refresh response did not include an access token")
        log("oauth refresh: ok")
        return token

    def _resolve_access_token(self) -> str:
        if self.config.youtube_access_token:
            log("oauth access token: using YOUTUBE_ACCESS_TOKEN")
            return self.config.youtube_access_token
        return self._refresh_access_token()

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

class OllamaClient:
    """Wraps Ollama warmup, install-on-miss, and transcript summarization calls."""

    def __init__(self, session: Any, model: str, system_prompt: str):
        self.session = session
        self.model = model
        self.system_prompt = system_prompt

    @staticmethod
    def ollama_model_missing(status_code: int, detail: str, model: str) -> bool:
        if status_code != 404:
            return False
        normalized_detail = detail.lower()
        normalized_model = model.lower()
        return "not found" in normalized_detail and normalized_model in normalized_detail

    @staticmethod
    def ensure_model_installed(model: str) -> None:
        ollama_bin = shutil.which("ollama")
        if not ollama_bin:
            raise FatalError(
                f"Ollama model install failed for {model}: missing 'ollama' command in PATH"
            )

        log(f"ollama install: pull {model}", essential=True)
        try:
            completed = subprocess.run(
                [ollama_bin, "pull", model],
                check=True,
                capture_output=True,
                text=True,
                timeout=3600,
            )
        except subprocess.TimeoutExpired as exc:
            raise FatalError(f"Ollama model install timed out for {model}") from exc
        except subprocess.CalledProcessError as exc:
            output = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()
            reason = output[-1] if output else str(exc)
            raise FatalError(f"Ollama model install failed for {model}: {reason[:300]}") from exc

        output = (completed.stderr or completed.stdout or "").strip()
        if output:
            log(f"ollama install result model={model}:\n{output}")
        log(f"ollama install: ready {model}", essential=True)

    def generate(self, prompt: str) -> OllamaGenerateResult:
        requests_module = AppEnvironment.import_requests()
        url = f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate"
        payload = {
            "model": self.model,
            "system": self.system_prompt,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0},
        }
        try:
            response = self.session.post(url, json=payload, timeout=LOCAL_MODEL_TIMEOUT_SECONDS)
        except requests_module.RequestException as exc:
            raise VideoFailure(f"Ollama request failed for {self.model}: {exc}") from exc

        if response.status_code != 200:
            detail = response.text[:300].replace("\n", " ")
            raise VideoFailure(
                f"Ollama request failed for {self.model}: HTTP {response.status_code}: {detail}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise VideoFailure(f"Ollama returned invalid JSON for {self.model}") from exc

        generated = str(data.get("response", ""))
        if not generated.strip():
            raise VideoFailure(f"Ollama returned an empty response for {self.model}")
        return OllamaGenerateResult(response=generated)

    def preload_model(self) -> None:
        requests_module = AppEnvironment.import_requests()
        url = f"{OLLAMA_BASE_URL.rstrip('/')}/api/generate"
        payload = {
            "model": self.model,
            "prompt": "ping",
            "stream": False,
            "think": False,
            "options": {"temperature": 0},
        }
        started_at = time_module.monotonic()
        did_install = False
        while True:
            try:
                response = self.session.post(url, json=payload, timeout=LOCAL_MODEL_TIMEOUT_SECONDS)
            except requests_module.RequestException as exc:
                raise FatalError(f"Ollama warmup failed for {self.model}: {exc}") from exc

            if response.status_code == 200:
                break

            detail = response.text[:300].replace("\n", " ")
            if not did_install and self.ollama_model_missing(response.status_code, detail, self.model):
                self.ensure_model_installed(self.model)
                did_install = True
                continue
            raise FatalError(
                f"Ollama warmup failed for {self.model}: "
                f"HTTP {response.status_code}: {detail}"
            )

        try:
            response.json()
        except json.JSONDecodeError as exc:
            raise FatalError(f"Ollama warmup returned invalid JSON for {self.model}") from exc

        log(
            "ollama warmup: "
            f"model={self.model} elapsed={format_duration(time_module.monotonic() - started_at)}",
            essential=True,
        )

    def summarize_transcript(self, transcript: str, *, language_code: str = "") -> str:
        prompt = self.build_summary_prompt(transcript, language_code=language_code)
        started_at = time_module.monotonic()
        try:
            log(f"summary: request model={self.model}")
            result = self.generate(prompt)
            summary = result.response.strip()
            if not summary:
                raise VideoFailure("summary unavailable: model returned an empty response")
            log(f"summary: ok model={self.model}")
            log(f"summary result model={self.model}:\n{summary}")
            return summary
        except VideoFailure as exc:
            log(f"summary: failed model={self.model}: {exception_log_message(exc)}")
            raise VideoFailure(f"summary unavailable: {exc}") from exc
        finally:
            log(
                "summary: duration "
                f"model={self.model}: elapsed="
                f"{format_duration(time_module.monotonic() - started_at)}",
                essential=True,
            )

    @staticmethod
    def build_summary_prompt(transcript: str, *, language_code: str = "") -> str:
        cleaned_transcript = transcript.strip()
        if not cleaned_transcript:
            return ""
        normalized_language_code = language_code.strip().lower()
        if not normalized_language_code:
            return cleaned_transcript
        return f"Summary language code: {normalized_language_code}.\n\n{cleaned_transcript}"

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

    def send_run_failure_message(self) -> None:
        log("run failure: send telegram", essential=True)
        self.send_message("TextTube run failed.")

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

class MlxWhisperService:
    """Handles local mlx-whisper server lifecycle and audio transcription fallback."""

    @staticmethod
    def mlx_whisper_service_url(path: str) -> str:
        return f"{MLX_WHISPER_BASE_URL.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def mlx_whisper_unavailable_message(detail: str) -> str:
        return (
            "audio transcript unavailable: mlx-whisper host unavailable "
            f"at {MLX_WHISPER_BASE_URL}: {detail}"
        )

    @staticmethod
    def video_audio_cache_path(state_root: Path, video_id: str) -> Path:
        return state_root / "var" / CACHE_DIR_NAME / f"{video_id}{AUDIO_CACHE_EXTENSION}"

    @staticmethod
    def require_ffmpeg() -> None:
        if shutil.which("ffmpeg"):
            return
        raise FatalError("ffmpeg is required on the Mac that runs TextTube.")

    @staticmethod
    def mlx_is_ready() -> bool:
        try:
            with request.urlopen(MlxWhisperService.mlx_whisper_service_url("/healthz"), timeout=1) as response:
                return response.status == 200
        except (OSError, error.URLError):
            return False

    @staticmethod
    def mlx_port_in_use() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            return sock.connect_ex((MLX_WHISPER_HOST, MLX_WHISPER_PORT)) == 0

    @staticmethod
    def stop_process(process: ManagedMlxProcess | None) -> None:
        if process is None:
            return
        if process.process.poll() is None:
            MlxWhisperService._signal_process_group(process.process, signal.SIGINT)
            try:
                process.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                MlxWhisperService._signal_process_group(process.process, signal.SIGTERM)
                try:
                    process.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    MlxWhisperService._signal_process_group(process.process, signal.SIGKILL)
                    process.process.wait()
        process.log_file.close()

    @staticmethod
    def _signal_process_group(process: subprocess.Popen[bytes], sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return

    @staticmethod
    def start_managed_process(state_root: Path) -> ManagedMlxProcess | None:
        if MlxWhisperService.mlx_is_ready():
            log("mlx-whisper: reusing existing helper", essential=True)
            return None
        if MlxWhisperService.mlx_port_in_use():
            raise FatalError(
                f"Port {MLX_WHISPER_PORT} is already in use and mlx-whisper did not pass /healthz"
            )

        log_directory = state_root / "var" / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        log_path = log_directory / MLX_WHISPER_LOG_FILE_NAME
        log_file = log_path.open("ab")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from texttube_app import MlxWhisperService; "
                "raise SystemExit(MlxWhisperService.run_server())",
            ],
            cwd=state_root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        managed_process = ManagedMlxProcess(process=process, log_file=log_file)

        for _ in range(MLX_READY_TIMEOUT_SECONDS):
            if managed_process.process.poll() is not None:
                MlxWhisperService.stop_process(managed_process)
                raise FatalError(f"mlx-whisper exited before becoming ready; check {log_path}")
            if MlxWhisperService.mlx_is_ready():
                log(f"mlx-whisper: ready log={log_path}", essential=True)
                return managed_process
            time_module.sleep(1)

        MlxWhisperService.stop_process(managed_process)
        raise FatalError(f"mlx-whisper did not become ready before timeout; check {log_path}")

    @staticmethod
    def transcribe_file(audio_path: str, mlx_whisper_model: str) -> str:
        import mlx_whisper

        MlxWhisperService.require_ffmpeg()
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=mlx_whisper_model,
            verbose=False,
            temperature=0.0,
            condition_on_previous_text=False,
        )
        text = str(result.get("text", "")).strip()
        if not text:
            raise RuntimeError("mlx-whisper returned no text")
        return text

    @staticmethod
    def run_server() -> int:
        MlxWhisperService.require_ffmpeg()
        mlx_whisper_model = ConfigLoader.resolve_mlx_whisper_model(
            AppEnvironment.texttube_home() / ".secrets"
        )

        with ProcessPoolExecutor(max_workers=MLX_TRANSCRIBE_WORKERS) as executor:
            MlxHandler.executor = executor
            MlxHandler.mlx_whisper_model = mlx_whisper_model
            server = ThreadingHTTPServer((MLX_WHISPER_HOST, MLX_WHISPER_PORT), MlxHandler)
            print(
                f"mlx-whisper listening on {MLX_WHISPER_BASE_URL} "
                f"model={mlx_whisper_model} transcribe_workers={MLX_TRANSCRIBE_WORKERS}",
                flush=True,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("mlx-whisper shutting down", flush=True)
            finally:
                server.server_close()
        return 0

    @staticmethod
    def fetch_audio_transcript(
        video_id: str,
        mlx_whisper_model: str,
        *,
        audio_cache_path: Path | None = None,
    ) -> "TranscriptResult":
        log(f"transcript audio: start {video_id} model={mlx_whisper_model}")
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
                text = MlxWhisperService.fetch_mlx_audio_transcript(chunk_paths, mlx_whisper_model)
                log(f"transcript audio: ok {video_id}: mlx {len(chunk_paths)} chunks")
                log(f"transcript audio result {video_id}:\n{text}")
                return TranscriptResult(text=text)
        finally:
            duration = format_duration(time_module.perf_counter() - started_at)
            log(f"transcript audio: duration {video_id}: {duration}", essential=True)

    @staticmethod
    def fetch_mlx_audio_transcript(chunk_paths: list[Path], mlx_whisper_model: str) -> str:
        requests_module = AppEnvironment.import_requests()
        max_workers = max(1, min(MLX_TRANSCRIBE_WORKERS, len(chunk_paths)))
        results = [""] * len(chunk_paths)
        errors: list[str] = []
        service_url = MlxWhisperService.mlx_whisper_service_url("/transcribe")
        log(f"transcript audio: mlx model={mlx_whisper_model} workers={max_workers}")

        def transcribe_chunk(index: int, chunk_path: Path) -> tuple[int, str]:
            log(f"transcript audio: mlx chunk {chunk_path.name}")
            try:
                payload = chunk_path.read_bytes()
                response = requests_module.post(
                    service_url,
                    data=payload,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-File-Suffix": chunk_path.suffix or ".m4a",
                    },
                    timeout=LOCAL_MODEL_TIMEOUT_SECONDS,
                )
            except requests_module.ConnectTimeout as exc:
                raise VideoFailure(
                    MlxWhisperService.mlx_whisper_unavailable_message(
                        "connection attempt timed out"
                    )
                ) from exc
            except requests_module.ConnectionError as exc:
                raise VideoFailure(
                    MlxWhisperService.mlx_whisper_unavailable_message(
                        exception_log_message(exc)
                    )
                ) from exc
            except requests_module.RequestException as exc:
                raise VideoFailure(
                    "audio transcript unavailable: mlx-whisper request failed: "
                    f"{exception_log_message(exc)}"
                ) from exc

            if response.status_code != 200:
                detail = response.text[:300].replace("\n", " ")
                raise VideoFailure(
                    f"audio transcript unavailable: mlx-whisper failed: "
                    f"HTTP {response.status_code}: {detail}"
                )

            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                raise VideoFailure(
                    "audio transcript unavailable: mlx-whisper returned invalid JSON"
                ) from exc

            text = str(data.get("text", "")).strip()
            if not text:
                raise VideoFailure("audio transcript unavailable: mlx-whisper returned no text")
            return index, text

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(transcribe_chunk, index, chunk_path): chunk_path.name
                for index, chunk_path in enumerate(chunk_paths)
            }
            for future in concurrent.futures.as_completed(future_map):
                chunk_name = future_map[future]
                try:
                    index, text = future.result()
                    results[index] = text
                except VideoFailure as exc:
                    errors.append(f"{chunk_name}: {exc}")

        if errors:
            raise VideoFailure("; ".join(errors))

        text = "\n".join(part for part in results if part).strip()
        if not text:
            raise VideoFailure("audio transcript unavailable: mlx-whisper returned no text")
        return text

class MlxHandler(BaseHTTPRequestHandler):
    """Serves the local HTTP interface used by chunked mlx-whisper transcription."""

    executor: ProcessPoolExecutor | None = None
    mlx_whisper_model = MLX_WHISPER_MODEL

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/healthz":
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(200, {"ok": True})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/transcribe":
            self.send_json(404, {"error": "not found"})
            return

        if self.executor is None:
            self.send_json(500, {"error": "transcription executor is not configured"})
            return

        content_length = self.headers.get("Content-Length", "").strip()
        if not content_length:
            self.send_json(411, {"error": "missing Content-Length"})
            return

        try:
            size = int(content_length)
        except ValueError:
            self.send_json(400, {"error": "invalid Content-Length"})
            return
        if size <= 0:
            self.send_json(400, {"error": "empty audio payload"})
            return

        body = self.rfile.read(size)
        suffix = self.headers.get("X-File-Suffix", ".m4a").strip() or ".m4a"
        if not suffix.startswith("."):
            suffix = f".{suffix.lstrip('.')}"

        with tempfile.NamedTemporaryFile(
            delete=False,
            prefix="texttube-mlx-",
            suffix=suffix,
        ) as audio_file:
            audio_file.write(body)
            temp_path = audio_file.name

        try:
            text = self.executor.submit(
                MlxWhisperService.transcribe_file,
                temp_path,
                self.mlx_whisper_model,
            ).result()
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})
        else:
            self.send_json(200, {"text": text})
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[mlx-service] {self.address_string()} - {format % args}", flush=True)

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

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
                "Missing Python dependency: requests. Install requirements.txt into the local venv."
            ) from exc
        return requests

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
    """Loads configuration from .secrets plus environment overrides."""

    @staticmethod
    def read_dotenv(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not path.exists():
            return values

        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                raise FatalError(f"{path} line {line_number} is not KEY=VALUE format")

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                raise FatalError(f"{path} line {line_number} has an empty key")
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            values[key] = value

        return values

    @staticmethod
    def merged_environment(secrets_path: Path) -> dict[str, str]:
        values = ConfigLoader.read_dotenv(secrets_path)
        values.update(os.environ)
        return values

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
    def apply_runtime_defaults(args: argparse.Namespace, secrets_path: Path) -> None:
        values = ConfigLoader.merged_environment(secrets_path)

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
    def load_config(secrets_path: Path, *, allow_manual_access_token: bool = False) -> Config:
        values = ConfigLoader.merged_environment(secrets_path)
        values.pop("YOUTUBE_ACCESS_TOKEN", None)
        youtube_access_token = ""
        if allow_manual_access_token:
            youtube_access_token = os.environ.get("YOUTUBE_ACCESS_TOKEN", "").strip()

        if youtube_access_token:
            google_client_id = ""
            google_client_secret = ""
            google_refresh_token = ""
        else:
            google_client_id = ConfigLoader.require_env(values, "GOOGLE_OAUTH_CLIENT_ID")
            google_client_secret = ConfigLoader.require_env(values, "GOOGLE_OAUTH_CLIENT_SECRET")
            google_refresh_token = ConfigLoader.require_env(values, "GOOGLE_OAUTH_REFRESH_TOKEN")

        return Config(
            telegram_bot_token=ConfigLoader.require_env(values, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=ConfigLoader.require_env(values, "TELEGRAM_CHAT_ID"),
            google_client_id=google_client_id,
            google_client_secret=google_client_secret,
            google_refresh_token=google_refresh_token,
            youtube_access_token=youtube_access_token,
        )

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
    def resolve_transcript_language_preferences(secrets_path: Path) -> tuple[str, ...]:
        configured = ConfigLoader.merged_environment(secrets_path).get(
            "TRANSCRIPT_LANGUAGES",
            "",
        ).strip()
        if configured:
            return ConfigLoader.parse_transcript_language_preferences(configured)
        return ()

    @staticmethod
    def resolve_ollama_model(secrets_path: Path) -> str:
        configured = ConfigLoader.merged_environment(secrets_path).get(
            "OLLAMA_MODEL",
            "",
        ).strip()
        return configured or OLLAMA_MODEL

    @staticmethod
    def resolve_mlx_whisper_model(secrets_path: Path) -> str:
        configured = ConfigLoader.merged_environment(secrets_path).get(
            "MLX_WHISPER_MODEL",
            "",
        ).strip()
        return configured or MLX_WHISPER_MODEL

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
    def subscription_window(
        state_root: Path,
        *,
        ignore_saved_window: bool = False,
    ) -> tuple[datetime, datetime]:
        window_end = datetime.now(timezone.utc).replace(microsecond=0)
        state_path = SubscriptionState.last_subscription_window_end_path(state_root)
        if ignore_saved_window or not state_path.exists():
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
    secrets_path: Path

    @classmethod
    def discover(cls) -> "RuntimePaths":
        code_root = AppEnvironment.app_root()
        state_root = AppEnvironment.texttube_home()
        return cls(
            code_root=code_root,
            state_root=state_root,
            secrets_path=state_root / ".secrets",
        )

    def prompt_path(self) -> Path:
        configured = ConfigLoader.merged_environment(self.secrets_path).get(
            "SUMMARIZER_MD",
            "",
        ).strip()
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
        return MlxWhisperService.video_audio_cache_path(self.state_root, video_id)

    def transcript_cache_path(self, video_id: str, *, enabled: bool) -> Path | None:
        if not enabled:
            return None
        return self.state_root / "var" / CACHE_DIR_NAME / f"{video_id}{TRANSCRIPT_CACHE_EXTENSION}"

class HttpJsonClient:
    """Wraps JSON HTTP requests and enforces consistent fatal error handling."""

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

@dataclass
class ManagedMlxProcess:
    """Owns the spawned mlx-whisper helper process and its log file handle."""

    process: subprocess.Popen[bytes]
    log_file: Any

@dataclass
class LazyMlxWhisperManager:
    """Starts the local mlx-whisper helper only when audio fallback is needed."""

    state_root: Path
    _process: ManagedMlxProcess | None = field(default=None, init=False)

    def ensure_started(self) -> None:
        if self._process is None:
            self._process = MlxWhisperService.start_managed_process(self.state_root)

    def stop(self) -> None:
        MlxWhisperService.stop_process(self._process)
        self._process = None

class FatalError(Exception):
    """Fatal setup or API failure that should stop the whole run."""

class VideoFailure(Exception):
    """Per-video failure that should still allow later videos to continue."""

class TelegramFailure(Exception):
    """Telegram delivery failure for a single outbound message."""

@dataclass(frozen=True)
class Config:
    """Runtime secrets and API credentials for one TextTube invocation."""

    telegram_bot_token: str
    telegram_chat_id: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    youtube_access_token: str

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
class OllamaGenerateResult:
    """Minimal Ollama generate response payload used by summarization."""

    response: str

@dataclass(frozen=True)
class TranscriptResult:
    """Transcript text plus the best available language hint for summarization."""

    text: str
    language_code: str = ""

if __name__ == "__main__":
    raise SystemExit(TextTubeCli.main())
