"""Thin wrapper around CometAPI (OpenAI-compatible).

We isolate provider behind a single interface so we can later add a fallback
to Anthropic direct without touching call sites.
"""
import json
import logging
from typing import Any

from openai import AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings

logger = logging.getLogger(__name__)


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.COMETAPI_KEY,
        base_url=settings.COMETAPI_BASE_URL,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def chat_completion(
    *,
    system: str,
    user: str,
    model: str | None = None,
    json_mode: bool = False,
    temperature: float = 0.7,
    max_tokens: int = 3000,
) -> str:
    """Single-turn chat. Returns the assistant content string."""
    kwargs: dict[str, Any] = {
        "model": model or settings.COMETAPI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = await _client().chat.completions.create(**kwargs)
    content = response.choices[0].message.content
    if content is None:
        raise RuntimeError("Empty completion content from provider")
    return content


async def chat_json(
    *,
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 3000,
) -> dict[str, Any]:
    """Chat completion that must return valid JSON. Parses and returns the dict."""
    raw = await chat_completion(
        system=system,
        user=user,
        model=model,
        json_mode=True,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("AI returned invalid JSON: %s", raw[:500])
        raise RuntimeError(f"AI returned invalid JSON: {exc}") from exc


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def embed(text: str, model: str | None = None) -> list[float]:
    """Single text → embedding vector. Used for voice_samples retrieval."""
    response = await _client().embeddings.create(
        model=model or settings.COMETAPI_MODEL_EMBEDDING,
        input=text,
    )
    return list(response.data[0].embedding)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def transcribe_audio(local_path, model: str | None = None) -> str:
    """Whisper transcription. local_path is pathlib.Path or str pointing to a
    file on disk small enough for the provider (we chunk callers if not)."""
    with open(local_path, "rb") as f:
        response = await _client().audio.transcriptions.create(
            model=model or settings.COMETAPI_MODEL_WHISPER,
            file=f,
        )
    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Empty whisper response")
    return text
