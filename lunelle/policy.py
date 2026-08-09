"""Explicit generation presets for high-throughput and high-control work."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import OUTPUT_HERO

MODES = ("batch", "precision")


@dataclass(frozen=True)
class GenerationPolicy:
    mode: str
    allow_automatic_creative_repair: bool
    max_creative_versions: int
    split_confidence_threshold: float
    prefer_human_split: bool
    preserve_candidate_history: bool = True
    allow_detail_references: bool = True
    allow_manual_correction_prompt: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


POLICIES = {
    "batch": GenerationPolicy(
        mode="batch",
        allow_automatic_creative_repair=True,
        max_creative_versions=5,
        split_confidence_threshold=0.85,
        prefer_human_split=False,
    ),
    "precision": GenerationPolicy(
        mode="precision",
        allow_automatic_creative_repair=False,
        max_creative_versions=5,
        split_confidence_threshold=0.85,
        prefer_human_split=True,
    ),
}


def resolve_policy(mode: str | None, output_type: str) -> GenerationPolicy:
    """Hero defaults to Precision; matrix/grid/wearing default to Batch."""
    resolved = mode or ("precision" if output_type == OUTPUT_HERO else "batch")
    try:
        return POLICIES[resolved]
    except KeyError as exc:
        raise ValueError(f"invalid generation mode {resolved!r}; expected {MODES}") from exc
