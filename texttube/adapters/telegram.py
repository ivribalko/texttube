"""Telegram message formatting and delivery adapter."""

from __future__ import annotations

import html
from typing import Any

from texttube.config import AppConfig, REQUEST_TIMEOUT_SECONDS
from texttube.domain import DeliveryFailure, Summary, SummarySource, Video
from texttube.ports import Log

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
DESCRIPTION_FALLBACK_NOTICE = "Summary based on the video description."


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
