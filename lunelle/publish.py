"""Publish a style's approved try-on assets to the deployed Worker API.

Flow (per the Worker's documented contract):
- result cells  -> R2 `tryon/results/{tryon_id}-{tone}-{code}-v{version}.webp`
                   plus the conventional `{tryon_id}-{tone}-{code}.webp`
- cover icon    -> R2 `tryon/icons/{tryon_id}-light-icon.webp`
- plan image    -> R2 `tryon/plans/{tryon_id}-plan.webp`
- D1: upsert `tryon_styles` as published and upsert each `tryon_assets` result
  row with qa_status='pass' / visual_status='approved', so the manifest endpoint
  (`/v1/styles/:id`) and the static endpoint agree — this closes the documented
  D1/R2 double-source drift.

View codes follow the theme's VIEW_MODEL_MAP:
01=p2_open_hands, 02=p3_right_hand, 03=p4_thumb_visible, 04=p5_left_hand.

WHY VERSIONED, AND WHY BOTH KEYS
The Worker caches images immutable for a year, so overwriting a key meant a
republished cell could serve the old image for months. Versioned keys never
overwrite, and `/v1/styles/:id` reads `r2_key` from D1, so the manifest path picks
up the new image immediately. The conventional key is written too because
`/api/tryon/result` builds its key by convention and would 404 on a versioned one
— that endpoint keeps its existing (unchanged) cache behaviour.

WHY THIS IS SAFE WITHOUT A DISTRIBUTED TRANSACTION
Publishing spans R2 and D1 over several HTTP calls, and the previous flow ran
`DELETE FROM tryon_assets` and then one INSERT per cell as separate requests: a
failure in between left the customer-facing manifest EMPTY while R2 still held the
images. Three properties replace the transaction we cannot have:

1. Upsert in place, never delete-then-recreate, so the manifest is never empty
   mid-publish. Each cell's row is replaced by a single statement, and a single
   statement is atomic.
2. R2 is written before D1, and versioned keys never overwrite, so every row the
   manifest can point at — old or new — resolves to an object that exists.
3. Every intended write is recorded in `publish_versions` BEFORE the first remote
   call, so a partial publish is visible and re-running completes it. Both remote
   operations are idempotent (versioned keys, stable per-cell row ids), which is
   what makes the re-run safe rather than merely likely to work.

The worst partial state is therefore a manifest holding a mix of new and previous
versions, all serviceable, with the ledger showing exactly what is unfinished.
D1's REST endpoint does document multi-statement requests as running as a batch,
which would give true atomicity, but how `params` bind across statements is not
something this code could verify without a live database — so the design does not
depend on it.
"""

from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

from PIL import Image

from .cloudflare import CloudflareClient
from .db import Database, transaction, utcnow
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
TONE_RE = re.compile(r"^[a-z]{3,12}$")
ICON_MAX = 600

PLANNED = "planned"
UPLOADING = "uploading"
UPLOADED = "uploaded"
COMMITTED = "committed"
FAILED = "failed"


class PublishError(Exception):
    pass


def _versioned_key(tryon_id: str, tone: str, code: str, version: int) -> str:
    return f"tryon/results/{tryon_id}-{tone}-{code}-v{version}.webp"


def _conventional_key(tryon_id: str, tone: str, code: str) -> str:
    """The key `/api/tryon/result` builds by convention; it must keep existing."""
    return f"tryon/results/{tryon_id}-{tone}-{code}.webp"


def _allocate_version(db: Database, style_id: str, tryon_id: str, plan: dict) -> dict:
    """Reserve the next version and record the intended end state.

    Allocation and the plan land in one transaction, so two concurrent publishes
    cannot take the same version number, and no remote call happens before the
    intent is durable.
    """
    conn = db.conn()
    with transaction(conn):
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM publish_versions"
            " WHERE style_id = ?", (style_id,),
        ).fetchone()
        version = int(row["v"]) + 1
        now = utcnow()
        plan = {**plan, "version": version}
        cur = conn.execute(
            "INSERT INTO publish_versions (style_id, tryon_style_id, version, state,"
            " plan_json, progress_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,'{}',?,?)",
            (style_id, tryon_id, version, PLANNED,
             json.dumps(plan, ensure_ascii=False), now, now),
        )
        publish_id = int(cur.lastrowid or 0)
    return {"publish_id": publish_id, "version": version, "plan": plan}


