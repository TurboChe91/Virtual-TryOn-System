"""Spend accounting and the budget circuit breaker.

Every task in this system costs real money at a real provider, and the try-on
matrix queues 16 of them from a single click. Three independent controls:

1. Preview   — `estimate_plan` prices a batch before anything is queued.
2. Confirm   — anything above LUNELLE_CONFIRM_COST_USD requires the caller to
               echo back the figure it was shown (see server.generate_matrix).
3. Breaker   — LUNELLE_DAILY_BUDGET_USD caps spend per rolling 24h. Checked when
               queueing AND again in the worker immediately before each provider
               call, because a batch authorized an hour ago must not be able to
               spend money the budget no longer allows.

Accounting deliberately differs between the two checks:

- Queueing counts realized spend PLUS the estimated cost of everything still
  in flight, so you cannot over-commit the budget by queueing many batches.
- The worker counts realized spend plus only the task it is about to run.
  Counting in-flight work there would make any batch larger than the remaining
  budget trip on its own queue and deadlock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Config
from .db import Database
from .models import ACTIVE_STATUSES

logger = logging.getLogger(__name__)

WINDOW_HOURS = 24


class BudgetExceeded(Exception):
    """Raised when an action would push rolling spend past the configured cap."""

    def __init__(self, message: str, snapshot: SpendSnapshot):
        super().__init__(message)
        self.snapshot = snapshot


@dataclass(frozen=True)
class SpendSnapshot:
    """Spend in the rolling window. All figures USD."""

    window_hours: int
    realized_usd: float          # provider-reported, plus estimates for finished work
    committed_usd: float         # estimated cost of pending/running/retrying tasks
    limit_usd: float             # 0 == no limit configured
    images_generated: int

    @property
    def total_usd(self) -> float:
        return round(self.realized_usd + self.committed_usd, 4)

    @property
    def enabled(self) -> bool:
        return self.limit_usd > 0

    @property
    def remaining_usd(self) -> float:
        """Headroom against realized+committed. Infinite when no cap is set."""
        if not self.enabled:
            return float("inf")
        return round(max(0.0, self.limit_usd - self.total_usd), 4)

    def as_dict(self) -> dict:
        return {
            "window_hours": self.window_hours,
            "realized_usd": round(self.realized_usd, 4),
            "committed_usd": round(self.committed_usd, 4),
            "total_usd": self.total_usd,
            "limit_usd": round(self.limit_usd, 4),
            "remaining_usd": None if not self.enabled else self.remaining_usd,
            "enabled": self.enabled,
            "images_generated": self.images_generated,
        }


def _window_start(hours: int = WINDOW_HOURS) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S%z")


def spend_snapshot(db: Database, config: Config, *, hours: int = WINDOW_HOURS) -> SpendSnapshot:
    """Current spend in the rolling window.

    `realized` prefers the provider-reported figure and falls back to the
    estimate for successful tasks whose provider does not report spend — the
    same convention `stats.collect_stats` uses, so the two never disagree.
    """
    conn = db.conn()
    since = _window_start(hours)
    realized = conn.execute(
        "SELECT COALESCE(SUM("
        "  CASE WHEN actual_cost_usd IS NOT NULL THEN actual_cost_usd"
        "       ELSE COALESCE(estimated_cost_usd, 0) END"
        "), 0) AS spend,"
        " COUNT(*) AS images"
        " FROM tasks WHERE status = 'success' AND COALESCE(completed_at, created_at) >= ?",
        (since,),
    ).fetchone()
    # Failed attempts can still bill (provider charged, download failed), so any
    # attempt with a recorded cost counts even if its task did not succeed.
    failed_attempts = conn.execute(
        "SELECT COALESCE(SUM(a.cost_usd), 0) AS spend FROM attempts a"
        " JOIN tasks t ON t.task_id = a.task_id"
        " WHERE a.cost_usd IS NOT NULL AND a.outcome = 'error' AND a.started_at >= ?",
        (since,),
    ).fetchone()
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    committed = conn.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) AS spend FROM tasks"  # noqa: S608
        f" WHERE status IN ({placeholders})",  # noqa: S608 - fixed status constants
        tuple(ACTIVE_STATUSES),
    ).fetchone()
    return SpendSnapshot(
        window_hours=hours,
        realized_usd=float(realized["spend"]) + float(failed_attempts["spend"]),
        committed_usd=float(committed["spend"]),
        limit_usd=float(config.daily_budget_usd),
        images_generated=int(realized["images"]),
    )


def estimate_plan(config: Config, *, image_count: int, price_per_image: float) -> dict:
    """Price a batch before it is queued.

    Includes the worst case with automatic retries and auto-regeneration, because
    the figure an operator approves must be the most they can be charged, not the
    happy path.
    """
    base = round(image_count * price_per_image, 4)
    # Each task may re-roll once per auto_regen_max, and each attempt may retry.
    worst_case_multiplier = (1 + config.auto_regen_max) * (1 + config.max_retries)
    return {
        "image_count": image_count,
        "price_per_image_usd": round(price_per_image, 4),
        "estimated_usd": base,
        "worst_case_usd": round(base * worst_case_multiplier, 4),
        "worst_case_note": (
            f"assumes every image needs {config.max_retries} retries and "
            f"{config.auto_regen_max} auto-regeneration(s)"
        ),
        "requires_confirmation": base >= config.confirm_cost_usd > 0,
        "confirm_threshold_usd": round(config.confirm_cost_usd, 4),
    }


def check_can_queue(db: Database, config: Config, *, additional_usd: float) -> SpendSnapshot:
    """Breaker for the queueing path: realized + committed + this batch.

    Raises BudgetExceeded rather than silently trimming the batch — a partially
    queued matrix is worse than a refused one, because the operator would have to
    work out which cells are missing.
    """
    snapshot = spend_snapshot(db, config)
    if not snapshot.enabled:
        return snapshot
    if snapshot.total_usd + additional_usd > snapshot.limit_usd:
        raise BudgetExceeded(
            f"budget_exceeded: this batch would cost ~${additional_usd:.4f}, but only "
            f"${snapshot.remaining_usd:.4f} of the ${snapshot.limit_usd:.2f} "
            f"{snapshot.window_hours}h budget remains "
            f"(${snapshot.realized_usd:.4f} spent, "
            f"${snapshot.committed_usd:.4f} already queued)",
            snapshot,
        )
    return snapshot


def check_can_spend(db: Database, config: Config, *, task_cost_usd: float) -> SpendSnapshot:
    """Breaker for the worker path: realized spend plus only this task.

    In-flight work is excluded on purpose (see module docstring): including it
    would let a batch trip the breaker on the strength of its own queue.
    """
    snapshot = spend_snapshot(db, config)
    if not snapshot.enabled:
        return snapshot
    if snapshot.realized_usd + task_cost_usd > snapshot.limit_usd:
        raise BudgetExceeded(
            f"budget_exceeded: ${snapshot.realized_usd:.4f} already spent in the last "
            f"{snapshot.window_hours}h against a ${snapshot.limit_usd:.2f} cap; "
            f"refusing a further ~${task_cost_usd:.4f}. Raise "
            f"LUNELLE_DAILY_BUDGET_USD or wait for the window to roll.",
            snapshot,
        )
    return snapshot
