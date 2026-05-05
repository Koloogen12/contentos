"""Audio file transcription. Splits files >24MB into chunks before sending."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from pydub import AudioSegment

from app.config import settings
from app.services import ai_client

logger = logging.getLogger(__name__)

CHUNK_TARGET_MB = 20
WHISPER_HARD_LIMIT_MB = 25


def _file_size_mb(path: Path) -> float:
    return path.stat().st_size / (1024 * 1024)


def _split_into_chunks(src: Path) -> list[Path]:
    audio = AudioSegment.from_file(src)
    duration_ms = len(audio)
    target_size_mb = CHUNK_TARGET_MB
    estimated_mb = _file_size_mb(src)
    chunks_n = max(1, int(estimated_mb // target_size_mb) + 1)
    chunk_ms = duration_ms // chunks_n + 1

    out_dir = Path(tempfile.mkdtemp(prefix="audiochunk-", dir=settings.TEMP_DIR))
    paths: list[Path] = []
    for i in range(chunks_n):
        start = i * chunk_ms
        end = min(duration_ms, (i + 1) * chunk_ms)
        if start >= end:
            break
        seg = audio[start:end]
        out = out_dir / f"part-{i:03d}-{uuid.uuid4().hex[:6]}.mp3"
        seg.export(out, format="mp3", bitrate="64k")
        paths.append(out)
    return paths


async def transcribe(local_path: Path) -> str:
    if _file_size_mb(local_path) <= WHISPER_HARD_LIMIT_MB:
        return await ai_client.transcribe_audio(local_path)

    chunks = await asyncio.to_thread(_split_into_chunks, local_path)
    try:
        texts: list[str] = []
        for c in chunks:
            texts.append(await ai_client.transcribe_audio(c))
        return "\n".join(t.strip() for t in texts if t.strip())
    finally:
        for c in chunks:
            try:
                c.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            chunks[0].parent.rmdir()
        except (OSError, IndexError):
            pass
