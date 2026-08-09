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
from dataclasses import dataclass, field
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
from .build import describe as describe_build
from .build import detect_drift, drift_allowed, stamp
from .config import Config
from .db import Database
from .inputs import (
    DEFERRABLE_ROLES,
    InputUnavailable,
    ResolvedInputs,
    channel_fingerprint,
    compare_to_snapshot,
    load_all,
    request_manifest,
)
from .logging_setup import task_logger
from .models import (
    ERROR_BUDGET_EXCEEDED,
    ERROR_DEPENDENCY_MISSING,
    ERROR_SNAPSHOT_MISMATCH,
    OUTPUT_GRID,
    OUTPUT_HERO,
    OUTPUT_MATRIX_CELL,
    OUTPUT_WEARING,
    QA_ERROR,
    QA_RUNNING,
)
from .profiles import ProviderResolver
from .prompts import matrix_visible_nails, strip_reference_block
from .providers import GenerationRequest, ImageProvider, ProviderError
from .qa import run_qa, store_qa_result
from .snapshots import (
    SNAPSHOT_VERSION,
    get_snapshot,
    profile_snapshot,
    record_execution,
)
from .tasks import TaskService

logger = logging.getLogger(__name__)

IDLE_POLL_S = 1.0


class DependencyMissing(Exception):
    """A required input asset is absent, so the task must not call the provider."""


class SnapshotMismatch(Exception):
    """The request does not match the snapshot, so the provider must not be called."""


@dataclass(frozen=True)
class ExecutionPlan:
    """One attempt's inputs, all of them from the snapshot.

    Exists so the prompt, size and images cannot come from different places. They
    used to: the prompt from `tasks.prompt`, the size recomputed from current
    settings, the images re-resolved from the current style row — three sources for
    one request, none compared against the plan.
    """

    snapshot: dict
    fingerprint: str
    prompt: str
    negative_prompt: str
    size: tuple[int, int]
    inputs: ResolvedInputs
    #: Roles resolved at execution time because they cannot exist at queue time.
    #: Compared by role and position only — there is no frozen digest for them.
    deferred_roles: set[str] = field(default_factory=set)


