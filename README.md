# TextTube

TextTube watches recent YouTube subscription uploads, creates concise summaries with OpenAI, and sends one Telegram message per processed video. Docker Compose pulls the public multi-platform image from GitHub Container Registry and provides persistent Google authorization, scheduling, and manual runs.

See [ARCHITECTURE.md](ARCHITECTURE.md) for processing rules, failure behavior, and component design. The transcript-summary prompt is [SUMMARIZER.md](SUMMARIZER.md).

## Requirements

- Docker Engine with Docker Compose
- An OpenAI API key with API billing enabled
- A Telegram bot token and target chat ID
- A Google Cloud project with YouTube Data API v3 enabled
- Google OAuth credentials with application type `TVs and Limited Input devices`

All inference runs through OpenAI using the official OpenAI Python SDK. The container does not download or run model weights.

## Prepare Google OAuth Credentials

In the [Google Cloud console](https://console.cloud.google.com/):

- [Enable YouTube Data API v3](https://console.cloud.google.com/apis/library/youtube.googleapis.com).
- [Configure the OAuth consent screen](https://console.cloud.google.com/auth/branding) and [add the Google account as a test user](https://console.cloud.google.com/auth/audience) when the app remains in testing.
- [Create an OAuth client ID](https://console.cloud.google.com/auth/clients) with application type [`TVs and Limited Input devices`](https://developers.google.com/youtube/v3/guides/auth/devices).
- Supply its client ID and client secret through the [Compose environment](compose.yaml).

[Desktop-app OAuth credentials do not work with the device authorization service](https://developers.google.com/youtube/v3/guides/auth/devices).

## Start and Authorize YouTube

Start the stack from the directory containing the deployed `compose.yaml`. On first startup, keep Compose attached so the authorization URL and device code remain visible:

```sh
docker compose up --pull always
```

The unified `texttube` service checks the managed volume for a refresh token. When the token is missing, expired, or revoked, it prints Google’s verification URL and device code and polls for approval. Open the URL on any phone or computer, enter the displayed code, and approve YouTube read-only access.

The service starts scheduling after authorization becomes healthy. Approval stores the refresh token with owner-only permissions in the managed `texttube-data` volume. The refresh token is never printed or placed in a Compose environment variable.

Deployment interfaces that start Compose in the background must expose the service log so the URL and code can be read. Follow it from a separate shell when the Compose file is available:

```sh
docker compose logs --follow texttube
```

The service validates the refresh token when it starts and once per hour. A failed validation makes `texttube` unhealthy and automatically prints a new device login. Google authorizations for external apps left in [Testing status expire after seven days](https://support.google.com/cloud/answer/15549945). Production refresh tokens have [no single fixed lifetime](https://developers.google.com/identity/protocols/oauth2#expiration); Google lists revocation, six months without use, time-limited access, and token-count limits among the reasons they can stop working.

No callback port, public domain, workstation helper, repository checkout, or `compose.local.yaml` is required on the server.

Manual commands use `docker compose run` and do not disturb the long-running service.

## Local Source Runs

Keep local Compose configuration in a repository-root `.env` file. The file is ignored by Git, and `compose.local.yaml` requires and loads it for containers built from the current source. Define the required credentials listed under [Requirements](#requirements), plus `CRON` when running the default `serve` mode.

Authorize YouTube with the current source:

```sh
docker compose --env-file .env \
  --file compose.yaml --file compose.local.yaml \
  run --build --rm texttube auth --once
```

Run one subscription pass with the current source:

```sh
docker compose --env-file .env \
  --file compose.yaml --file compose.local.yaml \
  run --build --rm texttube app
```

Append application arguments after `app` for a selected-video or diagnostic run.

## Compose Commands

Pull the latest published image:

```sh
docker compose pull
```

Recreate the unified service with the latest image:

```sh
docker compose up --detach --pull always
```

Run one subscription pass:

```sh
docker compose run --rm texttube app
```

Run one video with cache reuse and verbose logging:

```sh
docker compose run --rm texttube app \
  --video "https://www.youtube.com/watch?v=VIDEO_ID" --cache --verbose
```

Run without the default 100-message limit:

```sh
docker compose run --rm texttube app --limit 0
```

Validate or replace Google authorization in a one-off container:

```sh
docker compose run --rm texttube auth --once
```

## Logs

Authorization, scheduler, and scheduled application output share the `texttube` service’s standard streams. Follow them with timestamps:

```sh
docker compose logs --follow --timestamps texttube
```

Each scheduled or manual application run also writes its visible output to a separate UTC-timestamped file under `/data/var/logs/`. A new app run removes these files when they are 30 days old or older. Inspect retained run logs from the managed volume:

```sh
docker compose exec texttube ls -l /data/var/logs
```

Manual `app` and `auth` runs still write directly to their attached terminal. The documented `app --rm` workflow removes its one-off container while preserving the application run file in the managed volume. Authorization and scheduler-only messages remain standard-stream output and are available through Docker’s configured logging driver.

## Runtime Configuration

Compose supplies application values through the process environment. Command-line flags override overlapping runtime defaults. `TEXTTUBE_HOME` is fixed at `/data`, and the built-in `SUMMARIZER.md` is used.

`CRON` is required by the default `serve` mode but is ignored by manual application and authorization commands. It must be a standard five-field cron expression and is evaluated in UTC; shortcuts such as `@daily` are not accepted.

`TRANSCRIPT_LANGUAGES` is an ordered, comma-separated list. When YouTube's default audio language belongs to that list, its native captions are attempted first; otherwise configured order is preserved. Captions in every other available language follow. YouTube's caption or default-audio language code is passed to transcript summarization when available. A transcript already in one of the configured languages is summarized in the same language; for any other transcript language, the model chooses the most appropriate configured language and translates the summary into it. Audio is downloaded and transcribed only when every available native caption fails.

The model names are application constants rather than operator settings:

- `gpt-5.6-luna` generates transcript and description summaries.
- `gpt-transcribe` transcribes fallback audio.

## Important Processing Boundaries

- Videos up to three minutes long are treated as probable Shorts and skipped.
- Videos with unknown duration or a duration longer than 60 minutes may use native captions but never download or transcribe audio.
- A failed transcript path falls back to a title-guided summary of the cleaned video description.
- If the description summary also fails, TextTube sends `Summary unavailable.`.
- The subscription cutoff advances after the run finishes, including runs where individual videos use fallbacks.
- Scheduled runs never overlap.

The complete processing and failure rules are canonical in [ARCHITECTURE.md](ARCHITECTURE.md).

## Runtime Data

Compose persists credentials, state, and caches in the managed `texttube-data` volume mounted at `/data/var`.

- `/data/var/state/google_oauth_refresh_token` stores the Google refresh token with owner-only permissions.
- `/data/var/state/last_subscription_window_end_utc.txt` stores the completed subscription cutoff.
- `/data/var/cache/` stores transcript and audio entries created by manual runs with `--cache`.
- `/data/var/logs/` stores one timestamped file for each application run and retains it for less than 30 days.
- `/data/var/texttube.lock` enforces singleton scheduled runs.

Removing the OAuth token makes the service request approval again at its next validation; restarting `texttube` triggers that check immediately. Deleting the cutoff file resets the next subscription window to the previous 24 hours; the lock-safe maintenance command is documented in [AGENTS.md](AGENTS.md).

## Validation

Run all validation in Docker. No local Python installation or execution is required.

```sh
OPENAI_API_KEY=check \
TELEGRAM_BOT_TOKEN=check \
TELEGRAM_CHAT_ID=check \
GOOGLE_OAUTH_CLIENT_ID=check \
GOOGLE_OAUTH_CLIENT_SECRET=check \
CRON='your five-field expression' \
docker compose config --quiet
```

```sh
docker build --tag texttube:check .
```

```sh
docker run --rm --entrypoint python texttube:check \
  -m compileall -q /app
```

## Container Publication

A push to `main` runs [.github/workflows/publish-container.yaml](.github/workflows/publish-container.yaml). GitHub Actions builds one `linux/amd64` and `linux/arm64` image manifest and publishes it to `ghcr.io/ivribalko/texttube` with:

- `latest`, consumed by `compose.yaml`
- `sha-<commit>`, an immutable source-revision tag

GitHub creates the package as private on its first publication even though this repository is public. After the first successful workflow run:

- Open the `texttube` package settings under the `ivribalko` GitHub account.
- Change package visibility to **Public**.
- Re-run the workflow or pull `ghcr.io/ivribalko/texttube:latest` anonymously to verify public access.

The visibility change is permanent. Later pushes update the already-public package without registry credentials in the deployment environment.

## License

TextTube is available under the [MIT License](LICENSE).

## References

- [Publishing Docker images with GitHub Actions](https://docs.github.com/actions/tutorials/publish-packages/publish-docker-images)
- [GitHub Container Registry](https://docs.github.com/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [YouTube Data API Python quickstart](https://developers.google.com/youtube/v3/quickstart/python)
- [Google OAuth 2.0 for TVs and Limited-Input Devices](https://developers.google.com/youtube/v3/guides/auth/devices)
- [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [OpenAI transcription API](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
- [Official OpenAI Python SDK](https://github.com/openai/openai-python)
