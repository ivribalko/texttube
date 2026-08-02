# Architecture

TextTube is a Docker-first functional modular monolith that summarizes recent YouTube subscription uploads and sends one Telegram message per processed video.

This document is canonical for structure, dependency direction, data flow, state, processing rules, and failure behavior. [README.md](README.md) is the operator guide. [SUMMARIZER.md](SUMMARIZER.md) contains the summary prompt contracts.

## Design Constraints

- All language and audio inference runs through OpenAI.
- The application remains a small Python package without an application framework.
- Business orchestration has no SDK, HTTP, filesystem, or subprocess knowledge.
- Python installation, execution, and validation stay inside Docker.
- Docker Compose exposes one service backed by one managed data volume.
- The official YouTube captions API is not used for caption downloads.
- Model roles and cost choices are fixed application constants.
- Scheduled runs are singletons.

## Repository Layout

```text
texttube/
├── domain.py
├── config.py
├── ports.py
├── pipeline.py
├── adapters/
│   ├── google_auth.py
│   ├── openai.py
│   ├── scheduler.py
│   ├── service.py
│   ├── state.py
│   ├── telegram.py
│   ├── transcripts.py
│   └── youtube.py
└── entrypoints/
    ├── app.py
    ├── auth.py
    ├── scheduler.py
    └── service.py
```

- `domain.py` contains immutable `Video`, `Transcript`, `Summary`, and outcome values plus application-level failures.
- `ports.py` declares small interfaces for video discovery, transcription, summarization, delivery, state, cache paths, and logging.
- `pipeline.py` contains readable application and per-video orchestration.
- `config.py` owns constants, environment loading, normalized runtime options, value parsing, and runtime path discovery.
- `adapters/` contains every OpenAI, YouTube, Google OAuth, Telegram, HTTP, filesystem, cron, and subprocess implementation.
- `entrypoints/` contains the executable `app`, `auth`, `scheduler`, and `service` process surfaces. These modules parse commands, construct dependencies, and are launched with `python -m`.
- `SUMMARIZER.md` defines transcript and description summary input and output behavior.
- `Dockerfile` builds the shared Linux image.
- `compose.yaml` defines the single published-image service and managed volume.
- `compose.local.yaml` replaces the published image with a local build and loads the required repository-root `.env` file.

## Dependency Direction

Imports point inward:

```text
entrypoints → adapters → ports → domain
     │                      ↑
     └──────── pipeline ────┘
```

`pipeline.py` imports only `domain.py` and `ports.py`. Adapters may import domain values, port types, and configuration, but never the pipeline. Entrypoints are the only modules that know concrete adapter combinations. This keeps business flow and infrastructure from importing each other arbitrarily.

## Container Runtime

The workflow publishes `ghcr.io/ivribalko/texttube:latest` for `linux/amd64` and `linux/arm64`, plus an immutable `sha-<commit>` tag. Compose pulls `latest`, sets `TEXTTUBE_HOME=/data`, and mounts `texttube-data` at `/data/var`.

The container entrypoint accepts these modes:

- `serve` runs authorization maintenance and the cron scheduler under one supervisor. This is the Compose default.
- `app` performs one manual subscription or selected-video run.
- `auth --once` validates or replaces authorization and exits.
- `scheduler` runs the scheduler alone for diagnostics.
- `healthcheck` reports whether the stored refresh token was validated recently.

The image starts `texttube.entrypoints.service` as a Python module. The scheduler launches `texttube.entrypoints.app` the same way, and the Compose healthcheck invokes `texttube.entrypoints.auth` directly. These package modules are the only process launch surfaces.

In `serve` mode, authorization maintenance starts first. Scheduling starts after the first successful token validation, matching the former Compose health dependency. Both workers share shutdown state. If either worker exits unexpectedly, the supervisor stops the other and exits nonzero so the container restart policy can recover. Scheduler application runs remain isolated subprocesses, and signals are forwarded to an active subprocess.

