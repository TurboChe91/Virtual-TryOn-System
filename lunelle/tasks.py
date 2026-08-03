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
    ERROR_DEPENDENCY_FAILED,
    FAILED,
    GENERATABLE_OUTPUT_TYPES,
    MANUAL_RETRY_STATUSES,
    OUTPUT_GRID,
    OUTPUT_HERO,
    OUTPUT_WEARING,
    PENDING,
    QA_DONE,
    QA_ERROR,
    QA_NOT_READY,
    QA_PENDING,
    QA_STATES,
    RETRYING,
    REVIEW_APPROVED,
    REVIEW_GENERATED,
    REVIEW_PUBLISH_READY,
    REVIEW_PUBLISHED,
    REVIEW_REJECTED,
    REVIEW_WAITING,
    RUNNING,
    SUCCESS,
    IllegalTransition,
    check_review_transition,
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
from .snapshots import (
    build_snapshot,
    compute_fingerprint,
    config_fingerprint_fields,
    contract_hashes_for,
    profile_snapshot,
    store_snapshot_locked,
)

logger = logging.getLogger(__name__)


from .assets import describe_inputs_locked  # noqa: E402
from .budget import (  # noqa: E402
    check_can_queue_locked,
    estimate_plan,
    lineage_status,
    open_lineage_locked,
    spend_snapshot,
)
from .errors import (  # noqa: E402  (re-exported for callers)
    ConflictError,
    CostConfirmationRequired,
    NotFoundError,
)

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

    def set_tryon_id(self, style_id: str, tryon_id: str) -> None:
        self._set_style_column(style_id, "tryon_style_id", tryon_id.strip() or None)

    def _set_style_column(self, style_id: str, column: str, value) -> None:
        assert column in ("reference_image_path", "plan_image_path", "identity_text",
                          "tryon_style_id")
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
        provider, model, price, profile_row = self._generation_channel_row()
        return provider, model, price, (profile_row["name"] if profile_row else None)

    def _generation_channel_row(self):
        """As above, but returns the profile ROW so a snapshot can freeze the
        channel's identity (never its key — see snapshots.profile_snapshot)."""
        from .profiles import ProfileService

        row = ProfileService(self.db).active_row()
        if row is None:
            model = self.config.image_model
            return self.config.image_provider, model, self.config.price_for(model), None
        price = row["price_per_image_usd"]
        if price is None:
            price = self.config.price_for(row["model"])
        return "openai-compat", row["model"], float(price), row

    def _snapshot_for(
        self, conn, *, style: dict, output_type: str, prompt: str,
        negative_prompt: str, prompt_version: str, provider: str, model: str,
        size: tuple[int, int], profile_row, input_paths: list[Path],
        asset_kind: str, extra: dict | None = None,
    ) -> tuple[dict, str]:
        """Build (snapshot, input_fingerprint) for one task.

        What can be frozen at creation time is frozen here. Inputs that only exist
        at execution time — the grid a wearing shot will use is generated later in
        the same batch — are recorded as intent flags in `extra`, and the actual
        digests land in task_executions. Claiming a digest for a file that does not
        exist yet would make the snapshot a guess.
        """
        assets_described = describe_inputs_locked(conn, input_paths, kind=asset_kind)
        merged_extra = dict(config_fingerprint_fields(self.config))
        if extra:
            merged_extra.update(extra)
        snapshot = build_snapshot(
            style=style,
            spec={k: v for k, v in style["spec"].items() if not k.startswith("_")},
            output_type=output_type,
            prompt=prompt,
            negative_prompt=negative_prompt,
            prompt_version=prompt_version,
            contract_hashes=contract_hashes_for(output_type),
            provider=provider,
            model=model,
            size=size,
            reference_mode=self.config.reference_mode,
            disable_watermark=self.config.disable_provider_watermark,
            api_profile=profile_snapshot(profile_row),
            input_assets=assets_described,
            extra=merged_extra,
        )
        return snapshot, compute_fingerprint(snapshot)

    def _size_for_output(self, output_type: str) -> tuple[int, int]:
        if output_type == OUTPUT_GRID:
            return self.config.grid_size
        if output_type == OUTPUT_HERO:
            return self.config.hero_size
        return self.config.wearing_size

    @staticmethod
    def idempotency_key(style_id: str, output_type: str, prompt_version: str, prompt: str, nonce: str) -> str:
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        raw = f"{style_id}|{output_type}|{prompt_version}|{prompt_hash}|{nonce}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def create_generation(
        self, style_id: str, output_types: list[str], *, force: bool = False,
        note: str = "", root_override: str | None = None,
        parent_task_id: str | None = None, lineage_reason: str | None = None,
    ) -> GenerationPlan:
        """Queue operator-requested generations.

        `root_override` attaches the new tasks to an existing lineage instead of
        opening a new one — used by automatic re-generation, so a re-roll spends
        the original root's shared allowance rather than granting itself a fresh
        one (which is how the old per-path counters could alternate forever).
        `parent_task_id` records the direct ancestor: the root gives the budget
        scope, the parent gives the step, and reconstructing a repair chain needs
        both.
        """
        style = self.get_style(style_id)
        bundle = self.prompt_bundle_for(style)
        for output_type in output_types:
            if output_type not in GENERATABLE_OUTPUT_TYPES:
                raise ValueError(f"invalid output type {output_type!r}")
        provider_name, model, price, profile_row = self._generation_channel_row()
        profile_name = profile_row["name"] if profile_row else None

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
                # An operator-requested task is its own lineage root, and gets a
                # fresh shared allowance for whatever automatic work follows.
                root_task_id = root_override or task_id
                # Freeze the inputs. Only assets that exist NOW get a digest; the
                # grid a wearing shot will use does not exist yet, so its intent is
                # recorded and the actual digest lands in task_executions.
                input_paths: list[Path] = []
                if metadata.get("use_style_reference"):
                    candidate = Path(metadata.get("style_reference_path", ""))
                    if candidate.is_file():
                        input_paths.append(candidate)
                if output_type == OUTPUT_HERO and style.get("plan_image_path"):
                    plan_path = Path(style["plan_image_path"])
                    if plan_path.is_file():
                        input_paths.append(plan_path)
                snapshot, fingerprint = self._snapshot_for(
                    conn, style=style, output_type=output_type, prompt=prompt,
                    negative_prompt=bundle.negative_prompt, prompt_version=version,
                    provider=provider_name, model=model,
                    size=self._size_for_output(output_type),
                    profile_row=profile_row, input_paths=input_paths,
                    asset_kind="plan" if output_type == OUTPUT_HERO else "reference",
                    extra={
                        "pending_reference_intent": sorted(
                            k for k in ("use_reference", "use_hero_reference",
                                        "use_style_reference")
                            if metadata.get(k)
                        ),
                        "waits_for_grid": bool(wait_for),
                    },
                )
                depth = self._depth_after(conn, parent_task_id)
                conn.execute(
                    "INSERT INTO tasks (task_id, batch_id, style_id, sku, output_type, prompt,"
                    " negative_prompt, prompt_version, provider, model, status, retry_count,"
                    " max_retries, estimated_cost_usd, idempotency_key, wait_for_task_id,"
                    " created_at, updated_at, root_task_id, parent_task_id, lineage_depth,"
                    " lineage_reason, input_fingerprint, metadata_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        root_task_id,
                        parent_task_id,
                        depth,
                        lineage_reason or ("operator_request" if parent_task_id is None
                                           else "regeneration"),
                        fingerprint,
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
                # Same transaction: a task must never exist without the record of
                # what it was built from.
                store_snapshot_locked(conn, task_id, snapshot, fingerprint)
                if root_override is None:
                    open_lineage_locked(
                        conn, self.config, root_task_id=task_id, style_id=style_id,
                        output_type=output_type, price_per_image=price,
                    )
                created.append({"task_id": task_id, "status": PENDING,
                                "output_type": output_type, "root_task_id": root_task_id})
                if output_type == OUTPUT_GRID:
                    grid_task_id_this_round = task_id

        logger.info(
            "generation planned",
            extra={"ctx": {"batch_id": batch_id, "style_id": style_id, "sku": style["sku"],
                            "stage": "plan", "status": f"created={len(created)} reused={len(reused)}"}},
        )
        return GenerationPlan(batch_id=batch_id, created=created, reused=reused, skipped=skipped)

    def estimate_matrix(self, style_id: str, *, tones: list[str] | None = None,
                        views: list[str] | None = None, force: bool = False) -> dict:
        """Price a matrix batch WITHOUT queueing anything.

        Counts only cells that would actually be created: re-running a matrix
        where most cells already succeeded should quote the incremental cost, not
        the full 16, or the operator learns to ignore the number.
        """
        from .models import OUTPUT_MATRIX_CELL

        style = self.get_style(style_id)
        spec = self.spec_for(style)
        tones, views = self._matrix_axes(tones, views)
        _, _, price, _ = self._generation_channel()
        use_refs = self.config.reference_mode != "off"

        conn = self.db.conn()
        would_create = 0
        already_done = 0
        in_progress = 0
        for tone in tones:
            for view in views:
                prompt = build_matrix_prompt(
                    spec, style.get("identity_text"), tone, view, with_reference=use_refs
                )
                key = self.idempotency_key(
                    style_id, OUTPUT_MATRIX_CELL, MATRIX_PROMPT_VERSION, prompt,
                    "FORCE" if force else "",
                )
                existing = conn.execute(
                    "SELECT status FROM tasks WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing is None:
                    would_create += 1
                elif existing["status"] == SUCCESS:
                    already_done += 1
                elif existing["status"] in (PENDING, RUNNING, RETRYING):
                    in_progress += 1
                else:
                    would_create += 1  # failed/cancelled cells get re-queued
        estimate = estimate_plan(self.config, image_count=would_create,
                                 price_per_image=price)
        estimate.update({
            "style_id": style_id,
            "sku": style["sku"],
            "cells_requested": len(tones) * len(views),
            "cells_already_succeeded": already_done,
            "cells_in_progress": in_progress,
            "tones": tones,
            "views": views,
        })
        estimate["budget"] = spend_snapshot(self.db, self.config).as_dict()
        return estimate

    @staticmethod
    def _depth_after(conn, parent_task_id: str | None) -> int:
        """Depth of a child of `parent_task_id`. Roots are 0."""
        if parent_task_id is None:
            return 0
        row = conn.execute(
            "SELECT lineage_depth FROM tasks WHERE task_id = ?", (parent_task_id,)
        ).fetchone()
        return (int(row["lineage_depth"]) + 1) if row is not None else 1

    def _matrix_input_paths(self, conn, style: dict, tone: str, view: str) -> list[Path]:
        """Design authority then hand model, in the order the provider receives
        them (Image 1 = design, Image 2 = base hand)."""
        paths: list[Path] = []
        plan = Path(style.get("plan_image_path") or "")
        if plan.is_file():
            paths.append(plan)
        else:
            grid = conn.execute(
                "SELECT output_path FROM tasks WHERE style_id = ? AND output_type = ?"
                " AND status = ? ORDER BY completed_at DESC LIMIT 1",
                (style["style_id"], OUTPUT_GRID, SUCCESS),
            ).fetchone()
            if grid and grid["output_path"] and Path(grid["output_path"]).is_file():
                paths.append(Path(grid["output_path"]))
        hand = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (f"hand_model_{tone}_{view}",),
        ).fetchone()
        if hand and Path(hand["value"]).is_file():
            paths.append(Path(hand["value"]))
        return paths

    def _matrix_axes(self, tones: list[str] | None,
                     views: list[str] | None) -> tuple[list[str], list[str]]:
        tones = [t for t in (tones or MATRIX_TONES) if t in MATRIX_TONES]
        views = [v for v in (views or MATRIX_VIEWS) if v in MATRIX_VIEWS]
        if not tones or not views:
            raise ValueError(f"tones must be within {MATRIX_TONES} and views within {MATRIX_VIEWS}")
        return tones, views

    def create_matrix_generation(
        self, style_id: str, *, tones: list[str] | None = None,
        views: list[str] | None = None, force: bool = False, note: str = "",
        confirmed_max_usd: float | None = None,
    ) -> GenerationPlan:
        """Queue the try-on matrix for one style: every tone x view cell.

        `confirmed_max_usd` is the worst-case ceiling the caller was shown by
        `estimate_matrix`. Above LUNELLE_CONFIRM_COST_USD it must be supplied and
        must still match, so a single click can never commit an unbounded spend,
        and a price change between preview and submit re-prompts instead of
        silently charging more.
        """
        from .models import OUTPUT_MATRIX_CELL

        style = self.get_style(style_id)
        spec = self.spec_for(style)
        tones, views = self._matrix_axes(tones, views)
        provider_name, model, price, profile_row = self._generation_channel_row()
        profile_name = profile_row["name"] if profile_row else None
        use_refs = self.config.reference_mode != "off"

        estimate = self.estimate_matrix(style_id, tones=tones, views=views, force=force)
        self._require_cost_authorization(estimate, confirmed_max_usd)

        conn = self.db.conn()
        batch_id = new_batch_id()
        now = utcnow()
        created: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        with transaction(conn):
            # Breaker INSIDE the write transaction: BEGIN IMMEDIATE serializes
            # writers, so two concurrent callers cannot both pass the check and
            # then both insert. Checking before the transaction was a
            # check-then-act race that could overrun the cap.
            check_can_queue_locked(conn, self.config,
                                   additional_usd=estimate["estimated_usd"])
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
                    # A cell's inputs exist now: the design authority (plan or the
                    # latest grid) and the tone+view hand model. Freezing them by
                    # digest is what makes a re-upload unable to rewrite history.
                    cell_inputs = self._matrix_input_paths(conn, style, tone, view)
                    snapshot, fingerprint = self._snapshot_for(
                        conn, style=style, output_type=OUTPUT_MATRIX_CELL, prompt=prompt,
                        negative_prompt="", prompt_version=MATRIX_PROMPT_VERSION,
                        provider=provider_name, model=model,
                        size=self.config.wearing_size, profile_row=profile_row,
                        input_paths=cell_inputs, asset_kind="hand_model",
                        extra={"tone": tone, "view": view},
                    )
                    conn.execute(
                        "INSERT INTO tasks (task_id, batch_id, style_id, sku, output_type, prompt,"
                        " negative_prompt, prompt_version, provider, model, status, retry_count,"
                        " max_retries, estimated_cost_usd, idempotency_key, wait_for_task_id,"
                        " created_at, updated_at, root_task_id, parent_task_id, lineage_depth,"
                        " lineage_reason, input_fingerprint, metadata_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            task_id, batch_id, style_id, style["sku"], OUTPUT_MATRIX_CELL,
                            prompt, "", MATRIX_PROMPT_VERSION, provider_name, model,
                            PENDING, 0, self.config.max_retries, price, key, None,
                            now, now, task_id, None, 0, "operator_request",
                            fingerprint, json.dumps(metadata, ensure_ascii=False),
                        ),
                    )
                    store_snapshot_locked(conn, task_id, snapshot, fingerprint)
                    # Each cell is its own root: cells are independent assets, so
                    # one cell's corrections must not consume another's allowance.
                    open_lineage_locked(
                        conn, self.config, root_task_id=task_id, style_id=style_id,
                        output_type=OUTPUT_MATRIX_CELL, price_per_image=price,
                    )
                    created.append({"task_id": task_id, "status": PENDING,
                                    "output_type": OUTPUT_MATRIX_CELL, **cell})
        logger.info(
            "matrix planned",
            extra={"ctx": {"batch_id": batch_id, "style_id": style_id, "stage": "plan",
                            "status": f"created={len(created)} skipped={len(skipped)}"}},
        )
        return GenerationPlan(batch_id=batch_id, created=created, reused=reused, skipped=skipped)

    def _require_cost_authorization(self, estimate: dict,
                                    confirmed_max_usd: float | None) -> None:
        """Enforce the confirmation contract for an expensive batch.

        The authorized figure is the WORST case (`confirm_max_usd`), not the
        expected cost: approving $0.64 and being billed $3.84 is not consent.
        """
        if not estimate["requires_confirmation"]:
            return
        ceiling = estimate["confirm_max_usd"]
        if confirmed_max_usd is None:
            raise CostConfirmationRequired(
                f"cost_confirmation_required: this batch generates "
                f"{estimate['image_count']} image(s), expected "
                f"${estimate['estimated_usd']:.4f} and AT MOST ${ceiling:.4f} "
                f"({estimate['worst_case_note']}). Re-send with "
                f"confirm_max_usd={ceiling} to authorize that ceiling.",
                estimate,
            )
        # Tolerance of one hundredth of a cent absorbs float noise only.
        if abs(float(confirmed_max_usd) - ceiling) > 0.0001:
            raise CostConfirmationRequired(
                f"cost_confirmation_mismatch: you authorized a ceiling of "
                f"${float(confirmed_max_usd):.4f} but this batch's ceiling is now "
                f"${ceiling:.4f}. Review the new estimate and confirm again.",
                estimate,
            )

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

        provider_name, model, price, profile_row = self._generation_channel_row()
        profile_name = profile_row["name"] if profile_row else None
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
        # A correction stays inside the source's lineage, so an automatic
        # correction spends the shared allowance instead of starting a new one.
        root_task_id = source.get("root_task_id") or source["task_id"]
        with transaction(conn):
            conn.execute(
                "INSERT INTO batches (batch_id, note, created_at) VALUES (?,?,?)",
                (batch_id, f"correction v{version} of {task_id}", now),
            )
            # A correction's inputs are the locked base image plus any detail
            # references, all of which exist now.
            correction_inputs = [Path(source["output_path"])]
            correction_inputs.extend(details)
            snapshot, fingerprint = self._snapshot_for(
                conn, style=self.get_style(source["style_id"]),
                output_type=source["output_type"], prompt=prompt,
                negative_prompt=source["negative_prompt"],
                prompt_version=source["prompt_version"],
                provider=provider_name, model=model,
                size=self._size_for_output(source["output_type"]),
                profile_row=profile_row, input_paths=correction_inputs,
                asset_kind="correction_detail",
                extra={
                    "correction_of": task_id,
                    "version": version,
                    # The instruction is an input: the same base image with a
                    # different instruction is a different task.
                    "correction_text": correction_text.strip()[:2000],
                },
            )
            conn.execute(
                "INSERT INTO tasks (task_id, batch_id, style_id, sku, output_type, prompt,"
                " negative_prompt, prompt_version, provider, model, status, retry_count,"
                " max_retries, estimated_cost_usd, idempotency_key, wait_for_task_id,"
                " created_at, updated_at, root_task_id, parent_task_id, lineage_depth,"
                " lineage_reason, input_fingerprint, metadata_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id_, batch_id, source["style_id"], source["sku"],
                    source["output_type"], prompt, source["negative_prompt"],
                    source["prompt_version"], provider_name, model, PENDING, 0,
                    self.config.max_retries, price,
                    self.idempotency_key(source["style_id"], source["output_type"],
                                         source["prompt_version"], prompt, batch_id),
                    None, now, now, root_task_id,
                    # The corrected task IS the parent — a correction chain is the
                    # deepest lineage this system produces, so its ancestry has to
                    # be a column rather than a metadata key.
                    task_id, self._depth_after(conn, task_id), "correction",
                    fingerprint, json.dumps(metadata, ensure_ascii=False),
                ),
            )
            store_snapshot_locked(conn, new_id_, snapshot, fingerprint)
            # A manual correction of a task that predates the ledger needs a
            # lineage to draw on; opening it here keeps old assets correctable.
            open_lineage_locked(
                conn, self.config, root_task_id=root_task_id,
                style_id=source["style_id"], output_type=source["output_type"],
                price_per_image=price,
            )
        logger.info(
            "correction planned",
            extra={"ctx": {"task_id": new_id_, "stage": "correction",
                            "style_id": source["style_id"], "status": f"v{version}"}},
        )
        return self.get_task(new_id_, with_details=False)

    # ---------------- task queries ----------------

    def lineage_for(self, task_id: str) -> dict:
        """Shared automatic-work budget for this task's lineage, plus the chain.

        Exposed so an operator can see why automatic correction stopped: without
        it, "the system stopped trying" looks identical to "the system is broken".
        The chain answers the other half — what this asset is a repair OF.
        """
        task = self.get_task(task_id, with_details=False)
        root = task.get("root_task_id") or task_id
        status = lineage_status(self.db, root)
        status["ancestors"] = self.ancestors_of(task_id)
        status["descendants"] = self.descendants_of(task_id)
        status["this_task"] = {
            "task_id": task_id,
            "parent_task_id": task.get("parent_task_id"),
            "lineage_depth": task.get("lineage_depth", 0),
            "lineage_reason": task.get("lineage_reason"),
        }
        return status

    def ancestors_of(self, task_id: str, limit: int = 50) -> list[dict]:
        """Walk parent links from this task back to the root, nearest first."""
        conn = self.db.conn()
        chain: list[dict] = []
        seen = {task_id}
        current = task_id
        while len(chain) < limit:
            row = conn.execute(
                "SELECT parent_task_id FROM tasks WHERE task_id = ?", (current,)
            ).fetchone()
            if row is None or not row["parent_task_id"]:
                break
            parent_id = row["parent_task_id"]
            if parent_id in seen:  # pragma: no cover - malformed cycle guard
                break
            seen.add(parent_id)
            parent = conn.execute(
                "SELECT task_id, output_type, status, review_state, lineage_depth,"
                " lineage_reason, output_path, created_at, input_fingerprint"
                " FROM tasks WHERE task_id = ?", (parent_id,)
            ).fetchone()
            if parent is None:
                break
            chain.append(dict(parent))
            current = parent_id
        return chain

    def descendants_of(self, task_id: str, limit: int = 100) -> list[dict]:
        """Everything derived from this task, breadth-first."""
        conn = self.db.conn()
        out: list[dict] = []
        queue = [task_id]
        seen = {task_id}
        while queue and len(out) < limit:
            current = queue.pop(0)
            rows = conn.execute(
                "SELECT task_id, output_type, status, review_state, lineage_depth,"
                " lineage_reason, output_path, created_at, input_fingerprint"
                " FROM tasks WHERE parent_task_id = ? ORDER BY created_at",
                (current,),
            ).fetchall()
            for row in rows:
                if row["task_id"] in seen:  # pragma: no cover - cycle guard
                    continue
                seen.add(row["task_id"])
                out.append(dict(row))
                queue.append(row["task_id"])
        return out

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
            # `qa` is the gate-relevant HEURISTIC verdict; advisory LLM verdicts
            # are surfaced separately so review and gating can never act on one
            # by accident (the LLM row is usually the most recent).
            doc["qa"] = self._latest_qa(conn, task_id, "heuristic")
            doc["qa_advisory"] = self._latest_qa(conn, task_id, "llm")
        return doc

    @staticmethod
    def _latest_qa(conn: sqlite3.Connection, task_id: str, source: str) -> dict | None:
        row = conn.execute(
            "SELECT * FROM qa_results WHERE task_id = ? AND source = ?"
            " ORDER BY qa_id DESC LIMIT 1",
            (task_id, source),
        ).fetchone()
        if row is None:
            return None
        qa_doc = dict(row)
        qa_doc["issues"] = json.loads(qa_doc.pop("issues_json"))
        qa_doc["checks"] = json.loads(qa_doc.pop("checks_json"))
        return qa_doc

    def list_tasks(
        self,
        *,
        sku: str | None = None,
        status: str | None = None,
        output_type: str | None = None,
        batch_id: str | None = None,
        qa_state: str | None = None,
        review_state: str | None = None,
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
        if qa_state:
            clauses.append("qa_state = ?")
            params.append(qa_state)
        if review_state:
            clauses.append("review_state = ?")
            params.append(review_state)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.db.conn().execute(
            f"SELECT task_id, batch_id, style_id, sku, output_type, prompt_version, provider,"  # noqa: S608
            f" model, status, retry_count, max_retries, estimated_cost_usd, actual_cost_usd,"
            f" created_at, started_at, completed_at, updated_at, output_path, error_code,"
            f" qa_state, review_state, reviewed_at, root_task_id,"
            f" parent_task_id, lineage_depth, lineage_reason, input_fingerprint,"
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
            # A dependency must have SUCCEEDED, not merely stopped being active.
            # The previous condition (`NOT IN (pending, running, retrying)`) let a
            # failed or cancelled dependency release its dependent, which then ran
            # and silently produced a degraded asset at full price. Propagation
            # normally fails such dependents first; this is the backstop for the
            # window where it has not run yet.
            row = conn.execute(
                "SELECT * FROM tasks"
                " WHERE status IN (?, ?)"
                " AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                " AND (wait_for_task_id IS NULL OR wait_for_task_id IN"
                "      (SELECT task_id FROM tasks WHERE status = 'success'))"
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
        # qa_state and review_state are written in the SAME transaction as the
        # success transition. Without this, a caller that sees status=success has
        # no way to tell "QA not run yet" from "QA found nothing" — that was the
        # race behind the flaky /review 409. Reordering the QA call alone would
        # only shrink the window; the explicit state removes it.
        doc = self.transition(
            task_id,
            SUCCESS,
            output_path=str(output_path),
            external_request_id=external_request_id,
            actual_cost_usd=actual_cost_usd,
            completed_at=utcnow(),
            error_code=None,
            error_message=None,
            qa_state=QA_PENDING,
            review_state=REVIEW_GENERATED,
            metadata_json=json.dumps(metadata, ensure_ascii=False),
        )
        self.revive_dependents(task_id)
        return doc

    def revive_dependents(self, task_id: str) -> list[str]:
        """Re-queue tasks that were failed ONLY because this one had failed.

        Without this, an operator who retries a failed grid watches it succeed
        while the wearing shot stays dead — and would reasonably assume the pair is
        complete. Deliberately narrow: only `failed` dependents whose error_code is
        exactly dependency_failed. A dependent that failed for its own reason keeps
        that failure, because this task succeeding says nothing about it.
        """
        conn = self.db.conn()
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE wait_for_task_id = ? AND status = ?"
            " AND error_code = ?",
            (task_id, FAILED, ERROR_DEPENDENCY_FAILED),
        ).fetchall()
        revived: list[str] = []
        for row in rows:
            try:
                self.transition(
                    row["task_id"], PENDING,
                    error_code=None, error_message=None, next_attempt_at=None,
                    retry_count=0,  # its dependency is fixed; grant a fresh budget
                )
            except IllegalTransition:  # pragma: no cover - raced by a worker
                continue
            revived.append(row["task_id"])
        if revived:
            logger.info(
                "dependents re-queued after their dependency succeeded",
                extra={"ctx": {"task_id": task_id, "stage": "dependency",
                                "status": f"revived={len(revived)}"}},
            )
        return revived

    # ---------------- QA / review state ----------------

    def set_qa_state(self, task_id: str, qa_state: str) -> None:
        """Advance the QA pipeline state (independent of task status)."""
        if qa_state not in QA_STATES:
            raise ValueError(f"unknown qa_state {qa_state!r}")
        conn = self.db.conn()
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET qa_state = ?, updated_at = ? WHERE task_id = ?",
                (qa_state, utcnow(), task_id),
            )

    def finish_qa(self, task_id: str, qa_doc: dict) -> None:
        """Store the heuristic QA verdict and open human review atomically.

        One transaction so no caller can observe qa_state='done' without the
        row, or the row without the state.
        """
        from .qa import insert_qa_result

        conn = self.db.conn()
        with transaction(conn):
            insert_qa_result(conn, task_id, qa_doc, source="heuristic")
            conn.execute(
                "UPDATE tasks SET qa_state = ?, review_state = ?, updated_at = ?"
                " WHERE task_id = ? AND review_state = ?",
                (QA_DONE, REVIEW_WAITING, utcnow(), task_id, REVIEW_GENERATED),
            )
            # A re-run of QA on an already-reviewed asset must not silently keep
            # the old approval: the verdict it was approved against is gone.
            conn.execute(
                "UPDATE tasks SET qa_state = ?, review_state = ?, reviewed_at = NULL,"
                " reviewed_by = NULL, updated_at = ?"
                " WHERE task_id = ? AND review_state IN (?, ?, ?, ?)",
                (QA_DONE, REVIEW_WAITING, utcnow(), task_id,
                 REVIEW_APPROVED, REVIEW_REJECTED, REVIEW_PUBLISH_READY, REVIEW_PUBLISHED),
            )

    def record_review(self, task_id: str, *, approved: bool, note: str = "",
                      reviewer: str = "") -> dict:
        """Record a human verdict. Requires a landed heuristic QA verdict."""
        task = self.get_task(task_id)
        if task["status"] != SUCCESS:
            raise ConflictError("only successful tasks can be reviewed")
        if task["qa_state"] in QA_NOT_READY:
            raise ConflictError(
                "qa_not_ready: automatic QA has not finished for this task yet"
            )
        if task["qa_state"] == QA_ERROR or task["qa"] is None:
            raise ConflictError(
                "no QA result exists for this task; re-run QA before reviewing"
            )
        new_state = REVIEW_APPROVED if approved else REVIEW_REJECTED
        check_review_transition(task["review_state"], new_state)
        now = utcnow()
        conn = self.db.conn()
        with transaction(conn):
            conn.execute(
                "INSERT INTO asset_reviews (task_id, qa_id, decision, note, reviewer,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (task_id, task["qa"]["qa_id"], new_state, note[:2000], reviewer[:120], now),
            )
            # Keep needs_human_review in sync so the gate's two conditions agree.
            conn.execute(
                "UPDATE qa_results SET needs_human_review = ? WHERE qa_id = ?",
                (0 if approved else 1, task["qa"]["qa_id"]),
            )
            cur = conn.execute(
                "UPDATE tasks SET review_state = ?, reviewed_at = ?, reviewed_by = ?,"
                " updated_at = ? WHERE task_id = ? AND review_state = ?",
                (new_state, now, reviewer[:120], now, task_id, task["review_state"]),
            )
            if cur.rowcount != 1:  # concurrent review changed the state under us
                raise ConflictError("review state changed concurrently; re-read the task")
        return self.get_task(task_id)

    def mark_review_state(self, task_id: str, new_state: str) -> None:
        """System-driven review transition (publish_ready / published)."""
        conn = self.db.conn()
        with transaction(conn):
            row = conn.execute(
                "SELECT review_state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"task {task_id} not found")
            check_review_transition(row["review_state"], new_state)
            conn.execute(
                "UPDATE tasks SET review_state = ?, updated_at = ?"
                " WHERE task_id = ? AND review_state = ?",
                (new_state, utcnow(), task_id, row["review_state"]),
            )

    def mark_published(self, task_id: str) -> None:
        """Record that an asset reached production.

        `publish_ready` is a system-derived state, never settable by a human, so
        it is passed through here on the way to `published` rather than being
        exposed as something a reviewer can declare.
        """
        task = self.get_task(task_id, with_details=False)
        if task["review_state"] == REVIEW_PUBLISHED:
            return
        if task["review_state"] == REVIEW_APPROVED:
            self.mark_review_state(task_id, REVIEW_PUBLISH_READY)
        self.mark_review_state(task_id, REVIEW_PUBLISHED)

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
        doc = self.transition(
            task_id,
            FAILED,
            completed_at=utcnow(),
            error_code=error_code,
            error_message=safe_message,
        )
        # Terminal failure: anything waiting on this task can never proceed, so
        # fail it now rather than letting it run degraded or wait forever.
        self.propagate_dependency_failure(task_id)
        return doc

    def propagate_dependency_failure(self, task_id: str) -> list[str]:
        """Fail everything waiting on a terminally-failed task, transitively.

        Called when a task reaches `failed` or `cancelled`. Without this, a
        dependent either waits forever or — before the claim_next fix — ran anyway
        and produced a silently degraded asset.

        Transitive because a chain is possible: grid -> wearing -> correction. The
        walk is breadth-first over wait_for_task_id with a visited set, so a
        malformed cycle cannot spin.
        """
        conn = self.db.conn()
        failed: list[str] = []
        queue = [task_id]
        seen = {task_id}
        while queue:
            current = queue.pop(0)
            dependents = conn.execute(
                "SELECT task_id, status FROM tasks WHERE wait_for_task_id = ?"
                " AND status IN (?, ?)",
                (current, PENDING, RETRYING),
            ).fetchall()
            for row in dependents:
                dependent_id = row["task_id"]
                if dependent_id in seen:  # pragma: no cover - cycle guard
                    continue
                seen.add(dependent_id)
                try:
                    self.transition(
                        dependent_id,
                        FAILED,
                        completed_at=utcnow(),
                        error_code=ERROR_DEPENDENCY_FAILED,
                        error_message=(
                            f"dependency {current} failed terminally; refusing to "
                            f"generate a degraded asset without it"
                        ),
                    )
                except IllegalTransition:
                    # Expected only when a worker claimed the dependent between
                    # the SELECT and here. Logged rather than swallowed: silently
                    # skipping is how a genuinely unreachable transition hid as
                    # "task stuck in pending".
                    logger.warning(
                        "could not propagate dependency failure to %s (raced)",
                        dependent_id,
                    )
                    continue
                failed.append(dependent_id)
                queue.append(dependent_id)
        if failed:
            logger.warning(
                "dependency failure propagated",
                extra={"ctx": {"task_id": task_id, "stage": "dependency",
                                "status": f"failed={len(failed)}"}},
            )
        return failed

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
        doc = self.transition(task_id, CANCELLED, completed_at=utcnow())
        # Cancellation is terminal too: a dependent would otherwise wait for a
        # task that is never going to run.
        self.propagate_dependency_failure(task_id)
        return doc

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
