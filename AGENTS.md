# Repository Instructions

- Read `ARCHITECTURE.md` before starting work and preserve its design.
- Keep `README.md` focused on operator setup, deployment, commands, and validation.
- Keep `ARCHITECTURE.md` canonical for components, data flow, state, processing rules, and failure behavior.
- Keep `SUMMARIZER.md` limited to the transcript-summary prompt contract.
- Keep Markdown synchronized with behavior changes without duplicating one topic across several files.
- Keep top-of-file source comments aligned with each file’s architectural role without restating detailed behavior.
- Use one Docker Compose service for authorization maintenance, scheduled runs, and explicit manual commands.
- Keep `compose.yaml` deployable by itself with the public image, environment variables, and a named data volume.
- Keep scheduler implementation in `texttube/adapters/scheduler.py`, dependency construction in `texttube/entrypoints/scheduler.py`, and `texttube_scheduler.py` as a compatibility entrypoint.
- Keep Compose pinned to the public `ghcr.io/ivribalko/texttube:latest` image.
- Publish `linux/amd64` and `linux/arm64` images from pushes to `main` with `latest` and immutable commit tags.
- Keep the application small and avoid framework dependencies.
- Keep API credentials in Compose environment variables and the Google refresh token only in the managed data volume.
- Never print the Google refresh token or expose it through a Compose environment variable.
- Send application output only to container stdout and stderr; do not duplicate logs on the managed data volume.
- Use `OPENAI_API_KEY` for all model API calls.
- Use the official OpenAI Python SDK for all OpenAI API calls.
- Keep the transcript-summary prompt in `SUMMARIZER.md`.
- Keep description-fallback requirements in `ARCHITECTURE.md` and the code-owned fallback prompt.
- Prefer constants over new configuration parameters unless configurability is explicitly requested.
- Do not use the official YouTube captions API for caption downloads; use `youtube-transcript-api` and eligible OpenAI audio transcription fallback.
- Preserve the rule that videos longer than 60 minutes never download or transcribe audio.
- Verify duration boundaries on both sides whenever the audio eligibility rule changes.
- Do not add automated tests unless project requirements change.
- Add a short purpose description to every class, struct, enum, and view.
- Do not use numbered lists in Markdown files.

## Manual Run

Authorize YouTube with the current repository source after configuring the required environment variables:

```sh
docker compose --file compose.yaml --file compose.local.yaml \
  run --build --rm texttube auth --once
```

Build and run the current repository source after completing Google authorization:

```sh
docker compose --file compose.yaml --file compose.local.yaml \
  run --build --rm texttube app
```

Run one selected video by passing its URL or ID after the service name:

```sh
docker compose --file compose.yaml --file compose.local.yaml \
  run --build --rm texttube app \
  --video "https://www.youtube.com/watch?v=VIDEO_ID" \
  --cache \
  --verbose
```

Append application arguments such as `--video URL_OR_ID`, `--cache`, `--limit N`, or `--verbose` after `app`. The local override preserves the Compose-managed environment and data volume while replacing the published image with a build from the current source. Manual-run output remains in the live terminal, and `--rm` removes the container when it exits.

## Subscription Cutoff

The application has no cutoff-reset flag or environment variable. To reset the subscription window manually, delete the cutoff file from the named volume while holding the scheduler lock:

```sh
docker compose exec texttube flock /data/var/texttube.lock \
  rm --force /data/var/state/last_subscription_window_end_utc.txt
```

The command waits for an active scheduled run to finish before deleting its saved cutoff. The next subscription run processes the previous 24 hours.

## Validation

- Do not run Python commands on the host; run every Python workflow inside Docker.
- Build with `docker build --tag texttube:check .` before Python validation.
- Compile image sources with `docker run --rm --entrypoint python texttube:check -m compileall -q /app`.
- Validate authorization health states, scheduler cron parsing, and Compose interpolation without rendering secrets.
- Use a representative manual run only when the user explicitly requests application execution.
- Before committing, run `git diff --check` and inspect the staged diff for credentials or personal data.
