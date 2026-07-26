"""Test-only provider that renders deterministic placeholder images with Pillow.

It exists so unit/integration tests can exercise the full task pipeline without
paid API calls. It is REFUSED in production (config.validate_for_serve) and its
output is stamped into response metadata as provider=mock so it can never be
mistaken for a real generation.
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageDraw

from .base import GenerationRequest, GenerationResult, ImageProvider, ProviderError


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

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self._calls += 1
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

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return GenerationResult(
            image_bytes=buf.getvalue(),
            external_request_id=f"mock-{self._calls}",
            actual_cost_usd=0.0,
            reference_used=bool(request.reference_images),
            response_meta={"provider": "mock", "deterministic_seed": seed},
        )
