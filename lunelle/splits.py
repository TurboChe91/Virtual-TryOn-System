"""Versioned automatic and manual splits for a style's 2x5 design plan.

Automatic detection remains the fast path, but it is no longer an invisible
one-shot decision. Every result has confidence gates, a visual preview, ten
content-addressed nail crops, and an immutable revision id. An operator can
replace uncertain boxes with a manual revision without deleting the automatic
evidence or changing already-queued tasks.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .assets import resolve_path, store_bytes, store_file
from .config import Config
from .db import Database, transaction, utcnow
from .errors import ConflictError, NotFoundError
from .planview import PLAN_CELLS, PlanError, crop_cell, detect_cells

AUTO_PASS_CONFIDENCE = 0.85


def _revision_id() -> str:
    return "crop_" + uuid.uuid4().hex[:20]


def _boxes_document(boxes: dict[str, tuple[int, int, int, int]]) -> dict[str, list[int]]:
    return {nail_id: list(boxes[nail_id]) for nail_id in sorted(boxes)}


def _decode_boxes(value: str | dict) -> dict[str, tuple[int, int, int, int]]:
    raw = json.loads(value) if isinstance(value, str) else value
    decoded: dict[str, tuple[int, int, int, int]] = {}
    for nail_id, box in raw.items():
        if len(box) != 4:
            raise PlanError(f"stored bbox for {nail_id} is malformed")
        decoded[nail_id] = (
            int(box[0]), int(box[1]), int(box[2]), int(box[3])
        )
    return decoded


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _validate_boxes(
    boxes: dict[str, tuple[int, int, int, int]], image_size: tuple[int, int],
) -> None:
    expected = set(PLAN_CELLS)
    if set(boxes) != expected:
        missing = sorted(expected - set(boxes))
        extra = sorted(set(boxes) - expected)
        raise PlanError(f"split must contain nail-01..nail-10; missing={missing}, extra={extra}")
    width, height = image_size
    for nail_id, box in boxes.items():
        if len(box) != 4:
            raise PlanError(f"{nail_id} bbox must contain [left, top, right, bottom]")
        left, top, right, bottom = box
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise PlanError(
                f"{nail_id} bbox {list(box)} is outside {width}x{height} plan"
            )


def _confidence(
    plan: Path, boxes: dict[str, tuple[int, int, int, int]],
) -> tuple[float, list[str]]:
    """Conservative structural confidence; suspicious results require review."""
    reasons: list[str] = []
    with Image.open(plan) as source:
        image = source.convert("RGB")
        width, height = image.size

    areas = [
        (right - left) * (bottom - top)
        for left, top, right, bottom in boxes.values()
    ]
    median = sorted(areas)[len(areas) // 2]
    outliers = [
        nail_id for nail_id, (left, top, right, bottom) in boxes.items()
        if ((right - left) * (bottom - top)) / max(1, median) < 0.35
        or ((right - left) * (bottom - top)) / max(1, median) > 2.8
    ]
    if outliers:
        reasons.append("area_outlier:" + ",".join(outliers))

    edge_nails = [
        nail_id for nail_id, (left, top, right, bottom) in boxes.items()
        if left <= 1 or top <= 1 or right >= width - 1 or bottom >= height - 1
    ]
    if edge_nails:
        reasons.append("touches_plan_edge:" + ",".join(edge_nails))

    tight: list[str] = []
    for start in (1, 6):
        row = [f"nail-{number:02d}" for number in range(start, start + 5)]
        for left_id, right_id in zip(row, row[1:], strict=False):
            gap = boxes[right_id][0] - boxes[left_id][2]
            if gap < max(2, round(width * 0.004)):
                tight.append(f"{left_id}/{right_id}")
    if tight:
        reasons.append("tight_or_merged:" + ",".join(tight))

    # Strongly penalize conditions associated with identity corruption. The
    # confidence is intentionally not a probability; it is a deterministic gate.
    score = 1.0 - 0.10 * len(outliers) - 0.12 * len(edge_nails) - 0.10 * len(tight)
    return max(0.0, min(1.0, round(score, 3))), reasons


def _preview(
    plan: Path, boxes: dict[str, tuple[int, int, int, int]], *,
    confidence: float, reasons: list[str], source: str,
) -> bytes:
    with Image.open(plan) as original:
        image = original.convert("RGB")
    scale = min(1.0, 1400 / max(image.size))
    if scale < 1:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    draw = ImageDraw.Draw(image)
    font = _font(max(14, round(image.width / 45)))
    line = max(2, round(image.width / 300))
    colour = (20, 145, 95) if confidence >= AUTO_PASS_CONFIDENCE else (210, 70, 55)
    for nail_id, (left, top, right, bottom) in boxes.items():
        scaled = tuple(round(value * scale) for value in (left, top, right, bottom))
        draw.rectangle(scaled, outline=colour, width=line)
        draw.text((scaled[0] + 4, scaled[1] + 4), nail_id, fill=colour, font=font)
    banner_h = max(48, round(image.height * 0.11))
    canvas = Image.new("RGB", (image.width, image.height + banner_h), "white")
    canvas.paste(image, (0, banner_h))
    header = ImageDraw.Draw(canvas)
    status = "PASS" if confidence >= AUTO_PASS_CONFIDENCE and not reasons else "REVIEW"
    header.text(
        (12, 8), f"{source.upper()} SPLIT · {status} · confidence {confidence:.3f}",
        fill=colour, font=font,
    )
    if reasons:
        header.text((12, 30), " | ".join(reasons), fill=(80, 80, 80), font=_font(12))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _contact_sheet(
    plan: Path, boxes: dict[str, tuple[int, int, int, int]],
) -> bytes:
    tile_w, tile_h, gap, margin = 220, 300, 12, 20
    label_h = 38
    canvas = Image.new(
        "RGB",
        (margin * 2 + tile_w * 5 + gap * 4,
         margin * 2 + (tile_h + label_h) * 2 + gap),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    for number in range(1, 11):
        nail_id = f"nail-{number:02d}"
        row, column = divmod(number - 1, 5)
        left = margin + column * (tile_w + gap)
        top = margin + row * (tile_h + label_h + gap)
        draw.rectangle((left, top, left + tile_w, top + tile_h),
                       outline=(0, 145, 255), width=3)
        crop = crop_cell(plan, nail_id, upscale_to=720, cells=boxes)
        scale = min((tile_w - 18) / crop.width, (tile_h - 18) / crop.height)
        crop = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            Image.Resampling.LANCZOS,
        )
        canvas.paste(crop, (left + (tile_w - crop.width) // 2,
                            top + (tile_h - crop.height) // 2))
        draw.text((left + tile_w // 2, top + tile_h + 7), nail_id,
                  fill=(20, 20, 20), font=_font(20), anchor="ma")
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class SplitService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    def _style_plan(self, style_id: str) -> Path:
        row = self.db.conn().execute(
            "SELECT plan_image_path FROM styles WHERE style_id = ?", (style_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"style {style_id} not found")
        path = Path(row["plan_image_path"] or "")
        if not path.is_file():
            raise ConflictError("style has no uploaded 2x5 plan image")
        return path

    def _insert(
        self, *, style_id: str, plan: Path,
        boxes: dict[str, tuple[int, int, int, int]], source: str,
        confidence: float, reasons: list[str], review_state: str,
        created_by: str, approved_by: str | None = None,
    ) -> dict:
        with Image.open(plan) as image:
            _validate_boxes(boxes, image.size)
        plan_ref = store_file(self.db, self.config, plan, kind="plan")
        preview_ref = store_bytes(
            self.db, self.config,
            _preview(plan, boxes, confidence=confidence, reasons=reasons, source=source),
            kind="split_preview", ext=".png", mime_type="image/png",
        )
        contact_ref = store_bytes(
            self.db, self.config, _contact_sheet(plan, boxes),
            kind="contact_sheet", ext=".png", mime_type="image/png",
        )
        crop_refs = {}
        for nail_id in sorted(boxes):
            crop = crop_cell(plan, nail_id, upscale_to=960, cells=boxes)
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG", optimize=True)
            crop_refs[nail_id] = store_bytes(
                self.db, self.config, buffer.getvalue(), kind="nail_crop",
                ext=".png", mime_type="image/png",
            )

        revision_id = _revision_id()
        now = utcnow()
        gate_status = "pass" if confidence >= AUTO_PASS_CONFIDENCE and not reasons else "review_required"
        with transaction(self.db.conn()) as conn:
            number = int(conn.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 AS n "
                "FROM crop_revisions WHERE style_id = ?", (style_id,),
            ).fetchone()["n"])
            conn.execute(
                "INSERT INTO crop_revisions (crop_revision_id, style_id, revision_number, "
                "source_plan_digest, source, confidence, gate_status, gate_reasons_json, "
                "review_state, boxes_json, preview_digest, contact_sheet_digest, created_by, "
                "approved_by, created_at, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id, style_id, number, plan_ref.digest, source, confidence,
                    gate_status, json.dumps(reasons), review_state,
                    json.dumps(_boxes_document(boxes), separators=(",", ":"), sort_keys=True),
                    preview_ref.digest, contact_ref.digest, created_by, approved_by,
                    now, now if approved_by else None,
                ),
            )
            conn.executemany(
                "INSERT INTO nail_crops (crop_revision_id, nail_id, bbox_json, "
                "rotation_degrees, asset_digest) VALUES (?,?,?,?,?)",
                [
                    (revision_id, nail_id, json.dumps(list(boxes[nail_id])), 0,
                     crop_refs[nail_id].digest)
                    for nail_id in sorted(boxes)
                ],
            )
        return self.get(revision_id)

    def analyze(self, style_id: str, *, created_by: str = "system:auto") -> dict:
        """Create or reuse an automatic revision for the current plan bytes."""
        plan = self._style_plan(style_id)
        plan_ref = store_file(self.db, self.config, plan, kind="plan")
        existing = self.db.conn().execute(
            "SELECT crop_revision_id FROM crop_revisions WHERE style_id = ? "
            "AND source_plan_digest = ? AND source = 'auto' "
            "ORDER BY revision_number DESC LIMIT 1",
            (style_id, plan_ref.digest),
        ).fetchone()
        if existing:
            return self.get(existing["crop_revision_id"])
        try:
            boxes = detect_cells(plan)
        except PlanError as exc:
            # There are no safe identities to persist when detection cannot find
            # exactly ten. Surface the actionable reason; the manual API accepts
            # ten operator-provided boxes against the source plan directly.
            raise ConflictError(f"automatic split needs manual override: {exc}") from exc
        confidence, reasons = _confidence(plan, boxes)
        state = (
            "auto_approved"
            if confidence >= AUTO_PASS_CONFIDENCE and not reasons
            else "waiting_review"
        )
        return self._insert(
            style_id=style_id, plan=plan, boxes=boxes, source="auto",
            confidence=confidence, reasons=reasons, review_state=state,
            created_by=created_by,
        )

    def create_manual(
        self, style_id: str, boxes: dict[str, tuple[int, int, int, int]], *,
        created_by: str,
    ) -> dict:
        plan = self._style_plan(style_id)
        return self._insert(
            style_id=style_id, plan=plan, boxes=boxes, source="manual",
            confidence=1.0, reasons=[], review_state="approved",
            created_by=created_by, approved_by=created_by,
        )

    def approve(self, revision_id: str, *, reviewer: str) -> dict:
        revision = self.get(revision_id)
        current_plan = store_file(
            self.db, self.config, self._style_plan(revision["style_id"]), kind="plan"
        )
        if current_plan.digest != revision["source_plan_digest"]:
            raise ConflictError("cannot approve a split from an older plan upload")
        with transaction(self.db.conn()) as conn:
            conn.execute(
                "UPDATE crop_revisions SET review_state = 'approved', approved_by = ?, "
                "approved_at = ? WHERE crop_revision_id = ?",
                (reviewer, utcnow(), revision_id),
            )
        return self.get(revision_id)

    def reject(self, revision_id: str, *, reviewer: str) -> dict:
        self.get(revision_id)
        with transaction(self.db.conn()) as conn:
            conn.execute(
                "UPDATE crop_revisions SET review_state = 'rejected', approved_by = ?, "
                "approved_at = ? WHERE crop_revision_id = ?",
                (reviewer, utcnow(), revision_id),
            )
        return self.get(revision_id)

    def get(self, revision_id: str) -> dict:
        row = self.db.conn().execute(
            "SELECT * FROM crop_revisions WHERE crop_revision_id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"crop revision {revision_id} not found")
        return self._expand(dict(row))

    def list(self, style_id: str) -> list[dict]:
        # Preserve the normal style-not-found contract even when no revisions exist.
        if self.db.conn().execute(
            "SELECT 1 FROM styles WHERE style_id = ?", (style_id,)
        ).fetchone() is None:
            raise NotFoundError(f"style {style_id} not found")
        rows = self.db.conn().execute(
            "SELECT * FROM crop_revisions WHERE style_id = ? "
            "ORDER BY revision_number DESC", (style_id,),
        ).fetchall()
        return [self._expand(dict(row)) for row in rows]

    def selected(
        self, style_id: str, *, require_human_approval: bool,
    ) -> tuple[dict[str, tuple[int, int, int, int]], dict]:
        """Boxes and revision used for generation, auto-analyzing when needed."""
        plan = self._style_plan(style_id)
        plan_ref = store_file(self.db, self.config, plan, kind="plan")
        allowed = ("approved",) if require_human_approval else ("approved", "auto_approved")
        placeholders = ",".join("?" for _ in allowed)
        row = self.db.conn().execute(
            f"SELECT crop_revision_id FROM crop_revisions WHERE style_id = ? "  # noqa: S608
            f"AND source_plan_digest = ? AND review_state IN ({placeholders}) "  # noqa: S608
            "ORDER BY revision_number DESC LIMIT 1",
            (style_id, plan_ref.digest, *allowed),
        ).fetchone()
        if row is None and not require_human_approval:
            revision = self.analyze(style_id)
            if revision["review_state"] == "auto_approved":
                return _decode_boxes(revision["boxes"]), revision
        elif row is not None:
            revision = self.get(row["crop_revision_id"])
            return _decode_boxes(revision["boxes"]), revision
        requirement = "human-approved" if require_human_approval else "approved"
        raise ConflictError(
            f"current plan has no {requirement} split revision; review the contact "
            "sheet or submit a manual split override"
        )

    def nail_crop(self, revision_id: str, nail_id: str) -> Path:
        if nail_id not in PLAN_CELLS:
            raise ValueError(f"unknown nail id {nail_id!r}")
        row = self.db.conn().execute(
            "SELECT asset_digest FROM nail_crops WHERE crop_revision_id = ? AND nail_id = ?",
            (revision_id, nail_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{nail_id} is not present in crop revision {revision_id}")
        path = resolve_path(self.db, row["asset_digest"])
        if path is None:
            raise ConflictError(f"stored crop bytes for {revision_id}/{nail_id} are missing")
        return path

    @staticmethod
    def _expand(row: dict) -> dict:
        row["gate_reasons"] = json.loads(row.pop("gate_reasons_json"))
        row["boxes"] = json.loads(row.pop("boxes_json"))
        row["preview_url"] = (
            f"/api/assets/{row['preview_digest']}" if row.get("preview_digest") else None
        )
        row["contact_sheet_url"] = (
            f"/api/assets/{row['contact_sheet_digest']}"
            if row.get("contact_sheet_digest") else None
        )
        row["source_plan_url"] = f"/api/assets/{row['source_plan_digest']}"
        return row
