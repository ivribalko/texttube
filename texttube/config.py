"""Runtime constants, environment loading, value parsing, and path discovery."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from texttube.domain import FatalError

OPENAI_SUMMARY_MODEL = "gpt-5.6-luna"
OPENAI_TRANSCRIPTION_MODEL = "gpt-transcribe"
REQUEST_TIMEOUT_SECONDS = 30
OPENAI_SUMMARY_TIMEOUT_SECONDS = 180
OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS = 1800
MAX_AUDIO_TRANSCRIPTION_DURATION_SECONDS = 60 * 60
MAX_SHORT_DURATION_SECONDS = 3 * 60
DEFAULT_VIDEO_LIMIT = 100
CACHE_DIR_NAME = "cache"
LOG_DIR_NAME = "logs"
LOG_FILE_PREFIX = "texttube-"
LOG_RETENTION_DAYS = 30
AUDIO_CACHE_EXTENSION = ".m4a"
TRANSCRIPT_CACHE_EXTENSION = ".txt"
SUBSCRIPTION_STATE_DIR_NAME = "state"
LAST_SUBSCRIPTION_WINDOW_END_FILE = "last_subscription_window_end_utc.txt"
GOOGLE_OAUTH_REFRESH_TOKEN_FILE = "google_oauth_refresh_token"
TRANSCRIPT_LANGUAGE_SEPARATOR = ","
GOOGLE_OAUTH_AUTH_COMMAND = (
    "docker compose run --rm texttube auth --once"
)
GENERIC_RUN_FAILURE_MESSAGE = "TextTube run failed."
GOOGLE_OAUTH_REAUTHORIZATION_MESSAGE = (
    "TextTube could not access YouTube because Google authorization expired or was revoked. "
    "Run {auth_command} to reconnect YouTube. The next run will process the preserved "
    "subscription window."
)


@dataclass(frozen=True)
class AppConfig:
    """Runtime secrets and API credentials for one TextTube invocation."""

    openai_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str


@dataclass(frozen=True)
class RuntimeOptions:
    """Normalized command-line and environment options for one run."""

    limit: int
    video_id: str | None
    cache: bool
    verbose: bool
    transcript_languages: tuple[str, ...]


@dataclass(frozen=True)
class RuntimePaths:
    """Centralizes immutable code and managed-data paths."""

    code_root: Path
    state_root: Path

    @classmethod
    def discover(cls) -> "RuntimePaths":
        """Resolve paths from the installed package and process environment."""
        code_root = Path(__file__).resolve().parent.parent
        configured = os.environ.get("TEXTTUBE_HOME", "").strip()
        state_root = Path(configured).expanduser().resolve() if configured else code_root
        return cls(code_root=code_root, state_root=state_root)

    def prompt_path(self) -> Path:
        """Resolve the transcript-summary prompt path."""
        configured = os.environ.get("SUMMARIZER_MD", "").strip()
        if not configured:
            return self.code_root / "SUMMARIZER.md"
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_absolute() else self.code_root / candidate

    def display_path(self, path: Path) -> str:
        """Prefer a repository-relative path in operator output."""
        try:
            return str(path.relative_to(self.code_root))
        except ValueError:
            return str(path)

    def google_refresh_token_path(self) -> Path:
        """Return the managed Google refresh-token path."""
        return (
            self.state_root
            / "var"
            / SUBSCRIPTION_STATE_DIR_NAME
            / GOOGLE_OAUTH_REFRESH_TOKEN_FILE
        )

    def log_dir(self) -> Path:
        """Return the managed application-run log directory."""
        return self.state_root / "var" / LOG_DIR_NAME


class ValueParser:
    """Parses timestamps, video IDs, durations, and normalized lists."""

    @staticmethod
    def parse_rfc3339(value: str) -> datetime:
        """Parse a timestamp and normalize it to UTC."""
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def parse_youtube_video_id(value: str) -> str:
        """Extract and validate a YouTube video ID from a URL or bare value."""
        candidate = value.strip()
        if not candidate:
            raise FatalError("--video must include a YouTube video URL or ID")
        patterns = (
            r"(?:youtube\.com|youtube-nocookie\.com)/watch\?[^#]*\bv=([^&#]+)",
            r"(?:youtube\.com|youtube-nocookie\.com)/(?:embed|shorts|live)/([^?&#/]+)",
            r"youtu\.be/([^?&#/]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, candidate)
            if match:
                return ValueParser.clean_youtube_video_id(match.group(1))
        return ValueParser.clean_youtube_video_id(candidate)

    @staticmethod
    def clean_youtube_video_id(value: str) -> str:
        """Validate and normalize one candidate video ID."""
        video_id = value.strip()
        video_id = video_id.split("?", 1)[0].split("&", 1)[0].split("#", 1)[0].strip("/")
        if not re.fullmatch(r"[\w-]{11}", video_id):
            raise FatalError(f"Invalid YouTube video URL or ID: {value}")
        return video_id

    @staticmethod
    def parse_iso8601_duration_seconds(value: str) -> int | None:
        """Convert a YouTube ISO 8601 duration to seconds."""
        match = re.fullmatch(
            r"P(?:(?P<days>\d+)D)?"
            r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
            value,
        )
        if not match:
            return None
        return (
            int(match.group("days") or 0) * 86400
            + int(match.group("hours") or 0) * 3600
            + int(match.group("minutes") or 0) * 60
            + int(match.group("seconds") or 0)
        )

    @staticmethod
    def chunks(values: list[str], size: int) -> list[list[str]]:
        """Split a list into API-sized chunks."""
        return [values[index : index + size] for index in range(0, len(values), size)]


class ConfigLoader:
    """Loads process configuration and the volume-backed Google refresh token."""

    @staticmethod
    def parse_optional_bool(values: dict[str, str], key: str) -> bool | None:
        """Parse the optional lowercase true flag used by Compose."""
        raw_value = values.get(key, "").strip()
        return None if not raw_value else raw_value == "true"

    @staticmethod
    def parse_optional_int(values: dict[str, str], key: str) -> int | None:
        """Parse one optional integer environment value."""
        raw_value = values.get(key, "").strip()
        if not raw_value:
            return None
        try:
            return int(raw_value)
        except ValueError as exc:
            raise FatalError(f"Invalid integer configuration for {key}: {raw_value}") from exc

    @staticmethod
    def require_env(values: dict[str, str], key: str) -> str:
        """Return one required nonempty environment value."""
        value = values.get(key, "").strip()
        if not value:
            raise FatalError(f"Missing required configuration: {key}")
        return value

    @classmethod
    def load_app_config(cls, token_path: Path) -> AppConfig:
        """Load API credentials and the protected refresh token."""
        values = dict(os.environ)
        return AppConfig(
            openai_api_key=cls.require_env(values, "OPENAI_API_KEY"),
            telegram_bot_token=cls.require_env(values, "TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=cls.require_env(values, "TELEGRAM_CHAT_ID"),
            google_client_id=cls.require_env(values, "GOOGLE_OAUTH_CLIENT_ID"),
            google_client_secret=cls.require_env(values, "GOOGLE_OAUTH_CLIENT_SECRET"),
            google_refresh_token=cls.read_google_refresh_token(token_path),
        )

    @staticmethod
    def read_google_refresh_token(path: Path) -> str:
        """Read the refresh token without exposing its value."""
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
    def parse_transcript_languages(raw_value: str) -> tuple[str, ...]:
        """Normalize comma-separated transcript language preferences."""
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

    @classmethod
    def load_runtime_options(cls, args: argparse.Namespace) -> RuntimeOptions:
        """Merge parsed CLI arguments with supported environment defaults."""
        values = dict(os.environ)
        configured_limit = cls.parse_optional_int(values, "TEXTTUBE_LIMIT")
        limit = args.limit
        if limit is None:
            limit = configured_limit if configured_limit is not None else DEFAULT_VIDEO_LIMIT
        if limit < 0:
            raise FatalError("--limit must be 0 or greater")
        configured_verbose = cls.parse_optional_bool(values, "TEXTTUBE_VERBOSE")
        verbose = args.verbose or bool(configured_verbose)
        video_id = ValueParser.parse_youtube_video_id(args.video) if args.video else None
        return RuntimeOptions(
            limit=limit,
            video_id=video_id,
            cache=args.cache,
            verbose=verbose,
            transcript_languages=cls.parse_transcript_languages(
                values.get("TRANSCRIPT_LANGUAGES", "").strip()
            ),
        )
