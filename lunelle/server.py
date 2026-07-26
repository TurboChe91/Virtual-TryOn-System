"""FastAPI application: REST API + minimal admin UI + health endpoints."""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, UnidentifiedImageError

from . import __version__
from .config import Config, load_config
from .db import Database, db_healthy, migrate
from .export import ExportError, run_export
from .logging_setup import setup_logging
from .models import OUTPUT_GRID, OUTPUT_HERO, OUTPUT_TYPES, STATUSES, IllegalTransition
from .profiles import ProfileService
from .providers import build_chat_fn, build_provider
from .qa import run_qa
from .schemas import (
    ExportRequest,
    GenerateRequest,
    IdentityRequest,
    ProfileCreateRequest,
    ProfileUpdateRequest,
    RetryRequest,
    ReviewRequest,
    StyleCreateRequest,
)
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
        app.state.profiles = ProfileService(db)
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
            # Drain: give in-flight provider calls time to finish and record
            # their results before connections are torn down.
            worker.stop(timeout=config.request_timeout_s + 30)
            if not worker.is_alive():
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

    @app.exception_handler(IllegalTransition)
    async def _illegal_transition(_req, exc):
        # A concurrent state change (e.g. worker claimed the task mid-request)
        # is a conflict, not a server fault.
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

    def _validated_upload(data: bytes) -> tuple[str, str]:
        """Shared image-upload validation: returns (format, extension)."""
        limit = config.max_upload_mb * 1024 * 1024
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
        return fmt, ALLOWED_UPLOAD_FORMATS[fmt]

    @app.post("/api/styles/{style_id}/reference-image",
              dependencies=[Depends(require_admin)])
    async def upload_reference(style_id: str, request: Request,
                               kind: str = Query("reference", pattern="^(reference|plan)$"),
                               file: UploadFile = File(...)):  # noqa: B008 - FastAPI dependency idiom
        """Store a style asset: kind=reference (photo/style ref) or kind=plan (2x5 set plan)."""
        service: TaskService = request.app.state.service
        service.get_style(style_id)  # 404 if missing
        data = await file.read(config.max_upload_mb * 1024 * 1024 + 1)
        fmt, ext = _validated_upload(data)
        # Server-generated name: user filenames never touch the filesystem.
        digest = hashlib.sha256(data).hexdigest()[:12]
        prefix = "plan" if kind == "plan" else "ref"
        dest = config.upload_dir / style_id / f"{prefix}-{digest}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if kind == "plan":
            service.set_plan_image(style_id, dest)
        else:
            service.set_reference_image(style_id, dest)
        return {"stored": str(dest.name), "kind": kind, "format": fmt, "bytes": len(data)}

    @app.post("/api/styles/from-image", status_code=201, dependencies=[Depends(require_admin)])
    async def create_style_from_image(request: Request,
                                      file: UploadFile = File(...),  # noqa: B008 - FastAPI idiom
                                      sku: str = Query("", max_length=48)):
        """Customer flow B: upload a design photo, let the vision LLM draft the style."""
        from . import vocab
        from .llm import LLMUnavailable, build_llm_chat, style_fields_from_image

        service: TaskService = request.app.state.service
        data = await file.read(config.max_upload_mb * 1024 * 1024 + 1)
        _fmt, ext = _validated_upload(data)
        digest = hashlib.sha256(data).hexdigest()[:12]
        staging = config.upload_dir / "_incoming" / f"style-{digest}{ext}"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(data)

        try:
            chat = build_llm_chat(config, request.app.state.db)
        except LLMUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            fields = style_fields_from_image(chat, staging)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"识别失败: {exc}") from exc
        # Vocab enums are suggestions from the model; invalid ones fall back to defaults.
        if fields.get("shape") not in vocab.SHAPES:
            fields.pop("shape", None)
        if fields.get("length") not in vocab.LENGTHS:
            fields.pop("length", None)

        body = StyleCreateRequest(sku=sku or None, **fields)
        outcome = build_style_spec(body, service.taken_skus())
        style = service.create_style(
            outcome.spec, source_type="hybrid",
            source_input={"from_image": True, **body.model_dump()},
            parser="vision-llm", warnings=outcome.warnings,
        )
        dest = config.upload_dir / style["style_id"] / f"ref-{digest}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(dest)
        service.set_reference_image(style["style_id"], dest)
        style = service.get_style(style["style_id"])
        return {"style": style, "recognized": fields, "warnings": outcome.warnings,
                "parser": "vision-llm"}

    @app.post("/api/styles/{style_id}/identify", dependencies=[Depends(require_admin)])
    def identify_style(style_id: str, request: Request):
        """Vision LLM writes the ten-nail identity text from the plan (or reference) image."""
        from .llm import LLMUnavailable, build_llm_chat, identify_nail_identities

        service: TaskService = request.app.state.service
        style = service.get_style(style_id)
        source = None
        for key in ("plan_image_path", "reference_image_path"):
            candidate = Path(style.get(key) or "")
            if candidate.is_file():
                source = candidate
                break
        if source is None:
            raise HTTPException(status_code=409,
                                detail="style has no plan or reference image; upload one first")
        try:
            chat = build_llm_chat(config, request.app.state.db)
        except LLMUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            identity = identify_nail_identities(chat, source)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"识别失败: {exc}") from exc
        service.set_identity_text(style_id, identity)
        return {"style_id": style_id, "identity_text": identity,
                "source": source.name}

    @app.put("/api/styles/{style_id}/identity", dependencies=[Depends(require_admin)])
    def set_identity(style_id: str, body: IdentityRequest, request: Request):
        service: TaskService = request.app.state.service
        service.get_style(style_id)
        service.set_identity_text(style_id, body.identity_text)
        return {"style_id": style_id, "identity_set": bool(body.identity_text.strip())}

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

    @app.post("/api/tasks/{task_id}/correct", status_code=202,
              dependencies=[Depends(require_admin)])
    async def correct_task(task_id: str, request: Request,
                           correction: str = Form(..., min_length=4, max_length=2000),
                           owner_override: str = Form(""),
                           file: UploadFile | None = File(None)):  # noqa: B008 - FastAPI idiom
        """SOP v2+: locked-base local edit with an explicit correction; optional
        single-nail detail reference attached as the final image."""
        service: TaskService = request.app.state.service
        details: list[Path] = []
        if file is not None and file.filename:
            data = await file.read(config.max_upload_mb * 1024 * 1024 + 1)
            _fmt, ext = _validated_upload(data)
            source = service.get_task(task_id, with_details=False)
            digest = hashlib.sha256(data).hexdigest()[:12]
            dest = config.upload_dir / source["style_id"] / f"detail-{digest}{ext}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            details.append(dest)
        try:
            task = service.create_correction(
                task_id, correction_text=correction,
                detail_references=details, owner_override=owner_override,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"task_id": task["task_id"], "version": task["metadata"]["version"],
                "corrects": task_id}

    @app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_admin)])
    def cancel_task(task_id: str, request: Request):
        return request.app.state.service.cancel(task_id)

    @app.post("/api/tasks/{task_id}/qa", dependencies=[Depends(require_admin)])
    def rerun_qa(task_id: str, request: Request):
        service: TaskService = request.app.state.service
        task = service.get_task(task_id, with_details=False)
        if task["status"] != "success" or not task.get("output_path"):
            raise ConflictError("QA can only run on successful tasks with an output image")
        if task["output_type"] == OUTPUT_GRID:
            size = config.grid_size
        elif task["output_type"] == OUTPUT_HERO:
            size = config.hero_size
        else:
            size = config.wearing_size
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

    @app.post("/api/tasks/{task_id}/review", dependencies=[Depends(require_admin)])
    def review_task(task_id: str, body: ReviewRequest, request: Request):
        """Record the human review verdict for a successful task's latest QA result."""
        service: TaskService = request.app.state.service
        task = service.get_task(task_id)
        if task["status"] != "success":
            raise ConflictError("only successful tasks can be reviewed")
        if task["qa"] is None:
            raise ConflictError("task has no QA result to review; run QA first")
        from .db import transaction, utcnow
        db: Database = request.app.state.db
        conn = db.conn()
        with transaction(conn):
            conn.execute(
                "UPDATE qa_results SET needs_human_review = ? WHERE qa_id = ?",
                (0 if body.approved else 1, task["qa"]["qa_id"]),
            )
        metadata = task["metadata"]
        metadata.setdefault("reviews", []).append(
            {"at": utcnow(), "approved": body.approved, "note": body.note}
        )
        with transaction(conn):
            import json as _json
            conn.execute(
                "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE task_id = ?",
                (_json.dumps(metadata, ensure_ascii=False), utcnow(), task_id),
            )
        return {"task_id": task_id, "approved": body.approved,
                "needs_human_review": not body.approved}

    # ---------------- API channel profiles ----------------

    @app.get("/api/profiles")
    def list_profiles(request: Request, kind: str | None = Query(None, pattern="^(image|llm)$")):
        profiles: ProfileService = request.app.state.profiles
        return {"profiles": profiles.list_profiles(kind=kind),
                "env_fallback_model": config.image_model,
                "env_fallback_llm": config.text_model or None}

    @app.post("/api/profiles", status_code=201, dependencies=[Depends(require_admin)])
    def create_profile(body: ProfileCreateRequest, request: Request):
        profiles: ProfileService = request.app.state.profiles
        try:
            return profiles.create(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/profiles/{profile_id}", dependencies=[Depends(require_admin)])
    def update_profile(profile_id: str, body: ProfileUpdateRequest, request: Request):
        profiles: ProfileService = request.app.state.profiles
        try:
            return profiles.update(profile_id, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/profiles/{profile_id}", status_code=204,
                dependencies=[Depends(require_admin)])
    def delete_profile(profile_id: str, request: Request):
        request.app.state.profiles.delete(profile_id)

    @app.post("/api/profiles/{profile_id}/activate", dependencies=[Depends(require_admin)])
    def activate_profile(profile_id: str, request: Request):
        return request.app.state.profiles.activate(profile_id)

    @app.post("/api/profiles/deactivate", dependencies=[Depends(require_admin)])
    def deactivate_profiles(request: Request,
                            kind: str | None = Query(None, pattern="^(image|llm)$")):
        request.app.state.profiles.deactivate_all(kind=kind)
        return {"active": None, "fallback": "environment configuration"}

    @app.post("/api/profiles/{profile_id}/test", dependencies=[Depends(require_admin)])
    def test_profile(profile_id: str, request: Request):
        """Cheap connectivity + auth probe: GET {base_url}/models, no image cost."""
        import httpx

        profiles: ProfileService = request.app.state.profiles
        row = profiles._get_row(profile_id)  # noqa: SLF001 - server needs the raw key once
        try:
            response = httpx.get(
                f"{row['base_url']}/models",
                headers={"Authorization": f"Bearer {row['api_key']}"},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "stage": "transport", "detail": str(exc)[:200]}
        if response.status_code != 200:
            return {"ok": False, "stage": "auth_or_endpoint",
                    "http_status": response.status_code,
                    "detail": response.text[:200]}
        model_present = None
        try:
            ids = [m.get("id") for m in response.json().get("data", [])]
            model_present = row["model"] in ids
        except (ValueError, AttributeError):
            ids = []
        return {"ok": True, "http_status": 200, "models_listed": len(ids),
                "configured_model_present": model_present}

    # ---------------- hand models (per skin tone) ----------------

    SKIN_TONES = ("light", "medium", "tan", "deep")

    def _hand_model_setting(conn, tone: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (f"hand_model_{tone}",)
        ).fetchone()
        return row["value"] if row else None

    @app.get("/api/settings/hand-models")
    def hand_models(request: Request):
        conn = request.app.state.db.conn()
        out = {}
        for tone in SKIN_TONES:
            path = _hand_model_setting(conn, tone)
            out[tone] = {"configured": bool(path and Path(path).is_file())}
        return {"hand_models": out}

    @app.post("/api/settings/hand-models/{tone}", dependencies=[Depends(require_admin)])
    async def upload_hand_model(tone: str, request: Request,
                                file: UploadFile = File(...)):  # noqa: B008 - FastAPI idiom
        if tone not in SKIN_TONES:
            raise HTTPException(status_code=422, detail=f"tone must be one of {SKIN_TONES}")
        data = await file.read(config.max_upload_mb * 1024 * 1024 + 1)
        fmt, ext = _validated_upload(data)
        dest = config.upload_dir / "hand-models" / f"{tone}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        from .db import transaction, utcnow
        conn = request.app.state.db.conn()
        with transaction(conn):
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (f"hand_model_{tone}", str(dest), utcnow()),
            )
        return {"tone": tone, "stored": dest.name, "format": fmt, "bytes": len(data)}

    @app.get("/api/settings/hand-models/{tone}/image")
    def hand_model_image(tone: str, request: Request):
        if tone not in SKIN_TONES:
            raise HTTPException(status_code=422, detail=f"tone must be one of {SKIN_TONES}")
        path_text = _hand_model_setting(request.app.state.db.conn(), tone)
        if not path_text or not Path(path_text).is_file():
            raise HTTPException(status_code=404, detail="hand model not configured")
        path = Path(path_text).resolve()
        if not path.is_relative_to(config.upload_dir.resolve()):
            raise HTTPException(status_code=403, detail="path outside storage root")
        return FileResponse(path)

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

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page():
        html_path = Path(__file__).parent / "web" / "settings.html"
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
