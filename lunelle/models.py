"""Domain constants: task statuses, output types, and the task state machine."""

from __future__ import annotations

import uuid

# ---- Output types ----------------------------------------------------------

OUTPUT_GRID = "grid"
OUTPUT_WEARING = "wearing"
OUTPUT_HERO = "hero"
# Reserved by migration 0002 for the upcoming matrix/repair pipelines.
OUTPUT_MATRIX_CELL = "matrix_cell"
OUTPUT_REPAIR = "repair"
OUTPUT_TYPES = (OUTPUT_GRID, OUTPUT_WEARING, OUTPUT_HERO, OUTPUT_MATRIX_CELL, OUTPUT_REPAIR)
# Types that /api/styles/{id}/generate accepts today.
GENERATABLE_OUTPUT_TYPES = (OUTPUT_GRID, OUTPUT_WEARING, OUTPUT_HERO)

# ---- Task statuses ---------------------------------------------------------

PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
RETRYING = "retrying"
CANCELLED = "cancelled"

STATUSES = (PENDING, RUNNING, SUCCESS, FAILED, RETRYING, CANCELLED)

# Statuses in which a task may still consume the worker.
ACTIVE_STATUSES = (PENDING, RUNNING, RETRYING)
# Statuses the worker's claim query picks up (given next_attempt_at is due).
CLAIMABLE_STATUSES = (PENDING, RETRYING)
# Terminal statuses that a manual retry may re-queue.
MANUAL_RETRY_STATUSES = (FAILED, CANCELLED)

# Explicit transition table — the only legal state changes.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING: frozenset({RUNNING, CANCELLED}),
    RETRYING: frozenset({RUNNING, CANCELLED}),
    RUNNING: frozenset({SUCCESS, FAILED, RETRYING}),
    FAILED: frozenset({PENDING}),      # manual re-queue
    CANCELLED: frozenset({PENDING}),   # manual re-queue
    SUCCESS: frozenset(),              # regeneration = new task, never reuse
}


class IllegalTransition(Exception):
    def __init__(self, current: str, new: str):
        super().__init__(f"Illegal task status transition {current!r} -> {new!r}")
        self.current = current
        self.new = new


def check_transition(current: str, new: str) -> None:
    if new not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise IllegalTransition(current, new)


# ---- QA pipeline state (orthogonal to task status) -------------------------
#
# A task reaching `success` only means the image exists. QA runs after, so
# `qa_state` makes that window explicit and observable instead of leaving
# callers to race it: complete_success writes status=success AND
# qa_state=pending in one transaction, and the QA result lands with
# qa_state=done in another.

QA_PENDING = "pending"
QA_RUNNING = "running"
QA_DONE = "done"
QA_ERROR = "error"
QA_SKIPPED = "skipped"

QA_STATES = (QA_PENDING, QA_RUNNING, QA_DONE, QA_ERROR, QA_SKIPPED)
#: States in which no QA verdict exists yet, so review must not be accepted.
QA_NOT_READY = (QA_PENDING, QA_RUNNING)

# ---- Human review lifecycle ------------------------------------------------

REVIEW_GENERATED = "generated"
REVIEW_WAITING = "waiting_human_review"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_PUBLISH_READY = "publish_ready"
REVIEW_PUBLISHED = "published"

REVIEW_STATES = (
    REVIEW_GENERATED, REVIEW_WAITING, REVIEW_APPROVED,
    REVIEW_REJECTED, REVIEW_PUBLISH_READY, REVIEW_PUBLISHED,
)

#: Review states the export/publish gate accepts. `publish_ready` is derived by
#: the system (approved AND the machine gate agrees), never set by a human, so a
#: human cannot bypass the gate by declaring something ready.
PUBLISHABLE_REVIEW_STATES = frozenset(
    {REVIEW_APPROVED, REVIEW_PUBLISH_READY, REVIEW_PUBLISHED}
)

#: Legal review transitions. Re-review of published/rejected assets is allowed
#: (an asset can be pulled back), but nothing may jump straight to approved
#: without QA having landed first (enforced in TaskService.record_review).
ALLOWED_REVIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    REVIEW_GENERATED: frozenset({REVIEW_WAITING}),
    REVIEW_WAITING: frozenset({REVIEW_APPROVED, REVIEW_REJECTED}),
    REVIEW_APPROVED: frozenset({REVIEW_PUBLISH_READY, REVIEW_REJECTED, REVIEW_WAITING}),
    REVIEW_REJECTED: frozenset({REVIEW_APPROVED, REVIEW_WAITING}),
    REVIEW_PUBLISH_READY: frozenset({REVIEW_PUBLISHED, REVIEW_REJECTED, REVIEW_WAITING}),
    REVIEW_PUBLISHED: frozenset({REVIEW_PUBLISH_READY, REVIEW_REJECTED, REVIEW_WAITING}),
}


class IllegalReviewTransition(Exception):
    def __init__(self, current: str, new: str):
        super().__init__(f"Illegal review transition {current!r} -> {new!r}")
        self.current = current
        self.new = new


def check_review_transition(current: str, new: str) -> None:
    if new not in ALLOWED_REVIEW_TRANSITIONS.get(current, frozenset()):
        raise IllegalReviewTransition(current, new)


# ---- Error codes -----------------------------------------------------------

# Retryable: transient conditions where the same request may later succeed.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "timeout",
        "network",
        "dns",
        "rate_limited",
        "server_error",
        "download_failed",
        "interrupted",
    }
)
# Non-retryable: retrying would waste money or cannot help.
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "auth_invalid",
        "bad_request",
        "content_policy",
        "invalid_response",
        "file_write_failed",
        "disk_full",
        "config_error",
        "unsupported",
        # An outbound URL was refused by the SSRF guard (see lunelle/urlguard.py).
        # It will point at the same internal address next time, so retrying only
        # repeats a blocked request.
        "unsafe_url",
        # A required input asset is absent (e.g. no hand model for a matrix
        # cell's tone+view). Retrying cannot help until an operator uploads it,
        # so this fails fast BEFORE spending money on a degraded render.
        "dependency_missing",
        # The rolling spend cap would be exceeded. Not auto-retried: retrying
        # would just trip the breaker again. Recovers via manual retry once the
        # window rolls or the cap is raised.
        "budget_exceeded",
    }
)

#: Error code used when a task cannot run because a required input is missing.
ERROR_DEPENDENCY_MISSING = "dependency_missing"
#: Error code used when the budget circuit breaker refuses a provider call.
ERROR_BUDGET_EXCEEDED = "budget_exceeded"


def is_retryable(error_code: str | None) -> bool:
    if error_code is None:
        return False
    return error_code in RETRYABLE_ERROR_CODES


# ---- ID helpers -------------------------------------------------------------


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def new_style_id() -> str:
    return new_id("st")


def new_task_id() -> str:
    return new_id("tk")


def new_batch_id() -> str:
    return new_id("bt")
