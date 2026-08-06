#!/usr/bin/env python
"""Import colour-annotated hand photos and derive one mask per nail.

Input is two parallel trees, keyed by tone and view:

    <annotations>/{light,medium,tan,deep}/{p2,p3,p4,p5}.png   colour-annotated
    <bases>/{light,medium,tan,deep}/<view file>               clean photo

The clean photo is what gets sent to the provider. The annotated one is read
only to derive masks and is never uploaded anywhere.

Every import verifies, and refuses on failure:
  - the colour set matches the view's visible_nails in matrix_views.json
  - each nail region is a single connected blob
  - the annotation is pixel-aligned with the clean photo

Re-running is safe: an unchanged pair is skipped, and a changed base photo
creates a NEW revision rather than mutating the old one, so task snapshots that
reference the old revision keep resolving.

    python scripts/import_hand_models.py --dry-run
    python scripts/import_hand_models.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lunelle.assets import digest_of_file, register_locked, store_bytes
from lunelle.config import load_config
from lunelle.db import Database, transaction, utcnow
from lunelle.nailslots import SlotError, derive_slots, verify_alignment
from lunelle.prompts import MATRIX_CONTRACT

#: Asset locations. These live outside the repo (they are large binary source
#: material), so the defaults are this operator's paths and both are overridable
#: by env var or flag — a checkout on another machine passes its own.
DEFAULT_ANNOTATIONS = Path(
    os.environ.get("LUNELLE_ANNOTATION_DIR", "/Users/turboche/Desktop/标注")
)
DEFAULT_BASES = Path(os.environ.get(
    "LUNELLE_HAND_BASE_DIR",
    "/Users/turboche/Desktop/Lunelle_AI_ImageGen/fork/shopify-theme/assets/"
    "tryon-ai/models/natural",
))

#: Short view key (as used in the annotation filenames) -> contract view name.
VIEW_KEYS = {
    "p2": "p2_open_hands",
    "p3": "p3_right_hand",
    "p4": "p4_thumb_visible",
    "p5": "p5_left_hand",
}
#: Clean-photo filename per view in the theme asset tree.
BASE_FILES = {
    "p2": "p2-open-hands.webp",
    "p3": "p3-right-hand.webp",
    "p4": "p4-thumb-visible.webp",
    "p5": "p5-left-hand.webp",
}
TONES = ("light", "medium", "tan", "deep")


def resolve_base(bases: Path, tone: str, view_key: str, db: Database,
                 config, contract_view: str, annotation: Path) -> Path | None:
    """Clean photo matching this annotation's dimensions.

    Two sources are tried, and SIZE decides between them rather than preference
    order: masks are pixel coordinates, so a base of different dimensions is the
    wrong file no matter how canonical it looks. The theme tree holds p2 at
    1672x941 while the annotations are 1448x1086 — for that view the copy already
    registered in Studio is the one the annotations were painted on.
    """
    from PIL import Image

    with Image.open(annotation) as image:
        want = image.size

    candidates: list[Path] = []
    theme = bases / tone / BASE_FILES[view_key]
    if theme.is_file():
        candidates.append(theme)
    row = db.conn().execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (f"hand_model_{tone}_{contract_view}",),
    ).fetchone()
    if row is not None:
        existing = Path(dict(row)["value"])
        if existing.is_file():
            candidates.append(existing)

    for candidate in candidates:
        with Image.open(candidate) as image:
            if image.size == want:
                return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--bases", type=Path, default=DEFAULT_BASES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--note", default="imported from colour annotation set")
    args = parser.parse_args()

    config = load_config()
    db = Database(config.db_path)

    imported = 0
    skipped = 0
    failures: list[str] = []
    total_slots = 0

    for tone in TONES:
        for view_key, contract_view in VIEW_KEYS.items():
            label = f"{tone}/{view_key}"
            annotation = args.annotations / tone / f"{view_key}.png"
            if not annotation.is_file():
                failures.append(f"{label}: annotation missing at {annotation}")
                continue
            base = resolve_base(args.bases, tone, view_key, db, config,
                                contract_view, annotation)
            if base is None:
                failures.append(
                    f"{label}: no clean base photo matching the annotation's size")
                continue

            expected = MATRIX_CONTRACT["views"][contract_view]["visible_nails"]
            try:
                alignment = verify_alignment(annotation, base)
                slots = derive_slots(annotation, expected_nails=expected)
            except SlotError as exc:
                failures.append(f"{label}: {exc}")
                continue

            from PIL import Image

            with Image.open(base) as image:
                width, height = image.size

            base_digest = digest_of_file(base)
            conn = db.conn()
            existing = conn.execute(
                "SELECT hand_model_id, base_digest, revision FROM hand_models"
                " WHERE tone = ? AND view = ? AND retired_at IS NULL",
                (tone, contract_view),
            ).fetchone()
            if existing is not None and dict(existing)["base_digest"] == base_digest:
                print(f"  {label:14} unchanged, skipping "
                      f"({len(slots)} slots already imported)")
                skipped += 1
                continue

            revision = (dict(existing)["revision"] + 1) if existing is not None else 1
            hand_model_id = f"hm_{tone}_{view_key}_r{revision}"
            print(f"  {label:14} {len(slots)} slots  {width}x{height}  "
                  f"align max={alignment['max_diff']}  -> revision {revision}")
            total_slots += len(slots)
            if args.dry_run:
                continue

            now = utcnow()
            # Masks are stored outside the write transaction: store_bytes owns its
            # own transaction, and SQLite cannot nest BEGIN IMMEDIATE.
            stored = []
            for slot in slots:
                ref = store_bytes(db, config, slot.png_bytes,
                                  kind="nail_mask", ext=".png")
                stored.append((slot, ref))

            with transaction(conn):
                base_ref = register_locked(conn, base, kind="hand_base")
                ann_ref = register_locked(conn, annotation, kind="hand_annotation")
                if existing is not None:
                    conn.execute(
                        "UPDATE hand_models SET retired_at = ? WHERE hand_model_id = ?",
                        (now, dict(existing)["hand_model_id"]),
                    )
                conn.execute(
                    "INSERT INTO hand_models (hand_model_id, tone, view, revision,"
                    " base_digest, annotation_digest, width, height, source_note,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (hand_model_id, tone, contract_view, revision,
                     base_ref.digest, ann_ref.digest if ann_ref else None,
                     width, height, args.note, now),
                )
                for slot, ref in stored:
                    x, y, w, h = slot.bbox
                    conn.execute(
                        "INSERT INTO hand_model_slots (hand_model_id, nail_id, hand,"
                        " finger, mask_digest, bbox_x, bbox_y, bbox_w, bbox_h,"
                        " area_px, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (hand_model_id, slot.nail_id, slot.hand, slot.finger,
                         ref.digest, x, y, w, h, slot.area_px, now),
                    )
            imported += 1

    print()
    print(f"imported {imported}, skipped {skipped}, slots {total_slots}")
    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for line in failures:
            print(f"  - {line}")
        return 1
    if args.dry_run:
        print("dry run — nothing written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
