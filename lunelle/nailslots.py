"""Per-nail masks derived from the colour-annotated hand photos.

The problem this solves: nail identity used to live only in prompt prose —
"LEFT upper SCREEN LEFT-to-RIGHT = nail-05, nail-04, nail-03, nail-02". That
asks the model to count fingers and infer sides, and it gets it wrong. A mask
binds identity to geometry instead, so there is nothing to miscount.

Two things in here are contracts rather than implementation details, and both are
deliberately code constants rather than uploaded data — changing either must be a
reviewed code change, not a silent asset swap:

1. NAIL_COLORS — which exact RGB marks which nail.
2. MASK_EDITABLE_IS_OPAQUE — the alpha polarity the image endpoint expects.

On (2), read this before "fixing" it: the OpenAI /images/edits documentation says
TRANSPARENT pixels are the editable region. The relay in use here does the
OPPOSITE. Measured directly, same base photo and prompt, only the mask differing:

    OpenAI polarity (nail transparent):   nail changed  4.4  |  rest changed 4.7
    no mask at all:                       nail changed 192.1 |  rest changed 3.9
    inverted     (nail opaque):           nail changed 177.4 |  rest changed 5.6

Under the documented polarity the nail was the one region left alone, so the mask
was being honoured with reversed meaning — not ignored. If someone later "corrects"
this to match the OpenAI docs, the symptom is that masks appear to do nothing, and
it is genuinely hard to trace. `tests/unit/test_nailslots.py` pins it.

Note this module never sends the annotated image anywhere: it exists purely to
derive masks. The annotated photo is provenance, the clean photo is the input.
"""

from __future__ import annotations

import hashlib
import io
from collections import deque
from dataclasses import dataclass
from pathlib import Path

#: Exact annotation colour -> nail id. Fully saturated, channel values in
#: {0, 128, 255}, chosen to be far apart and absent from real skin/fabric tones.
NAIL_COLORS: dict[str, tuple[int, int, int]] = {
    "nail-01": (255, 0, 0),
    "nail-02": (0, 255, 0),
    "nail-03": (0, 0, 255),
    "nail-04": (255, 255, 0),
    "nail-05": (255, 0, 255),
    "nail-06": (0, 255, 255),
    "nail-07": (255, 128, 0),
    "nail-08": (128, 0, 255),
    "nail-09": (255, 0, 128),
    "nail-10": (0, 255, 128),
}

COLOR_TO_NAIL: dict[tuple[int, int, int], str] = {v: k for k, v in NAIL_COLORS.items()}

#: Physiological binding. Permanent: nail-01 is the left thumb in every view.
#: Screen position is a per-view rendering result and can never define identity.
NAIL_ANATOMY: dict[str, tuple[str, str]] = {
    "nail-01": ("left", "thumb"),
    "nail-02": ("left", "index"),
    "nail-03": ("left", "middle"),
    "nail-04": ("left", "ring"),
    "nail-05": ("left", "pinky"),
    "nail-06": ("right", "thumb"),
    "nail-07": ("right", "index"),
    "nail-08": ("right", "middle"),
    "nail-09": ("right", "ring"),
    "nail-10": ("right", "pinky"),
}

#: See the module docstring. True = opaque marks the editable region, which is the
#: REVERSE of the OpenAI /images/edits documentation, and is what this relay does.
MASK_EDITABLE_IS_OPAQUE = True

#: Largest tolerated per-pixel difference between the annotated photo and the
#: clean photo outside the colour regions. Zero in practice for all 16 current
#: assets; a small allowance covers lossy re-encoding of a future asset.
ALIGNMENT_TOLERANCE = 2


class SlotError(ValueError):
    """The annotation set does not satisfy the contract."""


@dataclass(frozen=True)
class SlotMask:
    nail_id: str
    hand: str
    finger: str
    png_bytes: bytes
    bbox: tuple[int, int, int, int]  # x, y, w, h
    area_px: int

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.png_bytes).hexdigest()


def _load_rgb(path: Path):
    from PIL import Image

    with Image.open(path) as image:
        return image.convert("RGB")


def find_color_regions(annotation: Path) -> dict[str, list[tuple[int, int]]]:
    """Map each nail id to its annotated pixel coordinates.

    Only exact matches count. The annotation is flat-filled by construction, so a
    near-match would mean the file was re-encoded lossily — which we want to fail
    loudly rather than silently absorb.
    """
    rgb = _load_rgb(annotation)
    pixels = rgb.load()
    width, height = rgb.size
    regions: dict[str, list[tuple[int, int]]] = {}
    for y in range(height):
        for x in range(width):
            nail_id = COLOR_TO_NAIL.get(pixels[x, y])
            if nail_id is not None:
                regions.setdefault(nail_id, []).append((x, y))
    return regions


