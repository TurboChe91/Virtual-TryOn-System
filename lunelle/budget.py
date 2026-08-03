"""Spend accounting, the budget circuit breaker, and per-lineage budgets.

Every task costs real money at a real provider, and the try-on matrix queues 16
from one click. Four controls, in the order money can escape:

1. Preview   — `estimate_plan` prices a batch before anything is queued.
2. Confirm   — a batch whose WORST case reaches LUNELLE_CONFIRM_COST_USD must be
               authorized by echoing that ceiling back (see
               TaskService._require_cost_authorization).
3. Reserve   — spend is claimed in a transaction BEFORE each provider call and
               settled at the real cost after, so the rolling cap holds with any
               number of worker threads. A plain read-then-spend check does not:
               two workers both read "under budget", then both spend.
4. Lineage   — automatic re-generations and corrections draw from ONE shared
               per-root budget (count and dollars), so they cannot alternate and
               reset each other's limits.

Accounting differs by purpose, deliberately:

- Queue authorization counts settled + reserved spend PLUS the estimated cost of
  work already queued, so many batches cannot collectively overrun the cap.
- The worker's reservation counts settled + reserved only. Counting queued work
  there would make any batch larger than the remaining budget deadlock on its own
  queue.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Config
from .db import Database, transaction, utcnow
from .models import ACTIVE_STATUSES

logger = logging.getLogger(__name__)

WINDOW_HOURS = 24

RESERVED = "reserved"
SETTLED = "settled"
RELEASED = "released"

#: Comparison tolerance, a hundredth of a cent. Summed float prices do not land
#: on their decimal value: 0.05 + 0.05 + 0.05 == 0.15000000000000002, so a naive
#: `> limit` refuses a call that exactly fits a $0.15 budget. Money is compared
#: to this tolerance rather than exactly.
EPSILON_USD = 1e-9


def _exceeds(total: float, limit: float) -> bool:
    """True when `total` is over `limit` by more than float noise."""
    return total - limit > EPSILON_USD


class BudgetExceeded(Exception):
    """Raised when an action would push rolling spend past the configured cap."""

    def __init__(self, message: str, snapshot: SpendSnapshot):
        super().__init__(message)
        self.snapshot = snapshot


class LineageBudgetExceeded(Exception):
    """Raised when a lineage has used up its shared automatic-work budget."""

    def __init__(self, message: str, lineage: dict):
        super().__init__(message)
        self.lineage = lineage


@dataclass(frozen=True)
class SpendSnapshot:
    """Spend in the rolling window. All figures USD."""

    window_hours: int
    realized_usd: float          # settled reservations (actual cost where known)
    reserved_usd: float          # in-flight provider calls
    committed_usd: float         # estimated cost of queued-but-unstarted work
    limit_usd: float             # 0 == no limit configured
    images_generated: int

    @property
    def spent_usd(self) -> float:
        """Money already gone or currently in flight."""
        return round(self.realized_usd + self.reserved_usd, 6)

    @property
    def total_usd(self) -> float:
        """Spent plus everything queued — the queue-authorization view."""
        return round(self.realized_usd + self.reserved_usd + self.committed_usd, 6)

    @property
    def enabled(self) -> bool:
        return self.limit_usd > 0

    @property
    def remaining_usd(self) -> float:
        if not self.enabled:
            return float("inf")
        return round(max(0.0, self.limit_usd - self.total_usd), 6)

    def as_dict(self) -> dict:
        return {
            "window_hours": self.window_hours,
            "realized_usd": round(self.realized_usd, 4),
            "reserved_usd": round(self.reserved_usd, 4),
            "committed_usd": round(self.committed_usd, 4),
            "spent_usd": round(self.spent_usd, 4),
            "total_usd": round(self.total_usd, 4),
            "limit_usd": round(self.limit_usd, 4),
            "remaining_usd": None if not self.enabled else round(self.remaining_usd, 4),
            "enabled": self.enabled,
            "images_generated": self.images_generated,
        }


def _window_start(hours: int = WINDOW_HOURS) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def snapshot_locked(conn: sqlite3.Connection, config: Config, *,
                    hours: int = WINDOW_HOURS) -> SpendSnapshot:
    """Spend snapshot on an existing connection.

    Separate from `spend_snapshot` so callers inside a write transaction reuse
    the same accounting without opening a second one — the atomicity in item 3
    depends on the check and the claim sharing one transaction.
    """
    since = _window_start(hours)
    ledger = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN state = 'settled'"
        "   THEN COALESCE(actual_usd, estimated_usd) ELSE 0 END), 0) AS realized,"
        " COALESCE(SUM(CASE WHEN state = 'reserved' THEN estimated_usd ELSE 0 END), 0)"
        "   AS reserved,"
        " COALESCE(SUM(CASE WHEN state = 'settled' THEN 1 ELSE 0 END), 0) AS billed_calls"
        " FROM spend_reservations WHERE created_at >= ?",
        (since,),
    ).fetchone()
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    # Queued work that has not reserved yet. Tasks with a live reservation are
    # excluded so an in-flight attempt is never counted twice.
    committed = conn.execute(
        "SELECT COALESCE(SUM(t.estimated_cost_usd), 0) AS spend FROM tasks t"  # noqa: S608
        f" WHERE t.status IN ({placeholders})"  # noqa: S608 - fixed status constants
        " AND NOT EXISTS (SELECT 1 FROM spend_reservations r"
        "   WHERE r.task_id = t.task_id AND r.state = 'reserved')",
        tuple(ACTIVE_STATUSES),
    ).fetchone()
    images = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks"
        " WHERE status = 'success' AND COALESCE(completed_at, created_at) >= ?",
        (since,),
    ).fetchone()
    return SpendSnapshot(
        window_hours=hours,
        realized_usd=float(ledger["realized"]),
        reserved_usd=float(ledger["reserved"]),
        committed_usd=float(committed["spend"]),
        limit_usd=float(config.daily_budget_usd),
        images_generated=int(images["n"]),
    )


def spend_snapshot(db: Database, config: Config, *,
                   hours: int = WINDOW_HOURS) -> SpendSnapshot:
    return snapshot_locked(db.conn(), config, hours=hours)


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def estimate_plan(config: Config, *, image_count: int, price_per_image: float) -> dict:
    """Price a batch before it is queued.

    Both the confirmation threshold and the figure a caller must authorize are the
    WORST case. Authorizing the expected figure would mean approving $0.64 and
    being charged up to $3.84 — the number someone signs off on has to be the
    ceiling they are exposed to.

    The worst case is a real bound now: retries multiply each attempt, and the
    per-root lineage ledger caps automatic descendants. While auto-regen and
    auto-correct kept separate counters that reset each other, no such bound
    existed.
    """
    base = round(image_count * price_per_image, 4)
    attempts_per_image = 1 + config.max_retries
    generations_per_image = 1 + config.max_lineage_descendants
    worst_case = round(base * attempts_per_image * generations_per_image, 4)
    threshold = config.confirm_cost_usd
    return {
        "image_count": image_count,
        "price_per_image_usd": round(price_per_image, 4),
        "estimated_usd": base,
        "worst_case_usd": worst_case,
        "worst_case_note": (
            f"every image exhausting {config.max_retries} retries, and each root "
            f"spending its full lineage budget of {config.max_lineage_descendants} "
            f"automatic re-generation(s)/correction(s)"
        ),
        # Gate on the ceiling: a cheap-looking batch with a large worst case is
        # exactly what deserves a confirmation prompt.
        "requires_confirmation": worst_case >= threshold > 0,
        "confirm_threshold_usd": round(threshold, 4),
        #: The value a caller must echo back as confirm_max_usd.
        "confirm_max_usd": worst_case,
    }


# ---------------------------------------------------------------------------
# Queue authorization
# ---------------------------------------------------------------------------


def check_can_queue_locked(conn: sqlite3.Connection, config: Config, *,
                           additional_usd: float) -> SpendSnapshot:
    """Queue-time breaker, to be called INSIDE the caller's write transaction.

    Being inside the transaction is the point: `transaction()` uses BEGIN
    IMMEDIATE, which serializes writers, so two callers cannot both pass the check
    and then both insert. Raises rather than trimming the batch — a partially
    queued matrix is worse than a refused one, because the operator then has to
    work out which cells are missing.
    """
    snapshot = snapshot_locked(conn, config)
    if not snapshot.enabled:
        return snapshot
    if _exceeds(snapshot.total_usd + additional_usd, snapshot.limit_usd):
        raise BudgetExceeded(
            f"budget_exceeded: this batch would cost ~${additional_usd:.4f}, but only "
            f"${snapshot.remaining_usd:.4f} of the ${snapshot.limit_usd:.2f} "
            f"{snapshot.window_hours}h budget remains "
            f"(${snapshot.realized_usd:.4f} spent, "
            f"${snapshot.reserved_usd:.4f} in flight, "
            f"${snapshot.committed_usd:.4f} already queued)",
            snapshot,
        )
    return snapshot


def check_can_queue(db: Database, config: Config, *,
                    additional_usd: float) -> SpendSnapshot:
    """Standalone queue check for callers with no transaction of their own.

    Prefer `check_can_queue_locked` from inside a write transaction: this variant
    cannot be atomic with a subsequent insert.
    """
    conn = db.conn()
    with transaction(conn):
        return check_can_queue_locked(conn, config, additional_usd=additional_usd)


# ---------------------------------------------------------------------------
# Reservations: the atomic claim before a paid call
# ---------------------------------------------------------------------------


def reserve_spend(db: Database, config: Config, *, task_id: str, attempt_no: int,
                  estimated_usd: float, root_task_id: str | None) -> int:
    """Claim budget for one provider call. Returns the reservation id.

    The check and the claim happen in ONE transaction, so concurrent workers
    serialize: whoever commits first is counted by whoever comes second. This is
    what makes the cap hold with LUNELLE_MAX_CONCURRENCY > 1, which a
    read-then-spend check could not do.

    Raises BudgetExceeded before any money is spent. Counts settled + reserved
    only, NOT queued work: including the queue would let a batch trip the breaker
    on the strength of its own pending tasks and deadlock.
    """
    conn = db.conn()
    with transaction(conn):
        snapshot = snapshot_locked(conn, config)
        if snapshot.enabled and _exceeds(
            snapshot.spent_usd + estimated_usd, snapshot.limit_usd
        ):
            raise BudgetExceeded(
                f"budget_exceeded: ${snapshot.realized_usd:.4f} spent and "
                f"${snapshot.reserved_usd:.4f} in flight in the last "
                f"{snapshot.window_hours}h against a ${snapshot.limit_usd:.2f} cap; "
                f"refusing a further ~${estimated_usd:.4f}. Raise "
                f"LUNELLE_DAILY_BUDGET_USD or wait for the window to roll.",
                snapshot,
            )
        now = utcnow()
        cur = conn.execute(
            "INSERT INTO spend_reservations (task_id, attempt_no, root_task_id,"
            " estimated_usd, actual_usd, state, created_at)"
            " VALUES (?,?,?,?,NULL,?,?)"
            # A retried attempt number can recur after a release; replace it.
            " ON CONFLICT(task_id, attempt_no) DO UPDATE SET"
            "   estimated_usd = excluded.estimated_usd, state = excluded.state,"
            "   actual_usd = NULL, created_at = excluded.created_at, settled_at = NULL",
            (task_id, attempt_no, root_task_id, estimated_usd, RESERVED, now),
        )
        reservation_id = cur.lastrowid
        if reservation_id is None or not cur.rowcount:  # pragma: no cover - defensive
            row = conn.execute(
                "SELECT reservation_id FROM spend_reservations"
                " WHERE task_id = ? AND attempt_no = ?", (task_id, attempt_no)
            ).fetchone()
            reservation_id = int(row["reservation_id"])
    return int(reservation_id)


def settle_spend(db: Database, *, task_id: str, attempt_no: int,
                 actual_usd: float | None) -> None:
    """Record the real cost of a completed call.

    `actual_usd=None` keeps the reservation's estimate, which is the honest
    reading for providers that do not report spend (every OpenAI-compatible image
    API so far): the money was spent, only the exact figure is unknown.
    """
    conn = db.conn()
    with transaction(conn):
        if actual_usd is None:
            conn.execute(
                "UPDATE spend_reservations SET state = ?, actual_usd = estimated_usd,"
                " settled_at = ? WHERE task_id = ? AND attempt_no = ? AND state = ?",
                (SETTLED, utcnow(), task_id, attempt_no, RESERVED),
            )
        else:
            conn.execute(
                "UPDATE spend_reservations SET state = ?, actual_usd = ?, settled_at = ?"
                " WHERE task_id = ? AND attempt_no = ? AND state = ?",
                (SETTLED, actual_usd, utcnow(), task_id, attempt_no, RESERVED),
            )


def release_spend(db: Database, *, task_id: str, attempt_no: int,
                  known_cost_usd: float | None = None) -> None:
    """Free a reservation whose call did not bill, or settle it at a known cost.

    Releasing on an unknown-cost failure trades a little accuracy for liveness: a
    provider that errors after billing would be under-counted. Holding the
    reservation instead would let a run of transient failures permanently consume
    the day's budget, which is the worse failure. Where the provider does report a
    charge, pass it and the reservation settles at that figure.
    """
    conn = db.conn()
    with transaction(conn):
        if known_cost_usd is not None:
            conn.execute(
                "UPDATE spend_reservations SET state = ?, actual_usd = ?, settled_at = ?"
                " WHERE task_id = ? AND attempt_no = ? AND state = ?",
                (SETTLED, known_cost_usd, utcnow(), task_id, attempt_no, RESERVED),
            )
        else:
            conn.execute(
                "UPDATE spend_reservations SET state = ?, settled_at = ?"
                " WHERE task_id = ? AND attempt_no = ? AND state = ?",
                (RELEASED, utcnow(), task_id, attempt_no, RESERVED),
            )


def recover_orphan_reservations(db: Database) -> int:
    """Settle reservations left in flight by a crash, at their estimate.

    A crash between reserve and settle leaves a row claiming budget forever.
    Settling (not releasing) is the safe direction: the call may well have reached
    the provider and billed, so the money is assumed spent.
    """
    conn = db.conn()
    with transaction(conn):
        cur = conn.execute(
            "UPDATE spend_reservations SET state = ?, actual_usd ="
            " COALESCE(actual_usd, estimated_usd), settled_at = ?"
            " WHERE state = ? AND task_id IN ("
            "   SELECT task_id FROM tasks WHERE status != 'running')",
            (SETTLED, utcnow(), RESERVED),
        )
        count = cur.rowcount or 0
    if count:
        logger.warning(
            "settled %d orphaned spend reservation(s) at their estimate", count
        )
    return count


# ---------------------------------------------------------------------------
# Lineage budgets: ONE allowance shared by auto-regen and auto-correct
# ---------------------------------------------------------------------------


def lineage_max_usd(config: Config, price_per_image: float) -> float:
    """Dollar ceiling for one root's automatic work, including the root itself."""
    per_generation = price_per_image * (1 + config.max_retries)
    return round(per_generation * (1 + config.max_lineage_descendants), 6)