def _set_state(db: Database, publish_id: int, state: str, *,
               progress: dict | None = None, error: str | None = None) -> None:
    conn = db.conn()
    now = utcnow()
    with transaction(conn):
        if progress is not None:
            conn.execute(
                "UPDATE publish_versions SET state = ?, progress_json = ?, error = ?,"
                " updated_at = ?, committed_at = CASE WHEN ? = 'committed' THEN ?"
                "   ELSE committed_at END WHERE publish_id = ?",
                (state, json.dumps(progress, ensure_ascii=False), error, now,
                 state, now, publish_id),
            )
        else:
            conn.execute(
                "UPDATE publish_versions SET state = ?, error = ?, updated_at = ?,"
                " committed_at = CASE WHEN ? = 'committed' THEN ? ELSE committed_at END"
                " WHERE publish_id = ?",
                (state, error, now, state, now, publish_id),
            )


def publish_history(db: Database, style_id: str, limit: int = 20) -> list[dict]:
    """Publish attempts for a style, newest first, with unfinished ones visible."""
    rows = db.conn().execute(
        "SELECT publish_id, tryon_style_id, version, state, error, created_at,"
        " updated_at, committed_at, plan_json, progress_json"
        " FROM publish_versions WHERE style_id = ?"
        " ORDER BY version DESC LIMIT ?",
        (style_id, limit),
    ).fetchall()
    out = []
    for row in rows:
        doc = dict(row)
        doc["plan"] = json.loads(doc.pop("plan_json"))
        doc["progress"] = json.loads(doc.pop("progress_json") or "{}")
        out.append(doc)
    return out


