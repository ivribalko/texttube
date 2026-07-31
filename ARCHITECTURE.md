# Architecture

TextTube is a Docker-first Python application that summarizes recent YouTube subscription uploads and sends one Telegram message per processed video.

This document is the canonical source for structure, data flow, processing rules, state, and failure behavior. [README.md](README.md) is the operator guide. [SUMMARIZER.md](SUMMARIZER.md) is the transcript-summary system prompt.

## Design Constraints

- All language and audio inference runs through OpenAI.
- The application remains a small Python program without an application framework.
- Python installation, dependency management, execution, and validation stay inside Docker; the host runs only Docker and Compose commands.
- Docker Compose is the packaged runtime and pulls the public GitHub Container Registry image.
- GitHub Actions publishes multi-platform images for 64-bit Intel/AMD and ARM Linux hosts.
- Compose receives API credentials through process environment variables and stores the Google refresh token only in its managed volume.
- The official YouTube captions API is not used for caption downloads.
- Model roles and cost choices are fixed application constants.
- Scheduled runs are singletons.

## Repository Layout

- `texttube_app.py` owns application configuration, YouTube access, captions, OpenAI calls, Telegram delivery, CLI behavior, lifecycle handling, and subscription state.
- `texttube_auth.py` validates Google refresh tokens, manages device authorization, exposes health readiness, and securely stores replacement tokens.
- `texttube_scheduler.py` parses cron expressions, waits for scheduled occurrences, locks runs, launches the application, and forwards shutdown signals.
- `SUMMARIZER.md` defines transcript-summary input and output behavior.
- `Dockerfile` builds the shared Linux image with `texttube_app.py` as its direct entrypoint.
- `.github/workflows/publish-container.yaml` builds and publishes the image after pushes to `main`.
- `compose.yaml` defines the persistent `auth` and `scheduler` services, profiled manual `app` service, health dependency, environment mapping, and named data volume.
- `compose.local.yaml` overrides the authorization and manual application services with a build from the current repository source.
- `requirements.txt` pins the Python dependencies.
- `README.md` documents setup, deployment, commands, and validation.
- `AGENTS.md` contains repository working conventions.

## Runtime Layout

The workflow publishes `ghcr.io/ivribalko/texttube:latest` as a multi-platform image for `linux/amd64` and `linux/arm64`. It also publishes an immutable `sha-<commit>` tag for each source revision. Compose always pulls `latest`, stores immutable application files under `/app`, sets `TEXTTUBE_HOME=/data`, and mounts the managed `texttube-data` volume at `/data/var`:

- `/data/var/state/last_subscription_window_end_utc.txt` stores the last completed subscription cutoff.
- `/data/var/state/google_oauth_refresh_token` stores the Google refresh token with mode `0600`.
- `/data/var/cache/<video_id>.txt` stores an optional transcript cache entry.
- `/data/var/cache/<video_id>.m4a` stores an optional audio cache entry.
- `/data/var/texttube.lock` serializes scheduled runs.

Manual runs create or reuse cache entries only when `--cache` is present. Temporary uncached audio and chunks are deleted after each video. The scheduler subprocess inherits the scheduler container’s stdout and stderr, so scheduler messages and scheduled application output share one service log. The persistent authorization service has a separate container log, while profiled manual application runs use attached stdout and stderr and are removed by the documented `--rm` workflow. The container runtime manages all retained output through its configured logging driver, and the data volume contains no log copy.

The local Compose override builds authorization and manual application services as `texttube:local` while leaving scheduled deployment on the published image.

## Configuration and Authentication

Compose maps its environment directly into its services. `ConfigLoader` reads process environment values, applies supported command-line overrides, and loads the Google refresh token from the managed volume. `TextTubeScheduler` reads and validates `CRON` independently. Required stack credentials are:

- `OPENAI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`

`TRANSCRIPT_LANGUAGES` is optional and controls native-caption preference order. `TEXTTUBE_LIMIT` and `TEXTTUBE_VERBOSE` provide CLI defaults. `SUMMARIZER_MD` selects the transcript prompt.

The Google credentials must use application type `TVs and Limited Input devices`. On startup, the persistent `auth` service exchanges any stored refresh token for an access token to validate it. A successful validation creates container-local readiness, and Compose starts the application services only after the authorization health check passes. Missing or rejected refresh tokens trigger the YouTube read-only device flow: the service shows Google’s verification URL and user code, polls at Google’s requested interval, and atomically writes the returned refresh token to the named volume with owner-only permissions. It never prints the refresh token and requires no callback port or browser inside the container.

## Application Components

