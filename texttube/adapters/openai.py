"""OpenAI summary and audio-transcription adapters with subprocess tooling."""

from __future__ import annotations

import hashlib
import html
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from texttube.config import (
    OPENAI_SUMMARY_MODEL,
    OPENAI_SUMMARY_TIMEOUT_SECONDS,
    OPENAI_TRANSCRIPTION_MODEL,
    OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS,
)
from texttube.domain import FatalError, Transcript, Video, VideoFailure
from texttube.ports import Log

TRANSCRIPT_SUMMARIZER_HEADING = "# Transcript summarizer"
DESCRIPTION_SUMMARIZER_HEADING = "# Description summarizer"


def content_fingerprint(content: str) -> str:
    """Return a stable digest for correlating content without logging its text."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def split_summary_prompts(document: str) -> tuple[str, str]:
    """Split and validate the transcript and description prompt contracts."""
    normalized = document.strip()
    description_marker = f"\n{DESCRIPTION_SUMMARIZER_HEADING}\n"
    if not normalized.startswith(f"{TRANSCRIPT_SUMMARIZER_HEADING}\n"):
        raise FatalError(
            f"Summary prompt must start with `{TRANSCRIPT_SUMMARIZER_HEADING}`."
        )
    transcript_prompt, separator, description_body = normalized.partition(
        description_marker
    )
    transcript_body = transcript_prompt.removeprefix(
        TRANSCRIPT_SUMMARIZER_HEADING
    ).strip()
    if not transcript_body:
        raise FatalError(
            "Summary prompt must contain a nonempty "
            f"`{TRANSCRIPT_SUMMARIZER_HEADING}` section."
        )
    if not separator or not description_body.strip():
        raise FatalError(
            "Summary prompt must contain a nonempty "
            f"`{DESCRIPTION_SUMMARIZER_HEADING}` section."
        )
    return (
        transcript_prompt.strip(),
        f"{DESCRIPTION_SUMMARIZER_HEADING}\n{description_body.strip()}",
    )


def import_openai() -> Any:
    """Import the official SDK with an operator-friendly missing dependency error."""
    try:
        import openai
    except ModuleNotFoundError as exc:
        raise FatalError(
            "Missing Python dependency: openai. Install requirements.txt or use Docker."
        ) from exc
    return openai


class DescriptionCleaner:
    """Removes links before description text is sent to the summary model."""

    LINK_PATTERN = re.compile(
        r"(?i)(?:https?://|www\.)\S+|"
        r"\b[\w.-]+\.(?:com|org|net|io|co|tv|me|gg|ly)(?:/\S*)?"
    )

    @classmethod
    def prepare_for_model(cls, description: str) -> str:
        """Strip links and blank lines from description content."""
        without_links = cls.LINK_PATTERN.sub("", html.unescape(description))
        return "\n".join(
            line.strip() for line in without_links.splitlines() if line.strip()
        )


class OpenAISummarizer:
    """Creates transcript and description summaries through the Responses API."""

    def __init__(
        self,
        client: Any,
        transcript_prompt: str,
        description_prompt: str,
        log: Log,
    ):
        self.client = client
        self.transcript_prompt = transcript_prompt
        self.description_prompt = description_prompt
        self.log = log

    def _generate(self, prompt: str, *, instructions: str) -> str:
        """Generate one plain-text response through the official SDK."""
        openai_module = import_openai()
        try:
            response = self.client.with_options(
                timeout=OPENAI_SUMMARY_TIMEOUT_SECONDS,
            ).responses.create(
                model=OPENAI_SUMMARY_MODEL,
                instructions=instructions,
                input=prompt,
                max_output_tokens=500,
                store=False,
            )
        except openai_module.OpenAIError as exc:
            raise VideoFailure(
                f"OpenAI request failed for {OPENAI_SUMMARY_MODEL}: "
                f"{self.log.exception(exc)}"
            ) from exc
        generated = str(response.output_text or "").strip()
        if not generated:
            raise VideoFailure(f"OpenAI returned no text for {OPENAI_SUMMARY_MODEL}")
        return generated

    def summarize_transcript(self, video: Video, transcript: Transcript) -> str:
        """Summarize transcript text using the repository prompt contract."""
        prompt = self.build_summary_prompt(
            transcript.text,
            language_code=transcript.language_code,
        )
        if not prompt:
            raise VideoFailure("summary unavailable: transcript was empty")
        return self._summarize(
            prompt,
            instructions=self.transcript_prompt,
            video_id=video.video_id,
            source="transcript",
        )

    def summarize_description(self, video: Video) -> str:
        """Summarize cleaned video metadata when transcript processing fails."""
        cleaned_description = DescriptionCleaner.prepare_for_model(video.description)
        if not cleaned_description:
            raise VideoFailure("description summary unavailable: description was empty")
        self.log.write(
            f"summary: description cleaned video={video.video_id} "
            f"chars={len(video.description)}->{len(cleaned_description)}"
        )
        prompt = f"Title: {video.title.strip()}\n\nDescription:\n{cleaned_description}"
        summary = self._summarize(
            prompt,
            instructions=self.description_prompt,
            video_id=video.video_id,
            source="description",
        )
        link_free_summary = DescriptionCleaner.prepare_for_model(summary)
        if not link_free_summary:
            raise VideoFailure("description summary unavailable: only links remained")
        normalized_summary = re.sub(r"\s+", " ", link_free_summary).strip()
        self.log.write(
            f"summary: description final video={video.video_id} "
            f"chars={len(summary)}->{len(normalized_summary)}"
        )
        return normalized_summary

    def _summarize(
        self,
        prompt: str,
        *,
        instructions: str,
        video_id: str,
        source: str,
    ) -> str:
        """Run one timed summary request and normalize its output."""
        started_at = time.monotonic()
        try:
            self.log.write(
                f"summary: request video={video_id} source={source} "
                f"model={OPENAI_SUMMARY_MODEL} chars={len(prompt)} "
                f"input_sha256={content_fingerprint(prompt)} "
                f"instructions_sha256={content_fingerprint(instructions)}"
            )
            summary = self._generate(prompt, instructions=instructions).strip()
            if not summary:
                raise VideoFailure("summary unavailable: model returned an empty response")
            self.log.write(
                f"summary: ok video={video_id} source={source} "
                f"model={OPENAI_SUMMARY_MODEL} chars={len(summary)} "
                f"lines={len(summary.splitlines())}"
            )
            self.log.write(
                f"summary: result video={video_id} source={source}:\n{summary}"
            )
            return summary
        except VideoFailure as exc:
            self.log.write(
                f"summary: failed video={video_id} source={source} "
                f"model={OPENAI_SUMMARY_MODEL}: "
                f"{self.log.exception(exc)}"
            )
            raise VideoFailure(f"summary unavailable: {exc}") from exc
        finally:
            elapsed = time.monotonic() - started_at
            self.log.write(
                f"summary: duration video={video_id} source={source} "
                f"model={OPENAI_SUMMARY_MODEL}: elapsed={elapsed:.1f}s",
                essential=True,
            )

    @staticmethod
    def build_summary_prompt(
        transcript: str,
        *,
        language_code: str = "",
    ) -> str:
        """Attach the YouTube source language fact when available."""
        cleaned_transcript = transcript.strip()
        if not cleaned_transcript:
            return ""
        normalized_language_code = language_code.strip().lower()
        if not normalized_language_code:
            return cleaned_transcript
        return (
            f"YouTube source language code: {normalized_language_code}.\n\n"
            f"{cleaned_transcript}"
        )


class OpenAIAudioTranscriber:
    """Downloads YouTube audio and transcribes resource-sized chunks with OpenAI."""

    def __init__(self, client: Any, log: Log):
        self.client = client
        self.log = log

    def fetch(self, video_id: str) -> Transcript:
        """Download temporary audio, split it, and transcribe sequentially."""
        self.log.write(
            f"transcript audio: start {video_id} model={OPENAI_TRANSCRIPTION_MODEL}"
        )
        started_at = time.perf_counter()
        try:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            with tempfile.TemporaryDirectory(prefix="texttube-audio-") as temp_dir:
                temp_path = Path(temp_dir)
                output_template = str(temp_path / "audio.%(ext)s")
                command = [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--no-playlist",
                    "--extract-audio",
                    "--audio-format",
                    "m4a",
                    "--output",
                    output_template,
                    video_url,
                ]
                try:
                    self.log.write(f"transcript audio: yt-dlp {video_id}")
                    completed = subprocess.run(
                        command,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=3600,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise VideoFailure(
                        "audio transcript unavailable: audio download timed out"
                    ) from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()[-1:]
                    reason = detail[0] if detail else str(exc)
                    raise VideoFailure(
                        f"audio transcript unavailable: yt-dlp failed: {reason[:300]}"
                    ) from exc
                audio_files = [path for path in temp_path.iterdir() if path.is_file()]
                if not audio_files:
                    detail = completed.stderr.strip().splitlines()[-1:]
                    reason = (
                        detail[0]
                        if detail
                        else "yt-dlp did not create an audio file"
                    )
                    raise VideoFailure(
                        f"audio transcript unavailable: {reason[:300]}"
                    )
                audio_path = max(
                    audio_files, key=lambda path: path.stat().st_size
                )
                self.log.write(
                    f"transcript audio: downloaded {audio_path.name}"
                )
                chunk_dir = temp_path / "chunks"
                chunk_dir.mkdir()
                segment_command = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(audio_path),
                    "-f",
                    "segment",
                    "-segment_time",
                    "300",
                    "-c",
                    "copy",
                    str(chunk_dir / "chunk-%03d.m4a"),
                ]
                try:
                    self.log.write(f"transcript audio: ffmpeg segment {video_id}")
                    subprocess.run(
                        segment_command,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=1800,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise VideoFailure(
                        "audio transcript unavailable: audio chunking timed out"
                    ) from exc
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or str(exc)).strip().splitlines()[-1:]
                    reason = detail[0] if detail else str(exc)
                    raise VideoFailure(
                        f"audio transcript unavailable: ffmpeg failed: {reason[:300]}"
                    ) from exc
                chunk_paths = sorted(chunk_dir.glob("chunk-*.m4a")) or [audio_path]
                text = self._transcribe_chunks(chunk_paths)
                self.log.write(
                    f"transcript audio: ok {video_id}: openai {len(chunk_paths)} chunks"
                )
                self.log.write(
                    f"transcript audio: result {video_id}: chars={len(text)} "
                    f"lines={len(text.splitlines())} sha256={content_fingerprint(text)}"
                )
                return Transcript(text=text)
        finally:
            duration = time.perf_counter() - started_at
            self.log.write(
                f"transcript audio: duration {video_id}: {duration:.1f}s",
                essential=True,
            )

    def _transcribe_chunks(self, chunk_paths: list[Path]) -> str:
        """Transcribe audio chunks sequentially to bound container resources."""
        openai_module = import_openai()
        results: list[str] = []
        for chunk_path in chunk_paths:
            self.log.write(
                f"transcript audio: openai chunk {chunk_path.name} "
                f"model={OPENAI_TRANSCRIPTION_MODEL}"
            )
            try:
                with chunk_path.open("rb") as audio_file:
                    response = self.client.with_options(
                        timeout=OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS,
                    ).audio.transcriptions.create(
                        model=OPENAI_TRANSCRIPTION_MODEL,
                        file=audio_file,
                        response_format="json",
                    )
            except openai_module.OpenAIError as exc:
                raise VideoFailure(
                    "audio transcript unavailable: OpenAI request failed: "
                    f"{self.log.exception(exc)}"
                ) from exc
            text = str(response.text or "").strip()
            if not text:
                raise VideoFailure(
                    "audio transcript unavailable: OpenAI returned no text"
                )
            results.append(text)
        text = "\n".join(part for part in results if part).strip()
        if not text:
            raise VideoFailure("audio transcript unavailable: OpenAI returned no text")
        return text
