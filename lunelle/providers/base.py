"""Image provider contract shared by all adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GenerationRequest:
    prompt: str
    negative_prompt: str
    size: tuple[int, int]
    model: str
    task_id: str
    reference_images: list[Path] = field(default_factory=list)
    # Extra wire parameters (e.g. quality/output_format for gpt-image models);
    # merged verbatim into the request body by the adapter.
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class GenerationResult:
    image_bytes: bytes
    external_request_id: str | None
    actual_cost_usd: float | None
    reference_used: bool
    response_meta: dict


class ProviderError(Exception):
    """Classified provider failure. `code` must be one of models.*_ERROR_CODES."""

    def __init__(self, code: str, message: str, *, retryable: bool, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ProviderError(code={self.code!r}, retryable={self.retryable}, http={self.http_status})"


class ImageProvider(ABC):
    name: str = "base"

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Perform one real generation call. Raises ProviderError on failure."""
