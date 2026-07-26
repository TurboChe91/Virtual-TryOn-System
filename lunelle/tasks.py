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
    GENERATABLE_OUTPUT_TYPES,
    MANUAL_RETRY_STATUSES,
    OUTPUT_GRID,
    OUTPUT_HERO,
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
from .prompts import (
    MATRIX_PROMPT_VERSION,
    MATRIX_TONES,
    MATRIX_VIEWS,
    PromptBundle,
    build_correction_prompt,
    build_matrix_prompt,
    build_prompt_bundle,
    prompt_for_output_type,
    prompt_version_for,
)
from .schemas import StyleSpec

logger = logging.getLogger(__name__)


from .errors import ConflictError, NotFoundError  # noqa: E402  (re-export for callers)

CORRECTION_BUDGET = 5  # authorized versions per style+output_type (v1..v5), per SOP


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
        self._set_style_column(style_id, "reference_image_path", str(path))

    def set_plan_image(self, style_id: str, path: Path) -> None:
        self._set_style_column(style_id, "plan_image_path", str(path))

    def set_identity_text(self, style_id: str, text: str) -> None:
        self._set_style_column(style_id, "identity_text", text.strip() or None)

    def _set_style_column(self, style_id: str, column: str, value) -> None:
        assert column in ("reference_image_path", "plan_image_path", "identity_text")
        conn = self.db.conn()
        with transaction(conn):
            cur = conn.execute(  # noqa: S608 - column restricted by the assert above
                f"UPDATE styles SET {column} = ?, updated_at = ? WHERE style_id = ?",  # noqa: S608
                (value, utcnow(), style_id),
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
            identity_text=style.get("identity_text"),
            with_hero_reference=use_refs,
        )

    def _generation_channel(self) -> tuple[str, str, float, str | None]:
        """(provider, model, price, profile_name) — active DB profile wins over env."""
        from .profiles import ProfileService

        row = ProfileService(self.db).active_row()
        if row is None:
            model = self.config.image_model
            return self.config.image_provider, model, self.config.price_for(model), None
        price = row["price_per_image_usd"]
        if price is None:
            price = self.config.price_for(row["model"])
        return "openai-compat", row["model"], float(price), row["name"]

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
            if output_type not in GENERATABLE_OUTPUT_TYPES:
                raise ValueError(f"invalid output type {output_type!r}")
        provider_name, model, price, profile_name = self._generation_channel()

        conn = self.db.conn()
        batch_id = new_batch_id()
        now = utcnow()
        created: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []

        order = {OUTPUT_GRID: 0, OUTPUT_HERO: 1, OUTPUT_WEARING: 2}
        with transaction(conn):
            conn.execute(
                "INSERT INTO batches (batch_id, note, created_at) VALUES (?,?,?)",
                (batch_id, note or None, now),
            )
            grid_task_id_this_round: str | None = None

            for output_type in sorted(output_types, key=lambda t: order.get(t, 3)):
                prompt = prompt_for_output_type(bundle, output_type)
                version = prompt_version_for(output_type)
                nonce = batch_id if force else ""
                key = self.idempotency_key(
                    style_id, output_type, version, prompt, nonce
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
                if profile_name:
                    metadata["api_profile"] = profile_name
                if output_type == OUTPUT_WEARING and self.config.reference_mode != "off":
                    metadata["use_reference"] = True
                    if grid_task_id_this_round:
                        wait_for = grid_task_id_this_round
                if output_type == OUTPUT_HERO and self.config.reference_mode != "off":
                    metadata["use_hero_reference"] = True
                    # The plan upload is Image 1 when present; otherwise the hero
                    # rides on the latest grid, so gate it on a grid queued now.
                    if not style.get("plan_image_path") and grid_task_id_this_round:
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
                        version,
                        provider_name,
                        model,
                        PENDING,
                        0,
                        self.config.max_retries,
                        price,
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

    def create_matrix_generation(
        self, style_id: str, *, tones: list[str] | None = None,
        views: list[str] | None = None, force: bool = False, note: str = ""
    ) -> GenerationPlan:
        """Queue the try-on matrix for one style: every tone x view cell."""
        from .models import OUTPUT_MATRIX_CELL

        style = self.get_style(style_id)
        spec = self.spec_for(style)
        tones = [t for t in (tones or MATRIX_TONES) if t in MATRIX_TONES]
        views = [v for v in (views or MATRIX_VIEWS) if v in MATRIX_VIEWS]
        if not tones or not views:
            raise ValueError(f"tones must be within {MATRIX_TONES} and views within {MATRIX_VIEWS}")
        provider_name, model, price, profile_name = self._generation_channel()
        use_refs = self.config.reference_mode != "off"

        conn = self.db.conn()
        batch_id = new_batch_id()
        now = utcnow()
        created: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        with transaction(conn):
            conn.execute(
                "INSERT INTO batches (batch_id, note, created_at) VALUES (?,?,?)",
                (batch_id, note or f"try-on matrix {len(tones)}x{len(views)}", now),
            )
            for tone in tones:
                for view in views:
                    prompt = build_matrix_prompt(
                        spec, style.get("identity_text"), tone, view, with_reference=use_refs
                    )
                    nonce = batch_id if force else ""
                    key = self.idempotency_key(
                        style_id, OUTPUT_MATRIX_CELL, MATRIX_PROMPT_VERSION, prompt, nonce
                    )
                    existing = conn.execute(
                        "SELECT * FROM tasks WHERE idempotency_key = ?", (key,)
                    ).fetchone()
                    cell = {"tone": tone, "view": view}
                    if existing is not None:
                        status = existing["status"]
                        if status in (PENDING, RUNNING, RETRYING):
                            reused.append({"task_id": existing["task_id"], "status": status,
                                           "output_type": OUTPUT_MATRIX_CELL, **cell,
                                           "reason": "already in progress"})
                            continue
                        if status == SUCCESS:
                            skipped.append({"task_id": existing["task_id"], "status": status,
                                            "output_type": OUTPUT_MATRIX_CELL, **cell,
                                            "reason": "already succeeded"})
                            continue
                        self._transition_locked(conn, existing["task_id"], status, PENDING,
                                                error_code=None, error_message=None,
                                                next_attempt_at=None, retry_count=0)
                        reused.append({"task_id": existing["task_id"], "status": PENDING,
                                       "output_type": OUTPUT_MATRIX_CELL, **cell,
                                       "reason": "re-queued failed cell"})
                        continue
                    task_id = new_task_id()
                    metadata: dict = {"tone": tone, "view": view}
                    if note:
                        metadata["note"] = note
                    if profile_name:
                        metadata["api_profile"] = profile_name
                    if use_refs:
                        metadata["use_matrix_reference"] = True
                    conn.execute(
                        "INSERT INTO tasks (task_id, batch_id, style_id, sku, output_type, prompt,"
                        " negative_prompt, prompt_version, provider, model, status, retry_count,"
                        " max_retries, estimated_cost_usd, idempotency_key, wait_for_task_id,"
                        " created_at, updated_at, metadata_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            task_id, batch_id, style_id, style["sku"], OUTPUT_MATRIX_CELL,
                            prompt, "", MATRIX_PROMPT_VERSION, provider_name, model,
                            PENDING, 0, self.config.max_retries, price, key, None,
                            now, now, json.dumps(metadata, ensure_ascii=False),
                        ),
                    )
                    created.append({"task_id": task_id, "status": PENDING,
                                    "output_type": OUTPUT_MATRIX_CELL, **cell})
        logger.info(
            "matrix planned",
            extra={"ctx": {"batch_id": batch_id, "style_id": style_id, "stage": "plan",
                            "status": f"created={len(created)} skipped={len(skipped)}"}},
        )
        return GenerationPlan(batch_id=batch_id, created=created, reused=reused, skipped=skipped)

    def create_correction(
        self,
        task_id: str,
        *,
        correction_text: str,
        detail_references: list[Path] | None = None,
        owner_override: str = "",
    ) -> dict:
        """v2+ per SOP: a locked-base local edit of a successful candidate."""
        source = self.get_task(task_id, with_details=False)
        if source["status"] != SUCCESS or not source.get("output_path"):
            raise ConflictError("corrections start from a successful candidate with an image")
        if not correction_text.strip():
            raise ValueError("correction text is required — it is the only allowed change")
        details = [path for path in (detail_references or []) if path.is_file()]

        version = int(source["metadata"].get("version", 1)) + 1
        chain = self.db.conn().execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE style_id = ? AND output_type = ?",
            (source["style_id"], source["output_type"]),
        ).fetchone()["n"]
        if chain >= CORRECTION_BUDGET and not owner_override:
            raise ConflictError(
                f"attempt budget exhausted ({chain}/{CORRECTION_BUDGET} versions for this "
                f"style+type); per SOP this style is BLOCKED pending owner review. "
                f"Pass owner_override to continue."
            )

        provider_name, model, price, profile_name = self._generation_channel()
        prompt = build_correction_prompt(source["prompt"], correction_text, len(details))
        conn = self.db.conn()
        batch_id = new_batch_id()
        now = utcnow()
        new_id_ = new_task_id()
        metadata = {
            "correction_of": task_id,
            "version": version,
            "correction_text": correction_text.strip()[:2000],
            "correction_details": [str(path) for path in details],
        }
        if profile_name:
            metadata["api_profile"] = profile_name
        if owner_override:
            metadata["owner_override"] = owner_override
        for key in ("use_reference", "use_style_reference", "style_reference_path",
                    "use_hero_reference"):
            if source["metadata"].get(key):
                metadata[key] = source["metadata"][key]
        with transaction(conn):
            conn.execute(
                "INSERT INTO batches (batch_id, note, created_at) VALUES (?,?,?)",
                (batch_id, f"correction v{version} of {task_id}", now),
            )
            conn.execute(
                "INSERT INTO tasks (task_id, batch_id, style_id, sku, output_type, prompt,"
                " negative_prompt, prompt_version, provider, model, status, retry_count,"
                " max_retries, estimated_cost_usd, idempotency_key, wait_for_task_id,"
                " created_at, updated_at, metadata_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id_, batch_id, source["style_id"], source["sku"],
                    source["output_type"], prompt, source["negative_prompt"],
                    source["prompt_version"], provider_name, model, PENDING, 0,
                    self.config.max_retries, price,
                    self.idempotency_key(source["style_id"], source["output_type"],
                                         source["prompt_version"], prompt, batch_id),
                    None, now, now, json.dumps(metadata, ensure_ascii=False),
                ),
            )
        logger.info(
            "correction planned",
            extra={"ctx": {"task_id": new_id_, "stage": "correction",
                            "style_id": source["style_id"], "status": f"v{version}"}},
        )
        return self.get_task(new_id_, with_details=False)

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
            f" created_at, started_at, completed_at, updated_at, output_path, error_code,"
            f" metadata_json"
            f" FROM tasks {where} ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?",  # noqa: S608
            (*params, limit, offset),
        ).fetchall()
        out = []
        for r in rows:
            doc = dict(r)
            doc["metadata"] = json.loads(doc.pop("metadata_json") or "{}")
            out.append(doc)
        return out

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

    def stamp_metadata(self, task_id: str, extra: dict) -> None:
        """Merge keys into a task's metadata_json (no status transition)."""
        task = self.get_task(task_id, with_details=False)
        metadata = task["metadata"]
        metadata.update(extra)
        conn = self.db.conn()
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE task_id = ?",
                (json.dumps(metadata, ensure_ascii=False), utcnow(), task_id),
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