def count_blobs(coords: list[tuple[int, int]]) -> list[int]:
    """Sizes of each 4-connected component, largest first.

    A nail is one physical surface, so a well-formed region is a single blob.
    Fragments mean stray pixels or a colour reused somewhere it should not be.
    """
    remaining = set(coords)
    sizes: list[int] = []
    while remaining:
        start = remaining.pop()
        queue = deque([start])
        size = 1
        while queue:
            x, y = queue.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbour = (x + dx, y + dy)
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    queue.append(neighbour)
                    size += 1
        sizes.append(size)
    return sorted(sizes, reverse=True)


def verify_alignment(annotation: Path, base: Path, *,
                     tolerance: int = ALIGNMENT_TOLERANCE) -> dict:
    """Confirm the annotation was painted ON the clean photo, not a re-render.

    If the two disagree outside the colour regions, the mask coordinates do not
    describe the photo we actually send, and every mask would be subtly offset.
    """
    ann = _load_rgb(annotation)
    clean = _load_rgb(base)
    if ann.size != clean.size:
        raise SlotError(
            f"annotation {ann.size} and base {clean.size} differ in size; "
            "masks derived from one would not align with the other"
        )
    ap, cp = ann.load(), clean.load()
    width, height = ann.size
    worst = 0
    total = 0
    counted = 0
    for y in range(height):
        for x in range(width):
            if ap[x, y] in COLOR_TO_NAIL:
                continue
            diff = max(abs(ap[x, y][i] - cp[x, y][i]) for i in range(3))
            worst = max(worst, diff)
            total += diff
            counted += 1
    mean = total / counted if counted else 0.0
    if worst > tolerance:
        raise SlotError(
            f"annotation and base differ outside the colour regions "
            f"(max {worst} > {tolerance}); the annotation is not painted on this photo"
        )
    return {"max_diff": worst, "mean_diff": round(mean, 4), "compared_px": counted}


def build_mask(coords: list[tuple[int, int]], size: tuple[int, int]) -> bytes:
    """PNG where this nail is editable and everything else is preserved.

    Polarity follows MASK_EDITABLE_IS_OPAQUE — see the module docstring; it is the
    reverse of the OpenAI documentation and was established by measurement.
    """
    from PIL import Image

    width, height = size
    editable, preserved = (255, 0) if MASK_EDITABLE_IS_OPAQUE else (0, 255)
    mask = Image.new("RGBA", (width, height), (0, 0, 0, preserved))
    pixels = mask.load()
    if pixels is None:  # pragma: no cover - a freshly created image always loads
        raise SlotError("could not access mask pixel data")
    for x, y in coords:
        pixels[x, y] = (0, 0, 0, editable)
    buffer = io.BytesIO()
    mask.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def bbox_of(coords: list[tuple[int, int]]) -> tuple[int, int, int, int]:
    xs = [x for x, _ in coords]
    ys = [y for _, y in coords]
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def derive_slots(annotation: Path, *, expected_nails: list[str] | None = None,
                 fragment_tolerance: float = 0.02) -> list[SlotMask]:
    """Derive one mask per annotated nail, verifying the contract as it goes.

    `expected_nails` is the view's visible-nail list. Passing it turns a missing
    or extra colour into an error instead of a silently short mask set.
    """
    rgb = _load_rgb(annotation)
    size = rgb.size
    regions = find_color_regions(annotation)
    if not regions:
        raise SlotError(f"no annotation colours found in {annotation}")

    if expected_nails is not None:
        found, expected = set(regions), set(expected_nails)
        if found != expected:
            raise SlotError(
                f"{annotation.name}: colours present {sorted(found)} do not match "
                f"the view contract {sorted(expected)} "
                f"(missing {sorted(expected - found)}, extra {sorted(found - expected)})"
            )

    slots: list[SlotMask] = []
    for nail_id in sorted(regions):
        coords = regions[nail_id]
        blobs = count_blobs(coords)
        if len(blobs) > 1 and sum(blobs[1:]) > blobs[0] * fragment_tolerance:
            raise SlotError(
                f"{annotation.name}: {nail_id} is fragmented into {len(blobs)} pieces "
                f"(largest {blobs[0]}, stray {sum(blobs[1:])}); expected one region"
            )
        hand, finger = NAIL_ANATOMY[nail_id]
        slots.append(SlotMask(
            nail_id=nail_id, hand=hand, finger=finger,
            png_bytes=build_mask(coords, size),
            bbox=bbox_of(coords), area_px=len(coords),
        ))
    return slots
