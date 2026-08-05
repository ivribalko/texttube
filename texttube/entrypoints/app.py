"""Application CLI parsing and dependency composition for TextTube runs."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from texttube.adapters.openai import (
    OpenAIAudioTranscriber,
    OpenAISummarizer,
    import_openai,
    split_summary_prompts,
)
from texttube.adapters.state import (
    ApplicationLifecycle,
    ConsoleLog,
    FileCachePaths,
    FileSubscriptionState,
)
from texttube.adapters.telegram import TelegramDelivery
from texttube.adapters.transcripts import NativeTranscriptFetcher, TranscriptResolver
from texttube.adapters.youtube import YouTubeDiscovery
from texttube.config import (
    DEFAULT_VIDEO_LIMIT,
    GENERIC_RUN_FAILURE_MESSAGE,
    GOOGLE_OAUTH_AUTH_COMMAND,
    GOOGLE_OAUTH_REAUTHORIZATION_MESSAGE,
    MAX_AUDIO_TRANSCRIPTION_DURATION_SECONDS,
    MAX_SHORT_DURATION_SECONDS,
    MAX_VIDEO_PROCESSING_ATTEMPTS,
    OPENAI_SUMMARY_MODEL,
    ConfigLoader,
    RuntimePaths,
)
from texttube.domain import FatalError, GoogleOAuthReauthorizationRequired
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
    parser.add_argument(
        "--cache",
        action="store_true",
        help="reuse and update transcript caches",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show detailed progress and errors",
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
        log = ConsoleLog(verbose=False, log_dir=paths.log_dir())
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
            essential=True,
        )
        options = ConfigLoader.load_runtime_options(parse_args(arguments))
        log.verbose = options.verbose
        log.write("startup: parse args")
        log.write("startup: load config")
        config = ConfigLoader.load_app_config(paths.google_refresh_token_path())
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

        requests_module = import_requests()
        session = requests_module.Session()
        lifecycle.add_cleanup(session.close)
        openai_sdk = import_openai().OpenAI(
            api_key=config.openai_api_key,
            max_retries=0,
        )
        lifecycle.add_cleanup(openai_sdk.close)

        delivery = TelegramDelivery(session, config, log)
        policy = ProcessingPolicy(
            max_short_duration_seconds=MAX_SHORT_DURATION_SECONDS,
            max_audio_duration_seconds=MAX_AUDIO_TRANSCRIPTION_DURATION_SECONDS,
            default_video_limit=DEFAULT_VIDEO_LIMIT,
            max_video_processing_attempts=MAX_VIDEO_PROCESSING_ATTEMPTS,
        )
        transcription = TranscriptResolver(
            NativeTranscriptFetcher(options.transcript_languages, log),
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
            FileCachePaths(paths, enabled=options.cache),
            policy,
            log,
        )
        application = ApplicationPipeline(
            YouTubeDiscovery(session, config, log),
            video_pipeline,
            delivery,
            FileSubscriptionState(paths.state_root),
            policy,
            log,
        )

        log.write("startup: load prompt", essential=True)
        log.write(f"prompt: {paths.display_path(prompt_path)}")
        log.write(f"openai: summary={OPENAI_SUMMARY_MODEL} transcription=disabled")
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
        log.write("interrupt: shutting down", essential=True)
        return 130
    except FatalError as exc:
        log.write(f"fatal: {exc}", essential=True)
        _notify_run_failure(delivery, exc, log)
        return 1
    except Exception as exc:
        log.write(
            f"fatal: unexpected error: {log.exception(exc)}",
            essential=True,
        )
        _notify_run_failure(delivery, exc, log)
        return 1
    finally:
        lifecycle.cleanup()
        lifecycle.restore_signal_handlers()


def _notify_run_failure(
    delivery: TelegramDelivery | None,
    error: Exception,
    log: ConsoleLog,
) -> None:
    """Best-effort send the appropriate run-level failure message."""
    if delivery is None:
        return
    message = GENERIC_RUN_FAILURE_MESSAGE
    if isinstance(error, GoogleOAuthReauthorizationRequired):
        message = GOOGLE_OAUTH_REAUTHORIZATION_MESSAGE.format(
            auth_command=GOOGLE_OAUTH_AUTH_COMMAND
        )
    try:
        log.write("run failure: send telegram", essential=True)
        delivery.send_notice(message)
    except Exception:
        log.write("telegram run failure notification failed", essential=True)


if __name__ == "__main__":
    raise SystemExit(main())