def open_lineage_locked(conn: sqlite3.Connection, config: Config, *, root_task_id: str,
                        style_id: str, output_type: str,
                        price_per_image: float) -> None:
    """Create the ledger row for a new root, inside the caller's transaction."""
    now = utcnow()
    conn.execute(
        "INSERT INTO generation_lineages (root_task_id, style_id, output_type,"
        " descendant_count, max_descendants, lineage_spent_usd, lineage_max_usd,"
        " created_at, updated_at) VALUES (?,?,?,0,?,0,?,?,?)"
        " ON CONFLICT(root_task_id) DO NOTHING",
        (root_task_id, style_id, output_type, config.max_lineage_descendants,
         lineage_max_usd(config, price_per_image), now, now),
    )


def get_lineage(db: Database, root_task_id: str) -> dict | None:
    row = db.conn().execute(
        "SELECT * FROM generation_lineages WHERE root_task_id = ?", (root_task_id,)
    ).fetchone()
    return dict(row) if row else None


def lineage_status(db: Database, root_task_id: str) -> dict:
    """Ledger row plus live spend, for operators and error messages."""
    lineage = get_lineage(db, root_task_id)
    if lineage is None:
        return {"root_task_id": root_task_id, "tracked": False}
    spent = db.conn().execute(
        "SELECT COALESCE(SUM(CASE WHEN state = 'settled'"
        "   THEN COALESCE(actual_usd, estimated_usd)"
        "   WHEN state = 'reserved' THEN estimated_usd ELSE 0 END), 0) AS spend,"
        " COUNT(*) AS calls FROM spend_reservations WHERE root_task_id = ?",
        (root_task_id,),
    ).fetchone()
    return {
        "root_task_id": root_task_id,
        "tracked": True,
        "descendant_count": lineage["descendant_count"],
        "max_descendants": lineage["max_descendants"],
        "lineage_spent_usd": round(float(spent["spend"]), 4),
        "lineage_max_usd": round(float(lineage["lineage_max_usd"]), 4),
        "provider_calls": int(spent["calls"]),
        "exhausted": (
            lineage["descendant_count"] >= lineage["max_descendants"]
            or float(spent["spend"]) >= float(lineage["lineage_max_usd"]) - EPSILON_USD
        ),
    }


