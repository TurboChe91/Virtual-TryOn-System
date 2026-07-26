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

from .config import Config
from .db import Database
from .logging_setup import task_logger
from .models import OUTPUT_GRID, OUTPUT_HERO, OUTPUT_WEARING
from .profiles import ProviderResolver
from .prompts import strip_reference_block
from .providers import GenerationRequest, ImageProvider, ProviderError
from .qa import run_qa, store_qa_result
from .tasks import TaskService

logger = logging.getLogger(__name__)

IDLE_POLL_S = 1.0


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
        references = self._resolve_references(task, log)
        prompt = task["prompt"]
        expected_reference = (
            task["metadata"].get("use_reference")
            or task["metadata"].get("use_style_reference")
            or task["metadata"].get("use_hero_reference")
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

        started = time.monotonic()
        try:
            result = provider.generate(request)
        except ProviderError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
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

        # QA never breaks the task result; failures are logged and stored.
        try:
            grid_path = None
            if task["output_type"] == OUTPUT_WEARING:
                grid_task = self.service.latest_successful_grid(task["style_id"])
                if grid_task and grid_task.get("output_path"):
                    grid_path = Path(grid_task["output_path"])
            qa_doc = run_qa(
                output_type=task["output_type"],
                image_path=output_path,
                expected_size=size,
                min_side=min(self.config.qa_min_side, min(size)),
                grid_image_path=grid_path,
            )
            store_qa_result(self.db, task["task_id"], qa_doc)
            log.info(
                "qa recorded",
                ctx={"stage": "qa", "status": "pass" if qa_doc["passed"] else "fail"},
            )
        except Exception:  # noqa: BLE001 - QA is advisory
            log.warning("qa run crashed; result not recorded", ctx={"stage": "qa"})
            logger.exception("qa failure detail")

    def _size_for(self, output_type: str) -> tuple[int, int]:
        if output_type == OUTPUT_GRID:
            return self.config.grid_size
        if output_type == OUTPUT_HERO:
            return self.config.hero_size
        return self.config.wearing_size

    def _resolve_references(self, task: dict, log) -> list[Path]:
        if task["output_type"] == OUTPUT_HERO:
            return self._resolve_hero_references(task, log)
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
        plan: Path | None = None
        uploaded_plan = Path(style.get("plan_image_path") or "")
        if uploaded_plan.is_file():
            plan = uploaded_plan
        else:
            grid_task = self.service.latest_successful_grid(task["style_id"])
            if grid_task and grid_task.get("output_path"):
                grid_path = Path(grid_task["output_path"])
                if grid_path.is_file():
                    plan = grid_path
        if plan is None:
            log.info("no plan image or grid available; generating hero text-only",
                     ctx={"stage": "reference"})
            return []
        references = [plan]
        photo_ref = Path(style.get("reference_image_path") or "")
        if photo_ref.is_file():
            references.append(photo_ref)
        return references


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
