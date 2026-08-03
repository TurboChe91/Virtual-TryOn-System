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
import logging
import re
from pathlib import Path

from PIL import Image

from .cloudflare import CloudflareClient
from .db import Database
from .gating import GATE_COLUMNS_SQL, LATEST_QA_JOIN, block_reason, row_is_publishable
from .tasks import TaskService

logger = logging.getLogger(__name__)

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
    """Latest matrix cell per (tone, view) that passes the shared publish gate.

    Default-deny: a cell with no QA row, unfinished QA, a failing verdict, or no
    human approval is excluded and reported. The newest cell per (tone, view)
    decides — an older approved version never resurrects a rejected newer one,
    because publishing the older image would contradict the latest verdict.
    """
    conn = db.conn()
    rows = conn.execute(
        "SELECT t.task_id, t.output_path, t.metadata_json, t.status, t.qa_state,"  # noqa: S608
        " t.review_state," + GATE_COLUMNS_SQL +   # module constants, not user input
        " FROM tasks t" + LATEST_QA_JOIN +
        " WHERE t.style_id = ? AND t.output_type = 'matrix_cell'"
        " AND t.status = 'success' ORDER BY t.completed_at DESC",
        (style_id,),
    ).fetchall()
    import json as _json

    cells: dict = {}
    excluded: list[dict] = []
    for row in rows:
        task = dict(row)
        metadata = _json.loads(task["metadata_json"] or "{}")
        tone, view = metadata.get("tone"), metadata.get("view")
        if not tone or view not in VIEW_CODE or (tone, view) in cells:
            continue
        if not row_is_publishable(task):
            excluded.append({"tone": tone, "view": view,
                             "reason": block_reason(task) or "blocked"})
            cells[(tone, view)] = None  # newest verdict wins; block older passes
            continue
        path = Path(task["output_path"] or "")
        if not path.is_file():
            excluded.append({"tone": tone, "view": view,
                             "reason": "output file missing on disk"})
            cells[(tone, view)] = None
            continue
        cells[(tone, view)] = {"tone": tone, "view": view, "path": path,
                               "task_id": task["task_id"]}
    ready = {k: v for k, v in cells.items() if v}
    return ready, excluded


def _gated_latest_grid(db: Database, style_id: str) -> dict | None:
    """Latest grid asset that passes the publish gate, or None."""
    conn = db.conn()
    row = conn.execute(
        "SELECT t.task_id, t.output_path, t.status, t.qa_state, t.review_state,"  # noqa: S608
        + GATE_COLUMNS_SQL +                      # module constants, not user input
        " FROM tasks t" + LATEST_QA_JOIN +
        " WHERE t.style_id = ? AND t.output_type = 'grid' AND t.status = 'success'"
        " ORDER BY t.completed_at DESC LIMIT 1",
        (style_id,),
    ).fetchone()
    if row is None:
        return None
    task = dict(row)
    if not row_is_publishable(task):
        return None
    if not task.get("output_path") or not Path(task["output_path"]).is_file():
        return None
    return task


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

    # The cover icon is customer-facing too, so it goes through the same gate.
    cover_key = None
    thumb = _gated_latest_grid(db, style_id)
    if thumb is not None:
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
    # `qa_status='pass'` / `visual_status='approved'` are no longer asserted on
    # this code's own authority: every cell in `uploaded` already cleared the
    # gate (heuristic QA passed AND a human approved), so the values now report a
    # verified fact rather than assuming one.
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

    # Stamp what actually reached production. Best-effort: the assets are already
    # live, so a bookkeeping failure must not turn a successful publish into an
    # error the operator would retry.
    published_task_ids = [item["task_id"] for item in uploaded]
    if thumb is not None:
        published_task_ids.append(thumb["task_id"])
    for task_id in published_task_ids:
        try:
            service.mark_published(task_id)
        except Exception:  # noqa: BLE001 - never fail an already-live publish
            logger.exception("could not stamp published state for %s", task_id)

    return {
        "tryon_style_id": tryon_id,
        "uploaded_cells": uploaded,
        "excluded_cells": excluded,
        "cover_key": cover_key,
        "plan_key": plan_key,
        "manifest_url": f"https://api.finglow.cn/v1/styles/{tryon_id}",
    }