def claim_lineage_descendant(db: Database, *, root_task_id: str,
                             estimated_usd: float, kind: str) -> dict:
    """Claim one automatic descendant slot from the shared per-root budget.

    This is the fix for the alternating-counter defect: auto-regeneration and
    automatic correction both call this, so they draw down ONE count and ONE
    dollar allowance instead of two independent counters that reset each other.

    The claim is atomic — the UPDATE carries the limit in its WHERE clause, so two
    concurrent workers cannot both take the last slot.
    """
    conn = db.conn()
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM generation_lineages WHERE root_task_id = ?", (root_task_id,)
        ).fetchone()
        if row is None:
            raise LineageBudgetExceeded(
                f"lineage_untracked: no generation lineage for root {root_task_id}; "
                "refusing automatic work that cannot be bounded",
                {"root_task_id": root_task_id, "tracked": False},
            )
        spent = float(conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN state = 'settled'"
            "   THEN COALESCE(actual_usd, estimated_usd)"
            "   WHEN state = 'reserved' THEN estimated_usd ELSE 0 END), 0) AS spend"
            " FROM spend_reservations WHERE root_task_id = ?",
            (root_task_id,),
        ).fetchone()["spend"])
        info = {
            "root_task_id": root_task_id,
            "tracked": True,
            "descendant_count": row["descendant_count"],
            "max_descendants": row["max_descendants"],
            "lineage_spent_usd": round(spent, 4),
            "lineage_max_usd": round(float(row["lineage_max_usd"]), 4),
            "kind": kind,
        }
        if row["descendant_count"] >= row["max_descendants"]:
            raise LineageBudgetExceeded(
                f"lineage_budget_exhausted: root {root_task_id} has already produced "
                f"{row['descendant_count']}/{row['max_descendants']} automatic "
                f"descendant(s); refusing another {kind}",
                info,
            )
        if _exceeds(spent + estimated_usd, float(row["lineage_max_usd"])):
            raise LineageBudgetExceeded(
                f"lineage_budget_exhausted: root {root_task_id} has spent "
                f"${spent:.4f} of ${float(row['lineage_max_usd']):.4f}; a further "
                f"~${estimated_usd:.4f} for this {kind} would exceed it",
                info,
            )
        cur = conn.execute(
            "UPDATE generation_lineages SET descendant_count = descendant_count + 1,"
            " lineage_spent_usd = ?, updated_at = ?"
            " WHERE root_task_id = ? AND descendant_count = ?",
            (spent, utcnow(), root_task_id, row["descendant_count"]),
        )
        if cur.rowcount != 1:  # another worker took the slot first
            raise LineageBudgetExceeded(
                f"lineage_budget_race: another worker claimed a descendant slot for "
                f"root {root_task_id}; refusing to exceed the shared budget",
                info,
            )
        info["descendant_count"] = row["descendant_count"] + 1
    return info


def sync_lineage_spend(db: Database, root_task_id: str) -> None:
    """Refresh the denormalized lineage_spent_usd column after settlement.

    The authoritative figure is always recomputed from spend_reservations; this
    column exists so operators can read a lineage's spend without a join.
    """
    conn = db.conn()
    with transaction(conn):
        conn.execute(
            "UPDATE generation_lineages SET lineage_spent_usd = ("
            "  SELECT COALESCE(SUM(CASE WHEN state = 'settled'"
            "    THEN COALESCE(actual_usd, estimated_usd)"
            "    WHEN state = 'reserved' THEN estimated_usd ELSE 0 END), 0)"
            "  FROM spend_reservations WHERE root_task_id = ?"
            "), updated_at = ? WHERE root_task_id = ?",
            (root_task_id, utcnow(), root_task_id),
        )
