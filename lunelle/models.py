"""Domain constants: task statuses, output types, and the task state machine."""

from __future__ import annotations

import uuid

# ---- Output types ----------------------------------------------------------

OUTPUT_GRID = "grid"
OUTPUT_WEARING = "wearing"
OUTPUT_TYPES = (OUTPUT_GRID, OUTPUT_WEARING)

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
    }
)


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
