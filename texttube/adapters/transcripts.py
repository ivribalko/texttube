"""Native captions, rotating proxy recovery, and dormant audio fallback."""

from __future__ import annotations

import ipaddress
import time
from dataclasses import replace
from typing import Any

from texttube.config import (
    MAX_TRANSCRIPT_IP_ROTATIONS,
    REQUEST_TIMEOUT_SECONDS,
    VPN_ROTATION_POLL_SECONDS,
    VPN_ROTATION_TIMEOUT_SECONDS,
    TranscriptProxyConfig,
)
from texttube.domain import NativeTranscriptUnavailable, Transcript, Video, VideoFailure
from texttube.ports import AudioTranscription, Log


class TranscriptProxyRotator:
    """Reconnects a VPN proxy until a different public IP is ready."""

    def __init__(self, session: Any, config: TranscriptProxyConfig, log: Log):
        self.session = session
        self.config = config
        self.log = log
        self.rejected_ips: set[str] = set()

    def rotate(self) -> None:
        """Replace the active VPN public IP or raise a per-video failure."""
        previous_ip = self._public_ip()
        self.rejected_ips.add(previous_ip)
        self.log.write(
            "transcript proxy: reconnect for a different public IP",
            essential=True,
        )
        deadline = time.monotonic() + VPN_ROTATION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._set_status("stopped")
            self._wait_for_status("stopped", deadline)
            self._set_status("running")
            while time.monotonic() < deadline:
                if self._status() == "running":
                    current_ip = self._public_ip(required=False)
                    if not current_ip:
                        time.sleep(VPN_ROTATION_POLL_SECONDS)
                        continue
                    if current_ip not in self.rejected_ips:
                        self.log.write(
                            "transcript proxy: different public IP ready",
                            essential=True,
                        )
                        return
                    self.log.write(
                        "transcript proxy: public IP already rejected; reconnect again",
                        essential=True,
                    )
                    break
                time.sleep(VPN_ROTATION_POLL_SECONDS)
        raise VideoFailure(
            "transcript proxy did not provide a different public IP before timeout"
        )

    def _wait_for_status(self, status: str, deadline: float) -> None:
        """Wait for one gateway loop transition before requesting the next."""
        while time.monotonic() < deadline:
            if self._status() == status:
                return
            time.sleep(VPN_ROTATION_POLL_SECONDS)
        raise VideoFailure(
            f"transcript proxy did not transition to {status} before timeout"
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Call one authenticated VPN gateway control endpoint."""
        url = f"{self.config.control_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers={"X-API-Key": self.config.control_api_key},
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            raise VideoFailure(
                f"transcript proxy control request failed: {self.log.exception(exc)}"
            ) from exc
        try:
            if response.status_code != 200:
                raise VideoFailure(
                    "transcript proxy control request failed with "
                    f"HTTP {response.status_code}"
                )
            parsed = response.json()
        except ValueError as exc:
            raise VideoFailure(
                "transcript proxy control request returned invalid JSON"
            ) from exc
        finally:
            response.close()
        if not isinstance(parsed, dict):
            raise VideoFailure(
                "transcript proxy control request returned unexpected JSON"
            )
        return parsed

    def _set_status(self, status: str) -> None:
        """Set the VPN gateway loop state."""
        self._request_json("PUT", "/v1/vpn/status", payload={"status": status})

    def _status(self) -> str:
        """Return the normalized VPN gateway loop state."""
        return str(self._request_json("GET", "/v1/vpn/status").get("status", ""))

    def _public_ip(self, *, required: bool = True) -> str:
        """Return the VPN gateway's public IP without writing it to logs."""
        raw_value = self._request_json("GET", "/v1/publicip/ip").get("public_ip")
        value = str(raw_value).strip() if raw_value is not None else ""
        try:
            value = str(ipaddress.ip_address(value)) if value else ""
        except ValueError:
            value = ""
        if required and not value:
            raise VideoFailure("transcript proxy did not report its current public IP")
        return value


class NativeTranscriptFetcher:
    """Fetches YouTube transcripts and ranks preferred language matches."""

    def __init__(
        self,
        language_preferences: tuple[str, ...],
        log: Log,
        *,
        proxy_config: TranscriptProxyConfig | None = None,
        proxy_rotator: TranscriptProxyRotator | None = None,
    ):
        self.language_preferences = language_preferences
        self.log = log
        self.proxy_config = proxy_config
        self.proxy_rotator = proxy_rotator
        self.direct_ip_blocked = False

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
        """Fetch captions directly, falling back to VPN exits after an IP block."""
        if not self.direct_ip_blocked:
            try:
                return self._fetch_once(
                    video_id,
                    original_audio_language=original_audio_language,
                    use_proxy=False,
                )
            except self._ip_block_errors() as exc:
                if self.proxy_config is None:
                    raise VideoFailure(
                        f"transcript unavailable: {self.log.exception(exc)}"
                    ) from exc
                self.direct_ip_blocked = True
                self.log.write(
                    f"transcript native: direct IP blocked {video_id}; "
                    "retry through proxy",
                    essential=True,
                )

        return self._fetch_through_proxy(
            video_id,
            original_audio_language=original_audio_language,
        )

    def _fetch_through_proxy(
        self,
        video_id: str,
        *,
        original_audio_language: str = "",
    ) -> Transcript:
        """Fetch captions through the VPN and rotate only blocked exits."""
        for rotation in range(MAX_TRANSCRIPT_IP_ROTATIONS + 1):
            try:
                return self._fetch_once(
                    video_id,
                    original_audio_language=original_audio_language,
                    use_proxy=True,
                )
            except self._ip_block_errors() as exc:
                if self.proxy_rotator is None or rotation >= MAX_TRANSCRIPT_IP_ROTATIONS:
                    raise VideoFailure(
                        f"transcript unavailable: {self.log.exception(exc)}"
                    ) from exc
                self.log.write(
                    f"transcript native: IP blocked {video_id}; rotate proxy "
                    f"{rotation + 1}/{MAX_TRANSCRIPT_IP_ROTATIONS}",
                    essential=True,
                )
                self.proxy_rotator.rotate()
        raise AssertionError("unreachable transcript proxy rotation state")

    @staticmethod
    def _ip_block_errors() -> tuple[type[Exception], ...]:
        """Return the transcript library's explicit IP-block exception types."""
        try:
            from youtube_transcript_api._errors import IpBlocked, RequestBlocked
        except ModuleNotFoundError as exc:
            raise VideoFailure(
                "transcript unavailable: missing Python dependency youtube-transcript-api"
            ) from exc
        return (RequestBlocked, IpBlocked)

    def _fetch_once(
        self,
        video_id: str,
        *,
        original_audio_language: str = "",
        use_proxy: bool,
    ) -> Transcript:
        """Fetch the first nonempty transcript through the selected route."""
        self.log.write(f"transcript native: list {video_id}")
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api._errors import (
                CouldNotRetrieveTranscript,
                IpBlocked,
                NoTranscriptFound,
                RequestBlocked,
                TranscriptsDisabled,
                VideoUnavailable,
            )
            from youtube_transcript_api.proxies import GenericProxyConfig
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
        ip_block_errors = (RequestBlocked, IpBlocked)
        try:
            library_proxy_config = None
            if use_proxy and self.proxy_config is not None:
                library_proxy_config = GenericProxyConfig(
                    http_url=self.proxy_config.proxy_url,
                    https_url=self.proxy_config.proxy_url,
                )
            transcript_api = YouTubeTranscriptApi(proxy_config=library_proxy_config)
            if hasattr(transcript_api, "list"):
                transcript_list = transcript_api.list(video_id)
            else:
                transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        except ip_block_errors:
            raise
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
            except ip_block_errors:
                raise
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