- `TextTubeCli` parses arguments, applies defaults, creates the lifecycle owner, and converts fatal failures into process exit codes.
- `AuthorizationService` validates the stored token every hour, controls container health readiness, performs replacement device authorization, and owns atomic refresh-token storage.
- `TextTubeScheduler` validates a five-field expression with `croniter`, waits until each UTC occurrence, acquires the shared lock, and runs TextTube as a subprocess.
- `TextTubeApp` is the composition root: it wires runtime paths, shared external clients, focused services, and the selected run mode.
- `YouTubeClient` refreshes Google authorization, traverses subscriptions and upload playlists, enriches video metadata, deduplicates IDs, and enforces the subscription window.
- `VideoProcessor` owns the per-video use case: skip policy, summary fallback selection, message formatting, and delivery.
- `TranscriptService` owns transcript source selection and cache I/O, using native captions first and eligible audio transcription second.
- `TranscriptFetcher` discovers and retrieves native captions through `youtube-transcript-api`, ordered by configured language preference.
- `OpenAIAudioTranscriber` downloads eligible fallback audio with `yt-dlp`, creates five-minute chunks with `ffmpeg`, and transcribes chunks sequentially with `gpt-transcribe`.
- `OpenAIClient` uses the official OpenAI Python SDK and Responses API with `gpt-5.6-luna` for transcript and description summaries. Responses summary requests use `store: false`.
- `DescriptionCleaner` strips links before the description-summary request.
- `TelegramClient` formats HTML-safe messages, truncates them to Telegram limits, disables link previews, and sends run notices.
- `SubscriptionState`, `RuntimePaths`, `ValueParser`, `HttpJsonClient`, and `ApplicationLifecycle` provide state, path, parsing, HTTP, signal, and cleanup support.

## Per-Video Flow

- Videos with a known duration of three minutes or less are treated as probable Shorts and skipped.
- A cached transcript is used first when manual cache reuse is enabled.
- Native captions are attempted next.
- If native captions fail and the video is no longer than 60 minutes, audio is downloaded, chunked, and transcribed.
- If native captions fail and the video is longer than 60 minutes, no audio download or transcription is attempted.
- The resolved transcript is summarized with the transcript prompt.
- Any transcript retrieval, audio transcription, or transcript-summary failure switches to a title-guided OpenAI summary of the cleaned video description.
- If the description request also fails, the message body is `Summary unavailable.`.
- The final body is sent as one Telegram message with channel, title, and YouTube link.

Exactly 60 minutes remains eligible for audio transcription. The exclusion begins above 60 minutes.

## Summary Rules

`SUMMARIZER.md` applies only to transcript summaries. When the selected native caption language is known, TextTube prepends `Summary language code: <code>.` to the transcript input.

Description fallback has a separate code-owned prompt because its source and cleanup requirements differ:

- The video title establishes relevance but is not an independent factual source.
- The description supplies the factual content.
- Links, domains, social handles, promotions, affiliate text, calls to action, contacts, and channel boilerplate are removed.
- Output remains a compact plain-text paragraph.

The summary model is `gpt-5.6-luna`. The audio transcription model is `gpt-transcribe`. Neither is configurable at runtime.

## Subscription State and Scheduling

A subscription run records its start time as the prospective window end. The previous completed cutoff is the window start; when no cutoff exists, the start defaults to 24 hours earlier.

The cutoff is written only after subscription traversal completes. Fatal authentication, subscription, or run-level failures preserve the previous cutoff. Per-video fallbacks and failures do not abort traversal. A manual reset is performed outside the application by deleting the cutoff file while holding the scheduler lock.

The default message limit is 100. Probable Shorts do not count toward it. Successfully delivered transcript or description-fallback messages do count. When the default cap stops a run, TextTube sends a final limit notice.

The scheduler:

- requires one standard five-field expression from `CRON`
- rejects cron shortcuts and invalid expressions before waiting
- evaluates occurrences in UTC and recalculates after every completed or skipped run
- inherits the Compose environment and launches each run as an isolated subprocess
- invokes the application under a non-blocking `fcntl` file lock
- forwards application output directly to the scheduler container’s standard streams
- forwards `SIGINT` and `SIGTERM` to an active application subprocess

The authorization service:

- starts with the default Compose stack
- shares the application’s managed data volume
- remains unhealthy while no recently validated refresh token exists
- validates an existing refresh token at startup and every hour
- automatically performs device authorization when the token is missing or Google returns `invalid_grant`
- retries transient validation and authorization failures without exposing credentials
- stays running after authorization so Docker can continuously report health

The manual application and scheduler services declare a Compose dependency on healthy authorization. This gates their startup; Docker Compose does not stop an already-running dependent service if authorization later becomes unhealthy.

## Failure Behavior

- Every individual HTTP request is attempted once. The OpenAI client is configured with `max_retries=0`; device authorization performs protocol-required status polling rather than transport retries.
- Expected per-video failures switch to description fallback or allow later videos to continue.
- Fatal failures after Telegram initialization trigger a run-level Telegram notice.
- Google OAuth `invalid_grant` produces a reauthorization-specific notice and preserves the subscription window.
- Error details are hidden unless verbose logging is enabled.
- `SIGINT` and `SIGTERM` close the shared HTTP session and return exit code `130`.
- Invalid scheduler configuration exits with code `2`; scheduler shutdown signals interrupt waiting and propagate to an active application run.
- Authorization health is removed before replacement authorization, after failed validation, and during shutdown. Transient Google or network failures retry after one minute.

## External Dependencies

- Python standard library modules handle CLI parsing, configuration, files, subprocesses, signals, and state.
- `requests` handles Google device authorization, token refresh, YouTube Data API calls, and Telegram delivery.
- `openai` is the official client for Responses API summaries and audio transcription.
- `croniter` validates cron expressions and calculates their next UTC occurrence.
- `youtube-transcript-api` retrieves native captions.
- `yt-dlp`, its matching EJS challenge solver, and Deno download eligible fallback audio from YouTube.
- `ffmpeg` extracts and chunks audio inside the container.
- Python `fcntl` locking and the util-linux `flock` command coordinate scheduled runs and lock-safe maintenance.
- GitHub Actions and GitHub Container Registry build and distribute the public runtime image.
