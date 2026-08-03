"""Automated quality checks for generated images.

Philosophy (inherited from the predecessor projects' QA contract):
- deterministic, explainable checks only; every check reports its measurement
  AND its threshold so results are auditable;
- automation can FAIL an image, but it can never fully APPROVE one — aesthetic
  judgement (watermarks, hand deformities, motif fidelity) is flagged for human
  review, so `needs_human_review` is always true for wearing shots and any
  passing result is "approved pending human review".

Checks implemented:
  file_exists, file_readable, format, aspect_ratio, min_resolution, file_size,
  grid_nail_count (grid only, projection-profile detection ported from the
  predecessors' split_nails.py), color_consistency (wearing vs grid).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

from PIL import Image

from .db import Database, transaction, utcnow
from .models import OUTPUT_GRID, OUTPUT_WEARING

logger = logging.getLogger(__name__)

ALLOWED_FORMATS = ("PNG", "WEBP", "JPEG")
MIN_FILE_BYTES = 30 * 1024
MAX_FILE_BYTES = 40 * 1024 * 1024
ASPECT_TOLERANCE = 0.02
ANALYSIS_MAX_SIDE = 512
FOREGROUND_DELTA = 24
COLOR_MATCH_THRESHOLD = 90.0  # max RGB distance for "same color family"

MANUAL_REVIEW_ITEMS = {
    OUTPUT_GRID: [
        "no visible text or watermark",
        "nail designs match the style spec (colors, elements, finish)",
        "nails do not overlap and none are cropped",
    ],
    OUTPUT_WEARING: [
        "no visible text or watermark",
        "hand anatomy natural: exactly five fingers, no deformities",
        "nail set design matches the grid image",
        "press-on attachment looks natural (no sticker look)",
    ],
}


# ---------------- image analysis helpers ----------------


def _load_scaled(path: Path) -> Image.Image:
    with Image.open(path) as source:
        image: Image.Image = source.convert("RGB")
    w, h = image.size
    scale = max(w, h) / ANALYSIS_MAX_SIDE
    if scale > 1:
        image = image.resize((max(1, int(w / scale)), max(1, int(h / scale))))
    return image

def _border_color(pixels, w: int, h: int) -> tuple[int, int, int]:
    samples: list[tuple[int, int, int]] = []
    for x in range(0, w, max(1, w // 50)):
        samples.append(pixels[x, 0])
        samples.append(pixels[x, h - 1])
    for y in range(0, h, max(1, h // 50)):
        samples.append(pixels[0, y])
        samples.append(pixels[w - 1, y])
    n = len(samples)
    return (
        sum(p[0] for p in samples) // n,
        sum(p[1] for p in samples) // n,
        sum(p[2] for p in samples) // n,
    )


def _foreground_mask(image: Image.Image) -> tuple[list[list[bool]], int, int]:
    w, h = image.size
    pixels = image.load()
    assert pixels is not None
    bg = _border_color(pixels, w, h)
    mask = [[False] * w for _ in range(h)]
    for y in range(h):
        row = mask[y]
        for x in range(w):
            p = cast(tuple[int, int, int], pixels[x, y])
            if (
                abs(p[0] - bg[0]) > FOREGROUND_DELTA
                or abs(p[1] - bg[1]) > FOREGROUND_DELTA
                or abs(p[2] - bg[2]) > FOREGROUND_DELTA
            ):
                row[x] = True
    return mask, w, h


def _spans(profile: list[int], cutoff: float, min_width: int, max_gap: int) -> list[tuple[int, int]]:
    """Contiguous regions where profile > cutoff, merging small gaps (predecessor algorithm)."""
    raw: list[tuple[int, int]] = []
    start = None
    for i, value in enumerate(profile):
        if value > cutoff and start is None:
            start = i
        elif value <= cutoff and start is not None:
            raw.append((start, i - 1))
            start = None
    if start is not None:
        raw.append((start, len(profile) - 1))

    merged: list[tuple[int, int]] = []
    for span in raw:
        if merged and span[0] - merged[-1][1] <= max_gap:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return [s for s in merged if s[1] - s[0] + 1 >= min_width]


def detect_grid_layout(path: Path) -> dict:
    """Detect the 2x5 nail arrangement via projection profiles.

    Tightly packed product shots leave only shallow valleys between adjacent
    nails, so a single absolute cutoff under-segments (real case: 5 long
    coffin nails merged into one span). We sweep several cutoff levels —
    absolute (predecessor default) plus fractions of the profile peak — and
    keep the segmentation with the most plausible column/row count.
    """
    image = _load_scaled(path)
    mask, w, h = _foreground_mask(image)

    col_profile = [sum(1 for y in range(h) if mask[y][x]) for x in range(w)]
    row_profile = [sum(mask[y]) for y in range(h)]

    def best_spans(profile: list[int], base_cutoff: float, min_width: int,
                   max_gap: int, expected: int, hard_cap: int) -> list[tuple[int, int]]:
        peak = max(profile) if profile else 0
        candidates = [base_cutoff] + [peak * f for f in (0.15, 0.3, 0.5, 0.7)]
        best: list[tuple[int, int]] = []
        for cutoff in candidates:
            spans = _spans(profile, cutoff=cutoff, min_width=min_width, max_gap=max_gap)
            if len(spans) > hard_cap:  # over-segmented noise, not nails
                continue
            if len(best) == 0 or abs(len(spans) - expected) < abs(len(best) - expected):
                best = spans
        return best

    columns = best_spans(
        col_profile, base_cutoff=max(2.0, h * 0.035), min_width=max(4, int(w * 0.025)),
        max_gap=max(2, int(w * 0.006)), expected=5, hard_cap=8,
    )
    rows = best_spans(
        row_profile, base_cutoff=max(2.0, w * 0.025), min_width=max(4, int(h * 0.08)),
        max_gap=max(2, int(h * 0.01)), expected=2, hard_cap=4,
    )
    return {
        "detected_columns": len(columns),
        "detected_rows": len(rows),
        "estimated_nails": len(columns) * len(rows),
        "analysis_size": [w, h],
    }


def dominant_colors(path: Path, count: int = 5, *, foreground_only: bool) -> list[tuple[int, int, int]]:
    image = _load_scaled(path)
    if foreground_only:
        mask, w, h = _foreground_mask(image)
        pixels = image.load()
        assert pixels is not None
        fg = [pixels[x, y] for y in range(h) for x in range(w) if mask[y][x]]
        if not fg:
            return []
        sample = Image.new("RGB", (len(fg), 1))
        sample.putdata(fg)
        image = sample
    quantized = image.quantize(colors=count, method=Image.Quantize.FASTOCTREE)
    palette = list(quantized.getpalette() or [])
    color_counts = sorted(quantized.getcolors() or [], reverse=True)
    out: list[tuple[int, int, int]] = []
    for _, index in color_counts[:count]:
        i = cast(int, index)  # getcolors() index type is loose in PIL stubs
        r, g, b = palette[i * 3: i * 3 + 3]
        out.append((int(r), int(g), int(b)))
    return out


def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


# ---------------- the QA run ----------------


def run_qa(
    *,
    output_type: str,
    image_path: Path,
    expected_size: tuple[int, int],
    min_side: int,
    grid_image_path: Path | None = None,
) -> dict:
    """Pure function: run all applicable checks, return the QA document."""
    checks: dict[str, dict] = {}
    issues: list[str] = []
    hard_fail = False

    def record(name: str, passed: bool | None, detail: dict, issue: str | None = None, hard: bool = True):
        nonlocal hard_fail
        checks[name] = {"passed": passed, **detail}
        if passed is False and issue:
            issues.append(issue)
            if hard:
                hard_fail = True

    exists = image_path.is_file()
    record("file_exists", exists, {"path": str(image_path)}, "output file does not exist")
    if not exists:
        return _finalize(output_type, checks, issues, hard_fail=True, score_hint=0)

    size_bytes = image_path.stat().st_size
    record(
        "file_size",
        MIN_FILE_BYTES <= size_bytes <= MAX_FILE_BYTES,
        {"bytes": size_bytes, "min": MIN_FILE_BYTES, "max": MAX_FILE_BYTES},
        f"file size {size_bytes} outside [{MIN_FILE_BYTES}, {MAX_FILE_BYTES}]",
    )

    try:
        with Image.open(image_path) as probe:
            probe.verify()
        with Image.open(image_path) as image:
            fmt = image.format
            width, height = image.size
        readable = True
    except Exception as exc:  # noqa: BLE001 - any decode error means unreadable
        record("file_readable", False, {"error": str(exc)[:200]}, "image cannot be decoded")
        return _finalize(output_type, checks, issues, hard_fail=True, score_hint=0)
    record("file_readable", readable, {"format": fmt, "width": width, "height": height})

    record(
        "format",
        fmt in ALLOWED_FORMATS,
        {"format": fmt, "allowed": list(ALLOWED_FORMATS)},
        f"unexpected image format {fmt}",
    )

    expected_ratio = expected_size[0] / expected_size[1]
    actual_ratio = width / height
    ratio_ok = abs(actual_ratio - expected_ratio) / expected_ratio <= ASPECT_TOLERANCE
    record(
        "aspect_ratio",
        ratio_ok,
        {"actual": round(actual_ratio, 4), "expected": round(expected_ratio, 4),
         "tolerance": ASPECT_TOLERANCE},
        f"aspect ratio {actual_ratio:.3f} != expected {expected_ratio:.3f}",
    )

    resolution_ok = min(width, height) >= min_side
    record(
        "min_resolution",
        resolution_ok,
        {"width": width, "height": height, "min_side": min_side},
        f"resolution {width}x{height} below minimum side {min_side}",
    )

    if output_type == OUTPUT_GRID:
        try:
            layout = detect_grid_layout(image_path)
            grid_ok = layout["estimated_nails"] == 10 and layout["detected_rows"] == 2
            near = 8 <= layout["estimated_nails"] <= 12
            record(
                "grid_nail_count",
                grid_ok or near,
                {**layout, "expected": {"rows": 2, "columns": 5, "nails": 10},
                 "exact_match": grid_ok, "heuristic": True},
                f"grid layout detection found ~{layout['estimated_nails']} nails "
                f"({layout['detected_rows']} rows x {layout['detected_columns']} columns), expected 10",
                hard=False if near else True,
            )
        except Exception as exc:  # noqa: BLE001 - detection is best-effort
            record("grid_nail_count", None, {"error": str(exc)[:200], "heuristic": True})

    if output_type == OUTPUT_WEARING and grid_image_path and grid_image_path.is_file():
        try:
            grid_colors = dominant_colors(grid_image_path, foreground_only=True)[:3]
            wearing_colors = dominant_colors(image_path, foreground_only=False)
            matches = []
            for gc in grid_colors:
                best = min((_color_distance(gc, wc) for wc in wearing_colors), default=999.0)
                matches.append({"grid_color": list(gc), "best_distance": round(best, 1)})
            matched = sum(
                1 for m in matches if float(str(m["best_distance"])) <= COLOR_MATCH_THRESHOLD
            )
            consistent = not grid_colors or matched >= max(1, len(grid_colors) - 1)
            record(
                "color_consistency",
                consistent,
                {"matches": matches, "matched": matched, "of": len(grid_colors),
                 "threshold": COLOR_MATCH_THRESHOLD, "heuristic": True},
                "wearing image colors diverge from grid image dominant colors",
                hard=False,
            )
        except Exception as exc:  # noqa: BLE001 - heuristic only
            record("color_consistency", None, {"error": str(exc)[:200], "heuristic": True})

    return _finalize(output_type, checks, issues, hard_fail=hard_fail)


def _finalize(output_type: str, checks: dict, issues: list[str], *, hard_fail: bool,
              score_hint: int | None = None) -> dict:
    evaluated = [c for c in checks.values() if c.get("passed") is not None]
    passed_count = sum(1 for c in evaluated if c["passed"])
    score = score_hint if score_hint is not None else (
        int(100 * passed_count / len(evaluated)) if evaluated else 0
    )
    passed = not hard_fail
    return {
        "passed": passed,
        "score": score,
        "issues": issues,
        "checks": checks,
        "manual_review_items": MANUAL_REVIEW_ITEMS.get(output_type, []),
        "needs_human_review": True,  # automation never fully approves (project QA contract)
        "recommended_action": "regenerate" if hard_fail else "human_review",
    }


def insert_qa_result(conn, task_id: str, qa_doc: dict, *,
                     source: str = "heuristic") -> None:
    """Insert one QA row inside the caller's transaction.

    `source` separates the heuristic verdict (the only one the publish/export
    gate consults) from advisory LLM verdicts and manual entries.
    """
    conn.execute(
        "INSERT INTO qa_results (task_id, passed, score, issues_json, checks_json,"
        " recommended_action, needs_human_review, source, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            1 if qa_doc["passed"] else 0,
            qa_doc["score"],
            json.dumps(qa_doc["issues"], ensure_ascii=False),
            json.dumps(
                {"checks": qa_doc["checks"],
                 # Optional: LLM-gate verdicts and external callers don't carry it.
                 "manual_review_items": qa_doc.get("manual_review_items", [])},
                ensure_ascii=False,
            ),
            qa_doc["recommended_action"],
            1 if qa_doc["needs_human_review"] else 0,
            source,
            utcnow(),
        ),
    )


def store_qa_result(db: Database, task_id: str, qa_doc: dict, *,
                    source: str = "heuristic") -> None:
    conn = db.conn()
    with transaction(conn):
        insert_qa_result(conn, task_id, qa_doc, source=source)
