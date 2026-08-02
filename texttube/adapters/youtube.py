"""YouTube Data API discovery and Google token-refresh adapter."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterator

from texttube.config import AppConfig, REQUEST_TIMEOUT_SECONDS, ValueParser
from texttube.domain import FatalError, GoogleOAuthReauthorizationRequired, Video
from texttube.ports import Log

YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_REAUTHORIZATION_ERROR_CODE = "invalid_grant"
YOUTUBE_PAGE_SIZE = 50


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
        """Send one HTTP request and require a JSON object response."""
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise FatalError(
                "Missing Python dependency: requests. Install requirements.txt or use Docker."
            ) from exc
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise FatalError(f"Request failed for {url}: {exc}") from exc
        if response.status_code != expected_status:
            detail = response.text[:500].replace("\n", " ")
            raise FatalError(
                f"Request failed for {url}: HTTP {response.status_code}: {detail}"
            )
        try:
            parsed = response.json()
        except json.JSONDecodeError as exc:
            raise FatalError(f"Request returned invalid JSON for {url}") from exc
        if not isinstance(parsed, dict):
            raise FatalError(f"Request returned unexpected JSON for {url}")
        return parsed


class YouTubeDiscovery:
    """Discovers normalized videos through YouTube Data API workflows."""

    def __init__(self, session: Any, config: AppConfig, log: Log):
        self.session = session
        self.config = config
        self.log = log
        self._access_token: str | None = None

    def ensure_authorized(self) -> None:
        """Resolve a YouTube access token before subscription traversal."""
        self.access_token()

    def access_token(self) -> str:
        """Return a cached access token, refreshing it on first use."""
        if self._access_token is None:
            self._access_token = self._refresh_access_token()
        return self._access_token

    def fetch_video(self, video_id: str) -> Video:
        """Fetch and normalize one selected video's metadata."""
        self.log.write(f"video metadata: request {video_id}")
        page = self._get(
            "videos",
            {"part": "snippet,contentDetails", "id": video_id, "maxResults": 1},
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
            default_audio_language=str(
                snippet.get("defaultAudioLanguage", "")
            ).strip().lower(),
            description=str(snippet.get("description", "")).strip(),
            tags=tags,
        )

    def iter_recent_videos(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> Iterator[Video]:
        """Yield deduplicated recent uploads from all current subscriptions."""
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
            self.log.write(f"subscription page: {len(subscriptions)} channels")
            playlists = self._fetch_upload_playlists(subscriptions)
            playlist_count += len(playlists)
            self.log.write(f"upload playlists so far: {playlist_count}")
            for channel_id, (playlist_id, channel_title) in playlists.items():
                for video in self._iter_playlist_videos(
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
                self.log.write(f"subscriptions: {subscription_count}")
                self.log.write(f"upload playlists: {playlist_count}")
                return

    def _iter_playlist_videos(
        self,
        uploads_playlist_id: str,
        channel_id: str,
        channel_title: str,
        window_start: datetime,
        window_end: datetime,
    ) -> Iterator[Video]:
        """Yield recent enriched videos from one uploads playlist."""
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
            yield from self._enrich_video_details(videos)
            if saw_older_video or not next_page_token:
                return

    def _enrich_video_details(self, videos: list[Video]) -> list[Video]:
        """Add duration, description, tags, and canonical titles in batches."""
        by_id = {video.video_id: video for video in videos}
        if not by_id:
            return []
        for video_ids in ValueParser.chunks(list(by_id), 50):
            self.log.write(f"videos metadata: request {len(video_ids)}")
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
                tags_raw = snippet.get("tags") or []
                tags = tuple(str(tag) for tag in tags_raw if isinstance(tag, str))
                by_id[video_id] = replace(
                    by_id[video_id],
                    title=str(snippet.get("title", "")).strip() or by_id[video_id].title,
                    channel_title=str(snippet.get("channelTitle", "")).strip()
                    or by_id[video_id].channel_title,
                    description=str(snippet.get("description", "")).strip(),
                    duration_seconds=ValueParser.parse_iso8601_duration_seconds(
                        str(content_details.get("duration", "")).strip()
                    ),
                    default_audio_language=str(
                        snippet.get("defaultAudioLanguage", "")
                    ).strip().lower(),
                    tags=tags,
                )
        self.log.write(f"videos metadata: enriched {len(by_id)}")
        return [by_id[video.video_id] for video in videos if video.video_id in by_id]

    def _refresh_access_token(self) -> str:
        """Exchange the protected refresh token for an access token."""
        self.log.write("oauth refresh: request")
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
        self.log.write("oauth refresh: ok")
        return token

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one authorized YouTube Data API GET request."""
        return HttpJsonClient.request_json(
            self.session,
            "GET",
            f"{YOUTUBE_API_BASE_URL}/{endpoint}",
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {self.access_token()}"},
            params=params,
        )

    def _fetch_upload_playlists(
        self,
        subscriptions: list[tuple[str, str]],
    ) -> dict[str, tuple[str, str]]:
        """Resolve each subscribed channel's uploads playlist."""
        channel_titles = {channel_id: title for channel_id, title in subscriptions}
        playlist_by_channel: dict[str, tuple[str, str]] = {}
        for channel_ids in ValueParser.chunks(list(channel_titles), 50):
            self.log.write(f"channels metadata: request {len(channel_ids)}")
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
                    channel_id, channel_id
                )
                related = (item.get("contentDetails") or {}).get("relatedPlaylists") or {}
                uploads_playlist_id = str(related.get("uploads", "")).strip()
                if channel_id and uploads_playlist_id:
                    playlist_by_channel[channel_id] = (uploads_playlist_id, title)
        self.log.write(
            f"channels metadata: resolved {len(playlist_by_channel)} upload playlists"
        )
        return playlist_by_channel

    @staticmethod
    def _parse_subscription_items(items: Any) -> list[tuple[str, str]]:
        """Normalize a YouTube subscriptions page."""
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
        """Fetch and window-filter one uploads-playlist page."""
        videos: list[Video] = []
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": YOUTUBE_PAGE_SIZE,
        }
        if page_token:
            params["pageToken"] = page_token
        self.log.write(
            f"playlist items: request {channel_title} page={page_token or 'first'}"
        )
        page = self._get("playlistItems", params)
        saw_older_video = False
        for item in page.get("items", []):
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            video_id = str(content_details.get("videoId", "")).strip()
            published_raw = str(
                content_details.get("videoPublishedAt")
                or snippet.get("publishedAt")
                or ""
            )
            if not video_id or not published_raw:
                continue
            try:
                published_at = ValueParser.parse_rfc3339(published_raw)
            except ValueError:
                self.log.write(f"skip {video_id}: invalid published timestamp")
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
        self.log.write(
            f"playlist items: {channel_title} yielded {len(videos)} recent, "
            f"older_seen={'yes' if saw_older_video else 'no'}"
        )
        return videos, saw_older_video, next_page_token
