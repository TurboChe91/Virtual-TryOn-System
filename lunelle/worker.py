"""Background worker: claims due tasks and executes real image generation.

Runs as N daemon threads inside the service process (N = LUNELLE_MAX_CONCURRENCY),
which doubles as the provider-side concurrency cap. Each claimed task performs
exactly one provider call per attempt; retries are scheduled through the task
service's backoff logic, never looped here — so a crash can at most orphan one
`running` row, which startup recovery re-queues as `interrupted`.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import threading
import time
from pathlib import Path

from .assets import describe_inputs
from .budget import (
    BudgetExceeded,
    LineageBudgetExceeded,
    claim_lineage_descendant,
    recover_orphan_reservations,
    release_spend,
    reserve_spend,
    settle_spend,
    sync_lineage_spend,
)
from .config import Config
from .db import Database
from .logging_setup import task_logger
from .models import (
    ERROR_BUDGET_EXCEEDED,
    ERROR_DEPENDENCY_MISSING,
    OUTPUT_GRID,
    OUTPUT_HERO,
    OUTPUT_MATRIX_CELL,
    OUTPUT_WEARING,
    QA_ERROR,
    QA_RUNNING,
)
from .profiles import ProviderResolver
from .prompts import strip_reference_block
from .providers import GenerationRequest, ImageProvider, ProviderError
from .qa import run_qa, store_qa_result
from .snapshots import record_execution
from .tasks import TaskService

logger = logging.getLogger(__name__)

IDLE_POLL_S = 1.0


class DependencyMissing(Exception):
    """A required input asset is absent, so the task must not call the provider."""


class Worker:
    def __init__(self, config: Config, db: Database, service: TaskService, provider: ImageProvider):
        self.config = config
        self.db = db
        self.service = service
        self.provider = provider
        self.resolver = ProviderResolver(config, db, provider)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._threads:
            return
        self.service.recover_interrupted()
        # A crash between reserving and settling would otherwise leave budget
        # claimed forever.
        recover_orphan_reservations(self.db)
        self.recover_interrupted_qa()
        for index in range(self.config.max_concurrency):
            thread = threading.Thread(target=self._loop, name=f"lunelle-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        logger.info("worker started with %d thread(s)", len(self._threads))

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        alive = [t.name for t in self._threads if t.is_alive()]
        if alive:
            logger.warning("worker threads still running at shutdown: %s", alive)
            # Keep the stop flag set so the leftover daemon threads exit after
            # their in-flight provider call instead of claiming new work.
            self._threads = [t for t in self._threads if t.is_alive()]
            return
        self._threads.clear()
        self._stop.clear()

    def is_alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # ---------------- main loop ----------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                task = self.service.claim_next()
            except Exception:  # noqa: BLE001 - claim must never kill the loop
                logger.exception("claim_next failed; backing off")
                self._stop.wait(5)
                continue
            if task is None:
                self._stop.wait(IDLE_POLL_S)
                continue
            try:
                self._execute(task)
            except Exception:  # noqa: BLE001 - last-resort guard per task
                logger.exception(
                    "unexpected executor crash", extra={"ctx": {"task_id": task["task_id"]}}
                )
                try:
                    # Bounded by max_retries; transient surprises (file races,
                    # DB lock timeouts) deserve the same retry budget as
                    # `interrupted` recovery instead of a permanent failure.
                    self.service.complete_failure(
                        task["task_id"],
                        error_code="internal",
                        error_message="unexpected internal error; see logs",
                        retryable=True,
                    )
                except Exception:  # noqa: BLE001 - keep the loop alive no matter what
                    logger.exception("failed to record task failure")

    # ---------------- execution ----------------

    def _execute(self, task: dict) -> None:
        provider, active_profile = self.resolver.resolve()
        log = task_logger(
            __name__,
            task_id=task["task_id"],
            batch_id=task["batch_id"],
            style_id=task["style_id"],
            sku=task["sku"],
            output_type=task["output_type"],
            provider=provider.name,
            model=task["model"],
        )
        try:
            references = self._resolve_references(task, log)
        except DependencyMissing as exc:
            # Fail fast BEFORE the provider call: a matrix cell without its hand
            # model would render an unusable, off-pose image at full price and
            # then look like a success. Blocked beats silently degraded.
            log.warning("required input missing; task blocked",
                        ctx={"stage": "reference", "error_code": ERROR_DEPENDENCY_MISSING})
            self.service.complete_failure(
                task["task_id"], error_code=ERROR_DEPENDENCY_MISSING,
                error_message=str(exc), retryable=False,
            )
            return
        if task["metadata"].get("correction_of"):
            # SOP v2+: previous candidate rides after the authority images as the
            # locked edit base, then any single-nail detail references.
            try:
                source = self.service.get_task(task["metadata"]["correction_of"],
                                               with_details=False)
                previous = Path(source.get("output_path") or "")
                if previous.is_file():
                    references.append(previous)
                else:
                    log.warning("correction base image missing on disk",
                                ctx={"stage": "reference"})
            except Exception:  # noqa: BLE001 - degraded correction beats a crash
                log.warning("correction source task unavailable", ctx={"stage": "reference"})
            for detail in task["metadata"].get("correction_details", []):
                detail_path = Path(detail)
                if detail_path.is_file():
                    references.append(detail_path)
        prompt = task["prompt"]
        expected_reference = (
            task["metadata"].get("use_reference")
            or task["metadata"].get("use_style_reference")
            or task["metadata"].get("use_hero_reference")
            or task["metadata"].get("use_matrix_reference")
        )
        if expected_reference and not references:
            # Text-only fallback: the stored prompt must not claim an Image 1 exists.
            prompt = strip_reference_block(prompt)
            log.info("reference unavailable; stripped reference block from prompt",
                     ctx={"stage": "reference"})
        size = self._size_for(task["output_type"])
        extra: dict[str, str] = {}
        if task["output_type"] == OUTPUT_HERO and task["model"].startswith("gpt-image"):
            # Listing heroes are final assets; draft tiers are chosen per profile model.
            extra = {"quality": "high", "output_format": "png"}
        request = GenerationRequest(
            prompt=prompt,
            negative_prompt=task["negative_prompt"],
            size=size,
            model=task["model"],
            task_id=task["task_id"],
            reference_images=references,
            extra=extra,
        )
        fingerprint = hashlib.sha256(
            f"{prompt}|{size}|{task['model']}|{[str(r) for r in references]}".encode()
        ).hexdigest()[:16]
        attempt_no = self.service.start_attempt(
            task["task_id"], provider=provider.name, model=task["model"], fingerprint=fingerprint
        )
        log.info("attempt started", ctx={"stage": "generate", "attempt": attempt_no})

        # Claim the spend atomically BEFORE the paid call. A read-then-spend check
        # let two worker threads both see "under budget" and both spend; the
        # reservation is inserted in the same transaction as the check, so
        # concurrent workers serialize and the cap holds at any concurrency.
        estimated = float(task.get("estimated_cost_usd") or 0.0)
        root_task_id = task.get("root_task_id") or task["task_id"]
        try:
            reserve_spend(
                self.db, self.config, task_id=task["task_id"], attempt_no=attempt_no,
                estimated_usd=estimated, root_task_id=root_task_id,
            )
        except BudgetExceeded as exc:
            log.warning("budget breaker refused this task",
                        ctx={"stage": "budget", "error_code": ERROR_BUDGET_EXCEEDED,
                             "status": f"remaining={exc.snapshot.remaining_usd}"})
            self.service.finish_attempt(
                task["task_id"], attempt_no, outcome="error", duration_ms=0,
                error_code=ERROR_BUDGET_EXCEEDED, error_message=str(exc),
                reference_used=bool(references),
            )
            self.service.complete_failure(
                task["task_id"], error_code=ERROR_BUDGET_EXCEEDED,
                error_message=str(exc), retryable=False,
            )
            return

        # Record what is actually going out, before it goes. The plan lives in the
        # snapshot; this is the observation, and the two legitimately differ (a
        # missing reference strips the prompt's reference block).
        try:
            record_execution(
                self.db, task["task_id"], attempt_no,
                resolved_assets=describe_inputs(self.db, self.config, references,
                                                kind="reference"),
                prompt_sent=prompt, model=task["model"], provider=provider.name,
            )
        except Exception:  # noqa: BLE001 - provenance must not block generation
            logger.exception("could not record execution provenance for %s",
                             task["task_id"])

        started = time.monotonic()
        try:
            result = provider.generate(request)
        except ProviderError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            # Release rather than settle: no charge is known. A provider that
            # billed before erroring is under-counted, which is the lesser evil —
            # holding the reservation would let a run of transient failures
            # permanently consume the day's budget.
            release_spend(self.db, task_id=task["task_id"], attempt_no=attempt_no)
            sync_lineage_spend(self.db, root_task_id)
            self.service.finish_attempt(
                task["task_id"], attempt_no, outcome="error", duration_ms=duration_ms,
                http_status=exc.http_status, error_code=exc.code, error_message=exc.message,
                reference_used=bool(references),
            )
            log.warning(
                "provider call failed",
                ctx={"stage": "generate", "error_code": exc.code,
                     "http_status": exc.http_status, "duration_ms": duration_ms},
            )
            self.service.complete_failure(
                task["task_id"], error_code=exc.code, error_message=exc.message,
                retryable=exc.retryable,
            )
            return

        duration_ms = int((time.monotonic() - started) * 1000)

        # The provider call returned, so the money is spent. Settle before doing
        # anything that can fail, so a later error cannot lose the accounting.
        settle_spend(self.db, task_id=task["task_id"], attempt_no=attempt_no,
                     actual_usd=result.actual_cost_usd)
        sync_lineage_spend(self.db, root_task_id)

        # Persist the image before declaring success.
        output_path = self.service.output_file_for(task, attempt_no)
        try:
            _atomic_write(output_path, result.image_bytes)
        except OSError as exc:
            code = "disk_full" if exc.errno == errno.ENOSPC else "file_write_failed"
            self.service.finish_attempt(
                task["task_id"], attempt_no, outcome="error", duration_ms=duration_ms,
                external_request_id=result.external_request_id,
                error_code=code, error_message=str(exc), reference_used=result.reference_used,
                # Charged even though the write failed; keep it in the accounting.
                cost_usd=result.actual_cost_usd,
            )
            self.service.complete_failure(
                task["task_id"], error_code=code,
                error_message=f"could not write output file: {exc}", retryable=False,
            )
            return

        cost = result.actual_cost_usd
        self.service.finish_attempt(
            task["task_id"], attempt_no, outcome="success", duration_ms=duration_ms,
            external_request_id=result.external_request_id,
            reference_used=result.reference_used, cost_usd=cost,
        )
        self.service.complete_success(
            task["task_id"],
            output_path=output_path,
            external_request_id=result.external_request_id,
            actual_cost_usd=cost,
            extra_metadata={
                "attempt_no": attempt_no,
                "reference_used": result.reference_used,
                "response_meta": result.response_meta,
                "image_sha256": hashlib.sha256(result.image_bytes).hexdigest(),
                "api_profile": active_profile["name"] if active_profile else None,
            },
        )
        log.info(
            "task succeeded",
            ctx={"stage": "complete", "status": "success", "duration_ms": duration_ms},
        )

        # QA never breaks the task result, but its state is always recorded: a
        # crash leaves qa_state='error', which the publish/export gate refuses.
        # Silence is never mistaken for "QA passed".
        try:
            self.service.set_qa_state(task["task_id"], QA_RUNNING)
            grid_path = None
            if task["output_type"] == OUTPUT_WEARING:
                grid_task = self.service.latest_successful_grid(task["style_id"])
                if grid_task and grid_task.get("output_path"):
                    grid_path = Path(grid_task["output_path"])
            # Matrix cells reuse the wearing heuristics (hand shot, no grid diff).
            qa_type = OUTPUT_WEARING if task["output_type"] == OUTPUT_MATRIX_CELL \
                else task["output_type"]
            qa_doc = run_qa(
                output_type=qa_type,
                image_path=output_path,
                expected_size=size,
                min_side=min(self.config.qa_min_side, min(size)),
                grid_image_path=grid_path,
            )
            # Stores the verdict and opens human review in one transaction.
            self.service.finish_qa(task["task_id"], qa_doc)
            log.info(
                "qa recorded",
                ctx={"stage": "qa", "status": "pass" if qa_doc["passed"] else "fail"},
            )
            if not qa_doc["passed"]:
                self._maybe_auto_regen(task, qa_doc, log)
            else:
                self._maybe_llm_qa(task, output_path, log)
        except Exception:  # noqa: BLE001 - QA is advisory
            log.warning("qa run crashed; result not recorded", ctx={"stage": "qa"})
            logger.exception("qa failure detail")
            try:
                self.service.set_qa_state(task["task_id"], QA_ERROR)
            except Exception:  # noqa: BLE001 - never let bookkeeping kill the loop
                logger.exception("could not record qa_state=error")

    def recover_interrupted_qa(self) -> dict:
        """Finish QA for tasks that succeeded but whose QA never completed.

        A crash between `complete_success` and `finish_qa` leaves a task at
        qa_state pending/running. The publish/export gate refuses those (correct —
        no verdict exists), but they would sit there forever with no way forward.

        Re-running QA here is safe and free: it is purely local image analysis, no
        provider call. If the output file is gone the asset cannot be judged at
        all, so qa_state becomes `error` and the gate keeps refusing it until an
        operator regenerates.
        """
        rows = self.db.conn().execute(
            "SELECT task_id, output_type, style_id, output_path FROM tasks"
            " WHERE status = 'success' AND qa_state IN ('pending', 'running')"
        ).fetchall()
        summary = {"examined": len(rows), "requeued": 0, "missing_output": 0, "failed": 0}
        for row in rows:
            task_id = row["task_id"]
            output_path = Path(row["output_path"] or "")
            if not row["output_path"] or not output_path.is_file():
                self.service.set_qa_state(task_id, QA_ERROR)
                summary["missing_output"] += 1
                logger.warning(
                    "qa recovery: output missing for %s; marked qa_state=error", task_id
                )
                continue
            try:
                task = self.service.get_task(task_id, with_details=False)
                size = self._size_for(task["output_type"])
                grid_path = None
                if task["output_type"] == OUTPUT_WEARING:
                    grid_task = self.service.latest_successful_grid(task["style_id"])
                    if grid_task and grid_task.get("output_path"):
                        candidate = Path(grid_task["output_path"])
                        if candidate.is_file():
                            grid_path = candidate
                qa_type = (OUTPUT_WEARING if task["output_type"] == OUTPUT_MATRIX_CELL
                           else task["output_type"])
                qa_doc = run_qa(
                    output_type=qa_type,
                    image_path=output_path,
                    expected_size=size,
                    min_side=min(self.config.qa_min_side, min(size)),
                    grid_image_path=grid_path,
                )
                self.service.finish_qa(task_id, qa_doc)
                summary["requeued"] += 1
                logger.info(
                    "qa recovery: re-ran QA for %s (passed=%s)", task_id, qa_doc["passed"]
                )
            except Exception:  # noqa: BLE001 - recovery must not block startup
                summary["failed"] += 1
                logger.exception("qa recovery failed for %s", task_id)
                try:
                    self.service.set_qa_state(task_id, QA_ERROR)
                except Exception:  # noqa: BLE001
                    logger.exception("could not mark qa_state=error for %s", task_id)
        if summary["examined"]:
            logger.warning("qa recovery summary: %s", summary)
        return summary

    def _maybe_llm_qa(self, task: dict, output_path: Path, log) -> None:
        """Advisory vision-LLM check: verifies per-nail identity and, on failure,
        writes the correction and queues the next version itself (bounded by
        auto_regen_max on the correction depth).

        The verdict is ADVISORY ONLY. It is stored with source='llm' and always
        leaves needs_human_review set, so it can never satisfy the publish gate:
        automatic QA passing is not human approval.
        """
        if task["output_type"] not in (OUTPUT_GRID, OUTPUT_HERO, OUTPUT_MATRIX_CELL):
            return
        from .llm import LLMUnavailable, auto_qa_verdict, build_llm_chat

        try:
            chat = build_llm_chat(self.config, self.db)
        except LLMUnavailable:
            return  # heuristic QA + human review remain the gate
        style = self.service.get_style(task["style_id"])
        images = [output_path]
        for key in ("plan_image_path", "reference_image_path"):
            candidate = Path(style.get(key) or "")
            if candidate.is_file():
                images.append(candidate)
                break
        try:
            verdict = auto_qa_verdict(chat, images, style.get("identity_text") or "",
                                      task["output_type"])
        except (RuntimeError, ValueError):
            log.warning("llm qa failed; leaving human review flag set", ctx={"stage": "qa"})
            return
        store_qa_result(self.db, task["task_id"], {
            "passed": verdict["passed"],
            "score": 100 if verdict["passed"] else 40,
            "issues": verdict["issues"],
            "checks": {"llm_identity_qa": verdict},
            "recommended_action": "human_review" if verdict["passed"] else "correct",
            # Always true: an LLM pass is a recommendation, never an approval.
            "needs_human_review": True,
        }, source="llm")
        log.info("llm qa recorded",
                 ctx={"stage": "qa", "status": "pass" if verdict["passed"] else "fail"})
        if verdict["passed"]:
            return
        if not verdict["correction"]:
            return
        self._queue_automatic_work(task, log, kind="correction",
                                   correction_text=verdict["correction"])

    def _maybe_auto_regen(self, task: dict, qa_doc: dict, log) -> None:
        """First-shot-or-reroll policy: a hard automatic-QA failure earns a fresh
        regeneration, bounded by the shared per-root lineage budget."""
        self._queue_automatic_work(task, log, kind="regeneration")

    def _queue_automatic_work(self, task: dict, log, *, kind: str,
                              correction_text: str = "") -> None:
        """Queue one automatic re-generation or correction against the SHARED
        per-root budget.

        Both automatic paths funnel through here so they draw down ONE allowance.
        Previously each kept its own depth counter in task metadata and neither
        copied the other's to the child it created, so a task could alternate
        regen -> correct -> regen indefinitely, each hop resetting the counter the
        other path checked. The advertised worst-case cost was therefore not a
        bound. The lineage ledger is now that bound.
        """
        if not self.config.automatic_work_enabled:
            return
        root_task_id = task.get("root_task_id") or task["task_id"]
        estimated = float(task.get("estimated_cost_usd") or 0.0)
        try:
            claim = claim_lineage_descendant(
                self.db, root_task_id=root_task_id,
                estimated_usd=estimated, kind=kind,
            )
        except LineageBudgetExceeded as exc:
            # Expected end of the loop, not an error: log the reason and stop.
            log.info("automatic work refused by the lineage budget",
                     ctx={"stage": "qa", "status": f"{kind}:budget_exhausted"})
            logger.info("lineage budget stop: %s", exc)
            return
        try:
            if kind == "correction":
                new_task = self.service.create_correction(
                    task["task_id"], correction_text=correction_text)
                queued = [new_task["task_id"]]
            else:
                plan = self.service.create_generation(
                    task["style_id"], [task["output_type"]], force=True,
                    note=f"auto-regen after QA fail of {task['task_id']}",
                    root_override=root_task_id,
                    # The task whose QA failed is the direct ancestor, which is
                    # what makes a re-roll chain reconstructible.
                    parent_task_id=task["task_id"],
                    lineage_reason="auto_regeneration",
                )
                queued = [created["task_id"] for created in plan.created]
            for task_id in queued:
                self.service.stamp_metadata(task_id, {
                    f"auto_{kind}_of": task["task_id"],
                    "lineage_descendant_no": claim["descendant_count"],
                })
            log.info(
                f"automatic {kind} queued",
                ctx={"stage": "qa",
                     "status": f"lineage {claim['descendant_count']}/"
                               f"{claim['max_descendants']}"},
            )
        except Exception:  # noqa: BLE001 - scheduling is best-effort
            # The slot stays consumed. Releasing it on failure would let a
            # repeatedly-failing scheduler retry without bound, which is the very
            # thing this budget exists to prevent.
            logger.exception("automatic %s scheduling failed after claiming a slot", kind)

    def _size_for(self, output_type: str) -> tuple[int, int]:
        if output_type == OUTPUT_GRID:
            return self.config.grid_size
        if output_type == OUTPUT_HERO:
            return self.config.hero_size
        return self.config.wearing_size

    def _hand_model_for(self, tone: str, view: str) -> Path | None:
        """Cell base photo: exact tone+view asset, else the tone-level legacy one."""
        conn = self.db.conn()
        for key in (f"hand_model_{tone}_{view}", f"hand_model_{tone}"):
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                path = Path(row["value"])
                if path.is_file():
                    return path
        return None

    def _design_authority_for(self, style: dict) -> Path | None:
        """Plan upload first, else the latest successful grid."""
        plan = Path(style.get("plan_image_path") or "")
        if plan.is_file():
            return plan
        grid_task = self.service.latest_successful_grid(style["style_id"])
        if grid_task and grid_task.get("output_path"):
            grid_path = Path(grid_task["output_path"])
            if grid_path.is_file():
                return grid_path
        return None

    def _resolve_references(self, task: dict, log) -> list[Path]:
        if task["output_type"] == OUTPUT_HERO:
            return self._resolve_hero_references(task, log)
        if task["output_type"] == OUTPUT_MATRIX_CELL:
            return self._resolve_matrix_references(task, log)
        if task["output_type"] == OUTPUT_GRID:
            if not task["metadata"].get("use_style_reference"):
                return []
            ref = Path(task["metadata"].get("style_reference_path", ""))
            if ref.is_file():
                return [ref]
            log.warning("uploaded style reference missing on disk; text-only grid",
                        ctx={"stage": "reference"})
            return []
        if task["output_type"] != OUTPUT_WEARING or not task["metadata"].get("use_reference"):
            return []
        grid_task = self.service.latest_successful_grid(task["style_id"])
        if not grid_task or not grid_task.get("output_path"):
            log.info("no successful grid available; generating wearing shot text-only",
                     ctx={"stage": "reference"})
            return []
        grid_path = Path(grid_task["output_path"])
        if not grid_path.is_file():
            log.warning("grid output file missing on disk; text-only fallback",
                        ctx={"stage": "reference"})
            return []
        return [grid_path]

    def _resolve_hero_references(self, task: dict, log) -> list[Path]:
        """Image 1 = uploaded plan (or the latest grid), Image 2 = photo reference."""
        if not task["metadata"].get("use_hero_reference"):
            return []
        style = self.service.get_style(task["style_id"])
        plan = self._design_authority_for(style)
        if plan is None:
            log.info("no plan image or grid available; generating hero text-only",
                     ctx={"stage": "reference"})
            return []
        references = [plan]
        photo_ref = Path(style.get("reference_image_path") or "")
        if photo_ref.is_file():
            references.append(photo_ref)
        return references

    def _resolve_matrix_references(self, task: dict, log) -> list[Path]:
        """Image 1 = design authority, Image 2 = the cell tone+view hand model.

        Both are hard requirements. A try-on cell rendered without its base hand
        photo cannot match the pose, skin tone, or crop of the rest of the
        matrix, so it is worthless as a published asset — raising here blocks the
        task instead of billing for an image nobody can use.
        """
        if not task["metadata"].get("use_matrix_reference"):
            return []
        tone = task["metadata"].get("tone", "")
        view = task["metadata"].get("view", "")
        style = self.service.get_style(task["style_id"])
        plan = self._design_authority_for(style)
        if plan is None:
            raise DependencyMissing(
                "no design authority for this style: upload a plan image or "
                "generate a grid before queueing the try-on matrix"
            )
        hand = self._hand_model_for(tone, view)
        if hand is None:
            raise DependencyMissing(
                f"no hand model configured for tone={tone!r} view={view!r}: "
                "upload it on the settings page, then retry this cell"
            )
        return [plan, hand]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
