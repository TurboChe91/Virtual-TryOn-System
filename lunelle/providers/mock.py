"""Test-only provider that renders deterministic placeholder images with Pillow.

It exists so unit/integration tests can exercise the full task pipeline without
paid API calls. It is REFUSED in production (config.validate_for_serve) and its
output is stamped into response metadata as provider=mock so it can never be
mistaken for a real generation.
"""

from __future__ import annotations

import hashlib
import io
import random
from typing import cast

from PIL import Image, ImageDraw

from .base import GenerationRequest, GenerationResult, ImageProvider, ProviderError

#: Peak-to-peak grain amplitude. Must stay below qa.FOREGROUND_THRESHOLD (24) so
#: grain is never mistaken for image content by the projection-profile counter.
GRAIN_AMPLITUDE = 8


def _add_grain(image: Image.Image, seed: int) -> None:
    """Deterministic per-pixel noise, in place. Same seed => same bytes."""
    rng = random.Random(seed)  # noqa: S311 - test-image texture, not cryptography
    pixels = image.load()
    assert pixels is not None
    width, height = image.size
    for y in range(height):
        for x in range(width):
            r, g, b = cast("tuple[int, int, int]", pixels[x, y])
            jitter = rng.randint(-GRAIN_AMPLITUDE, GRAIN_AMPLITUDE)
            pixels[x, y] = (
                min(255, max(0, r + jitter)),
                min(255, max(0, g + jitter)),
                min(255, max(0, b + jitter)),
            )


class MockImageProvider(ImageProvider):
    name = "mock"

    def __init__(self, *, allowed: bool, fail_with: ProviderError | None = None, fail_times: int = 0):
        if not allowed:
            raise ProviderError(
                "config_error",
                "mock provider is only allowed when LUNELLE_ENV != production",
                retryable=False,
            )
        self._fail_with = fail_with
        self._fail_times = fail_times
        self._calls = 0
        #: Every size actually requested, in call order. Lets a test assert what
        #: reached the provider rather than what the caller meant to send.
        self.requested_sizes: list[tuple[int, int]] = []

    @property
    def calls(self) -> int:
        """Provider calls made so far — lets tests assert a fail-fast path spent nothing."""
        return self._calls

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self._calls += 1
        self.requested_sizes.append((request.size[0], request.size[1]))
        if self._fail_with is not None and (self._fail_times == 0 or self._calls <= self._fail_times):
            raise self._fail_with

        width, height = request.size
        seed = int(hashlib.sha256(request.prompt.encode()).hexdigest()[:8], 16)
        background = (245, 240, 232)
        image = Image.new("RGB", (width, height), background)
        draw = ImageDraw.Draw(image)
        color = ((seed >> 16) % 200 + 30, (seed >> 8) % 200 + 30, seed % 200 + 30)

        if "2 rows and 5 columns" in request.prompt:
            # Emulate a 2x5 nail grid so QA's nail counting has real structure.
            cell_w, cell_h = width // 5, height // 2
            for row in range(2):
                for col in range(5):
                    cx = col * cell_w + cell_w // 2
                    cy = row * cell_h + cell_h // 2
                    rx, ry = int(cell_w * 0.22), int(cell_h * 0.36)
                    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=color)
        else:
            # Emulate a hand-ish blob with five finger stubs.
            cx, cy = width // 2, int(height * 0.65)
            draw.ellipse([cx - width // 5, cy - height // 6, cx + width // 5, cy + height // 5],
                         fill=(224, 188, 160))
            for i in range(5):
                fx = cx - width // 6 + i * (width // 12)
                draw.ellipse([fx - 14, cy - height // 3, fx + 14, cy - height // 8],
                             fill=(224, 188, 160))
                draw.ellipse([fx - 12, cy - height // 3, fx + 12, cy - height // 4], fill=color)

        # Flat synthetic fills compress to ~3KB, below QA's 30KB floor (which
        # exists to catch truncated real downloads). Add deterministic
        # low-amplitude noise so the render is byte-realistic enough to pass
        # heuristic QA: the amplitude stays under the foreground threshold, so
        # nail-counting and dominant-colour checks see exactly the same shapes.
        _add_grain(image, seed)

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return GenerationResult(
            image_bytes=buf.getvalue(),
            external_request_id=f"mock-{self._calls}",
            # None, matching every real OpenAI-compatible image API: they do not
            # report spend. Reporting 0.0 made settlement free the whole
            # reservation, so the budget breaker looked far more permissive under
            # test than in production.
            actual_cost_usd=None,
            reference_used=bool(request.reference_images),
            response_meta={"provider": "mock", "deterministic_seed": seed},
        )
