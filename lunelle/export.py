"""Shopify asset export: images renamed per convention + manifest + CSVs.

Produces, under LUNELLE_EXPORT_DIR/export-<UTC timestamp>/:
  images/nail-style-<sku>-grid.webp
  images/nail-style-<sku>-wearing.webp
  manifest.json           full provenance (source tasks, hashes, QA)
  products.csv            Shopify product import skeleton
  generation-report.csv   per-asset generation/QA report

Only styles whose grid AND wearing assets pass the shared publish gate are
exported (see lunelle/gating.py): the task succeeded, heuristic QA finished and
passed, and a human approved it. Files are copies — task outputs are never moved
or modified.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from .config import Config
from .db import Database, utcnow
from .gating import LATEST_QA_JOIN, block_reason, row_is_publishable
from .models import OUTPUT_GRID, OUTPUT_WEARING, SUCCESS

logger = logging.getLogger(__name__)

WEBP_QUALITY = 92

PRODUCT_CSV_COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published",
    "Option1 Name", "Option1 Value", "Variant SKU", "Variant Price",
    "Variant Inventory Policy", "Variant Fulfillment Service",
    "Image Src", "Image Position", "Image Alt Text", "Status",
]

REPORT_CSV_COLUMNS = [
    "asset_type", "sku", "style_id", "style_name", "output_type", "task_id", "batch_id",
    "prompt_version", "provider", "model", "status", "retry_count", "cost_usd",
    "qa_passed", "qa_score", "needs_human_review", "output_file", "review_notes",
]


class ExportError(Exception):
    pass


def _claim_export_dir(base: Path, stamp: str) -> Path:
    """Atomically reserve a unique export directory name."""
    base.mkdir(parents=True, exist_ok=True)
    for suffix in [""] + [f"-{n}" for n in range(2, 100)]:
        candidate = base / f"export-{stamp}{suffix}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise ExportError("could not allocate a unique export directory")


def _latest_success(conn, style_id: str, output_type: str) -> dict | None:
    """Latest successful task of this type, joined to its latest HEURISTIC QA row.

    LLM verdicts are excluded from the join (see gating.LATEST_QA_JOIN): they are
    advisory and must never make an asset look gate-eligible.
    """
    row = conn.execute(
        "SELECT t.*, q.qa_id AS qa_id, q.passed AS qa_passed, q.score AS qa_score,"  # noqa: S608
        " q.needs_human_review AS qa_needs_review"
        " FROM tasks t" + LATEST_QA_JOIN +   # module constant, not user input
        " WHERE t.style_id = ? AND t.output_type = ? AND t.status = ?"
        " ORDER BY t.completed_at DESC LIMIT 1",
        (style_id, output_type, SUCCESS),
    ).fetchone()
    return dict(row) if row else None


def _convert_to_webp(source: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        converted = opened.convert("RGB")
        converted.save(dest, format="WEBP", quality=WEBP_QUALITY, method=6)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def run_export(
    db: Database,
    config: Config,
    *,
    skus: list[str] | None = None,
) -> dict:
    conn = db.conn()
    if skus:
        placeholders = ",".join("?" for _ in skus)
        styles = conn.execute(
            f"SELECT * FROM styles WHERE sku IN ({placeholders}) ORDER BY sku",  # noqa: S608
            skus,
        ).fetchall()
        found = {row["sku"] for row in styles}
        missing = [s for s in skus if s not in found]
        if missing:
            raise ExportError(f"unknown sku(s): {', '.join(missing)}")
    else:
        styles = conn.execute("SELECT * FROM styles ORDER BY sku").fetchall()

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # Claim a unique final name up front (guards concurrent exports), then
    # build everything in a staging dir and publish with one atomic rename.
    export_dir = _claim_export_dir(config.export_dir, stamp)
    staging_dir = export_dir.with_name(export_dir.name + ".partial")
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    images_dir = staging_dir / "images"

    items: list[dict] = []
    report_rows: list[dict] = []
    skipped: list[dict] = []

    for style in styles:
        style_doc = dict(style)
        spec = json.loads(style_doc["spec_json"])
        pair = {}
        blocked_reason: str | None = None
        for output_type in (OUTPUT_GRID, OUTPUT_WEARING):
            task = _latest_success(conn, style_doc["style_id"], output_type)
            if task is None or not task.get("output_path") or not Path(task["output_path"]).is_file():
                blocked_reason = f"missing a successful {output_type} asset"
                break
            # Default-deny: the single shared gate decides, and it treats an
            # absent QA row as "not allowed" rather than as "nothing to review".
            if not row_is_publishable(task):
                blocked_reason = f"{output_type}: {block_reason(task)}"
                break
            pair[output_type] = task
        if blocked_reason is not None:
            skipped.append({"sku": style_doc["sku"], "reason": blocked_reason})
            continue

        entry = {"sku": style_doc["sku"], "style_id": style_doc["style_id"],
                 "name": style_doc["name"], "spec": {k: v for k, v in spec.items()
                                                       if not k.startswith("_")},
                 "assets": {}}
        for output_type, task in pair.items():
            filename = f"nail-style-{style_doc['sku']}-{output_type}.webp"
            digest = _convert_to_webp(Path(task["output_path"]), images_dir / filename)
            entry["assets"][output_type] = {
                "file": f"images/{filename}",
                "sha256": digest,
                "source_task_id": task["task_id"],
                "source_output_path": task["output_path"],
                "prompt_version": task["prompt_version"],
                "provider": task["provider"],
                "model": task["model"],
                "qa_passed": bool(task["qa_passed"]) if task["qa_passed"] is not None else None,
                "qa_score": task["qa_score"],
                "needs_human_review": bool(task["qa_needs_review"])
                if task["qa_needs_review"] is not None else True,
            }
            report_rows.append({
                "asset_type": "listing_image",
                "sku": style_doc["sku"],
                "style_id": style_doc["style_id"],
                "style_name": style_doc["name"],
                "output_type": output_type,
                "task_id": task["task_id"],
                "batch_id": task["batch_id"],
                "prompt_version": task["prompt_version"],
                "provider": task["provider"],
                "model": task["model"],
                "status": task["status"],
                "retry_count": task["retry_count"],
                "cost_usd": task["actual_cost_usd"] if task["actual_cost_usd"] is not None
                else task["estimated_cost_usd"],
                "qa_passed": task["qa_passed"],
                "qa_score": task["qa_score"],
                "needs_human_review": task["qa_needs_review"],
                "output_file": f"images/{filename}",
                "review_notes": "",
            })
        items.append(entry)

    if not items:
        shutil.rmtree(staging_dir, ignore_errors=True)
        export_dir.rmdir()
        # Name the actual blockers: "missing" and "withheld pending review" are
        # very different problems, and the operator needs to know which it is.
        detail = "; ".join(f"{s['sku']}: {s['reason']}" for s in skipped[:5])
        suffix = f" ({detail})" if detail else ""
        more = f" and {len(skipped) - 5} more" if len(skipped) > 5 else ""
        raise ExportError(
            "no styles are exportable: every style is missing a grid/wearing asset "
            "or is withheld by the review gate" + suffix + more
        )

    manifest = {
        "export_id": f"export-{stamp}",
        "created_at": utcnow(),
        "item_count": len(items),
        "skipped": skipped,
        "naming_rule": "images/nail-style-<sku>-<output_type>.webp",
        "items": items,
    }
    (staging_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with open(staging_dir / "products.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PRODUCT_CSV_COLUMNS)
        writer.writeheader()
        for entry in items:
            base = {
                "Handle": entry["sku"],
                "Title": entry["name"],
                "Body (HTML)": f"<p>{entry['name']} press-on nail set.</p>",
                "Vendor": "Lunelle Nails",
                "Type": "Press-On Nails",
                "Tags": ",".join(entry["spec"].get("elements", [])[:5]),
                "Published": "FALSE",
                "Option1 Name": "Title",
                "Option1 Value": "Default Title",
                "Variant SKU": entry["sku"],
                "Variant Price": "",
                "Variant Inventory Policy": "deny",
                "Variant Fulfillment Service": "manual",
                "Status": "draft",
            }
            for position, output_type in enumerate((OUTPUT_GRID, OUTPUT_WEARING), start=1):
                row = dict(base) if position == 1 else {"Handle": entry["sku"]}
                row["Image Src"] = entry["assets"][output_type]["file"]
                row["Image Position"] = str(position)
                row["Image Alt Text"] = f"{entry['name']} - {output_type}"
                writer.writerow({col: row.get(col, "") for col in PRODUCT_CSV_COLUMNS})

    with open(staging_dir / "generation-report.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_CSV_COLUMNS)
        writer.writeheader()
        for row in report_rows:
            writer.writerow({col: ("" if row.get(col) is None else row.get(col))
                             for col in REPORT_CSV_COLUMNS})

    # Publish atomically: replace the claimed placeholder with the finished tree.
    export_dir.rmdir()
    staging_dir.rename(export_dir)
    logger.info("export complete: %s (%d styles, %d skipped)",
                export_dir.name, len(items), len(skipped))
    return {"export_dir": str(export_dir), **manifest}
