"""YouTube transcription helpers — captions first, audio + whisper fallback."""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

from app.config import settings
from app.services import ai_client

logger = logging.getLogger(__name__)

_VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


async def fetch_meta(url: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        ydl_opts = {"quiet": True, "skip_download": True, "noprogress": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title"),
            "duration_seconds": info.get("duration"),
            "channel": info.get("uploader") or info.get("channel"),
            "video_id": info.get("id"),
        }

    return await asyncio.to_thread(_run)


def _captions_text(video_id: str, langs: list[str]) -> tuple[str, str] | None:
    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
        return None
    except Exception:
        logger.exception("captions list failed")
        return None

    chosen = None
    chosen_lang = None
    for lang in langs:
        try:
            chosen = transcripts.find_transcript([lang])
            chosen_lang = lang
            break
        except NoTranscriptFound:
            continue
    if chosen is None:
        try:
            chosen = transcripts.find_generated_transcript(langs)
            chosen_lang = chosen.language_code
        except NoTranscriptFound:
            return None

    try:
        chunks = chosen.fetch()
    except Exception:
        logger.exception("captions fetch failed")
        return None

    text = " ".join(c.get("text", "").strip() for c in chunks if c.get("text"))
    return text.strip(), chosen_lang or "unknown"


async def _download_audio(url: str) -> Path:
    out_dir = Path(tempfile.mkdtemp(prefix="ytdl-", dir=settings.TEMP_DIR))
    out_path = out_dir / f"{uuid.uuid4().hex}.m4a"

    def _run() -> Path:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_path),
            "quiet": True,
            "noprogress": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        if out_path.exists():
            return out_path
        for candidate in out_dir.iterdir():
            return candidate
        raise FileNotFoundError("yt-dlp produced no output")

    return await asyncio.to_thread(_run)


async def transcribe(url: str) -> dict[str, Any]:
    """Returns dict with: content, transcript_method, transcript_language, video metadata."""
    meta = await fetch_meta(url)
    video_id = meta.get("video_id") or extract_video_id(url)
    if not video_id:
        raise ValueError("Не удалось определить video_id из URL")

    captions = await asyncio.to_thread(
        _captions_text, video_id, ["ru", "en", "uk", "kk"]
    )
    if captions is not None:
        text, lang = captions
        if text:
            return {
                "content": text,
                "transcript_method": "youtube_captions",
                "transcript_language": lang,
                "youtube_url": url,
                "youtube_video_id": video_id,
                "youtube_title": meta.get("title"),
                "youtube_duration_seconds": meta.get("duration_seconds"),
            }

    audio_path = await _download_audio(url)
    try:
        text = await ai_client.transcribe_audio(audio_path)
    finally:
        try:
            audio_path.unlink(missing_ok=True)
            audio_path.parent.rmdir()
        except OSError:
            pass

    return {
        "content": text,
        "transcript_method": "whisper",
        "transcript_language": meta.get("language") or "auto",
        "youtube_url": url,
        "youtube_video_id": video_id,
        "youtube_title": meta.get("title"),
        "youtube_duration_seconds": meta.get("duration_seconds"),
    }
