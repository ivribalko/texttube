"""Application CLI parsing and dependency composition for TextTube runs."""

from __future__ import annotations

import argparse
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from texttube.adapters.google_auth import DeviceAuthorizationExpired, authorize
from texttube.adapters.openai import (
    OpenAIAudioTranscriber,
    OpenAISummarizer,
    import_openai,
    split_summary_prompts,
)
from texttube.adapters.state import (
    ApplicationLifecycle,
    ConsoleLog,
    FileSubscriptionState,
)
from texttube.adapters.telegram import TelegramDelivery
from texttube.adapters.transcripts import (
    NativeTranscriptFetcher,
    TranscriptProxyRotator,
    TranscriptResolver,
)
from texttube.adapters.youtube import YouTubeDiscovery
from texttube.config import (
    DEFAULT_VIDEO_LIMIT,
    GENERIC_RUN_FAILURE_MESSAGE,
    MAX_AUDIO_TRANSCRIPTION_DURATION_SECONDS,
    MAX_SHORT_DURATION_SECONDS,
    MAX_NATIVE_CAPTION_ATTEMPTS,
    OPENAI_SUMMARY_MODEL,
    AppConfig,
    ConfigLoader,
    RuntimePaths,
)
from texttube.domain import (
    FatalError,
    GoogleOAuthAuthorizationTimeout,
    GoogleOAuthReauthorizationRequired,
)
from texttube.pipeline import ApplicationPipeline, ProcessingPolicy, VideoPipeline


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the stable application command-line interface."""
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
    return parser.parse_args(arguments)


def import_requests():
    """Import requests with an operator-friendly missing dependency error."""
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise FatalError(
            "Missing Python dependency: requests. Install requirements.txt or use Docker."
        ) from exc
    return requests


def main(arguments: Sequence[str] | None = None) -> int:
    """Construct adapters, execute one application run, and map failures to exits."""
    paths = RuntimePaths.discover()
    try:
        log = ConsoleLog(log_dir=paths.log_dir())
    except OSError as exc:
        print(f"TextTube could not initialize its run log: {exc}", file=sys.stderr)
        return 1
    lifecycle = ApplicationLifecycle(log)
    lifecycle.add_cleanup(log.close)
    lifecycle.install_signal_handlers()
    delivery: TelegramDelivery | None = None
    try:
        log.write(
            f"run log: {paths.display_path(log.path)}",
        )
        options = ConfigLoader.load_runtime_options(parse_args(arguments))
        log.write("startup: parse args")
        log.write("startup: load config")
        config = ConfigLoader.load_app_config(paths.google_refresh_token_path())

        requests_module = import_requests()
        session = requests_module.Session()
        lifecycle.add_cleanup(session.close)
        delivery = TelegramDelivery(session, config, log)
        config, discovery = ensure_youtube_authorization(
            session,
            config,
            paths.google_refresh_token_path(),
            delivery,
            log,
        )

        prompt_path = paths.prompt_path()
        if not prompt_path.exists():
            raise FatalError(f"Missing summarizer prompt file: {prompt_path}")
        prompt_document = prompt_path.read_text(encoding="utf-8").strip()
        if not prompt_document:
            raise FatalError(f"Summarizer prompt file is empty: {prompt_path}")
        transcript_prompt, description_prompt = split_summary_prompts(prompt_document)
        transcript_prompt = transcript_prompt.replace(
            "{{TRANSCRIPT_LANGUAGES}}",
            ", ".join(options.transcript_languages),
        )

        openai_sdk = import_openai().OpenAI(
            api_key=config.openai_api_key,
            max_retries=0,
        )
        lifecycle.add_cleanup(openai_sdk.close)

        policy = ProcessingPolicy(
            max_short_duration_seconds=MAX_SHORT_DURATION_SECONDS,
            max_audio_duration_seconds=MAX_AUDIO_TRANSCRIPTION_DURATION_SECONDS,
            default_video_limit=DEFAULT_VIDEO_LIMIT,
            max_native_caption_attempts=MAX_NATIVE_CAPTION_ATTEMPTS,
        )
        proxy_rotator = (
            TranscriptProxyRotator(session, config.transcript_proxy, log)
            if config.transcript_proxy is not None
            else None
        )
        transcription = TranscriptResolver(
            NativeTranscriptFetcher(
                options.transcript_languages,
                log,
                proxy_config=config.transcript_proxy,
                proxy_rotator=proxy_rotator,
            ),
            OpenAIAudioTranscriber(openai_sdk, log),
            log,
        )
        video_pipeline = VideoPipeline(
            transcription,
            OpenAISummarizer(
                openai_sdk,
                transcript_prompt,
                description_prompt,
                log,
            ),
            delivery,
            policy,
            log,
        )
        application = ApplicationPipeline(
            discovery,
            video_pipeline,
            delivery,
            FileSubscriptionState(paths.state_root),
            policy,
            log,
        )

        log.write("startup: load prompt")
        log.write(f"prompt: {paths.display_path(prompt_path)}")
        log.write(f"openai: summary={OPENAI_SUMMARY_MODEL} transcription=disabled")
        if config.transcript_proxy is not None:
            log.write(
                "transcript proxy: enabled with automatic IP rotation",
            )
        if options.transcript_languages:
            log.write(
                "transcript languages: "
                f"{', '.join(options.transcript_languages)}"
            )
        if options.video_id:
            application.run_single_video(options.video_id)
        else:
            application.run_subscriptions(options.limit)
        return 0
    except KeyboardInterrupt:
        log.write("interrupt: shutting down")
        return 130
    except GoogleOAuthAuthorizationTimeout as exc:
        log.write(f"authorization: {exc}")
        return 1
    except FatalError as exc:
        log.write(f"fatal: {exc}")
        _notify_run_failure(delivery, log)
        return 1
    except Exception as exc:
        log.write(
            f"fatal: unexpected error: {log.exception(exc)}",
        )
        _notify_run_failure(delivery, log)
        return 1
    finally:
        lifecycle.cleanup()
        lifecycle.restore_signal_handlers()


def check_startup_authorization() -> int:
    """Validate Google authorization once before the scheduler starts."""
    paths = RuntimePaths.discover()
    log = ConsoleLog()
    lifecycle = ApplicationLifecycle(log)
    lifecycle.add_cleanup(log.close)
    lifecycle.install_signal_handlers()
    delivery: TelegramDelivery | None = None
    try:
        log.write("container startup: load authorization config")
        config = ConfigLoader.load_app_config(paths.google_refresh_token_path())
        requests_module = import_requests()
        session = requests_module.Session()
        lifecycle.add_cleanup(session.close)
        delivery = TelegramDelivery(session, config, log)
        ensure_youtube_authorization(
            session,
            config,
            paths.google_refresh_token_path(),
            delivery,
            log,
        )
        log.write("container startup: youtube authorization is valid")
        return 0
    except KeyboardInterrupt:
        log.write("interrupt: shutting down")
        return 130
    except GoogleOAuthAuthorizationTimeout as exc:
        log.write(f"authorization: {exc}")
        return 1
    except Exception as exc:
        log.write(f"container startup authorization failed: {log.exception(exc)}")
        _notify_run_failure(delivery, log)
        return 1
    finally:
        lifecycle.cleanup()
        lifecycle.restore_signal_handlers()


def ensure_youtube_authorization(
    session: Any,
    config: AppConfig,
    token_path: Path,
    delivery: TelegramDelivery,
    log: ConsoleLog,
) -> tuple[AppConfig, YouTubeDiscovery]:
    """Validate authorization or complete device authorization inside this run."""
    if config.google_refresh_token:
        discovery = YouTubeDiscovery(session, config, log)
        log.write("startup: validate youtube authorization")
        try:
            discovery.ensure_authorized()
        except GoogleOAuthReauthorizationRequired as exc:
            log.write(f"startup: authorization unavailable: {exc}")
        else:
            return config, discovery

    log.write("startup: request Google device authorization")
    stop_requested = threading.Event()
    try:
        destination = authorize(
            config.google_client_id,
            config.google_client_secret,
            token_path,
            stop_requested,
            present_instructions=lambda verification_url, user_code, expires_in: (
                _send_authorization_instructions(
                    delivery,
                    verification_url,
                    user_code,
                    expires_in,
                    log,
                )
            ),
        )
    except DeviceAuthorizationExpired as exc:
        log.write(f"authorization: {exc}")
        delivery.send_authorization_timeout_notice(exc.expires_in)
        raise GoogleOAuthAuthorizationTimeout(str(exc)) from exc
    if destination is None:
        raise KeyboardInterrupt
    refresh_token = ConfigLoader.read_google_refresh_token(destination)
    if not refresh_token:
        raise FatalError("Google authorization completed without a stored refresh token")
    authorized_config = replace(config, google_refresh_token=refresh_token)
    discovery = YouTubeDiscovery(session, authorized_config, log)
    log.write("startup: validate restored youtube authorization")
    discovery.ensure_authorized()
    log.write("startup: authorization restored; continue run")
    return authorized_config, discovery


def _send_authorization_instructions(
    delivery: TelegramDelivery,
    verification_url: str,
    user_code: str,
    expires_in: int,
    log: ConsoleLog,
) -> None:
    """Send the Google verification link and device code through Telegram."""
    log.write("authorization required: send telegram link and code")
    delivery.send_authorization_notice(verification_url, user_code, expires_in)


def _notify_run_failure(
    delivery: TelegramDelivery | None,
    log: ConsoleLog,
) -> None:
    """Best-effort send one generic run-level failure message."""
    if delivery is None:
        return
    try:
        log.write("run failure: send telegram")
        delivery.send_notice(GENERIC_RUN_FAILURE_MESSAGE)
    except Exception:
        log.write("telegram run failure notification failed")


if __name__ == "__main__":
    raise SystemExit(main())
