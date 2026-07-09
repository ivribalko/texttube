# TextTube

macOS TextTube app summarizes recent videos from your YouTube subscriptions with a local Ollama model, filters out Shorts, uses local MLX Whisper audio-to-text when caption fallback is needed, and sends one Telegram message per video.

If a video cannot be fully processed, TextTube keeps the subscription run moving and sends a generic fallback Telegram message for that video.
If the whole run fails after app startup, such as a YouTube OAuth refresh failure, TextTube sends a generic Telegram failure message.

Design and runtime layout live in [ARCHITECTURE.md](ARCHITECTURE.md). Summarization instructions and output rules live in [SUMMARIZER.md](SUMMARIZER.md).

## Requirements

- Python 3.10+ available as `python3` for checkout runs. The Homebrew package uses `python@3.14`.
- Homebrew for installed scheduled runs.
- Ollama available on `127.0.0.1:11434`. Checkout runs only check readiness; installed init starts the Homebrew-managed `ollama` service when available.
- `ffmpeg` installed on the Mac host.
- Telegram bot token and chat ID.
- Google OAuth client credentials and refresh token for scheduled subscription runs, or a short-lived `YOUTUBE_ACCESS_TOKEN` environment variable for manual checkout runs.

Python dependency versions are pinned in [requirements.txt](requirements.txt) for both checkout runs and the Homebrew package.

## Secrets

Run interactive init to create `.secrets` for the active runtime.

For Homebrew installs:

```sh
texttube init
```

For checkout runs:

```sh
./texttube init
```

Init asks for Telegram and Google OAuth values, opens Google OAuth in a browser, and writes `GOOGLE_OAUTH_REFRESH_TOKEN` automatically. It never asks you to paste the refresh token.

`YOUTUBE_ACCESS_TOKEN` is optional only as a shell environment variable for repo checkout manual runs through `./texttube` or `./texttube run`. `.secrets` and scheduled service runs ignore it and continue to use the refresh-token flow.

Optional one-off or service-level model overrides:

- `OLLAMA_MODEL` overrides the default summarization model, which is `gemma4:e4b-mlx`.
- `MLX_WHISPER_MODEL` overrides the default audio-to-text model used for caption fallback, which is `mlx-community/whisper-large-v3-turbo`.
- `TRANSCRIPT_LANGUAGES` sets the preferred native caption languages in order, comma-separated, for example `en,es`.
- `TEXTTUBE_LIMIT` sets the default per-run video limit when `--limit` is not passed.
- `TEXTTUBE_VERBOSE` sets the default log verbosity when `--verbose` is not passed. Only the exact value `true` enables it; every other value disables it.

## Homebrew Install

Install the scheduled TextTube service from the public Homebrew tap:

```sh
brew install ivribalko/texttube/texttube
```

Initialize Homebrew runtime secrets and schedule:

```sh
texttube init
```

Installed init writes only under `$(brew --prefix)/var/texttube`, including `.secrets` and `service.cron`, then restarts the Homebrew-managed TextTube service. The service schedule uses a standard five-field cron expression such as `0 18 * * *`.

The packaged service uses:

- a private Homebrew-managed virtualenv under `$(brew --prefix)/var/texttube/venv`
- `$(brew --prefix)/var/texttube/.secrets` for runtime secrets
- `$(brew --prefix)/var/texttube/service.cron` for the Homebrew service schedule
- `$(brew --prefix)/var/texttube/var/logs/texttube.log` for scheduled-run and per-run helper logs
- `$(brew --prefix)/var/texttube/var/state/last_subscription_window_end_utc.txt` for the last successful subscription-run cutoff
- `$(brew --prefix)/var/texttube/var` for runtime artifacts, including single-video transcript and audio cache reuse under `var/cache/`

Manual service control:

```sh
brew services info ollama
```

```sh
brew services info texttube
```

```sh
brew services restart texttube
```

```sh
brew services stop texttube
```

## Checkout Setup

Checkout commands are local-only. `./texttube` never installs, configures, starts, stops, taps, untaps, or trusts Homebrew packages or services, and it only changes files inside the repository checkout.

Create or update checkout `.secrets`:

```sh
./texttube init
```

Run one checkout pass:

```sh
./texttube run
```

Checkout runs are not schedulable through `./texttube`; scheduling belongs to the Homebrew-installed service.

Developers who need both repositories can clone them independently as siblings:

```sh
git clone https://github.com/ivribalko/texttube.git
```

```sh
git clone https://github.com/ivribalko/homebrew-texttube.git
```

## OAuth Setup Notes

Optional one-off access token flow:

- use the [OAuth Playground](https://developers.google.com/oauthplayground/) with the `https://www.googleapis.com/auth/youtube.readonly` scope and:

```sh
YOUTUBE_ACCESS_TOKEN='your_oauth_playground_access_token' ./texttube --video 'https://www.youtube.com/watch?v=VIDEO_ID'
```

Durable refresh-token flow:

- Enable YouTube Data API v3 and create OAuth client credentials for a desktop app in Google Cloud `https://console.cloud.google.com/apis/library`
- In Google Cloud, add your Google account as a test user if the OAuth app is still in `Testing`: `https://console.cloud.google.com/auth/audience`
- Use init or auth to create or renew the stored Google refresh token:

```sh
texttube auth
```

```sh
./texttube auth
```

- The auth helper opens Google OAuth consent in a browser, listens only on `http://127.0.0.1:8080`, exchanges the callback code, and updates only `GOOGLE_OAUTH_REFRESH_TOKEN` in the active `.secrets` without printing tokens.

Official docs:

- [YouTube Data API Python quickstart](https://developers.google.com/youtube/v3/quickstart/python)
- [Google OAuth 2.0 for iOS & Desktop Apps](https://developers.google.com/identity/protocols/oauth2/native-app)

## Scheduled Run

The single `texttube` Homebrew service uses the cron expression written by installed `texttube init` and lets Homebrew trigger the run directly.

- `texttube init` asks for a standard five-field cron expression, for example `0 18 * * *`.
- Each subscription run processes videos published between the previous successful subscription-run cutoff and the current run start, then records the new cutoff in `$(brew --prefix)/var/texttube/var/state/last_subscription_window_end_utc.txt`.
- Duplicate Telegram messages are possible when a creator removes and reuploads the same content because YouTube gives the reupload a new video ID and publish timestamp.
- Checkout manual subscription runs can bypass the saved cutoff once with `./texttube --reset-cutoff`; the scheduled Homebrew service does not use that override.

## Manual Run

Print top-level help:

```sh
./texttube --help
```

Runtime defaults can come from `.secrets` or the shell environment. For overlapping settings, command-line flags take precedence over environment variables, and environment variables take precedence over `.secrets`.
Error logs hide exception details by default. Pass `--verbose` to print full exception text.

Run one subscription pass from the repository checkout with no limit and a reset cutoff:

```sh
./texttube --limit 0 --reset-cutoff
```

The checkout launcher runs one TextTube pass when no subcommand is provided.
For manual checkout runs, the launcher verifies Ollama is reachable on `127.0.0.1:11434` and exits with instructions when it is not.
If `YOUTUBE_ACCESS_TOKEN` is set in the shell environment that launches `./texttube` or `./texttube run`, the checkout launcher uses it directly for YouTube API calls instead of refreshing `GOOGLE_OAUTH_REFRESH_TOKEN`. The token is not read from `.secrets`.

Manual one-video run with cache reuse and verbose logging:

```sh
./texttube --video "https://www.youtube.com/watch?v=video_id" --cache --verbose
```

Preferred native caption selection comes from `TRANSCRIPT_LANGUAGES` in `.secrets` or the shell environment. For example, `TRANSCRIPT_LANGUAGES=en,en-US` makes TextTube try matching English native caption tracks before other available transcript languages and only fall back to the rest when no preferred match exists. If YouTube exposes an auto-generated transcript for the original spoken audio language and that language matches your list, TextTube promotes that language ahead of the rest of the fallback order while still preferring a manual transcript in that same language when available.
When TextTube knows the selected native transcript language code, it passes that code to the local summary model explicitly so summaries stay in the transcript language more reliably.

## Matrix Run

- Re-run the same cached video across a full override matrix with a small `zsh` helper.
- Use `""` in any override array to keep the default value for that run.
- Use the exact value `"ollama list"` in `OLLAMA_MODELS` to expand to every locally installed Ollama model:

```sh
VIDEO="https://www.youtube.com/watch?v=video_id"
OLLAMA_MODELS=("ollama list")
MLX_WHISPER_MODELS=("" "mlx-community/whisper-large-v3")
SUMMARIZER_PROMPTS=("" "SUMMARIZER.alt.md")

resolved_ollama_models=()
for requested_model in "${OLLAMA_MODELS[@]}"; do
  if [[ "${requested_model}" == "ollama list" ]]; then
    resolved_ollama_models+=("${(@f)$(ollama list | awk 'NR > 1 { print $1 }')}")
  else
    resolved_ollama_models+=("${requested_model}")
  fi
done

for ollama_model in "${resolved_ollama_models[@]}"; do
  for mlx_whisper_model in "${MLX_WHISPER_MODELS[@]}"; do
    for summarizer_prompt in "${SUMMARIZER_PROMPTS[@]}"; do
      (
        unset OLLAMA_MODEL MLX_WHISPER_MODEL SUMMARIZER_MD
        [[ -n "${ollama_model}" ]] && export OLLAMA_MODEL="${ollama_model}"
        [[ -n "${mlx_whisper_model}" ]] && export MLX_WHISPER_MODEL="${mlx_whisper_model}"
        [[ -n "${summarizer_prompt}" ]] && export SUMMARIZER_MD="${summarizer_prompt}"
        printf 'Running with OLLAMA_MODEL=%q MLX_WHISPER_MODEL=%q SUMMARIZER_MD=%q\n' \
          "${OLLAMA_MODEL}" "${MLX_WHISPER_MODEL}" "${SUMMARIZER_MD}"
        ./texttube --video "${VIDEO}" --verbose --cache
      )
    done
  done
done
```