def unfinished_publishes(db: Database) -> list[dict]:
    """Publishes that never reached `committed` — the operator-visible backlog."""
    rows = db.conn().execute(
        "SELECT publish_id, style_id, tryon_style_id, version, state, error,"
        " created_at, updated_at FROM publish_versions"
        " WHERE state IN (?,?,?,?) ORDER BY updated_at DESC",
        (PLANNED, UPLOADING, UPLOADED, FAILED),
    ).fetchall()
    return [dict(row) for row in rows]


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
    """Publish one style as a new version. Idempotent and resumable.

    Order matters: R2 first, then D1. A D1 row must never point at an object that
    is not there yet, and because versioned keys never overwrite, an interrupted
    run leaves the previous version fully serviceable.
    """
    style = service.get_style(style_id)
    tryon_id = (style.get("tryon_style_id") or "").strip()
    if not TRYON_ID_RE.match(tryon_id):
        raise PublishError("style needs a 3-digit try-on id (e.g. 001) before publishing")

    ready, excluded = collect_publishable_cells(db, style_id)
    if not ready:
        raise PublishError("no publishable matrix cells: generate the try-on matrix first")

    thumb = _gated_latest_grid(db, style_id)  # the cover icon shares the same gate
    plan_path = Path(style.get("plan_image_path") or "")

    # Record the full intended end state BEFORE any remote call, so a partial
    # publish never has to be reconstructed by guesswork.
    plan_cells = []
    for (tone, view), cell in sorted(ready.items()):
        if not TONE_RE.match(tone):  # defence in depth; tones are a fixed vocabulary
            raise PublishError(f"refusing to build an R2 key from tone {tone!r}")
        plan_cells.append({
            "tone": tone, "view": view, "code": VIEW_CODE[view],
            "task_id": cell["task_id"], "source_path": str(cell["path"]),
            "row_id": f"{tryon_id}-{tone}-{VIEW_CODE[view]}-result",
        })
    allocation = _allocate_version(db, style_id, tryon_id, {
        "tryon_style_id": tryon_id,
        "style_name": style["name"],
        "cells": plan_cells,
        "cover_task_id": thumb["task_id"] if thumb else None,
        "has_plan_image": plan_path.is_file(),
    })
    publish_id = allocation["publish_id"]
    version = allocation["version"]

    progress: dict = {"uploaded_keys": [], "committed_rows": []}
    try:
        _set_state(db, publish_id, UPLOADING, progress=progress)
        uploaded: list[dict] = []
        for cell in plan_cells:
            image = _webp(Path(cell["source_path"]))
            versioned = _versioned_key(tryon_id, cell["tone"], cell["code"], version)
            conventional = _conventional_key(tryon_id, cell["tone"], cell["code"])
            # Versioned key: immutable, so the manifest never serves a stale image
            # from the Worker's year-long cache.
            client.r2_put(versioned, image, "image/webp")
            # Conventional key: /api/tryon/result builds this by convention and
            # would 404 on a versioned one. Same cache behaviour as before.
            client.r2_put(conventional, image, "image/webp")
            progress["uploaded_keys"].extend([versioned, conventional])
            uploaded.append({**cell, "key": versioned,
                             "conventional_key": conventional, "version": version})
            _set_state(db, publish_id, UPLOADING, progress=progress)

        cover_key = None
        if thumb is not None:
            cover_key = f"tryon/icons/{tryon_id}-light-icon.webp"
            client.r2_put(cover_key, _webp(Path(thumb["output_path"]), max_side=ICON_MAX),
                          "image/webp")
            progress["uploaded_keys"].append(cover_key)

        plan_key = None
        if plan_path.is_file():
            plan_key = f"tryon/plans/{tryon_id}-plan.webp"
            client.r2_put(plan_key, _webp(plan_path), "image/webp")
            progress["uploaded_keys"].append(plan_key)

        # Every object is in R2; D1 may now point at any of them.
        _set_state(db, publish_id, UPLOADED, progress=progress)

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

        # --- D1: UPSERT each result row, one statement per cell ---
        # Upsert rather than delete-then-insert: the old flow's DELETE followed by
        # separate INSERTs left the manifest EMPTY if it failed in between. A
        # single-statement upsert is atomic, so each cell either shows its previous
        # version or its new one — never nothing.
        #
        # qa_status='pass' / visual_status='approved' report a verified fact here,
        # not an assumption: every cell cleared the publish gate (heuristic QA
        # passed AND a human approved) before reaching this function.
        for item in uploaded:
            client.d1_query(
                "INSERT INTO tryon_assets (id, style_id, kind, tone, view, r2_key,"
                " qa_status, visual_status, version, updated_at)"
                " VALUES (?, ?, 'result', ?, ?, ?, 'pass', 'approved', ?, datetime('now'))"
                " ON CONFLICT(id) DO UPDATE SET"
                "   r2_key = excluded.r2_key, qa_status = 'pass',"
                "   visual_status = 'approved', version = excluded.version,"
                "   updated_at = datetime('now')",
                [item["row_id"], tryon_id, item["tone"], item["code"],
                 item["key"], version],
            )
            progress["committed_rows"].append(item["row_id"])
            _set_state(db, publish_id, UPLOADED, progress=progress)

        # Drop rows for cells this style no longer publishes. Last, and separately:
        # if it fails the manifest carries one stale-but-serviceable extra cell,
        # which is a far smaller problem than an empty manifest.
        published_row_ids = {item["row_id"] for item in uploaded}
        existing = client.d1_query(
            "SELECT id FROM tryon_assets WHERE style_id = ? AND kind = 'result'",
            [tryon_id],
        )
        stale = [row["id"] for row in existing
                 if row.get("id") and row["id"] not in published_row_ids]
        for row_id in stale:
            client.d1_query("DELETE FROM tryon_assets WHERE id = ?", [row_id])
        progress["removed_rows"] = stale

        _set_state(db, publish_id, COMMITTED, progress=progress)
    except Exception as exc:  # noqa: BLE001 - state must be recorded for any failure
        _set_state(db, publish_id, FAILED, progress=progress,
                   error=f"{type(exc).__name__}: {exc}"[:1000])
        logger.exception("publish %s version %s failed", style_id, version)
        raise

    # Stamp what actually reached production. Best-effort: the assets are already
    # live, so a bookkeeping failure must not turn a successful publish into an
    # error the operator would retry.
    published_task_ids = [item["task_id"] for item in uploaded]
    if thumb is not None:
        published_task_ids.append(thumb["task_id"])
    for task_id in published_task_ids:
        try:
            service.mark_published(task_id, version=version)
        except Exception:  # noqa: BLE001 - never fail an already-live publish
            logger.exception("could not stamp published state for %s", task_id)

    return {
        "tryon_style_id": tryon_id,
        "publish_id": publish_id,
        "version": version,
        "uploaded_cells": uploaded,
        "excluded_cells": excluded,
        "cover_key": cover_key,
        "plan_key": plan_key,
        "removed_rows": progress.get("removed_rows", []),
        "manifest_url": f"https://api.finglow.cn/v1/styles/{tryon_id}",
    }
