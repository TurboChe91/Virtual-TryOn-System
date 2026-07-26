"""Provider factory + optional chat helper for LLM style structuring."""

from __future__ import annotations

import json
import logging

import httpx

from ..config import Config
from ..logging_setup import redact
from .base import GenerationRequest, GenerationResult, ImageProvider, ProviderError
from .mock import MockImageProvider
from .openai_compat import OpenAICompatProvider, OpenAICompatSettings

__all__ = [
    "GenerationRequest",
    "GenerationResult",
    "ImageProvider",
    "ProviderError",
    "build_provider",
    "build_chat_fn",
]

logger = logging.getLogger(__name__)


def build_provider(config: Config) -> ImageProvider:
    if config.image_provider == "mock":
        return MockImageProvider(allowed=not config.is_production)
    return OpenAICompatProvider(
        OpenAICompatSettings(
            base_url=config.image_api_base_url,
            api_key=config.image_api_key,
            timeout_s=config.request_timeout_s,
            reference_mode=config.reference_mode,
            disable_watermark=config.disable_provider_watermark,
        )
    )


def build_chat_fn(config: Config):
    """Return chat_fn(system, user) -> str for style structuring, or None if unconfigured."""
    if not (config.text_model and config.text_api_key and config.text_api_base_url):
        return None

    def chat_fn(system: str, user: str) -> str:
        body = {
            "model": config.text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": 1200,
        }
        try:
            with httpx.Client(timeout=60) as client:
                response = client.post(
                    f"{config.text_api_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {config.text_api_key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"text model request failed: {redact(str(exc))}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"text model returned HTTP {response.status_code}: {redact(response.text[:300])}"
            )
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("text model returned an unexpected response shape") from exc

    return chat_fn
