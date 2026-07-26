"""FastAPI application: REST API + minimal admin UI + health endpoints."""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, UnidentifiedImageError

from . import __version__
from .config import Config, load_config
from .db import Database, db_healthy, migrate
from .export import ExportError, run_export
from .logging_setup import setup_logging
from .models import OUTPUT_TYPES, STATUSES
from .providers import build_chat_fn, build_provider
from .qa import run_qa
from .schemas import ExportRequest, GenerateRequest, RetryRequest, StyleCreateRequest
from .stats import collect_stats
from .styles import StyleInputError, build_style_spec
from .tasks import ConflictError, NotFoundError, TaskService
from .worker import Worker

logger = logging.getLogger(__name__)

ALLOWED_UPLOAD_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


def create_app(config: Config | None = None, *, start_worker: bool = True) -> FastAPI:
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(config.log_dir, config.log_level)
        problems = config.validate_for_serve()
        if problems:
            raise RuntimeError("configuration invalid:\n- " + "\n- ".join(problems))
        config.ensure_dirs()
        db = Database(config.db_path)
        migrate(db.conn())
        service = TaskService(db, config)
        provider = build_provider(config)
        worker = Worker(config, db, service, provider)
        app.state.config = config
        app.state.db = db
        app.state.service = service
        app.state.worker = worker
        if start_worker and os.environ.get("LUNELLE_DISABLE_WORKER") != "1":
            worker.start()
        logger.info(
            "lunelle studio started",
            extra={"ctx": {"stage": "startup", "provider": config.image_provider,
                            "model": config.image_model}},
        )
        try:
            yield
        finally:
            worker.stop()
            db.close_all()

    app = FastAPI(title="Lunelle Studio", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if not config.is_production else None,
                  redoc_url=None)

    # ---------------- auth & errors ----------------

    def require_admin(request: Request) -> None:
        token = config.admin_token
        if not token:
            return
        supplied = request.headers.get("x-admin-token", "")
        if not supplied or not _constant_time_eq(supplied, token):
            raise HTTPException(status_code=401, detail="missing or invalid X-Admin-Token")

    @app.exception_handler(NotFoundError)
    async def _not_found(_req, exc):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_req, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(StyleInputError)
    async def _bad_style(_req, exc):
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(Exception)
    async def _internal(_req, exc):
        error_id = uuid.uuid4().hex[:12]
        logger.exception("unhandled error id=%s", error_id)
        body = {"error": "internal server error", "error_id": error_id}
        if config.debug:
            body["detail"] = str(exc)
        return JSONResponse(status_code=500, content=body)

    # ---------------- health ----------------

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    def ready(request: Request):
        db: Database = request.app.state.db
        worker: Worker = request.app.state.worker
        checks = {
            "database": db_healthy(db.conn()),
            "output_dir_writable": _writable(config.output_dir),
            "data_dir_writable": _writable(config.data_dir),
            "config_valid": not config.validate_for_serve(),
            "worker_alive": worker.is_alive() or os.environ.get("LUNELLE_DISABLE_WORKER") == "1",
        }
        ok = all(checks.values())
        return JSONResponse(status_code=200 if ok else 503,
                            content={"status": "ready" if ok else "degraded", "checks": checks})

    # ---------------- styles ----------------

    @app.post("/api/styles", status_code=201, dependencies=[Depends(require_admin)])
    def create_style(body: StyleCreateRequest, request: Request):
        service: TaskService = request.app.state.service
        chat_fn = build_chat_fn(config) if body.use_llm else None
        outcome = build_style_spec(body, service.taken_skus(), chat_fn=chat_fn)
        style = service.create_style(
            outcome.spec,
            source_type=outcome.source_type,
            source_input=body.model_dump(),
            parser=outcome.parser,
            warnings=outcome.warnings,
        )
        bundle = service.prompt_bundle_for(style)
        return {
            "style": style,
            "warnings": outcome.warnings,
            "parser": outcome.parser,
            "prompts_preview": {
                "prompt_version": bundle.prompt_version,
                "grid_prompt": bundle.grid_prompt,
                "wearing_prompt": bundle.wearing_prompt,
                "negative_prompt": bundle.negative_prompt,
                "quality_requirements": bundle.quality_requirements,
            },
        }

    @app.get("/api/styles")
    def list_styles(request: Request, limit: int = Query(200, ge=1, le=500),
                    offset: int = Query(0, ge=0)):
        return {"styles": request.app.state.service.list_styles(limit=limit, offset=offset)}

    @app.get("/api/styles/{style_id}")
    def get_style(style_id: str, request: Request):
        service: TaskService = request.app.state.service
        style = service.get_style(style_id)
        bundle = service.prompt_bundle_for(style)
        return {
            "style": style,
            "prompts": {
                "prompt_version": bundle.prompt_version,
                "grid_prompt": bundle.grid_prompt,
                "wearing_prompt": bundle.wearing_prompt,
                "negative_prompt": bundle.negative_prompt,
                "quality_requirements": bundle.quality_requirements,
            },
        }

    @app.post("/api/styles/{style_id}/reference-image",
              dependencies=[Depends(require_admin)])
    async def upload_reference(style_id: str, request: Request,
                               file: UploadFile = File(...)):  # noqa: B008 - FastAPI dependency idiom
        service: TaskService = request.app.state.service
        service.get_style(style_id)  # 404 if missing

        limit = config.max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(status_code=413,
                                detail=f"file exceeds {config.max_upload_mb}MB limit")
        if len(data) < 100:
            raise HTTPException(status_code=422, detail="file too small to be an image")
        try:
            import io
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
                fmt = probe.format
        except (UnidentifiedImageError, OSError) as exc:
            raise HTTPException(status_code=422, detail="file is not a valid image") from exc
        if fmt not in ALLOWED_UPLOAD_FORMATS:
            raise HTTPException(status_code=422,
                                detail=f"unsupported format {fmt}; allowed: PNG, JPEG, WEBP")

        # Server-generated name: user filenames never touch the filesystem.
        digest = hashlib.sha256(data).hexdigest()[:12]
        dest = config.upload_dir / style_id / f"ref-{digest}{ALLOWED_UPLOAD_FORMATS[fmt]}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        service.set_reference_image(style_id, dest)
        return {"stored": str(dest.name), "format": fmt, "bytes": len(data)}

    # ---------------- generation & tasks ----------------

    @app.post("/api/styles/{style_id}/generate", status_code=202,
              dependencies=[Depends(require_admin)])
    def generate(style_id: str, body: GenerateRequest, request: Request):
        plan = request.app.state.service.create_generation(
            style_id, body.output_types, force=body.force, note=body.note
        )
        return {"batch_id": plan.batch_id, "created": plan.created,
                "reused": plan.reused, "skipped": plan.skipped}

    @app.get("/api/tasks")
    def list_tasks(request: Request,
                   sku: str | None = None,
                   status: str | None = Query(None, pattern="^(" + "|".join(STATUSES) + ")$"),
                   output_type: str | None = Query(None, pattern="^(" + "|".join(OUTPUT_TYPES) + ")$"),
                   batch_id: str | None = None,
                   limit: int = Query(100, ge=1, le=500),
                   offset: int = Query(0, ge=0)):
        tasks = request.app.state.service.list_tasks(
            sku=sku, status=status, output_type=output_type, batch_id=batch_id,
            limit=limit, offset=offset,
        )
        return {"tasks": tasks}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, request: Request):
        return request.app.state.service.get_task(task_id)

    @app.get("/api/tasks/{task_id}/image")
    def task_image(task_id: str, request: Request):
        task = request.app.state.service.get_task(task_id, with_details=False)
        if not task.get("output_path"):
            raise HTTPException(status_code=404, detail="task has no output image")
        path = Path(task["output_path"]).resolve()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="output file missing on disk")
        if not path.is_relative_to(config.output_dir.resolve()):
            raise HTTPException(status_code=403, detail="output path outside storage root")
        return FileResponse(path)

    @app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(require_admin)])
    def retry_task(task_id: str, body: RetryRequest, request: Request):
        return request.app.state.service.manual_retry(task_id, note=body.note)

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_admin)])
    def cancel_task(task_id: str, request: Request):
        return request.app.state.service.cancel(task_id)

    @app.post("/api/tasks/{task_id}/qa", dependencies=[Depends(require_admin)])
    def rerun_qa(task_id: str, request: Request):
        service: TaskService = request.app.state.service
        task = service.get_task(task_id, with_details=False)
        if task["status"] != "success" or not task.get("output_path"):
            raise ConflictError("QA can only run on successful tasks with an output image")
        size = config.grid_size if task["output_type"] == "grid" else config.wearing_size
        grid_path = None
        if task["output_type"] == "wearing":
            grid_task = service.latest_successful_grid(task["style_id"])
            if grid_task and grid_task.get("output_path"):
                grid_path = Path(grid_task["output_path"])
        qa_doc = run_qa(output_type=task["output_type"], image_path=Path(task["output_path"]),
                        expected_size=size, min_side=min(config.qa_min_side, min(size)),
                        grid_image_path=grid_path)
        from .qa import store_qa_result
        store_qa_result(request.app.state.db, task_id, qa_doc)
        return qa_doc

    # ---------------- batches / stats / export ----------------

    @app.get("/api/batches")
    def list_batches(request: Request, limit: int = Query(50, ge=1, le=200)):
        return {"batches": request.app.state.service.list_batches(limit=limit)}

    @app.get("/api/stats")
    def stats(request: Request):
        return collect_stats(request.app.state.db)

    @app.post("/api/export", dependencies=[Depends(require_admin)])
    def export(body: ExportRequest, request: Request):
        try:
            return run_export(request.app.state.db, config,
                              skus=body.skus or None,
                              include_unreviewed=body.include_unreviewed)
        except ExportError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---------------- admin UI ----------------

    @app.get("/", response_class=HTMLResponse)
    def index():
        html_path = Path(__file__).parent / "web" / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    return app


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".probe-{uuid.uuid4().hex[:8]}"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())
