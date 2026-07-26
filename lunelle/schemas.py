"""Pydantic schemas: the structured style spec and API request/response models.

StyleSpec is the single trusted representation of a nail style. Any data coming
from users or from an LLM must pass through these validators before use.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import vocab

SKU_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")

_MAX_ITEM_LEN = 60


def _clean_str_list(values: list[str], max_items: int, field_name: str) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = " ".join(str(value).split()).strip()
        if not item:
            continue
        if len(item) > _MAX_ITEM_LEN:
            raise ValueError(f"{field_name} item too long (max {_MAX_ITEM_LEN} chars): {item[:40]}...")
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    if len(cleaned) > max_items:
        raise ValueError(f"{field_name} allows at most {max_items} items")
    return cleaned


class StyleSpec(BaseModel):
    """Validated structured style data — the contract for prompt generation."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sku: str = Field(..., description="kebab-case product SKU")
    name: str = Field(..., min_length=1, max_length=80)
    base_colors: list[str] = Field(default_factory=list)
    accent_colors: list[str] = Field(default_factory=list)
    elements: list[str] = Field(default_factory=list)
    texture: list[str] = Field(default_factory=list)
    shape: Literal[vocab.SHAPES] = vocab.DEFAULT_SHAPE  # type: ignore[valid-type]
    length: Literal[vocab.LENGTHS] = vocab.DEFAULT_LENGTH  # type: ignore[valid-type]
    visual_style: str = Field(default=vocab.DEFAULT_VISUAL_STYLE, max_length=120)
    avoid: list[str] = Field(default_factory=list)
    skin_tone: Literal[vocab.SKIN_TONES] = vocab.DEFAULT_SKIN_TONE  # type: ignore[valid-type]
    notes: str = Field(default="", max_length=500)

    @field_validator("sku")
    @classmethod
    def _sku_format(cls, v: str) -> str:
        v = v.lower()
        if not SKU_RE.match(v):
            raise ValueError(
                "sku must be 2-48 chars of lowercase letters, digits and hyphens, "
                "starting with a letter or digit"
            )
        return v

    @field_validator("base_colors")
    @classmethod
    def _base_colors(cls, v: list[str]) -> list[str]:
        cleaned = _clean_str_list(v, 4, "base_colors")
        return cleaned or list(vocab.DEFAULT_BASE_COLORS)

    @field_validator("accent_colors")
    @classmethod
    def _accent_colors(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v, 4, "accent_colors")

    @field_validator("elements")
    @classmethod
    def _elements(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v, 10, "elements")

    @field_validator("texture")
    @classmethod
    def _texture(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v, 5, "texture")

    @field_validator("avoid")
    @classmethod
    def _avoid(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v, 10, "avoid")


# ---- API models -------------------------------------------------------------


class StyleCreateRequest(BaseModel):
    """Create a style from structured fields, natural language, or both."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sku: str | None = Field(default=None, max_length=48)
    name: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=2000, description="natural language description")
    base_colors: list[str] = Field(default_factory=list)
    accent_colors: list[str] = Field(default_factory=list)
    elements: list[str] = Field(default_factory=list)
    texture: list[str] = Field(default_factory=list)
    shape: str | None = None
    length: str | None = None
    visual_style: str | None = Field(default=None, max_length=120)
    avoid: list[str] = Field(default_factory=list)
    skin_tone: str | None = None
    notes: str = Field(default="", max_length=500)
    use_llm: bool = Field(
        default=False,
        description="parse the description with the configured text model (validated afterwards)",
    )

    def has_any_input(self) -> bool:
        return bool(
            self.description
            or self.name
            or self.base_colors
            or self.elements
            or self.texture
            or self.accent_colors
        )


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_types: list[Literal["grid", "wearing", "hero"]] = Field(default=["grid", "wearing"])
    force: bool = Field(
        default=False,
        description="create a new generation version even if one already exists/succeeded",
    )
    note: str = Field(default="", max_length=200)

    @field_validator("output_types")
    @classmethod
    def _unique_types(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("output_types must not be empty")
        return list(dict.fromkeys(v))


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=200)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool = Field(..., description="true clears needs_human_review on the latest QA result")
    note: str = Field(default="", max_length=300)


class MatrixRequest(BaseModel):
    """Queue the try-on matrix; empty lists mean all tones / all views."""

    model_config = ConfigDict(extra="forbid")
    tones: list[Literal["light", "medium", "tan", "deep"]] = Field(default_factory=list)
    views: list[str] = Field(default_factory=list)
    force: bool = False
    note: str = Field(default="", max_length=200)


class IdentityRequest(BaseModel):
    """Per-nail identity text (the predecessor identity.txt format) for hero prompts."""

    model_config = ConfigDict(extra="forbid")
    identity_text: str = Field(default="", max_length=8000)


class ProfileCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=60)
    base_url: str = Field(..., min_length=9, max_length=300)
    api_key: str = Field(..., min_length=8, max_length=300)
    model: str = Field(..., min_length=1, max_length=120)
    kind: Literal["image", "llm"] = "image"
    reference_mode: Literal["auto", "seedream", "openai-edits", "off"] = "auto"
    supports_mask: bool = False
    price_per_image_usd: float | None = Field(default=None, ge=0, le=10)


class ProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=60)
    base_url: str | None = Field(default=None, min_length=9, max_length=300)
    api_key: str | None = Field(default=None, min_length=8, max_length=300)
    model: str | None = Field(default=None, min_length=1, max_length=120)
    reference_mode: Literal["auto", "seedream", "openai-edits", "off"] | None = None
    supports_mask: bool | None = None
    price_per_image_usd: float | None = Field(default=None, ge=0, le=10)


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skus: list[str] = Field(default_factory=list, description="empty = all exportable styles")
    include_unreviewed: bool = Field(
        default=True,
        description="include assets whose QA needs human review (flagged in the report)",
    )
