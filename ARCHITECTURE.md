# Architecture

TextTube is a small local Python app that summarizes recent YouTube subscription videos and sends one Telegram message per processed video.

This file is the canonical design reference for repository layout, component responsibilities, runtime state, and cross-component behavior. `README.md` stays focused on operating the app, and `SUMMARIZER.md` stays focused on model instructions and summary format.

## Repository Layout

- `texttube_app.py` is the single Python entrypoint. It owns configuration loading, YouTube API access, transcript fetching, audio-transcription fallback, Ollama calls, Telegram delivery, application command-line behavior, process signal handling, and subscription run state.
- `texttube` is the checkout launcher. It owns the user-facing CLI, local `.venv` setup, Ollama readiness checks, checkout OAuth refresh-token renewal, and Homebrew service installation from the repository checkout.
- `texttube_auth.py` is the checkout OAuth helper. It owns Google consent URL generation, the local OAuth callback listener, token exchange, and refresh-token replacement in `.secrets`.
- `Formula/texttube.rb` packages the scheduled Homebrew install. It stages the runtime files into `libexec`, installs a private virtualenv under Homebrew state, copies packaged `.secrets`, and defines the cron-style Homebrew service.
- `SUMMARIZER.md` is the single source of truth for summarization instructions and summary output rules.
- `README.md` is the operator guide for setup, secrets, manual runs, and service installation.
- `AGENTS.md` captures repository-specific working rules for Codex contributors.
- `.secrets` stores local runtime secrets and must never be committed.
- `requirements.txt` pins the Python dependencies used by both checkout runs and the packaged Homebrew install.
- `var/` holds mutable runtime artifacts for checkout runs such as logs, cached transcripts, cached audio files, and saved subscription cutoff state.
- `.venv/` is the shared repository-local Python environment used for checkout runs.
- `__pycache__/` is interpreter-generated cache output and has no design role.

## Runtime Layout

- Checkout runs use the repository root as `TEXTTUBE_HOME` when that variable is unset.
- Checkout runs keep mutable runtime data under `var/` in the repository checkout.
- Packaged Homebrew runs set `TEXTTUBE_HOME` to `$(brew --prefix)/var/texttube`.
- Packaged Homebrew runs keep their mutable runtime data under `$(brew --prefix)/var/texttube/var`.
- The persisted subscription window cutoff lives at `var/state/last_subscription_window_end_utc.txt` under the active runtime home.
- Optional single-video transcript reuse lives at `var/cache/<video_id>.txt` under the active runtime home.
- Optional single-video audio reuse lives at `var/cache/<video_id>.m4a` under the active runtime home.
- The lazily spawned `mlx-whisper` helper writes its log to `var/logs/mlx-whisper-run.log` under the active runtime home.

## Component Responsibilities

- `TextTubeCli` parses `--limit`, `--video`, `--cache`, `--verbose`, and `--reset-cutoff`, then applies runtime defaults for supported options from environment variables and `.secrets` before launching one application run through `TextTubeApp`.
- `TextTubeApp` wires together runtime paths, config, HTTP session lifecycle, prompt loading, YouTube access, summarization, Telegram delivery, run-level failure notification, and the single-video versus subscription control flow.
- `TranscriptSummarizer` applies the per-video decision flow: skip probable Shorts, reuse transcript and audio cache entries when enabled, prefer native captions, fall back to local audio transcription, summarize, and deliver the message.
- `YouTubeClient` resolves the bearer token, reads subscriptions and uploads playlists, batches video metadata enrichment, and enforces the subscription time window.
- `TranscriptFetcher` uses `youtube-transcript-api` for native caption discovery, orders transcript candidates by the configured language preferences, and converts unexpected transcript fetch errors into per-video failures.
- `MlxWhisperService`, `MlxHandler`, and `LazyMlxWhisperManager` implement the local `mlx-whisper` HTTP helper, its lazy lifecycle, and chunked audio transcription fallback.
- `OllamaClient` warms the configured local model, installs the model on first use when Ollama reports it missing, and generates transcript summaries.
- `TelegramClient` formats outbound messages and sends them through the Telegram Bot API with previews disabled.
- `ConfigLoader`, `RuntimePaths`, `SubscriptionState`, `ValueParser`, `HttpJsonClient`, and `ApplicationLifecycle` provide the shared support layer for configuration, paths, state, parsing, HTTP error handling, and cleanup.

## Component Interactions

