"""Aggregated statistics straight from the database — nothing hardcoded."""

from __future__ import annotations

from .db import Database


def collect_stats(db: Database) -> dict:
    conn = db.conn()

    by_status = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    }
    total = sum(by_status.values())
    success = by_status.get("success", 0)
    failed = by_status.get("failed", 0)
    finished = success + failed

    retries = conn.execute("SELECT COALESCE(SUM(retry_count), 0) AS n FROM tasks").fetchone()["n"]

    failure_reasons = [
        dict(row)
        for row in conn.execute(
            "SELECT error_code, COUNT(*) AS n FROM tasks"
            " WHERE status = 'failed' AND error_code IS NOT NULL"
            " GROUP BY error_code ORDER BY n DESC"
        )
    ]

    cost_row = conn.execute(
        "SELECT COALESCE(SUM(actual_cost_usd), 0) AS actual,"
        " COALESCE(SUM(CASE WHEN actual_cost_usd IS NULL AND status = 'success'"
        "   THEN estimated_cost_usd ELSE 0 END), 0) AS estimated_for_success,"
        " COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS images"
        " FROM tasks"
    ).fetchone()

    attempts_row = conn.execute(
        "SELECT COUNT(*) AS attempts, COALESCE(SUM(cost_usd), 0) AS attempt_cost"
        " FROM attempts WHERE outcome != 'started'"
    ).fetchone()

    per_sku = [
        dict(row)
        for row in conn.execute(
            "SELECT sku,"
            " SUM(CASE WHEN output_type='grid' AND status='success' THEN 1 ELSE 0 END) AS grid_success,"
            " SUM(CASE WHEN output_type='wearing' AND status='success' THEN 1 ELSE 0 END) AS wearing_success,"
            " SUM(CASE WHEN status IN ('pending','running','retrying') THEN 1 ELSE 0 END) AS active,"
            " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,"
            " COUNT(*) AS total"
            " FROM tasks GROUP BY sku ORDER BY sku"
        )
    ]
    for row in per_sku:
        row["complete"] = bool(row["grid_success"] and row["wearing_success"])

    qa_row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(passed), 0) AS passed FROM qa_results"
    ).fetchone()

    return {
        "tasks": {
            "total": total,
            "by_status": by_status,
            "success": success,
            "failed": failed,
            "running": by_status.get("running", 0),
            "pending": by_status.get("pending", 0),
            "retrying": by_status.get("retrying", 0),
            "cancelled": by_status.get("cancelled", 0),
            "success_rate": round(success / finished, 4) if finished else None,
            "total_retries": retries,
        },
        "failure_reasons": failure_reasons,
        "cost_usd": {
            "actual_recorded": round(cost_row["actual"], 4),
            "estimated_for_success_without_actual": round(cost_row["estimated_for_success"], 4),
            "successful_images": cost_row["images"],
            "provider_attempts": attempts_row["attempts"],
            "note": "actual_recorded is provider-reported spend; the estimate covers "
                    "successful generations whose provider does not report spend.",
        },
        "qa": {
            "total": qa_row["total"],
            "passed": qa_row["passed"],
        },
        "per_sku": per_sku,
    }
