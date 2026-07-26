"""Vision-LLM helpers: identity extraction and style recognition from images.

The chat channel resolves from the active kind='llm' profile first, then the
LUNELLE_TEXT_* environment settings. Responses are parsed defensively: models
wrap JSON in prose/fences at will, and vocab fields are validated upstream by
StyleSpec, so anything invalid degrades to defaults instead of failing hard.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from .config import Config
from .db import Database
from .logging_setup import redact
from .profiles import ProfileService

logger = logging.getLogger(__name__)

ChatFn = Callable[[str, str, list[Path]], str]


class LLMUnavailable(Exception):
    """No LLM channel configured (neither an active llm profile nor env text settings)."""


class _Channel(Protocol):  # pragma: no cover - typing aid
    base_url: str
    api_key: str
    model: str


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def build_llm_chat(config: Config, db: Database) -> ChatFn:
    """Return chat(system, user, images) -> str, or raise LLMUnavailable."""
    row = ProfileService(db).active_row(kind="llm")
    if row is not None:
        base_url, api_key, model = row["base_url"], row["api_key"], row["model"]
    elif config.text_model and config.text_api_key and config.text_api_base_url:
        base_url, api_key, model = config.text_api_base_url, config.text_api_key, config.text_model
    else:
        raise LLMUnavailable(
            "no LLM channel: activate an llm profile in settings or set LUNELLE_TEXT_* in .env"
        )

    def chat(system: str, user: str, images: list[Path]) -> str:
        content: list[dict] = [{"type": "text", "text": user}]
        for image in images:
            content.append({"type": "image_url", "image_url": {"url": _data_url(image)}})
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_tokens": 2000,
        }
        try:
            with httpx.Client(timeout=120) as client:
                response = client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LLM request failed: {redact(str(exc))}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"LLM returned HTTP {response.status_code}: {redact(response.text[:300])}"
            )
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM returned an unexpected response shape") from exc

    return chat


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (fences/prose tolerated)."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        doc = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM reply is not valid JSON: {text[:200]}") from exc
    if not isinstance(doc, dict):
        raise ValueError("LLM reply JSON is not an object")
    return doc


IDENTITY_SYSTEM = (
    "You are a senior press-on-nail product analyst. You describe nail designs "
    "precisely, in English, for an image-generation identity contract."
)

IDENTITY_USER = """Look at this press-on nail set image (a 2x5 plan grid of ten nails, or a worn set).
Write the per-nail identity file in EXACTLY this format, one line per nail, nail-01 through nail-10:

- nail-01: <colors, motifs with counts, decorations, finish>. Shape hint: <length + silhouette>.
- nail-02: ...
(continue through nail-10)

Rules:
- Grid order is authoritative when a 2x5 grid is shown:
  top row left-to-right = nail-01..05, bottom row = nail-06..10.
- Name concrete visible features (exact motif counts, materials, placement); never vague style labels.
- If two nails are near-twins, say so and state the distinguishing detail.
- End with one line starting "SET-WIDE:" that locks the shared palette and materials.
Output only the identity text, no preamble."""

STYLE_SYSTEM = (
    "You are a senior press-on-nail merchandiser. You convert a nail design photo "
    "into a structured product spec for an e-commerce catalog. Answer with JSON only."
)

STYLE_USER = """Analyze this nail design image and output ONE JSON object with these keys:
{"name": "<short product name in English, max 6 words>",
 "description": "<one-paragraph English description of the design>",
 "base_colors": ["..."], "accent_colors": ["..."],
 "elements": ["<visible motifs/decorations>"], "texture": ["<finishes>"],
 "shape": "<one of: almond|coffin|square|tapered-square|squoval|oval|round|stiletto>",
 "length": "<one of: short|medium|long|extra-long>",
 "visual_style": "<max 12 words>", "avoid": [],
 "notes": "<anything crucial for faithful reproduction, max 60 words>"}
Use only what is visible. JSON only, no commentary."""


def identify_nail_identities(chat: ChatFn, image: Path) -> str:
    reply = chat(IDENTITY_SYSTEM, IDENTITY_USER, [image]).strip()
    if "nail-01" not in reply or "nail-10" not in reply:
        raise ValueError("LLM identity reply is missing nail-01..nail-10 lines")
    return reply


def style_fields_from_image(chat: ChatFn, image: Path) -> dict:
    doc = extract_json(chat(STYLE_SYSTEM, STYLE_USER, [image]))
    fields: dict = {}
    for key in ("name", "description", "visual_style", "notes", "shape", "length"):
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()
    for key in ("base_colors", "accent_colors", "elements", "texture", "avoid"):
        value = doc.get(key)
        if isinstance(value, list):
            fields[key] = [str(item) for item in value if str(item).strip()][:10]
    return fields
