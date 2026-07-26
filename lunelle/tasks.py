"""Task service: styles, batches, generation tasks, state machine, idempotency.

All writes go through this module so status transitions and idempotency rules
cannot be bypassed. Raw SQL stays here and in stats.py only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Database, transaction, utcnow
from .models import (
    CANCELLED,
    CLAIMABLE_STATUSES,
    FAILED,
    MANUAL_RETRY_STATUSES,
    OUTPUT_GRID,
    OUTPUT_TYPES,
    OUTPUT_WEARING,
    PENDING,
    RETRYING,
    RUNNING,
    SUCCESS,
    IllegalTransition,
    check_transition,
    new_batch_id,
    new_style_id,
    new_task_id,
)
from .prompts import PromptBundle, build_prompt_bundle, prompt_for_output_type
from .schemas import StyleSpec

logger = logging.getLogger(__name__)


from .errors import ConflictError, NotFoundError  # noqa: E402  (re-export for callers)


@dataclass
class GenerationPlan:
    batch_id: str
    created: list[dict]
    reused: list[dict]
    skipped: list[dict]


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class TaskService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ---------------- styles ----------------

    def create_style(
        self,
        spec: StyleSpec,
        *,
        source_type: str,
        source_input: dict,
        parser: str,
        warnings: list[str],
    ) -> dict:
        style_id = new_style_id()
        now = utcnow()
        conn = self.db.conn()
        payload = spec.model_dump()
        payload["_meta"] = {"parser": parser, "warnings": warnings}
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO styles (style_id, sku, name, spec_json, source_type,"
                    " source_input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        style_id,
                        spec.sku,
                        spec.name,
                        json.dumps(payload, ensure_ascii=False),
                        source_type,
                        json.dumps(source_input, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"sku {spec.sku!r} already exists") from exc
        logger.info(
            "style created",
            extra={"ctx": {"style_id": style_id, "sku": spec.sku, "stage": "style_create"}},
        )
        return self.get_style(style_id)

    def get_style(self, style_id: str) -> dict:
        row = self.db.conn().execute(
            "SELECT * FROM styles WHERE style_id = ?", (style_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"style {style_id} not found")
        return self._style_row(row)

    def get_style_by_sku(self, sku: str) -> dict:
        row = self.db.conn().execute("SELECT * FROM styles WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise NotFoundError(f"style with sku {sku} not found")
        return self._style_row(row)

    def _style_row(self, row: sqlite3.Row) -> dict:
        doc = dict(row)
        doc["spec"] = json.loads(doc.pop("spec_json"))
        doc["source_input"] = json.loads(doc.pop("source_input_json"))
        return doc

    def list_styles(self, limit: int = 200, offset: int = 0) -> list[dict]:
        rows = self.db.conn().execute(
            "SELECT * FROM styles ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [self._style_row(r) for r in rows]

    def taken_skus(self) -> set[str]:
        return {r["sku"] for r in self.db.conn().execute("SELECT sku FROM styles")}

    def set_reference_image(self, style_id: str, path: Path) -> None:
        conn = self.db.conn()
        with transaction(conn):
            cur = conn.execute(
                "UPDATE styles SET reference_image_path = ?, updated_at = ? WHERE style_id = ?",
                (str(path), utcnow(), style_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"style {style_id} not found")

    def spec_for(self, style: dict) -> StyleSpec:
        data = {k: v for k, v in style["spec"].items() if not k.startswith("_")}
        return StyleSpec(**data)

    # ---------------- generation planning ----------------

    def prompt_bundle_for(self, style: dict) -> PromptBundle:
        spec = self.spec_for(style)
        use_refs = self.config.reference_mode != "off"
        has_style_ref = bool(style.get("reference_image_path"))
        return build_prompt_bundle(
            spec,
            self.config.grid_size,
            self.config.wearing_size,
            with_reference=use_refs,
            with_grid_reference=use_refs and has_style_ref,
        )

    @staticmethod
    def idempotency_key(style_id: str, output_type: str, prompt_version: str, prompt: str, nonce: str) -> str:
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        raw = f"{style_id}|{output_type}|{prompt_version}|{prompt_hash}|{nonce}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def create_generation(
        self, style_id: str, output_types: list[str], *, force: bool = False, note: str = ""
    ) -> GenerationPlan:
        style = self.get_style(style_id)
        bundle = self.prompt_bundle_for(style)
        for output_type in output_types:
            if output_type not in OUTPUT_TYPES:
                raise ValueError(f"invalid output type {output_type!r}")

        conn = self.db.conn()
        batch_id = new_batch_id()
        now = utcnow()
        created: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []

        with transaction(conn):
            conn.execute(
                "INSERT INTO batches (batch_id, note, created_at) VALUES (?,?,?)",
                (batch_id, note or None, now),
            )
            grid_task_id_this_round: str | None = None

            for output_type in sorted(output_types, key=lambda t: 0 if t == OUTPUT_GRID else 1):
                prompt = prompt_for_output_type(bundle, output_type)
                nonce = batch_id if force else ""
                key = self.idempotency_key(
                    style_id, output_type, bundle.prompt_version, prompt, nonce
                )
                existing = conn.execute(
                    "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing is not None:
                    status = existing["status"]
                    if status in (PENDING, RUNNING, RETRYING):
                        reused.append({"task_id": existing["task_id"], "status": status,
                                       "output_type": output_type, "reason": "already in progress"})
                        if output_type == OUTPUT_GRID:
                            grid_task_id_this_round = existing["task_id"]
                        continue
                    if status == SUCCESS:
                        skipped.append({"task_id": existing["task_id"], "status": status,
                                        "output_type": output_type,
                                        "reason": "already succeeded; use force=true to regenerate"})
                        continue
                    # failed / cancelled -> safe re-queue of the same task, fresh budget
                    self._transition_locked(conn, existing["task_id"], status, PENDING,
                                            error_code=None, error_message=None,
                                            next_attempt_at=None, retry_count=0)
                    reused.append({"task_id": existing["task_id"], "status": PENDING,
                                   "output_type": output_type, "reason": "re-queued failed task"})
                    if output_type == OUTPUT_GRID:
                        grid_task_id_this_round = existing["task_id"]
                    continue

                task_id = new_task_id()
                wait_for = None
                metadata: dict = {"note": note} if note else {}
                if output_type == OUTPUT_WEARING and self.config.reference_mode != "off":
                    metadata["use_reference"] = True
                    if grid_task_id_this_round:
                        wait_for = grid_task_id_this_round
                if (
                    output_type == OUTPUT_GRID
                    and self.config.reference_mode != "off"
                    and style.get("reference_image_path")
                ):
                    metadata["use_style_reference"] = True
                    metadata["style_reference_path"] = style["reference_image_path"]
                conn.execute(
                    "INSERT INTO tasks (task_id, batch_id, style_id, sku, output_type, prompt,"
                    " negative_prompt, prompt_version, provider, model, status, retry_count,"
                    " max_retries, estimated_cost_usd, idempotency_key, wait_for_task_id,"
                    " created_at, updated_at, metadata_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        batch_id,
                        style_id,
                        style["sku"],
                        output_type,
                        prompt,
                        bundle.negative_prompt,
                        bundle.prompt_version,
                        self.config.image_provider,
                        self.config.image_model,
                        PENDING,
                        0,
                        self.config.max_retries,
                        self.config.price_for(self.config.image_model),
                        key,
                        wait_for,
                        now,
                        now,
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
                created.append({"task_id": task_id, "status": PENDING, "output_type": output_type})
                if output_type == OUTPUT_GRID:
                    grid_task_id_this_round = task_id

        logger.info(
            "generation planned",
            extra={"ctx": {"batch_id": batch_id, "style_id": style_id, "sku": style["sku"],
                            "stage": "plan", "status": f"created={len(created)} reused={len(reused)}"}},
        )
        return GenerationPlan(batch_id=batch_id, created=created, reused=reused, skipped=skipped)

    # ---------------- task queries ----------------

    def get_task(self, task_id: str, *, with_details: bool = True) -> dict:
        conn = self.db.conn()
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"task {task_id} not found")
        doc = dict(row)
        doc["metadata"] = json.loads(doc.pop("metadata_json") or "{}")
        if with_details:
            doc["attempts"] = [dict(r) for r in conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
            )]
            qa = conn.execute(
                "SELECT * FROM qa_results WHERE task_id = ? ORDER BY qa_id DESC LIMIT 1", (task_id,)
            ).fetchone()
            if qa is not None:
                qa_doc = dict(qa)
                qa_doc["issues"] = json.loads(qa_doc.pop("issues_json"))
                qa_doc["checks"] = json.loads(qa_doc.pop("checks_json"))
                doc["qa"] = qa_doc
            else:
                doc["qa"] = None
        return doc

    def list_tasks(
        self,
        *,
        sku: str | None = None,
        status: str | None = None,
        output_type: str | None = None,
        batch_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        clauses, params = [], []
        if sku:
            clauses.append("sku = ?")
            params.append(sku)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if output_type:
            clauses.append("output_type = ?")
            params.append(output_type)
        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.conn().execute(
            f"SELECT task_id, batch_id, style_id, sku, output_type, prompt_version, provider,"  # noqa: S608
            f" model, status, retry_count, max_retries, estimated_cost_usd, actual_cost_usd,"
            f" created_at, started_at, completed_at, updated_at, output_path, error_code"
            f" FROM tasks {where} ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?",  # noqa: S608
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_batches(self, limit: int = 50) -> list[dict]:
        rows = self.db.conn().execute(
            "SELECT b.batch_id, b.note, b.created_at,"
            " COUNT(t.task_id) AS task_count,"
            " SUM(CASE WHEN t.status = 'success' THEN 1 ELSE 0 END) AS success_count,"
            " SUM(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed_count"
            " FROM batches b LEFT JOIN tasks t ON t.batch_id = b.batch_id"
            " GROUP BY b.batch_id ORDER BY b.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- state machine ----------------

    def _transition_locked(
        self, conn: sqlite3.Connection, task_id: str, current: str, new: str, **fields
    ) -> None:
        """Perform a checked transition inside an existing transaction."""
        check_transition(current, new)
        sets = ["status = ?", "updated_at = ?"]
        params: list = [new, utcnow()]
        for column, value in fields.items():
            sets.append(f"{column} = ?")
            params.append(value)
        params.extend([task_id, current])
        cur = conn.execute(  # noqa: S608 - column names are internal constants, values bound
            f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ? AND status = ?",  # noqa: S608
            params,
        )
        if cur.rowcount != 1:
            raise IllegalTransition(current, new)

    def transition(self, task_id: str, new: str, **fields) -> dict:
        conn = self.db.conn()
        with transaction(conn):
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task {task_id} not found")
            self._transition_locked(conn, task_id, row["status"], new, **fields)
        return self.get_task(task_id, with_details=False)

    # ---------------- worker operations ----------------

    def claim_next(self) -> dict | None:
        """Atomically claim one due task (pending/retrying, dependencies settled)."""
        conn = self.db.conn()
        now = utcnow()
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM tasks"
                " WHERE status IN (?, ?)"
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                " AND (wait_for_task_id IS NULL OR wait_for_task_id NOT IN"
                "      (SELECT task_id FROM tasks WHERE status IN ('pending','running','retrying')))"
                " ORDER BY created_at LIMIT 1",
                (*CLAIMABLE_STATUSES, now),
            ).fetchone()
            if row is None:
                return None
            self._transition_locked(
                conn, row["task_id"], row["status"], RUNNING,
                started_at=row["started_at"] or now, next_attempt_at=None,
            )
        return self.get_task(row["task_id"], with_details=False)

    def start_attempt(self, task_id: str, *, provider: str, model: str, fingerprint: str) -> int:
        conn = self.db.conn()
        with transaction(conn):
            row = conn.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM attempts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt_no = int(row["n"]) + 1
            conn.execute(
                "INSERT INTO attempts (task_id, attempt_no, provider, model, request_fingerprint,"
                " started_at, outcome) VALUES (?,?,?,?,?,?, 'started')",
                (task_id, attempt_no, provider, model, fingerprint, utcnow()),
            )
        return attempt_no

    def finish_attempt(
        self,
        task_id: str,
        attempt_no: int,
        *,
        outcome: str,
        duration_ms: int,
        external_request_id: str | None = None,
        http_status: int | None = None,
        reference_used: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        conn = self.db.conn()
        with transaction(conn):
            conn.execute(
                "UPDATE attempts SET outcome = ?, finished_at = ?, duration_ms = ?,"
                " external_request_id = ?, http_status = ?, reference_used = ?,"
                " error_code = ?, error_message = ?, cost_usd = ?"
                " WHERE task_id = ? AND attempt_no = ?",
                (
                    outcome, utcnow(), duration_ms, external_request_id, http_status,
                    1 if reference_used else 0, error_code,
                    (error_message or "")[:2000] or None, cost_usd, task_id, attempt_no,
                ),
            )

    def complete_success(
        self,
        task_id: str,
        *,
        output_path: Path,
        external_request_id: str | None,
        actual_cost_usd: float | None,
        extra_metadata: dict | None = None,
    ) -> dict:
        task = self.get_task(task_id, with_details=False)
        metadata = task["metadata"]
        if extra_metadata:
            metadata.update(extra_metadata)
        return self.transition(
            task_id,
            SUCCESS,
            output_path=str(output_path),
            external_request_id=external_request_id,
            actual_cost_usd=actual_cost_usd,
            completed_at=utcnow(),
            error_code=None,
            error_message=None,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )

    def complete_failure(
        self, task_id: str, *, error_code: str, error_message: str, retryable: bool
    ) -> dict:
        task = self.get_task(task_id, with_details=False)
        retry_count = task["retry_count"]
        max_retries = task["max_retries"]
        safe_message = (error_message or "")[:2000]
        if retryable and retry_count < max_retries:
            delay_s = self.config.retry_backoff_base_s * (2 ** retry_count)
            next_at = utcnow_plus(delay_s)
            doc = self.transition(
                task_id,
                RETRYING,
                retry_count=retry_count + 1,
                next_attempt_at=next_at,
                error_code=error_code,
                error_message=safe_message,
            )
            logger.warning(
                "task scheduled for retry",
                extra={"ctx": {"task_id": task_id, "error_code": error_code,
                                "attempt": retry_count + 1, "stage": "retry_scheduled"}},
            )
            return doc
        return self.transition(
            task_id,
            FAILED,
            completed_at=utcnow(),
            error_code=error_code,
            error_message=safe_message,
        )

    # ---------------- manual operations ----------------

    def manual_retry(self, task_id: str, note: str = "") -> dict:
        task = self.get_task(task_id, with_details=False)
        if task["status"] not in MANUAL_RETRY_STATUSES:
            raise ConflictError(
                f"task is {task['status']}; only failed or cancelled tasks can be retried. "
                "To regenerate a successful task, create a new generation with force=true."
            )
        metadata = task["metadata"]
        metadata.setdefault("manual_retries", []).append({"at": utcnow(), "note": note})
        return self.transition(
            task_id,
            PENDING,
            error_code=None,
            error_message=None,
            next_attempt_at=None,
            retry_count=0,  # a manual retry grants a fresh automatic-retry budget
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )

    def cancel(self, task_id: str) -> dict:
        task = self.get_task(task_id, with_details=False)
        if task["status"] not in (PENDING, RETRYING):
            raise ConflictError(f"cannot cancel a task in status {task['status']}")
        return self.transition(task_id, CANCELLED, completed_at=utcnow())

    # ---------------- recovery ----------------

    def recover_interrupted(self) -> int:
        """Mark tasks stuck in `running` after a crash as interrupted (retryable)."""
        conn = self.db.conn()
        rows = conn.execute("SELECT task_id FROM tasks WHERE status = ?", (RUNNING,)).fetchall()
        count = 0
        for row in rows:
            try:
                self.complete_failure(
                    row["task_id"],
                    error_code="interrupted",
                    error_message="service restarted while task was running",
                    retryable=True,
                )
                count += 1
            except IllegalTransition:  # pragma: no cover - raced by live worker
                continue
        if count:
            logger.warning("recovered %d interrupted task(s)", count)
        return count

    # ---------------- output helpers ----------------

    def latest_successful_grid(self, style_id: str) -> dict | None:
        row = self.db.conn().execute(
            "SELECT * FROM tasks WHERE style_id = ? AND output_type = ? AND status = ?"
            " ORDER BY completed_at DESC LIMIT 1",
            (style_id, OUTPUT_GRID, SUCCESS),
        ).fetchone()
        return _row_to_dict(row)

    def output_file_for(self, task: dict, attempt_no: int) -> Path:
        sku_dir = self.config.output_dir / task["sku"]
        return sku_dir / f"{task['sku']}-{task['output_type']}-{task['task_id']}-a{attempt_no}.png"


def utcnow_plus(seconds: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