- `./texttube` sets up the repository-local `.venv`, ensures the Homebrew-managed Ollama service is available, and launches `texttube_app.py` with `TEXTTUBE_MANUAL_RUN=1` for checkout runs.
- `./texttube auth` calls `texttube_auth.py` to renew `GOOGLE_OAUTH_REFRESH_TOKEN` in checkout `.secrets` by opening Google OAuth consent, listening for the local `127.0.0.1:8080` callback, and exchanging the callback code without printing tokens.
- `./texttube install --daily-time HH:MM` stages the packaged app files under checkout-local `var/homebrew/`, templates the schedule into the generated `local/texttube` Homebrew tap, maintains a local bare `origin` for that tap so Homebrew can update it cleanly, ensures Ollama is installed and started through Homebrew, and starts the scheduled TextTube service.
- `TextTubeApp` loads `.secrets` from the active runtime home, overlays environment variables, lets command-line flags override overlapping runtime defaults, resolves the prompt file, and creates one shared `requests.Session`.
- Checkout manual runs allow `YOUTUBE_ACCESS_TOKEN` only from the launching shell environment as a direct YouTube bearer token override. `.secrets` and scheduled service runs ignore that override and continue using the stored Google OAuth refresh token flow.
- A single-video run fetches one video’s metadata and skips the subscription traversal path.
- A subscription run computes the active window from the saved cutoff, defaulting the first run to the previous 24 hours, and advances the cutoff only after the run finishes.
- For each candidate video, TextTube enriches metadata, skips probable Shorts based on duration, obtains transcript text, generates the summary, and sends Telegram output before continuing to the next video.
- Unexpected per-video processing errors are downgraded to a generic fallback Telegram message so one bad video does not abort the rest of the subscription run.
- Fatal run-level failures after `TextTubeApp` is initialized, including YouTube OAuth refresh failures, send a generic Telegram notification.
- When native captions are unavailable, TextTube starts the local `mlx-whisper` helper only when needed, waits for `/healthz`, transcribes chunked audio through the fixed local HTTP endpoint, and stops the helper during application cleanup.
- Console `SIGINT` and `SIGTERM` converge on the shared application cleanup path, which closes the shared HTTP session, stops the managed `mlx-whisper` helper process group, and exits with code `130` for interrupts.
- Error logs hide exception details by default and include full raw exception text only when verbose logging is enabled.

## Processing Rules

- `SUMMARIZER.md` is loaded as the default system prompt for every Ollama call, optionally overridden by `SUMMARIZER_MD`.
- Preferred native caption selection comes from `TRANSCRIPT_LANGUAGES`, ordered as configured, with the original spoken-audio language promoted ahead of the rest of that order when YouTube exposes an auto-generated transcript in a configured language.
- When TextTube knows the selected native transcript language code, it prepends `Summary language code: <code>.` to the transcript prompt so the summary stays in that language more reliably.
- TextTube does not use the official YouTube captions API for caption downloads. It relies on `youtube-transcript-api` for native transcript retrieval and local audio transcription fallback when needed.
- Subscription processing deduplicates videos across channels and playlists and treats videos up to three minutes long as probable Shorts.
- Subscription processing deduplicates by YouTube video ID only, so creator reuploads can still produce duplicate Telegram messages when YouTube assigns a new video ID and publish timestamp to the same content.
- If transcript fetching, transcript fallback, or summary generation fails for a video, TextTube sends a generic Telegram failure message for that video.
- If authentication, subscription traversal, or another run-level step fails after app initialization, TextTube sends a generic Telegram failure message. Notification failures are logged and do not replace the original failure.
- If a subscription run stops because it reached the default `--limit` of `100`, TextTube sends one final Telegram message noting that the run ended at that cap.

## External Dependencies

- Python standard library modules handle CLI parsing, configuration loading, filesystem work, subprocess orchestration, local HTTP serving, and concurrency.
- `requests` handles Google OAuth, the YouTube Data API, Ollama, and Telegram Bot API calls.
- `youtube-transcript-api` handles native caption discovery.
- `yt-dlp` handles local audio download for fallback transcription.
- `mlx-whisper` handles Apple Silicon audio transcription through the local helper server.
- `ffmpeg` is required on the host Mac for audio decoding and chunking.
- Homebrew services manage the daily TextTube schedule and the local Ollama service used for summarization.

Keep the architecture simple. Add files only when `texttube_app.py` becomes genuinely hard to reason about, and keep external service behavior explicit at the call sites.
