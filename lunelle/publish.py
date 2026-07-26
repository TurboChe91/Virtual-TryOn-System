"""Publish a style's approved try-on assets to the deployed Worker API.

Flow (per the Worker's documented contract):
- result cells  -> R2 `tryon/results/{tryon_id}-{tone}-{code}.webp`
- cover icon    -> R2 `tryon/icons/{tryon_id}-light-icon.webp`
- plan image    -> R2 `tryon/plans/{tryon_id}-plan.webp`
- D1: upsert `tryon_styles` as published; replace `tryon_assets` result rows
  with qa_status='pass' / visual_status='approved' so the manifest endpoint
  (`/v1/styles/:id`) and the static endpoint agree — this closes the
  documented D1/R2 double-source drift.

View codes follow the theme's VIEW_MODEL_MAP:
01=p2_open_hands, 02=p3_right_hand, 03=p4_thumb_visible, 04=p5_left_hand.

Caveat from the Worker docs: images are cached immutable for a year, so
republishing a cell overwrites the same key and browsers may keep the old
image until cache eviction. Bump the Worker to versioned keys later if this
becomes a problem.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image

from .cloudflare import CloudflareClient
from .db import Database
from .tasks import TaskService

VIEW_CODE = {
    "p2_open_hands": "01",
    "p3_right_hand": "02",
    "p4_thumb_visible": "03",
    "p5_left_hand": "04",
}
TRYON_ID_RE = re.compile(r"^\d{3}$")
ICON_MAX = 600


class PublishError(Exception):
    pass


def _webp(path: Path, max_side: int | None = None, quality: int = 88) -> bytes:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        if max_side and max(rgb.size) > max_side:
            rgb.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        rgb.save(buf, "WEBP", quality=quality, method=4)
        return buf.getvalue()


def collect_publishable_cells(db: Database, style_id: str) -> tuple[dict, list[dict]]:
    """Latest successful matrix cell per (tone, view); a failing latest QA
    excludes the cell, absent QA does not (heuristics may be offline)."""
    conn = db.conn()
    rows = conn.execute(
        "SELECT t.task_id, t.output_path, t.metadata_json,"
        " (SELECT passed FROM qa_results q WHERE q.task_id = t.task_id"
        "  ORDER BY q.qa_id DESC LIMIT 1) AS qa_passed"
        " FROM tasks t WHERE t.style_id = ? AND t.output_type = 'matrix_cell'"
        " AND t.status = 'success' ORDER BY t.completed_at DESC",
        (style_id,),
    ).fetchall()
    import json as _json

    cells: dict = {}
    excluded: list[dict] = []
    for row in rows:
        metadata = _json.loads(row["metadata_json"] or "{}")
        tone, view = metadata.get("tone"), metadata.get("view")
        if not tone or view not in VIEW_CODE or (tone, view) in cells:
            continue
        if row["qa_passed"] == 0:
            excluded.append({"tone": tone, "view": view, "reason": "qa_failed"})
            cells[(tone, view)] = None  # newest verdict wins; block older passes
            continue
        path = Path(row["output_path"] or "")
        if path.is_file():
            cells[(tone, view)] = {"tone": tone, "view": view, "path": path,
                                   "task_id": row["task_id"]}
    ready = {k: v for k, v in cells.items() if v}
    return ready, excluded


def publish_style(db: Database, service: TaskService, client: CloudflareClient,
                  style_id: str) -> dict:
    style = service.get_style(style_id)
    tryon_id = (style.get("tryon_style_id") or "").strip()
    if not TRYON_ID_RE.match(tryon_id):
        raise PublishError("style needs a 3-digit try-on id (e.g. 001) before publishing")

    ready, excluded = collect_publishable_cells(db, style_id)
    if not ready:
        raise PublishError("no publishable matrix cells: generate the try-on matrix first")

    uploaded: list[dict] = []
    for (tone, view), cell in sorted(ready.items()):
        code = VIEW_CODE[view]
        key = f"tryon/results/{tryon_id}-{tone}-{code}.webp"
        client.r2_put(key, _webp(cell["path"]), "image/webp")
        uploaded.append({"tone": tone, "view": view, "code": code, "key": key,
                         "task_id": cell["task_id"]})

    cover_key = None
    thumb = service.latest_successful_grid(style_id)
    if thumb and thumb.get("output_path") and Path(thumb["output_path"]).is_file():
        cover_key = f"tryon/icons/{tryon_id}-light-icon.webp"
        client.r2_put(cover_key, _webp(Path(thumb["output_path"]), max_side=ICON_MAX),
                      "image/webp")

    plan_key = None
    plan_path = Path(style.get("plan_image_path") or "")
    if plan_path.is_file():
        plan_key = f"tryon/plans/{tryon_id}-plan.webp"
        client.r2_put(plan_key, _webp(plan_path), "image/webp")

    # --- D1: style row (preserve any existing Shopify mapping/sort) ---
    client.d1_query(
        "INSERT INTO tryon_styles (id, label, category, status, cover_r2_key,"
        " plan_r2_key, sort_order, updated_at)"
        " VALUES (?, ?, 'Try-On', 'published', ?, ?, ?, datetime('now'))"
        " ON CONFLICT(id) DO UPDATE SET"
        " label = excluded.label, status = 'published',"
        " cover_r2_key = COALESCE(excluded.cover_r2_key, tryon_styles.cover_r2_key),"
        " plan_r2_key = COALESCE(excluded.plan_r2_key, tryon_styles.plan_r2_key),"
        " updated_at = datetime('now')",
        [tryon_id, style["name"], cover_key, plan_key, int(tryon_id) * 10],
    )
    # --- D1: replace result asset rows so the manifest matches R2 exactly ---
    client.d1_query(
        "DELETE FROM tryon_assets WHERE style_id = ? AND kind = 'result'", [tryon_id]
    )
    for item in uploaded:
        client.d1_query(
            "INSERT INTO tryon_assets (id, style_id, kind, tone, view, r2_key,"
            " qa_status, visual_status, version, updated_at)"
            " VALUES (?, ?, 'result', ?, ?, ?, 'pass', 'approved', 1, datetime('now'))",
            [f"{tryon_id}-{item['tone']}-{item['code']}-result", tryon_id,
             item["tone"], item["code"], item["key"]],
        )

    return {
        "tryon_style_id": tryon_id,
        "uploaded_cells": uploaded,
        "excluded_cells": excluded,
        "cover_key": cover_key,
        "plan_key": plan_key,
        "manifest_url": f"https://api.finglow.cn/v1/styles/{tryon_id}",
    }
