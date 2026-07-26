"""Turn user input (structured fields and/or natural language) into a validated StyleSpec.

Two parsing paths:
- deterministic keyword parser (always available, no API cost, fully testable);
- optional LLM parser via an OpenAI-compatible chat endpoint. LLM output is
  never trusted as-is: it must parse as JSON and pass StyleSpec validation,
  otherwise we fall back to the deterministic result and record the error.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass

from pydantic import ValidationError

from . import vocab
from .schemas import SKU_RE, StyleCreateRequest, StyleSpec

logger = logging.getLogger(__name__)


class StyleInputError(Exception):
    """User input cannot produce a valid style spec."""


@dataclass
class ParseOutcome:
    spec: StyleSpec
    source_type: str  # structured | natural_language | hybrid
    parser: str  # deterministic | llm
    warnings: list[str]


# ---- SKU / slug helpers ------------------------------------------------------


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len].strip("-")


def derive_sku(request: StyleCreateRequest, taken: set[str]) -> str:
    if request.sku:
        sku = request.sku.lower()
        if not SKU_RE.match(sku):
            raise StyleInputError(
                "sku must be 2-48 chars of lowercase letters, digits and hyphens"
            )
        if sku in taken:
            raise StyleInputError(f"sku {sku!r} already exists")
        return sku
    base = slugify(request.name or "") or "nail-style"
    if not base.startswith("nail"):
        base = f"nail-{base}"
    candidate = base
    n = 1
    while candidate in taken or not SKU_RE.match(candidate):
        n += 1
        candidate = f"{base}-{n}"
        if n > 9999:  # pragma: no cover - defensive
            raise StyleInputError("could not derive a unique sku; supply one explicitly")
    return candidate


# ---- Deterministic parser ----------------------------------------------------


def _find_keywords(text: str, keywords: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for kw in keywords:
        # substring match is intentional: multi-word phrases first, so
        # "sky blue" wins before "blue" (callers pass ordered tuples).
        if kw in text and not any(kw in f for f in found):
            found.append(kw)
    return found


def parse_deterministic(request: StyleCreateRequest, sku: str) -> tuple[StyleSpec, list[str]]:
    """Merge explicit structured fields with keywords mined from the description."""
    warnings: list[str] = []
    text = request.description.lower()

    shape = None
    if request.shape:
        shape = vocab.SHAPE_KEYWORDS.get(request.shape.lower().strip())
        if shape is None:
            raise StyleInputError(
                f"unknown shape {request.shape!r}; valid: {', '.join(vocab.SHAPES)}"
            )
    if shape is None:
        for kw, canonical in vocab.SHAPE_KEYWORDS.items():
            if kw in text:
                shape = canonical
                break
    if shape is None:
        shape = vocab.DEFAULT_SHAPE
        warnings.append(f"shape not specified; defaulted to {shape}")

    length = None
    if request.length:
        length = vocab.LENGTH_KEYWORDS.get(request.length.lower().strip())
        if length is None:
            raise StyleInputError(
                f"unknown length {request.length!r}; valid: {', '.join(vocab.LENGTHS)}"
            )
    if length is None:
        for kw, canonical in vocab.LENGTH_KEYWORDS.items():
            if kw in text:
                length = canonical
                break
    if length is None:
        length = vocab.DEFAULT_LENGTH
        warnings.append(f"length not specified; defaulted to {length}")

    skin_tone = vocab.DEFAULT_SKIN_TONE
    if request.skin_tone:
        tone = request.skin_tone.lower().strip()
        if tone not in vocab.SKIN_TONES:
            raise StyleInputError(
                f"unknown skin_tone {request.skin_tone!r}; valid: {', '.join(vocab.SKIN_TONES)}"
            )
        skin_tone = tone

    base_colors = list(request.base_colors)
    if not base_colors:
        base_colors = _find_keywords(text, vocab.COLOR_KEYWORDS)[:4]
        if not base_colors:
            warnings.append("no colors recognized; defaulted to nude pink")

    elements = list(request.elements)
    if not elements:
        elements = _find_keywords(text, vocab.ELEMENT_KEYWORDS)[:10]

    texture = list(request.texture)
    if not texture:
        seen: list[str] = []
        for kw, canonical in vocab.FINISH_KEYWORDS.items():
            if kw in text and canonical not in seen:
                seen.append(canonical)
        texture = seen[:5]
    if not texture:
        texture = ["glossy"]
        warnings.append("finish not specified; defaulted to glossy")

    name = request.name or (base_colors[0].title() + " " + shape.title() if base_colors else sku)

    try:
        spec = StyleSpec(
            sku=sku,
            name=name[:80],
            base_colors=base_colors,
            accent_colors=request.accent_colors,
            elements=elements,
            texture=texture,
            shape=shape,
            length=length,
            visual_style=request.visual_style or vocab.DEFAULT_VISUAL_STYLE,
            avoid=request.avoid,
            skin_tone=skin_tone,
            notes=request.notes,
        )
    except ValidationError as exc:
        raise StyleInputError(f"style validation failed: {exc.errors()[0]['msg']}") from exc
    return spec, warnings


# ---- LLM parser ----------------------------------------------------------------

LLM_SYSTEM_PROMPT = """You convert a press-on nail style description into strict JSON.
Output ONLY a JSON object with these keys:
  name (string, short English style name),
  base_colors (array of 1-4 short color strings),
  accent_colors (array of 0-4 strings),
  elements (array of 0-10 short motif/decoration strings, e.g. "french tip", "pearl", "gold line"),
  texture (array of 0-5 of: glossy, matte, jelly, chrome, glitter, translucent, milky, pearl, cat-eye, velvet, marble, magnetic),
  shape (one of: almond, coffin, square, tapered-square, squoval, oval, round, stiletto),
  length (one of: short, medium, long, extra-long),
  visual_style (short string),
  avoid (array of 0-10 strings the design must NOT contain),
  skin_tone (one of: light, medium, tan, deep; default light).
