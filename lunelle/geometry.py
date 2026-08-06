"""Output size derived from the reference image's aspect ratio.

Why this module exists: a matrix cell is a *local edit* of a base hand photo.
The prompt tells the model to "match Image 2 hand pose, geometry, crop", but the
API's `size` parameter is a hard constraint while the prompt is only prose. When
the two disagree the size wins, so requesting 2048x2048 against a 4:3 base photo
made the model stretch the hand vertically by 1.33x — on every single cell,
deterministically, not as an occasional artifact.

So the request size must be derived from the base photo, not from a fixed config
value. The base photo itself is usually too small to request directly: Seedream
4.5 refuses anything under 3,686,400 px with

    image size must be at least 3686400 pixels

(verified against the live endpoint: 1448x1086 → HTTP 400; 2304x1728 and
2560x1440 → 200, echoing the requested size back). Hence "preserve the ratio,
scale up to the smallest legal size" rather than "pass the source size through".
"""

from __future__ import annotations

from pathlib import Path

#: Provider floor, quoted verbatim from the 400 the API returns below it.
#: 3,686,400 = 2560x1440.
MIN_PIXELS = 3_686_400

#: Generators are happiest on multiples of 16; snapping avoids the provider
#: silently rounding to something with a slightly different ratio.
QUANTUM = 16


def legal_size_for_ratio(width: int, height: int, *,
                         min_pixels: int = MIN_PIXELS,
                         quantum: int = QUANTUM) -> tuple[int, int]:
    """Smallest quantum-aligned size that keeps `width:height` and clears the floor.

    Aspect ratio is preserved to within one quantum step — the snap can shift it
    by a fraction of a percent, which is invisible, whereas the 1.33x stretch it
    replaces was not.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"reference size must be positive, got {width}x{height}")

    ratio = width / height
    # Start from the exact-area solution for this ratio, then grow until both the
    # snap and the floor are satisfied. Snapping down can drop us back under the
    # floor, so this loop is what guarantees the postcondition, not the formula.
    scale = (min_pixels / (width * height)) ** 0.5
    target_w = width * scale
    while True:
        w = max(quantum, round(target_w / quantum) * quantum)
        h = max(quantum, round((w / ratio) / quantum) * quantum)
        if w * h >= min_pixels:
            return w, h
        target_w += quantum


def size_from_reference(path: Path, *, min_pixels: int = MIN_PIXELS,
                        quantum: int = QUANTUM) -> tuple[int, int] | None:
    """Legal request size matching `path`'s aspect ratio, or None if unreadable.

    Returning None rather than raising keeps this a *refinement*: a caller that
    cannot read the reference falls back to its configured size and still
    generates, instead of failing a paid task over image metadata.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:  # noqa: BLE001 — unreadable/missing/not-an-image all mean "no opinion"
        return None
    if not width or not height:
        return None
    return legal_size_for_ratio(width, height, min_pixels=min_pixels, quantum=quantum)