class Worker:
    def __init__(self, config: Config, db: Database, service: TaskService, provider: ImageProvider):
        self.config = config
        self.db = db
        self.service = service
        self.provider = provider
        self.resolver = ProviderResolver(config, db, provider)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        #: Set once drift is seen, and never cleared. A process whose tree changed
        #: has already loaded some modules from the old tree and may lazily import
        #: others from the new one, so it is a mixture no record can describe.
        #: Reverting the file does not undo that; only a restart does.
        self._drift_halted = False

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
        build = describe_build()
        logger.info(
            "worker started with %d thread(s) on build %s (tree %s, git %s%s)",
            len(self._threads), build["build_id"], build["runtime_tree_sha256"][:12],
            (build["git_sha"] or "none")[:7], " dirty" if build["git_dirty"] else "",
        )
        # A process that starts with the tree already changed is stale from its
        # first claim. Say so at startup rather than at the first paid call.
        report = detect_drift(force=True)
        if report.drifted:
            self._note_drift(report)

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

    def _note_drift(self, report) -> None:
        """Record and act on a divergence between the loaded build and the disk.

        Refusing to claim is the whole point: on 2026-08-06 a stale process spent
        $0.76 on four cells whose prompt described an image it never sent, and the
        only reason anyone noticed was a manual `ps` against file mtimes. A worker
        that stops claiming turns that into an operator restarting a server.
        """
        if self._drift_halted:
            return
        self._drift_halted = True
        if drift_allowed():
            logger.warning(
                "runtime tree changed since this process loaded (%s); continuing "
                "because %s=1 — executions will be recorded with "
                "runtime_drift_detected=1",
                report.summary(), "LUNELLE_ALLOW_CODE_DRIFT",
            )
            return
        logger.error(
            "runtime tree changed since this process loaded (%s); refusing to claim "
            "further tasks. This process is running code that is no longer on disk: "
            "Python does not reload, so anything it renders would be attributed to "
            "the wrong build. Restart the service to pick up the change, or set "
            "LUNELLE_ALLOW_CODE_DRIFT=1 for a development run.",
            report.summary(),
        )

    def _may_claim(self) -> bool:
        """False when this process must not take new work.

        Checked BEFORE claiming rather than after: a task claimed and then abandoned
        has to be transitioned back out of `running`, and the simplest way not to
        need that is never to claim it.
        """
        if self._drift_halted:
            return drift_allowed()
        report = detect_drift()
        if report.drifted:
            self._note_drift(report)
            return drift_allowed()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._may_claim():
                # Idle rather than exit: the HTTP surface stays up, /ready reports
                # degraded, and the operator gets a diagnosable server instead of a
                # dead one.
                self._stop.wait(5)
                continue
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

    def _load_execution_plan(self, task: dict, active_profile: dict | None,
                             log) -> ExecutionPlan:
        """Everything this attempt will send, read from the snapshot and verified.

        This is the whole of C3. The worker used to re-derive its inputs here from
        the current style row, `app_settings` and the viewplan cache directory, while
        taking the prompt from the frozen `tasks.prompt` column. The two came from
        different moments and nothing compared them, so a prompt describing captioned
        tiles could ship with an uncaptioned image — which is what happened to four
        cells on 2026-08-06, at $0.76, producing output that was evidence for
        nothing.

        Now: one source. The snapshot names each input by role and digest, the store
        resolves it, and the bytes are re-hashed on the way in. Anything missing is a
        blocked task, never a substituted input.
        """
        record = get_snapshot(self.db, task["task_id"])
        if record is None:
            raise DependencyMissing(
                "this task has no input snapshot, so what it was queued to send "
                "cannot be established; re-queue it"
            )
        snapshot = record["snapshot"]

        # Both copies of the version are checked: the one inside the snapshot JSON
        # (covered by the input fingerprint) and the denormalised column. A real v1
        # row has both at 1; if they DISAGREE the record has been edited, which is
        # itself a reason not to execute it.
        versions = {snapshot.get("snapshot_version"), record.get("snapshot_version")}
        if versions != {SNAPSHOT_VERSION}:
            # A v1 snapshot tagged every input `kind` and none `role`, so which
            # image was Image 1 is not recoverable from it. Guessing is what this
            # work exists to stop.
            found = ", ".join(str(v) for v in sorted(versions, key=str))
            raise DependencyMissing(
                f"this task's snapshot is version {found}, and executing it needs "
                f"version {SNAPSHOT_VERSION} (inputs addressed by role and digest). "
                f"Re-queue the task to freeze current inputs."
            )

        unresolved = snapshot.get("unresolved_inputs") or []
        if unresolved:
            reasons = "; ".join(
                f"{entry.get('role', '?')}: {entry.get('reason', 'missing')}"
                for entry in unresolved
            )
            raise DependencyMissing(f"inputs were missing when this task was queued — {reasons}")

        # The channel is compared, not re-read into use. A profile row is edited in
        # place, so the same profile_id can point at a different endpoint with a
        # different key tomorrow; a queued task would then silently go somewhere its
        # snapshot never described.
        planned_channel = (snapshot.get("channel") or {}).get("channel_fingerprint")
        current_channel = channel_fingerprint(profile_snapshot(
            self.resolver.profiles.active_row()))
        if planned_channel and planned_channel != current_channel:
            raise DependencyMissing(
                f"the API channel changed after this task was queued "
                f"(queued against {planned_channel}, now {current_channel}). "
                f"Model, endpoint or key differs, so this task would not run on the "
                f"channel it was priced and planned for. Re-queue it."
            )

        try:
            resolved = load_all(self.db, snapshot.get("input_assets") or [])
        except InputUnavailable as exc:
            raise DependencyMissing(str(exc)) from exc

        # The one input that genuinely cannot be frozen: a wearing shot copies the
        # grid generated later in the same batch. Declared in the snapshot, resolved
        # here, and recorded in the manifest.
        deferred_roles: set[str] = set()
        for entry in snapshot.get("deferred_inputs") or []:
            role = entry.get("role", "")
            if role not in DEFERRABLE_ROLES:
                raise DependencyMissing(
                    f"snapshot defers input {role!r}, which must be frozen at queue "
                    f"time; re-queue this task"
                )
            grid = self._latest_grid_path(task)
            if grid is not None:
                resolved.append(role, grid)
                deferred_roles.add(role)
            else:
                log.info("no successful grid available; proceeding without it",
                         ctx={"stage": "reference"})

        size = snapshot.get("size") or list(self.config.wearing_size)
        return ExecutionPlan(
            snapshot=snapshot,
            fingerprint=record["input_fingerprint"],
            prompt=snapshot.get("prompt") or "",
            negative_prompt=snapshot.get("negative_prompt") or "",
            size=(int(size[0]), int(size[1])),
            inputs=resolved,
            deferred_roles=deferred_roles,
        )

    def _record_execution(self, task: dict, attempt_no: int, references: list[Path],
                          prompt: str, provider, manifest: dict,
                          plan: ExecutionPlan, *, matches: bool) -> None:
        """Persist the measured request. Never allowed to break generation."""
        try:
            record_execution(
                self.db, task["task_id"], attempt_no,
                resolved_assets=describe_inputs(self.db, self.config, references,
                                                kind="reference"),
                prompt_sent=prompt, model=task["model"], provider=provider.name,
                # Which code is sending this, and whether the tree had already moved
                # underneath it. Recorded before the call, like everything else here.
                build=stamp(
                    worker_instance=threading.current_thread().name,
                    drift=detect_drift(),
                ),
                request=manifest,
                snapshot_fingerprint=plan.fingerprint,
                matches_snapshot=matches,
            )
        except Exception:  # noqa: BLE001 - provenance must not block generation
            logger.exception("could not record execution provenance for %s",
                             task["task_id"])

    def _latest_grid_path(self, task: dict) -> Path | None:
        grid_task = self.service.latest_successful_grid(task["style_id"])
        if not grid_task or not grid_task.get("output_path"):
            return None
        path = Path(grid_task["output_path"])
        return path if path.is_file() else None

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
            plan = self._load_execution_plan(task, active_profile, log)
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
        resolved = plan.inputs
        references = resolved.paths
        prompt = plan.prompt
        expected_reference = (
            task["metadata"].get("use_reference")
            or task["metadata"].get("use_style_reference")
            or task["metadata"].get("use_hero_reference")
            or task["metadata"].get("use_matrix_reference")
        )
        prompt_transform = "none"
        if expected_reference and not references:
            # Text-only fallback: the stored prompt must not claim an Image 1 exists.
            # Only reachable for a genuinely deferred input (a wearing shot whose grid
            # does not exist yet); a matrix cell with no inputs is blocked above,
            # never degraded.
            #
            # DECLARED rather than silently allowed. The verifier re-applies this
            # exact transform to the snapshot's own prompt and compares hashes, so
            # naming a transform cannot excuse an arbitrary prompt.
            prompt = strip_reference_block(prompt)
            prompt_transform = "strip_reference_block"
            log.info("reference unavailable; stripped reference block from prompt",
                     ctx={"stage": "reference"})
        size = plan.size
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

        # Measure what is actually going out, from the bytes themselves. Built by
        # re-hashing each file at this boundary rather than by copying the snapshot's
        # digests forward — copying would make the comparison below prove only that
        # the snapshot equals itself.
        manifest = request_manifest(
            prompt=prompt, negative_prompt=task["negative_prompt"], size=size,
            model=task["model"], provider=provider.name, resolved=resolved,
            channel=channel_fingerprint(active_profile),
            prompt_transform=prompt_transform,
        )
        differences = compare_to_snapshot(plan.snapshot, manifest)
        # Written BEFORE the call either way: a blocked attempt is a record worth
        # keeping, and a crash mid-flight still leaves what went out.
        self._record_execution(task, attempt_no, references, prompt, provider,
                               manifest, plan, matches=not differences)
        if differences:
            # The precondition for spending money is that the plan and the request
            # are the same thing. They were not, so nothing is sent.
            detail = "; ".join(differences)
            log.error("request does not match the snapshot; refusing to call the provider",
                      ctx={"stage": "verify", "error_code": ERROR_SNAPSHOT_MISMATCH,
                           "status": detail[:200]})
            release_spend(self.db, task_id=task["task_id"], attempt_no=attempt_no)
            sync_lineage_spend(self.db, root_task_id)
            self.service.finish_attempt(
                task["task_id"], attempt_no, outcome="error", duration_ms=0,
                error_code=ERROR_SNAPSHOT_MISMATCH, error_message=detail,
                reference_used=bool(references),
            )
            self.service.complete_failure(
                task["task_id"], error_code=ERROR_SNAPSHOT_MISMATCH,
                error_message=(
                    f"what this attempt would send does not match what the task was "
                    f"queued to send: {detail}. Nothing was sent. Re-queue the task "
                    f"to freeze current inputs."
                ),
                retryable=False,
            )
            return

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
                # `resolved` — the exact inputs the provider received. QA used to
                # re-read style.plan_image_path and re-resolve the hand model, so it
                # judged the candidate against images the provider never saw: it
                # scored tk_47e7d1a54d 100 while the actual placement was 6/10,
                # because it was reading the uncaptioned plan.
                self._maybe_llm_qa(task, output_path, resolved, log)
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
                # The size the task was QUEUED to request, from its snapshot.
                # Recomputing it from current settings meant the aspect-ratio check
                # could be run against a size the attempt never asked for.
                size = self._snapshot_size(task)
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

    def _maybe_llm_qa(self, task: dict, output_path: Path,
                      resolved: ResolvedInputs, log) -> None:
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
        # Image 2 = the design authority THE PROVIDER RECEIVED, by role. Re-reading
        # style.plan_image_path here is what made the judge's verdict unusable as
        # evidence: it graded a candidate against a plan with no captions while the
        # provider had been sent a captioned view-plan (or, on 2026-08-06, the
        # reverse). Same bytes for both, or the verdict means nothing.
        authority = resolved.first("view_plan", "design_plan", "grid", "style_reference")
        if authority is not None:
            images.append(authority)
        # Image 3 = the immutable base hand photo, also as sent. Without it the judge
        # cannot see that the hand was stretched, which is the one defect a human
        # spots instantly. Only this view's nails are in frame, so pass that list too
        # rather than letting the judge assume 10.
        visible_nails: list[str] | None = None
        if task["output_type"] == OUTPUT_MATRIX_CELL:
            metadata = task.get("metadata") or {}
            view = metadata.get("view")
            base_hand = resolved.first("base_hand")
            if base_hand is not None:
                images.append(base_hand)
            if view:
                visible_nails = matrix_visible_nails(view)
        try:
            verdict = auto_qa_verdict(chat, images, style.get("identity_text") or "",
                                      task["output_type"], visible_nails=visible_nails)
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
        policy = task.get("metadata", {}).get("generation_policy") or {}
        if policy.get("allow_automatic_creative_repair") is False:
            log.info(
                "automatic creative repair disabled by Precision policy",
                ctx={"stage": "qa", "status": f"{kind}:manual_control"},
            )
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
                    mode=task.get("metadata", {}).get("generation_mode"),
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

    def _snapshot_size(self, task: dict) -> tuple[int, int]:
        """The size this task was queued to request.

        The only size the worker consults. It used to derive one here from the
        current `app_settings` hand model, so replacing a hand model changed the size
        an already-queued cell would request while the snapshot said otherwise. The
        configured fallback covers a task with no snapshot, which cannot execute
        anyway — `_load_execution_plan` blocks it.
        """
        record = get_snapshot(self.db, task["task_id"])
        if record:
            size = record["snapshot"].get("size")
            if isinstance(size, list) and len(size) == 2:
                return int(size[0]), int(size[1])
        if task["output_type"] == OUTPUT_GRID:
            return self.config.grid_size
        if task["output_type"] == OUTPUT_HERO:
            return self.config.hero_size
        return self.config.wearing_size

def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