Describe only what the text states or clearly implies. Do not invent decorations.
No markdown, no commentary — JSON only."""


def parse_with_llm(
    request: StyleCreateRequest,
    sku: str,
    *,
    chat_fn,
) -> tuple[StyleSpec, list[str]]:
    """chat_fn(system, user) -> str. Raises StyleInputError if output is unusable."""
    raw = chat_fn(LLM_SYSTEM_PROMPT, request.description)
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        doc = json.loads(text)
        if not isinstance(doc, dict):
            raise ValueError("LLM did not return a JSON object")
    except ValueError as exc:
        raise StyleInputError(f"LLM returned invalid JSON: {exc}") from exc

    # Explicit user-provided structured fields always beat LLM output.
    merged = {
        "sku": sku,
        "name": (request.name or str(doc.get("name") or "")[:80] or sku),
        "base_colors": request.base_colors or doc.get("base_colors") or [],
        "accent_colors": request.accent_colors or doc.get("accent_colors") or [],
        "elements": request.elements or doc.get("elements") or [],
        "texture": request.texture or doc.get("texture") or [],
        "shape": (request.shape or doc.get("shape") or vocab.DEFAULT_SHAPE),
        "length": (request.length or doc.get("length") or vocab.DEFAULT_LENGTH),
        "visual_style": request.visual_style or doc.get("visual_style") or vocab.DEFAULT_VISUAL_STYLE,
        "avoid": request.avoid or doc.get("avoid") or [],
        "skin_tone": request.skin_tone or doc.get("skin_tone") or vocab.DEFAULT_SKIN_TONE,
        "notes": request.notes,
    }
    # Normalize shape/length synonyms through keyword maps before validation.
    merged["shape"] = vocab.SHAPE_KEYWORDS.get(str(merged["shape"]).lower(), merged["shape"])
    merged["length"] = vocab.LENGTH_KEYWORDS.get(str(merged["length"]).lower(), merged["length"])
    try:
        spec = StyleSpec(**merged)
    except ValidationError as exc:
        raise StyleInputError(f"LLM output failed validation: {exc.errors()[0]['msg']}") from exc
    return spec, []


# ---- Entry point ----------------------------------------------------------------


def build_style_spec(
    request: StyleCreateRequest,
    taken_skus: set[str],
    *,
    chat_fn=None,
) -> ParseOutcome:
    if not request.has_any_input():
        raise StyleInputError("provide at least a description, a name, or structured fields")

    sku = derive_sku(request, taken_skus)

    structured = bool(
        request.base_colors or request.elements or request.texture or request.shape or request.length
    )
    source_type = "hybrid" if (structured and request.description) else (
        "structured" if structured else "natural_language"
    )

    if request.use_llm and request.description and chat_fn is not None:
        try:
            spec, warnings = parse_with_llm(request, sku, chat_fn=chat_fn)
            return ParseOutcome(spec, source_type, "llm", warnings)
        except StyleInputError as exc:
            logger.warning("LLM style parse failed, falling back to deterministic: %s", exc)
            spec, warnings = parse_deterministic(request, sku)
            warnings.append(f"llm parse failed ({exc}); used deterministic parser")
            return ParseOutcome(spec, source_type, "deterministic", warnings)

    spec, warnings = parse_deterministic(request, sku)
    return ParseOutcome(spec, source_type, "deterministic", warnings)
