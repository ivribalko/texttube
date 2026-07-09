"""Interactive setup helper for checkout and Homebrew TextTube runtimes.

This file owns local secret collection, cron schedule persistence, and the
browser OAuth flow used by `texttube init` and `./texttube init`.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import texttube_auth

DEFAULT_CRON = "0 18 * * *"
DEFAULT_TRANSCRIPT_LANGUAGES = "en"
SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "TRANSCRIPT_LANGUAGES",
)


def read_dotenv(path: Path) -> dict[str, str]:
    """Read a small dotenv file into key/value pairs."""
    return texttube_auth.read_dotenv(path) if path.exists() else {}


def quote_dotenv_value(value: str) -> str:
    """Return a dotenv-safe representation for one value."""
    if re.fullmatch(r"[A-Za-z0-9_./:@,+* -]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_dotenv(path: Path, values: dict[str, str]) -> None:
    """Atomically write TextTube secrets with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={quote_dotenv_value(value)}" for key, value in values.items()]
    fd, temp_name = tempfile.mkstemp(
        prefix=".secrets.",
        dir=str(path.parent),
        text=True,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        temp_file.write("\n".join(lines) + "\n")
    os.chmod(temp_name, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temp_name, path)


def prompt_value(
    key: str,
    explanation: str,
    *,
    existing: str = "",
    default: str = "",
    secret: bool = False,
) -> str:
    """Prompt for one configuration value while allowing existing values."""
    print("", file=sys.stderr)
    print(explanation, file=sys.stderr)
    suffix = ""
    if existing:
        suffix = " [press Enter to keep existing]"
    elif default:
        suffix = f" [{default}]"
    prompt = f"{key}{suffix}: "
    raw_value = getpass.getpass(prompt) if secret else input(prompt)
    value = raw_value.strip()
    if value:
        return value
    if existing:
        return existing
    return default


def validate_required(key: str, value: str) -> None:
    """Reject an empty required setup value."""
    if not value.strip():
        raise ValueError(f"{key} is required")


def validate_cron(value: str) -> str:
    """Validate the supported five-field cron expression shape."""
    normalized = " ".join(value.strip().split())
    if normalized.startswith("@"):
        raise ValueError("Cron nicknames such as @daily are not supported")
    fields = normalized.split()
    if len(fields) != 5:
        raise ValueError("Use exactly five cron fields: minute hour day-of-month month weekday")
    return normalized


def validate_transcript_languages(value: str) -> None:
    """Validate comma-separated transcript language codes."""
    for raw_part in value.split(","):
        language_code = raw_part.strip().lower()
        if not language_code:
            continue
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language_code):
            raise ValueError(
                f"Invalid transcript language '{raw_part.strip()}'; use values like en,en-us,ru"
            )


def collect_values(existing: dict[str, str], *, ask_cron: bool, existing_cron: str) -> tuple[dict[str, str], str]:
    """Collect all init values from the terminal."""
    print("TextTube interactive setup", file=sys.stderr)
    print("Secrets are written locally and are not printed back to the terminal.", file=sys.stderr)

    cron_value = existing_cron
    if ask_cron:
        while True:
            try:
                cron_value = validate_cron(
                    prompt_value(
                        "TEXTTUBE_SERVICE_CRON",
                        "Enter a standard five-field cron schedule for the Homebrew service. "
                        "Example: 0 18 * * * runs daily at 18:00 local time.",
                        existing=existing_cron,
                        default=DEFAULT_CRON,
                    )
                )
                break
            except ValueError as exc:
                print(f"Invalid schedule: {exc}", file=sys.stderr)

    prompts = {
        "TELEGRAM_BOT_TOKEN": (
            "Telegram bot token from BotFather. It usually looks like a numeric prefix, "
            "a colon, and a long token body.",
            True,
            "",
        ),
        "TELEGRAM_CHAT_ID": (
            "Telegram target chat id. User chats are usually numeric; groups and channels "
            "are often negative numeric ids.",
            False,
            "",
        ),
        "GOOGLE_OAUTH_CLIENT_ID": (
            "Google Cloud Desktop OAuth client id. Enable YouTube Data API v3, create an "
            "OAuth client for a desktop app, then copy the client id.",
            False,
            "",
        ),
        "GOOGLE_OAUTH_CLIENT_SECRET": (
            "Google Cloud Desktop OAuth client secret from the same OAuth client.",
            True,
            "",
        ),
        "TRANSCRIPT_LANGUAGES": (
            "Preferred native caption languages, comma-separated. Use language codes such "
            "as en,es or en,en-us.",
            False,
            DEFAULT_TRANSCRIPT_LANGUAGES,
        ),
    }

    prompted_values = {key: existing.get(key, "") for key in SECRET_KEYS}
    for key in SECRET_KEYS:
        explanation, is_secret, default = prompts[key]
        while True:
            try:
                prompted_values[key] = prompt_value(
                    key,
                    explanation,
                    existing=prompted_values.get(key, ""),
                    default=default,
                    secret=is_secret,
                ).strip()
                if key != "TRANSCRIPT_LANGUAGES":
                    validate_required(key, prompted_values[key])
                else:
                    validate_transcript_languages(prompted_values[key])
                break
            except ValueError as exc:
                print(f"Invalid value: {exc}", file=sys.stderr)

    values = dict(existing)
    values.update(prompted_values)
    return values, cron_value


def run_oauth(secrets_path: Path) -> None:
    """Run Google OAuth and persist the refresh token into the target secrets file."""
    env = os.environ.copy()
    env["TEXTTUBE_SECRETS_PATH"] = str(secrets_path)
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("texttube_auth.py"))], env=env)
    if result.returncode != 0:
        raise RuntimeError("Google OAuth did not complete successfully")


def restart_homebrew_services() -> None:
    """Restart Homebrew-managed services needed by the installed runtime."""
    subprocess.run(["brew", "services", "start", "ollama"], check=False)
    subprocess.run(["brew", "services", "restart", "texttube"], check=True)


def init_runtime(*, mode: str, home: Path) -> int:
    """Initialize one TextTube runtime home."""
    secrets_path = home / ".secrets"
    cron_path = home / "service.cron"
    existing = read_dotenv(secrets_path)
    existing_cron = cron_path.read_text(encoding="utf-8").strip() if cron_path.exists() else ""

    values, cron_value = collect_values(
        existing,
        ask_cron=mode == "homebrew",
        existing_cron=existing_cron,
    )
    write_dotenv(secrets_path, values)
    if mode == "homebrew":
        cron_path.write_text(cron_value + "\n", encoding="utf-8")
        os.chmod(cron_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    run_oauth(secrets_path)

    if mode == "homebrew":
        restart_homebrew_services()
        print(f"Initialized Homebrew TextTube runtime at {home}", file=sys.stderr)
    else:
        print(f"Initialized checkout TextTube runtime at {home}", file=sys.stderr)
    return 0


def parse_args() -> argparse.Namespace:
    """Parse init helper arguments."""
    parser = argparse.ArgumentParser(description="Initialize TextTube local configuration.")
    parser.add_argument("--mode", choices=("checkout", "homebrew"), required=True)
    parser.add_argument("--home", required=True)
    return parser.parse_args()


def main() -> int:
    """Run TextTube interactive setup."""
    args = parse_args()
    try:
        return init_runtime(mode=args.mode, home=Path(args.home).expanduser().resolve())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"init failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
