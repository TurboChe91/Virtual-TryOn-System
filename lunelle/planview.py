"""The 2x5 plan grid: addressing its cells, and compiling a pose-ordered view-plan.

Two jobs that share one piece of knowledge — where each nail sits in the plan.

**Cell addressing.** A plan lays the ten nails out in two rows of five: top row is
nail-01..05 left to right, bottom row nail-06..10. That is the plan's own grid
order and has nothing to do with where those nails appear on a hand. Conflating the
two is exactly how nail order went wrong: a hand's screen order is a rendering
result (p2's left hand runs 05,04,03,02 while p4's runs 02,03,04,05 — both verified
from mask pixels), whereas grid order is a fixed property of the plan.

Cells are located by finding ink, not by dividing the image into fifths. Real plans
are photographs: on the first one, the nails spanned 19%-84% of the width with
column widths from 121px to 208px, so equal-fifths slicing cut nails in half and
left two tiles empty.

**View-plan compilation.** Nail order used to be conveyed as prose — "LEFT upper
SCREEN LEFT-to-RIGHT = nail-05, nail-04, nail-03, nail-02" — which asks the model
to work out handedness and count fingers. It gets that wrong. Masked per-nail
editing was measured as an alternative and does not work on this channel at all:
asked to repaint nail-01 (left thumb, mask centroid at x=44.7%, y=71.6%), the model
painted the right index finger (x=56.1%, y=42.8%) and ignored the mask entirely.

So the position information has to be carried by the image the model copies from.
A view-plan re-lays the ten cells into the target pose's screen order and labels
each one, turning "count the fingers" into "copy left to right" — the approach the
predecessor project validated to an owner FINAL PASS.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

#: Grid position of each nail in the plan: (row, column), zero-based.
#: Top row nail-01..05, bottom row nail-06..10.
PLAN_CELLS: dict[str, tuple[int, int]] = {
    f"nail-{i:02d}": ((i - 1) // 5, (i - 1) % 5) for i in range(1, 11)
}

#: Inset applied when cropping a detected cell, as a fraction of the detected box.
#: Small, because detection already tracks the nail's real extent — this only trims
#: any halo left by the ink threshold.
CELL_INSET = 0.02

#: A column/row of ink must be at least this fraction of the peak to count as
#: occupied. Low enough to catch a pale gold nail against white, high enough to
#: ignore JPEG noise and soft shadows.
INK_THRESHOLD = 0.04

#: Anything paler than this in every channel is treated as background.
BACKGROUND_FLOOR = 235


class PlanError(ValueError):
    """The plan image cannot be addressed as a 2x5 grid."""


def _ink_rows(pixels: list[tuple[int, int, int]], width: int, height: int,
              step: int = 3) -> list[int]:
    """Non-background pixel count per image row."""
    return [
        sum(1 for x in range(0, width, step)
            if min(pixels[y * width + x]) < BACKGROUND_FLOOR)
        for y in range(height)
    ]


def _ink_columns(pixels: list[tuple[int, int, int]], width: int, *,
                 top: int, bottom: int, step: int = 3) -> list[int]:
    """Non-background pixel count per column, within one row band."""
    return [
        sum(1 for y in range(top, bottom + 1, step)
            if min(pixels[y * width + x]) < BACKGROUND_FLOOR)
        for x in range(width)
    ]


def _spans(profile: list[int], *, min_extent: int,
           threshold_ratio: float = INK_THRESHOLD) -> list[tuple[int, int]]:
    """Contiguous runs of occupied positions, ignoring runs shorter than min_extent."""
    peak = max(profile) if profile else 0
    if peak <= 0:
        return []
    threshold = peak * threshold_ratio
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate([*profile, 0]):
        if value > threshold and start is None:
            start = index
        elif value <= threshold and start is not None:
            if index - start >= min_extent:
                spans.append((start, index - 1))
            start = None
    return spans


def detect_cells(plan: Path) -> dict[str, tuple[int, int, int, int]]:
    """Find each nail's real box by locating ink, not by assuming a uniform grid.

    A plan is a photograph of ten nails on white, and they are neither evenly
    spaced nor edge to edge: on the first real plan the nails spanned 19%-84% of
    the width with column widths from 121px to 208px. Slicing into five equal
    columns therefore cut nails in half and produced two empty tiles — the uniform
    assumption is what made the first compiled view-plan unusable.

    Raises PlanError unless exactly two rows of five are found, because a partial
    detection would silently mis-assign nail identities, which is the one failure
    this whole module exists to prevent.
    """
    with Image.open(plan) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        pixels: list[tuple[int, int, int]] = list(rgb.getdata())

    rows = _spans(_ink_rows(pixels, width, height),
                  min_extent=max(4, height // 20))
    if len(rows) != 2:
        raise PlanError(
            f"expected 2 nail rows in {plan.name}, found {len(rows)}; "
            "the plan must be a 2x5 grid of ten nails on a plain background"
        )

    cells: dict[str, tuple[int, int, int, int]] = {}
    for row_index, (top, bottom) in enumerate(rows):
        band_height = bottom - top + 1
        columns = _spans(_ink_columns(pixels, width, top=top, bottom=bottom),
                         min_extent=max(4, width // 60))
        if len(columns) != 5:
            raise PlanError(
                f"expected 5 nails in row {row_index + 1} of {plan.name}, "
                f"found {len(columns)}"
            )
        for col_index, (left, right) in enumerate(columns):
            nail_id = f"nail-{row_index * 5 + col_index + 1:02d}"
            pad_x = (right - left + 1) * CELL_INSET
            pad_y = band_height * CELL_INSET
            cells[nail_id] = (
                max(0, int(left - pad_x)),
                max(0, int(top - pad_y)),
                min(width, int(right + 1 + pad_x)),
                min(height, int(bottom + 1 + pad_y)),
            )
    return cells


def cell_box(plan: Path, nail_id: str) -> tuple[int, int, int, int]:
    """Pixel box of one nail's cell, detected from the image."""
    if nail_id not in PLAN_CELLS:
        raise PlanError(f"unknown nail id {nail_id!r}")
    return detect_cells(plan)[nail_id]