Application output, scheduler messages, and authorization instructions remain visible on container stdout and stderr. Each scheduled or manual `app` invocation also writes its visible application output to a UTC-timestamped file in the managed volume. App startup removes run logs that are 30 days old or older. Manual runs remain attached and are removed by the documented `--rm` workflow without removing their volume-backed run logs.

Verbose application logging records the summary source, language hint, input and prompt fingerprints, model output, duration, and failures. Transcript and description input text and credentials are never logged.

The managed paths are:

- `/data/var/state/last_subscription_window_end_utc.txt` for the last completed subscription cutoff
- `/data/var/state/google_oauth_refresh_token` for the mode-`0600` Google refresh token
- `/data/var/cache/<video_id>.txt` for optional transcript cache entries
- `/data/var/cache/<video_id>.m4a` for optional audio cache entries
- `/data/var/logs/texttube-<UTC timestamp>.log` for one application run
- `/data/var/texttube.lock` for scheduled-run serialization

Manual runs create or reuse cache entries only with `--cache`. Temporary uncached audio and chunks are deleted after each video.

## Configuration and Authentication

Compose maps credentials directly into the unified service. Required credentials are:

- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`

Local source runs use the Git-ignored repository-root `.env` file through `compose.local.yaml`. Published-image deployments continue to accept values from the Compose environment without requiring that file.

`CRON` is required by `serve` and `scheduler` modes but ignored by manual `app` and `auth` commands. `TRANSCRIPT_LANGUAGES` controls native-caption preference order and acceptable transcript-summary languages. `TEXTTUBE_LIMIT` and `TEXTTUBE_VERBOSE` provide application defaults. `SUMMARIZER_MD` selects the summary prompt document outside the packaged Compose workflow.

Google credentials must use application type `TVs and Limited Input devices`. Authorization exchanges the stored refresh token for an access token at startup and hourly. A valid token updates container health readiness. A missing or rejected token triggers Google’s YouTube read-only device flow, prints only the verification URL and user code, polls at Google’s required interval, and atomically stores the replacement token with owner-only permissions. The refresh token is never printed or exposed through a Compose environment variable.

## Application Components

- `ApplicationPipeline` owns selected-video and subscription-window use cases, limit behavior, per-video isolation, and cutoff completion.
- `VideoPipeline` owns short-video policy, the audio eligibility decision, summary fallback selection, and delivery.
- `YouTubeDiscovery` refreshes Google authorization, traverses subscriptions and upload playlists, enriches metadata, deduplicates IDs, and enforces the subscription window.
- `TranscriptResolver` uses cached transcripts first, native captions second, and permitted audio transcription last.
- `NativeTranscriptFetcher` retrieves captions with `youtube-transcript-api`, promotes YouTube's default audio language when it is configured, and otherwise ranks configured languages in order.
- `OpenAIAudioTranscriber` downloads fallback audio with `yt-dlp`, creates five-minute chunks with `ffmpeg`, and transcribes chunks sequentially with `gpt-transcribe`.
- `OpenAISummarizer` uses the official OpenAI Python SDK and Responses API with `gpt-5.6-luna`. Summary requests use `store: false`.
- `TelegramDelivery` formats HTML-safe messages, truncates them to Telegram limits, disables link previews, and sends run notices.
- `FileSubscriptionState`, `FileCachePaths`, `ConsoleLog`, and `ApplicationLifecycle` adapt filesystem and process concerns. `ConsoleLog` tees visible application output to stderr and one timestamped run file, pruning files at the 30-day retention boundary when an app run starts.
- `AuthorizationService`, `CronScheduler`, and `StackService` provide authorization maintenance, isolated scheduling, and single-container supervision.

## Per-Video Flow

- Videos with a known duration of three minutes or less are probable Shorts and are skipped.
- A cached transcript is used first when manual cache reuse is enabled.
- Native captions are attempted next. YouTube's default audio language is first when it belongs to `TRANSCRIPT_LANGUAGES`; otherwise configured order is preserved. Every other available language follows.
- Any nonempty native transcript is summarized and translated when needed before audio fallback is considered.
- Only when every native caption fails and the video has a known duration no longer than 60 minutes is audio downloaded, chunked, and transcribed.
- If native captions fail and the duration is unknown or longer than 60 minutes, audio is never downloaded or transcribed.
- The resolved transcript is summarized with the transcript prompt.
- Transcript retrieval, transcription, or transcript-summary failure switches to a title-guided summary of the cleaned description.
- If description summarization also fails, the message body is `Summary unavailable.`.
- The final body is delivered with channel, title, and YouTube link.

Exactly 60 minutes remains eligible for audio transcription. Exclusion applies when duration is unknown or above 60 minutes.

## Summary Rules

`SUMMARIZER.md` contains required top-level transcript and description prompt sections. Startup validates and separates them before constructing the summarizer. TextTube passes YouTube's selected caption language code or default audio language code when known, plus the `TRANSCRIPT_LANGUAGES` preferences, only to the transcript contract. A transcript already in a preferred language is summarized in that language. For a transcript outside the preferred languages, including cached or audio text without language metadata, the model chooses the most appropriate preferred language and translates the summary into it. Without preferences, a known source language remains the summary language and unknown-language text uses its dominant language.

The description fallback contract is the second section of `SUMMARIZER.md`:

- The title establishes relevance but is not an independent factual source.
- The description supplies factual content.
- Links, domains, social handles, promotions, affiliate text, calls to action, contacts, and channel boilerplate are removed.
- Output is a compact plain-text paragraph.

The summary model is `gpt-5.6-luna`. The audio transcription model is `gpt-transcribe`. Neither is runtime-configurable.

## Subscription State and Scheduling

A subscription run records its start time as the prospective window end. The previous completed cutoff is the window start; without a cutoff, the start defaults to 24 hours earlier.

The cutoff is written only after subscription traversal completes. Fatal authentication, subscription, or run-level failures preserve the previous cutoff. Per-video fallbacks and failures do not abort traversal. Resetting the cutoff is an operator action performed while holding the scheduler lock.

The default message limit is 100. Probable Shorts do not count. Delivered transcript, description-fallback, and unavailable-summary messages count. Reaching the default cap sends a final limit notice.

The scheduler:

- requires one standard five-field expression from `CRON`
- rejects cron shortcuts and invalid expressions before waiting
- evaluates occurrences in UTC and recalculates after each run
- launches the application as an isolated subprocess under a non-blocking `fcntl` lock
- forwards application streams and shutdown signals

## Failure Behavior

- Every individual HTTP request is attempted once. The OpenAI client uses `max_retries=0`; device authorization performs protocol-required polling.
- Expected per-video failures use the description fallback or allow later videos to continue.
- Fatal failures after Telegram construction trigger a run-level notice.
- Google OAuth `invalid_grant` produces a reauthorization-specific notice and preserves the subscription window.
- Error details are hidden unless verbose logging is enabled.
- Application `SIGINT` and `SIGTERM` close shared clients and return exit code `130`.
- Invalid scheduler configuration exits with code `2`.
- Authorization readiness is removed before replacement authorization, after failed validation, and during shutdown. Transient failures retry after one minute.

## External Dependencies

- Python standard library modules handle CLI parsing, immutable values, files, subprocesses, signals, threads, and state.
- `requests` handles Google OAuth, YouTube Data API, and Telegram requests.
- `openai` is the official client for Responses API summaries and audio transcription.
- `croniter` validates expressions and calculates UTC occurrences.
- `youtube-transcript-api` retrieves native captions.
- `yt-dlp`, its EJS challenge solver, and Deno download eligible fallback audio.
- `ffmpeg` extracts and chunks audio in the container.
- Python `fcntl` and util-linux `flock` coordinate scheduled runs and maintenance.
- GitHub Actions and GitHub Container Registry build and distribute the public runtime image.
