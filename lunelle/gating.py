"""The one and only publish/export gate: default-deny.

Both the export path and the publish path go through here so they can never
drift apart. The gate is expressed twice — as a SQL fragment and as a Python
predicate over a task row — and a unit test asserts the two agree on every
combination, because that is exactly the kind of duplication that silently
diverges.

Default-deny means: anything missing counts as "not allowed". An absent QA row
yields NULL in a LEFT JOIN, and the previous code treated NULL as falsy, i.e.
"does not need review" — which let never-reviewed assets pass a reviewed-only
filter. COALESCE fixes the polarity here, once, for both callers.
"""

from __future__ import annotations

from .models import (
    PUBLISHABLE_REVIEW_STATES,
    QA_DONE,
    REVIEW_REJECTED,
    SUCCESS,
)

#: Columns a caller must select for `row_is_publishable` to work. Kept next to
#: the SQL so a caller adding a new condition cannot forget the projection.
REQUIRED_COLUMNS = (
    "status", "qa_state", "review_state", "qa_id", "qa_passed", "qa_needs_review",
)

#: SQL fragment over aliases t (tasks) and q (latest qa_results row for t).
#: Callers supply the LEFT JOIN; see `LATEST_QA_JOIN`.
GATE_SQL = (
    " t.status = 'success'"
    " AND t.qa_state = 'done'"
    " AND q.qa_id IS NOT NULL"
    " AND COALESCE(q.passed, 0) = 1"
    " AND COALESCE(q.needs_human_review, 1) = 0"
    " AND t.review_state IN ('approved', 'publish_ready', 'published')"
)

#: The latest *heuristic* QA row per task. LLM verdicts are advisory metadata and
#: must never satisfy the gate, so they are excluded here rather than at the
#: call site.
LATEST_QA_JOIN = (
    " LEFT JOIN qa_results q ON q.qa_id = ("
    "   SELECT qa_id FROM qa_results"
    "   WHERE task_id = t.task_id AND source = 'heuristic'"
    "   ORDER BY qa_id DESC LIMIT 1)"
)

#: Projection matching REQUIRED_COLUMNS, for callers that want the gate columns.
GATE_COLUMNS_SQL = (
    " q.qa_id AS qa_id, q.passed AS qa_passed,"
    " q.needs_human_review AS qa_needs_review"
)


def row_is_publishable(row: dict) -> bool:
    """Python mirror of GATE_SQL. Missing keys count as not-allowed."""
    if row.get("status") != SUCCESS:
        return False
    if row.get("qa_state") != QA_DONE:
        return False
    if row.get("qa_id") is None:
        return False
    if int(row.get("qa_passed") or 0) != 1:
        return False
    needs_review = row.get("qa_needs_review")
    if int(1 if needs_review is None else needs_review) != 0:
        return False
    return row.get("review_state") in PUBLISHABLE_REVIEW_STATES


def block_reason(row: dict) -> str | None:
    """Why the gate rejected this row, for operator-facing reports."""
    if row.get("status") != SUCCESS:
        return f"task is not successful (status={row.get('status')})"
    qa_state = row.get("qa_state")
    if qa_state != QA_DONE:
        if qa_state in ("pending", "running"):
            return "QA has not finished yet"
        if qa_state == "error":
            return "QA failed to run; no verdict exists"
        return f"QA state is {qa_state}"
    if row.get("qa_id") is None:
        return "no QA result recorded"
    if int(row.get("qa_passed") or 0) != 1:
        return "automatic QA did not pass"
    # Report the specific verdict before the generic flag: a rejected asset also
    # has needs_human_review set, and "awaiting review" would misdescribe it.
    state = row.get("review_state")
    if state == REVIEW_REJECTED:
        return "rejected by human review"
    needs_review = row.get("qa_needs_review")
    if int(1 if needs_review is None else needs_review) != 0:
        return "awaiting human review"
    if state not in PUBLISHABLE_REVIEW_STATES:
        return f"not approved by a human (review_state={state})"
    return None
