"""Shopify asset export: images renamed per convention + manifest + CSVs.

Produces, under LUNELLE_EXPORT_DIR/export-<UTC timestamp>/:
  images/nail-style-<sku>-grid.webp
  images/nail-style-<sku>-wearing.webp
  manifest.json           full provenance (source tasks, hashes, QA)
  products.csv            Shopify product import skeleton
  generation-report.csv   per-asset generation/QA report

Only styles whose grid AND wearing tasks succeeded are exported. Files are
copies — task outputs are never moved or modified.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from .config import Config
from .db import Database, utcnow
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


def _latest_success(conn, style_id: str, output_type: str) -> dict | None:
    row = conn.execute(
        "SELECT t.*, q.passed AS qa_passed, q.score AS qa_score,"
        " q.needs_human_review AS qa_needs_review"
        " FROM tasks t LEFT JOIN qa_results q ON q.qa_id ="
        "   (SELECT qa_id FROM qa_results WHERE task_id = t.task_id ORDER BY qa_id DESC LIMIT 1)"
        " WHERE t.style_id = ? AND t.output_type = ? AND t.status = ?"
        " ORDER BY t.completed_at DESC LIMIT 1",
        (style_id, output_type, SUCCESS),
    ).fetchone()
    return dict(row) if row else None


def _convert_to_webp(source: Path, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGB")
        image.save(dest, format="WEBP", quality=WEBP_QUALITY, method=6)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def run_export(
    db: Database,
    config: Config,
    *,
    skus: list[str] | None = None,
    include_unreviewed: bool = True,
) -> dict:
    conn = db.conn()
    if skus:
        placeholders = ",".join("?" for _ in skus)
        styles = conn.execute(
            f"SELECT * FROM styles WHERE sku IN ({placeholders}) ORDER BY sku", skus
        ).fetchall()
        found = {row["sku"] for row in styles}
        missing = [s for s in skus if s not in found]
        if missing:
            raise ExportError(f"unknown sku(s): {', '.join(missing)}")
    else:
        styles = conn.execute("SELECT * FROM styles ORDER BY sku").fetchall()

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    export_dir = config.export_dir / f"export-{stamp}"
    images_dir = export_dir / "images"

    items: list[dict] = []
    report_rows: list[dict] = []
    skipped: list[dict] = []

    for style in styles:
        style_doc = dict(style)
        spec = json.loads(style_doc["spec_json"])
        pair = {}
        complete = True
        for output_type in (OUTPUT_GRID, OUTPUT_WEARING):
            task = _latest_success(conn, style_doc["style_id"], output_type)
            if task is None or not task.get("output_path") or not Path(task["output_path"]).is_file():
                complete = False
                break
            if not include_unreviewed and task.get("qa_needs_review"):
                complete = False
                break
            pair[output_type] = task
        if not complete:
            skipped.append({"sku": style_doc["sku"],
                            "reason": "missing a successful grid or wearing asset"})
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
        raise ExportError(
            "no styles have both a successful grid and wearing image to export"
        )

    export_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "export_id": f"export-{stamp}",
        "created_at": utcnow(),
        "item_count": len(items),
        "skipped": skipped,
        "naming_rule": "images/nail-style-<sku>-<output_type>.webp",
        "items": items,
    }
    (export_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with open(export_dir / "products.csv", "w", newline="", encoding="utf-8") as fh:
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

    with open(export_dir / "generation-report.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_CSV_COLUMNS)
        writer.writeheader()
        for row in report_rows:
            writer.writerow({col: ("" if row.get(col) is None else row.get(col))
                             for col in REPORT_CSV_COLUMNS})

    logger.info("export complete: %s (%d styles, %d skipped)",
                export_dir.name, len(items), len(skipped))
    return {"export_dir": str(export_dir), **manifest}