def crop_cell(plan: Path, nail_id: str, *, upscale_to: int = 640,
              cells: dict[str, tuple[int, int, int, int]] | None = None) -> Image.Image:
    """One nail's cell, enlarged.

    Pass `cells` from a single `detect_cells` call when cropping several nails from
    the same plan; detection scans the whole image, so repeating it per nail is
    wasteful.

    Enlarging matters for both uses: a vision model counting studs on a 200px crop
    undercounts them, and a view-plan tile wants real detail to copy from.
    """
    boxes = cells if cells is not None else detect_cells(plan)
    if nail_id not in boxes:
        raise PlanError(f"{nail_id} was not detected in {plan.name}")
    with Image.open(plan) as image:
        cell = image.convert("RGB").crop(boxes[nail_id])
    if cell.width < upscale_to:
        scale = upscale_to / cell.width
        cell = cell.resize((upscale_to, max(1, round(cell.height * scale))),
                           Image.Resampling.LANCZOS)
    return cell


def write_cell(plan: Path, nail_id: str, dest_dir: Path, *,
               upscale_to: int = 640,
               cells: dict[str, tuple[int, int, int, int]] | None = None) -> Path:
    """Crop one cell to a PNG on disk and return its path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    cell = crop_cell(plan, nail_id, upscale_to=upscale_to, cells=cells)
    dest = dest_dir / f"{nail_id}.png"
    cell.save(dest, format="PNG")
    return dest


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    # Labels are annotation for the model to read, never rendered into the output.
    # A missing font must not break compilation, so fall back to the bitmap default.
    for candidate in ("/System/Library/Fonts/Helvetica.ttc",
                      "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:  # noqa: PERF203 - absent font is normal, try the next
            logger.debug("view-plan font unavailable: %s", candidate)
    return ImageFont.load_default()


def build_view_plan(plan: Path, rows: list[tuple[str, list[str]]], *,
                    anatomy: dict[str, tuple[str, str]],
                    tile_width: int = 240,
                    tile_height: int = 320) -> bytes:
    """Lay the plan's cells out in a pose's screen order, labelled.

    `rows` is [(row_title, [nail_id, ...]), ...] in the order they should appear,
    left to right within each row. A nail may not repeat, and every id must be a
    real plan cell, so a malformed contract fails here rather than producing a
    view-plan that silently duplicates or invents a nail. Partial sets are valid:
    single-hand views (p3/p5) legitimately show five.

    This is layout only: no mirroring, recolouring, or beautification. The nail art
    is copied through untouched, because the whole point is that Image 1 remains the
    design authority — a compiler that "improved" the art would corrupt the contract
    it exists to carry.
    """
    ordered = [nail_id for _, row in rows for nail_id in row]
    if not ordered:
        raise PlanError("view-plan needs at least one nail")
    unknown = sorted(set(ordered) - set(PLAN_CELLS))
    if unknown:
        raise PlanError(f"view-plan references unknown nails: {unknown}")
    duplicated = sorted({n for n in ordered if ordered.count(n) > 1})
    if duplicated:
        raise PlanError(f"view-plan repeats nails: {duplicated}")

    # Detect once: scanning the plan per tile would repeat a full-image pass ten times.
    boxes = detect_cells(plan)

    gap, margin = 12, 20
    label_h = 62
    columns = max(len(row) for _, row in rows)
    width = margin * 2 + columns * tile_width + (columns - 1) * gap
    row_h = 34 + tile_height + label_h
    height = margin * 2 + len(rows) * row_h + (len(rows) - 1) * gap

    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    title_font, id_font, role_font = _font(24), _font(26), _font(19)
    blue, grey = (0, 145, 255), (80, 80, 80)

    y = margin
    for title, row in rows:
        draw.text((margin, y), title, fill=(0, 0, 0), font=title_font)
        top = y + 34
        for column, nail_id in enumerate(row):
            left = margin + column * (tile_width + gap)
            draw.rectangle((left, top, left + tile_width, top + tile_height),
                           outline=blue, width=3)
            cell = crop_cell(plan, nail_id, upscale_to=tile_width - 24, cells=boxes)
            scale = min((tile_width - 24) / cell.width,
                        (tile_height - 24) / cell.height)
            sized = cell.resize((max(1, round(cell.width * scale)),
                                 max(1, round(cell.height * scale))), Image.Resampling.LANCZOS)
            canvas.paste(sized,
                         (left + (tile_width - sized.width) // 2,
                          top + (tile_height - sized.height) // 2))
            centre = left + tile_width // 2
            for text, font, colour, offset in (
                    (nail_id, id_font, (0, 0, 0), 8),
                    (" ".join(anatomy[nail_id]).upper(), role_font, grey, 34)):
                box = draw.textbbox((0, 0), text, font=font)
                draw.text((centre - (box[2] - box[0]) / 2, top + tile_height + offset),
                          text, fill=colour, font=font)
        y += row_h + gap

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def view_plan_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compile_view_plan(plan: Path, view: str, *, rows: list[tuple[str, list[str]]],
                      anatomy: dict[str, tuple[str, str]],
                      cache_dir: Path) -> Path:
    """Compile a view-plan to disk, reusing an identical earlier compile.

    Keyed on the plan's content digest and the view, so a re-uploaded or edited plan
    compiles fresh while sixteen matrix cells sharing four views compile four times
    instead of sixteen. Content-keyed rather than mtime-keyed because the cached file
    becomes a provider input: serving a stale view-plan would silently render the
    wrong nail order, which is the failure this module exists to prevent.
    """
    plan_digest = hashlib.sha256(plan.read_bytes()).hexdigest()[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"viewplan-{plan_digest}-{view}.png"
    if dest.is_file():
        return dest
    data = build_view_plan(plan, rows, anatomy=anatomy)
    # Write-then-rename so a concurrent reader never sees a half-written PNG.
    temp = dest.with_suffix(f".{os.getpid()}.tmp")
    temp.write_bytes(data)
    temp.replace(dest)
    return dest
