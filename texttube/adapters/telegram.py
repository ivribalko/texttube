"""Telegram message formatting and delivery adapter."""

from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

from texttube.config import AppConfig, REQUEST_TIMEOUT_SECONDS
from texttube.domain import DeliveryFailure, Summary, SummarySource, Video
from texttube.ports import Log

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
DESCRIPTION_FALLBACK_NOTICE = "Summary based on the video description."


class TelegramCommands:
    """Consumes authorized chat commands with a durable update cursor."""

    def __init__(self, session: Any, config: AppConfig, cursor_path: Path, log: Log):
        self.session = session
        self.config = config
        self.cursor_path = cursor_path
        self.log = log
        self.started_at = int(time.time())
        self.offset = int(cursor_path.read_text()) if cursor_path.exists() else 0

    def register_menu(self) -> bool:
        """Publish the application command and enable Telegram's command menu."""
        import requests

        commands = [{"command": "texttube_restart", "description": "Restart TextTube processing"}]
        chat_id = self.config.telegram_chat_id
        operations = [
            ("setMyCommands", {"commands": commands}),
            ("setMyCommands", {"commands": commands, "scope": {"type": "chat", "chat_id": chat_id}}),
            ("setChatMenuButton", {"menu_button": {"type": "commands"}}),
        ]
        if chat_id.isdigit():
            operations.append(("setChatMenuButton", {"chat_id": chat_id, "menu_button": {"type": "commands"}}))
        try:
            for method, payload in operations:
                with self.session.post(
                    f"https://api.telegram.org/bot{self.config.telegram_bot_token}/{method}",
                    json=payload,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                ) as response:
                    if response.status_code != 200 or not response.json().get("ok"):
                        self.log.write(f"telegram menu: {method} failed (HTTP {response.status_code})")
                        return False
        except (requests.RequestException, ValueError):
            self.log.write("telegram menu: registration failed")
            return False
        self.log.write("telegram menu: registered /texttube_restart")
        return True

    def poll(self) -> bool:
        """Consume one update batch and coalesce fresh restart requests."""
        import requests

        try:
            with self.session.post(
                f"https://api.telegram.org/bot{self.config.telegram_bot_token}/getUpdates",
                json={"offset": self.offset, "timeout": 0, "allowed_updates": ["message"]},
                timeout=10,
            ) as response:
                if response.status_code != 200:
                    self.log.write(f"telegram commands: HTTP {response.status_code}")
                    return False
                payload = response.json()
            if not payload.get("ok"):
                self.log.write("telegram commands: polling rejected")
                return False
        except (requests.RequestException, ValueError):
            self.log.write("telegram commands: polling failed")
            return False
        requested = False
        for update in payload.get("result", []):
            self.offset = int(update["update_id"]) + 1
            message = update.get("message", {})
            if (
                str(message.get("chat", {}).get("id", "")) == self.config.telegram_chat_id
                and not message.get("from", {}).get("is_bot", False)
                and message.get("date", 0) >= self.started_at
                and message.get("text", "").strip() == "/texttube_restart"
            ):
                requested = True
        if payload.get("result"):
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cursor_path.with_suffix(".tmp")
            temporary.write_text(str(self.offset), encoding="utf-8")
            temporary.replace(self.cursor_path)
        return requested


class TelegramDelivery:
    """Formats and sends video summaries and run-level notices to Telegram."""

    def __init__(self, session: Any, config: AppConfig, log: Log):
        self.session = session
        self.config = config
        self.log = log

    @staticmethod
    def format_message(video: Video, summary: Summary) -> str:
        """Build one HTML-safe message within Telegram's length boundary."""
        link = f"https://youtu.be/{video.video_id}"
        header = f"<i>{html.escape(video.channel_title)}</i>: {html.escape(video.title)}"
        body = summary.text.strip()
        if summary.source is SummarySource.DESCRIPTION:
            body = f"{DESCRIPTION_FALLBACK_NOTICE}\n\n{body}"
        escaped_body = html.escape(body)
        message = f"{header}\n\n{escaped_body}\n\n{link}"
        if len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return message
        fixed_length = len(header) + len(link) + 4
        available_body_length = TELEGRAM_MAX_MESSAGE_LENGTH - fixed_length
        if available_body_length <= 20:
            return message[: TELEGRAM_MAX_MESSAGE_LENGTH - 1]
        kept_lines: list[str] = []
        used = 0
        for line in escaped_body.splitlines():
            addition = len(line) + (1 if kept_lines else 0)
            if used + addition > available_body_length - 15:
                break
            kept_lines.append(line)
            used += addition
        trimmed_body = "\n".join(kept_lines).strip()
        if not trimmed_body:
            trimmed_body = escaped_body[: available_body_length - 15].strip()
        return f"{header}\n\n{trimmed_body}\n\n{link}"

    def deliver(self, video: Video, summary: Summary) -> None:
        """Format and send one video summary."""
        self._send_message(self.format_message(video, summary))

    def send_notice(self, message: str) -> None:
        """Send one plain run-level notice."""
        self._send_message(html.escape(message))

    def send_authorization_notice(
        self,
        verification_url: str,
        user_code: str,
        expires_in: int,
    ) -> None:
        """Send a clickable authorization link and copyable device code."""
        safe_url = html.escape(verification_url, quote=True)
        safe_code = html.escape(user_code)
        lifetime = self._format_duration(expires_in)
        self._send_message(
            "TextTube needs Google authorization.\n\n"
            f"Tap code to copy: <code>{safe_code}</code>\n\n"
            f"{safe_url}\n\n"
            f"This code expires in {lifetime}."
        )

    def send_authorization_timeout_notice(self, expires_in: int) -> None:
        """Announce that the application stopped waiting for device approval."""
        self.send_notice(
            "Google authorization code expired after "
            f"{self._format_duration(expires_in)}. "
            "Start another TextTube run to receive a new link and code."
        )

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format an authorization TTL for concise Telegram copy."""
        if seconds % 60 == 0:
            minutes = seconds // 60
            unit = "minute" if minutes == 1 else "minutes"
            return f"{minutes} {unit}"
        unit = "second" if seconds == 1 else "seconds"
        return f"{seconds} {unit}"

    def _send_message(self, text: str) -> None:
        """Send one message through Telegram's Bot API."""
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise DeliveryFailure(
                "Telegram send failed: missing Python dependency requests"
            ) from exc
        url = (
            "https://api.telegram.org/bot"
            f"{self.config.telegram_bot_token}/sendMessage"
        )
        payload = {
            "chat_id": self.config.telegram_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
        }
        try:
            self.log.write("telegram send: preview=off")
            response = self.session.post(
                url,
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise DeliveryFailure(f"Telegram send failed: {exc}") from exc
        if response.status_code != 200:
            detail = response.text[:300].replace("\n", " ")
            raise DeliveryFailure(
                f"Telegram send failed: HTTP {response.status_code}: {detail}"
            )
        self.log.write("telegram send: ok")
