"""FastAPI application: REST API + minimal admin UI + health endpoints."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, UnidentifiedImageError

from . import __version__
from .assets import assets_root, get_asset, store_bytes
from .budget import BudgetExceeded, spend_snapshot
from .build import describe as describe_build
from .build import detect_drift
from .config import Config, load_config
from .db import Database, db_healthy, migrate
from .errors import CostConfirmationRequired
from .export import ExportError, run_export
from .logging_setup import redact, setup_logging
from .models import (
    OUTPUT_GRID,
    OUTPUT_HERO,
    OUTPUT_TYPES,
    QA_STATES,
    REVIEW_STATES,
    STATUSES,
    IllegalReviewTransition,
    IllegalTransition,
)
from .profiles import ProfileService
from .providers import build_chat_fn, build_provider
from .qa import run_qa
from .schemas import (
    CloudflareConfigRequest,
    ExportRequest,
    GenerateRequest,
    IdentityRequest,
    ManualSplitRequest,
    MatrixRequest,
    ProfileCreateRequest,
    ProfileUpdateRequest,
    RetryRequest,
    ReviewRequest,
    SplitReviewRequest,
    StyleCreateRequest,
    TryonIdRequest,
)
from .snapshots import find_by_fingerprint, get_executions, get_snapshot
from .splits import SplitService
from .stats import collect_stats
from .styles import StyleInputError, build_style_spec
from .tasks import ConflictError, NotFoundError, TaskService
from .urlguard import UnsafeUrl, assert_safe_request_url, configure_allow_private_hosts
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
        # Publish the private-host policy to the outbound request guard before
        # anything can make a request. Defaults to blocking, so this only ever
        # widens the policy deliberately.
        configure_allow_private_hosts(config.allow_private_api_hosts)
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
        app.state.profiles = ProfileService(
            db, allow_private_hosts=config.allow_private_api_hosts
        )
        if start_worker and os.environ.get("LUNELLE_DISABLE_WORKER") != "1":
            worker.start()
        # The RESOLVED channel, not the env default. This log used to print
        # config.image_model ('doubao-seedream-4-5-251128') while the active profile
        # was actually sending to gpt-image-2, so the one line an operator checks at
        # startup named a model the process never used. Read from the profile row
        # directly: building a provider here would validate a URL and can fail.
        active = app.state.profiles.active_row()
        build = describe_build()
        logger.info(
            "lunelle studio started",
            extra={"ctx": {
                "stage": "startup",
                "provider": "openai-compat" if active else config.image_provider,
                "model": active["model"] if active else config.image_model,
                "status": f"build={build['build_id']}",
            }},
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

    def _reviewer_from(request: Request) -> str:
        """Best-effort reviewer identity for the audit trail.

        There is no user system yet, so record the admin-token fingerprint (never
        the token) plus the client host. Enough to tell two operators apart in an
        audit without storing a credential.
        """
        supplied = request.headers.get("x-admin-token", "")
        if supplied:
            digest = hashlib.sha256(supplied.encode()).hexdigest()[:8]
            return f"token:{digest}"
        client = request.client.host if request.client else "unknown"
        return f"host:{client}"

    @app.exception_handler(NotFoundError)
    async def _not_found(_req, exc):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.exception_handler(IllegalReviewTransition)
    async def _illegal_review(_req, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(CostConfirmationRequired)
    async def _needs_confirmation(_req, exc):
        # 409 + the estimate: the client is expected to show it and re-submit
        # with confirm_max_usd, not to retry blindly.
        return JSONResponse(
            status_code=409,
            content={"error": str(exc), "code": "cost_confirmation_required",
                     "estimate": exc.estimate},
        )

    @app.exception_handler(BudgetExceeded)
    async def _budget_exceeded(_req, exc):
        # 429: the request is well-formed and authorized, but the spend cap for
        # this window is used up. Retrying later (or raising the cap) is correct.
        return JSONResponse(
            status_code=429,
            content={"error": str(exc), "code": "budget_exceeded",
                     "budget": exc.snapshot.as_dict()},
        )

    @app.exception_handler(UnsafeUrl)
    async def _unsafe_url(_req, exc):
        return JSONResponse(status_code=422,
                            content={"error": str(exc), "code": "unsafe_url"})

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
        # build is here rather than only in /ready because it must be readable
        # BEFORE a paid run, from a probe that needs no admin token: "is the process
        # running the code I think it is" was the question nobody could answer.
        return {"status": "ok", "version": __version__, "build": describe_build()}

    @app.get("/ready")
    def ready(request: Request):
        db: Database = request.app.state.db
        worker: Worker = request.app.state.worker
        drift = detect_drift()
        checks = {
            "database": db_healthy(db.conn()),
            "output_dir_writable": _writable(config.output_dir),
            "data_dir_writable": _writable(config.data_dir),
            "config_valid": not config.validate_for_serve(),
            "worker_alive": worker.is_alive() or os.environ.get("LUNELLE_DISABLE_WORKER") == "1",
            # Degraded, not failed: the API still serves and the queue is intact.
            # The worker has stopped claiming, which is a state an operator must see
            # in the readiness probe rather than infer from an idle queue.
            "runtime_build_current": not drift.drifted,
        }
        ok = all(checks.values())
        content = {"status": "ready" if ok else "degraded", "checks": checks,
                   "build": describe_build()}
        if drift.drifted:
            content["runtime_drift"] = drift.summary()
        return JSONResponse(status_code=200 if ok else 503, content=content)

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

    @app.get("/api/assets/{digest}")
    def asset_image(digest: str, request: Request):
        """Serve an immutable derived preview/crop by its full content digest."""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(status_code=404, detail="asset not found")
        record = get_asset(request.app.state.db, digest)
        if record is None:
            raise HTTPException(status_code=404, detail="asset not found")
        path = Path(record["path"]).resolve()
        if not path.is_file():
            raise HTTPException(status_code=404, detail="asset bytes missing")
        if not path.is_relative_to(config.asset_dir.resolve()):
            raise HTTPException(status_code=403, detail="asset path outside storage root")
        return FileResponse(path, media_type=record["mime_type"])

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

        # A new plan supersedes any identity derived from an older image, so refresh
        # it here rather than leaving a stale per-nail contract pointing at a design
        # that is no longer the authority.
        identity_derived = False
        identity_error = None
        split = None
        split_error = None
        if kind == "plan":
            try:
                split = SplitService(request.app.state.db, config).analyze(
                    style_id, created_by=_reviewer_from(request)
                )
            except (ConflictError, ValueError) as exc:
                split_error = str(exc)[:300]
            try:
                from .llm import (
                    LLMUnavailable,
                    build_llm_chat,
                    identify_nail_identities,
                )

                chat = build_llm_chat(config, request.app.state.db)
                service.set_identity_text(style_id, identify_nail_identities(chat, dest))
                identity_derived = True
            except LLMUnavailable:
                identity_error = "no LLM channel configured"
            except (RuntimeError, ValueError) as exc:
                identity_error = str(exc)[:200]
            if identity_error:
                logger.warning("identity derivation failed for %s: %s",
                               style_id, identity_error)
        return {"stored": str(dest.name), "kind": kind, "format": fmt,
                "bytes": len(data), "identity_derived": identity_derived,
                "identity_error": identity_error, "split": split,
                "split_error": split_error}

    @app.post("/api/styles/from-image", status_code=201, dependencies=[Depends(require_admin)])
    async def create_style_from_image(request: Request,
                                      file: UploadFile = File(...),  # noqa: B008 - FastAPI idiom
                                      sku: str = Query("", max_length=48),
                                      kind: str = Query("plan",
                                                        pattern="^(reference|plan)$")):
        """Customer flow B: upload a design photo, let the vision LLM draft the style.

        `kind` defaults to plan because the usual upload IS the 2x5 set plan, and the
        plan is the design authority every downstream step reads. Pass
        kind=reference for a worn photo.
        """
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
            source_input={"from_image": True, "uploaded_as": kind, **body.model_dump()},
            parser="vision-llm", warnings=outcome.warnings,
        )
        prefix = "plan" if kind == "plan" else "ref"
        dest = config.upload_dir / style["style_id"] / f"{prefix}-{digest}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(dest)
        if kind == "plan":
            service.set_plan_image(style["style_id"], dest)
        else:
            service.set_reference_image(style["style_id"], dest)

        # Per-nail identity is a precondition for the matrix getting nail order right,
        # and it is derivable from the image we already have. Deriving it on demand
        # rather than leaving it to a remembered manual step is the difference between
        # "distribute these elements tastefully" and an actual per-nail contract.
        identity_error = None
        try:
            from .llm import identify_nail_identities

            identity = identify_nail_identities(chat, dest)
            service.set_identity_text(style["style_id"], identity)
        except (RuntimeError, ValueError) as exc:
            # Non-fatal: the style exists and is editable. Say so rather than
            # pretending the style is fully specified.
            identity_error = str(exc)[:200]
            logger.warning("identity derivation failed for %s: %s",
                           style["style_id"], identity_error)

        style = service.get_style(style["style_id"])
        split = None
        split_error = None
        if kind == "plan":
            try:
                split = SplitService(request.app.state.db, config).analyze(
                    style["style_id"], created_by=_reviewer_from(request)
                )
            except (ConflictError, ValueError) as exc:
                split_error = str(exc)[:300]
        return {"style": style, "recognized": fields, "warnings": outcome.warnings,
                "parser": "vision-llm", "uploaded_as": kind,
                "identity_derived": identity_error is None,
                "identity_error": identity_error, "split": split,
                "split_error": split_error}

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

    # ---------------- plan split revisions ----------------

    @app.get("/api/styles/{style_id}/splits", dependencies=[Depends(require_admin)])
    def list_splits(style_id: str, request: Request):
        revisions = SplitService(request.app.state.db, config).list(style_id)
        return {"style_id": style_id, "revisions": revisions}

    @app.post("/api/styles/{style_id}/splits/analyze",
              dependencies=[Depends(require_admin)])
    def analyze_split(style_id: str, request: Request):
        return SplitService(request.app.state.db, config).analyze(
            style_id, created_by=_reviewer_from(request)
        )

    @app.post("/api/styles/{style_id}/splits/manual", status_code=201,
              dependencies=[Depends(require_admin)])
    def manual_split(style_id: str, body: ManualSplitRequest, request: Request):
        boxes = {item.nail_id: item.bbox for item in body.boxes}
        try:
            return SplitService(request.app.state.db, config).create_manual(
                style_id, boxes, created_by=_reviewer_from(request)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/splits/{revision_id}/review", dependencies=[Depends(require_admin)])
    def review_split(revision_id: str, body: SplitReviewRequest, request: Request):
        splitter = SplitService(request.app.state.db, config)
        reviewer = _reviewer_from(request)
        return (
            splitter.approve(revision_id, reviewer=reviewer)
            if body.approved else splitter.reject(revision_id, reviewer=reviewer)
        )

    # ---------------- generation & tasks ----------------

    @app.post("/api/styles/{style_id}/generate", status_code=202,
              dependencies=[Depends(require_admin)])
    def generate(style_id: str, body: GenerateRequest, request: Request):
        plan = request.app.state.service.create_generation(
            style_id, body.output_types, force=body.force, note=body.note,
            mode=body.mode,
        )
        return {"batch_id": plan.batch_id, "created": plan.created,
                "reused": plan.reused, "skipped": plan.skipped}

    @app.put("/api/styles/{style_id}/tryon-id", dependencies=[Depends(require_admin)])
    def set_tryon_id(style_id: str, body: TryonIdRequest, request: Request):
        import re as _re

        service: TaskService = request.app.state.service
        service.get_style(style_id)
        if body.tryon_style_id and not _re.fullmatch(r"\d{3}", body.tryon_style_id):
            raise HTTPException(status_code=422, detail="try-on id must be 3 digits, e.g. 001")
        try:
            service.set_tryon_id(style_id, body.tryon_style_id)
        except Exception as exc:  # noqa: BLE001 - unique index collision
            raise HTTPException(status_code=409,
                                detail="该编号已被其他款式占用") from exc
        return {"style_id": style_id, "tryon_style_id": body.tryon_style_id or None}

    @app.get("/api/styles/{style_id}/publish-readiness")
    def publish_readiness(style_id: str, request: Request):
        from .cloudflare import load_cf_config
        from .publish import collect_publishable_cells

        service: TaskService = request.app.state.service
        style = service.get_style(style_id)
        ready, excluded = collect_publishable_cells(request.app.state.db, style_id)
        return {
            "tryon_style_id": style.get("tryon_style_id"),
            "cloudflare_configured": load_cf_config(request.app.state.db) is not None,
            "ready_cells": len(ready),
            "excluded_cells": excluded,
        }

    @app.post("/api/styles/{style_id}/publish", dependencies=[Depends(require_admin)])
    def publish(style_id: str, request: Request):
        from .cloudflare import CloudflareClient, CloudflareError, load_cf_config
        from .publish import PublishError, publish_style

        cf = load_cf_config(request.app.state.db)
        if cf is None:
            raise HTTPException(status_code=409,
                                detail="Cloudflare 发布通道未配置；先在设置页填入凭证")
        try:
            return publish_style(request.app.state.db, request.app.state.service,
                                 CloudflareClient(cf), style_id)
        except PublishError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CloudflareError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/styles/{style_id}/publish-history",
             dependencies=[Depends(require_admin)])
    def publish_history_endpoint(style_id: str, request: Request):
        """Every publish attempt for this style, newest first.

        Unfinished attempts stay visible with the state they stopped at, because a
        half-finished publish that nobody can see is the failure mode this ledger
        exists to remove.
        """
        from .publish import publish_history

        service: TaskService = request.app.state.service
        service.get_style(style_id)  # 404 if missing
        return {"style_id": style_id,
                "publishes": publish_history(request.app.state.db, style_id)}

    @app.get("/api/publishes/unfinished", dependencies=[Depends(require_admin)])
    def unfinished_publishes_endpoint(request: Request):
        """Publishes that never reached `committed` — the operator's backlog.

        Re-running publish for the style completes them: R2 keys are versioned so
        they never overwrite, and D1 rows upsert on a stable id, so a re-run is
        idempotent rather than merely hopeful.
        """
        from .publish import unfinished_publishes

        rows = unfinished_publishes(request.app.state.db)
        return {"count": len(rows), "publishes": rows}

    # ---------------- Cloudflare publish channel settings ----------------

    @app.get("/api/settings/cloudflare", dependencies=[Depends(require_admin)])
    def cloudflare_settings(request: Request):
        """Admin-only: the token is fingerprinted, but account/database/bucket
        IDs are themselves sensitive — they name the production infrastructure."""
        from .cloudflare import cf_config_public

        return cf_config_public(request.app.state.db)

    @app.delete("/api/settings/cloudflare", dependencies=[Depends(require_admin)])
    def clear_cloudflare(request: Request):
        """Remove stored credentials.

        `save` treats blank fields as "keep existing" (so a partial update need
        not resend the token), which left no way to revoke. This is that way.
        """
        from .cloudflare import clear_cf_config

        cleared = clear_cf_config(request.app.state.db)
        logger.info("cloudflare credentials cleared",
                    extra={"ctx": {"stage": "settings", "status": f"keys={cleared}"}})
        return {"cleared": cleared, "configured": False}

    @app.post("/api/settings/cloudflare", dependencies=[Depends(require_admin)])
    def save_cloudflare(body: CloudflareConfigRequest, request: Request):
        from .cloudflare import cf_config_public, save_cf_config

        save_cf_config(request.app.state.db, body.model_dump())
        return cf_config_public(request.app.state.db)

    @app.post("/api/settings/cloudflare/test", dependencies=[Depends(require_admin)])
    def test_cloudflare(request: Request):
        from .cloudflare import CloudflareClient, load_cf_config

        cf = load_cf_config(request.app.state.db)
        if cf is None:
            raise HTTPException(status_code=409, detail="凭证不完整")
        return CloudflareClient(cf).test()

    @app.post("/api/styles/{style_id}/matrix/estimate",
              dependencies=[Depends(require_admin)])
    def estimate_matrix(style_id: str, body: MatrixRequest, request: Request):
        """Price a matrix batch without queueing anything.

        Read-only and free. The UI calls this before showing the confirmation
        dialog, so the figure the operator approves is the one the server will
        enforce.
        """
        try:
            return request.app.state.service.estimate_matrix(
                style_id, tones=body.tones or None, views=body.views or None,
                force=body.force, mode=body.mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/styles/{style_id}/matrix", status_code=202,
              dependencies=[Depends(require_admin)])
    def generate_matrix(style_id: str, body: MatrixRequest, request: Request):
        try:
            plan = request.app.state.service.create_matrix_generation(
                style_id, tones=body.tones or None, views=body.views or None,
                force=body.force, note=body.note,
                confirmed_max_usd=body.confirm_max_usd,
                mode=body.mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"batch_id": plan.batch_id, "created": plan.created,
                "reused": plan.reused, "skipped": plan.skipped}

    @app.get("/api/budget", dependencies=[Depends(require_admin)])
    def budget(request: Request):
        """Rolling-window spend against the cap. Admin-only: it reveals business volume."""
        return spend_snapshot(request.app.state.db, config).as_dict()

    @app.get("/api/tasks/{task_id}/snapshot", dependencies=[Depends(require_admin)])
    def task_snapshot(task_id: str, request: Request):
        """Frozen inputs for this task, plus what each attempt actually sent.

        Admin-only: a snapshot contains the full prompt and the channel's identity.
        Tasks created before snapshots existed report `available: false` rather than
        a synthesized snapshot — a fabricated one would be indistinguishable from a
        real one while being a guess.
        """
        service: TaskService = request.app.state.service
        service.get_task(task_id, with_details=False)  # 404 if missing
        db: Database = request.app.state.db
        record = get_snapshot(db, task_id)
        if record is None:
            return {"task_id": task_id, "available": False,
                    "reason": "task predates input snapshots"}
        return {
            "task_id": task_id,
            "available": True,
            "input_fingerprint": record["input_fingerprint"],
            "snapshot_version": record["snapshot_version"],
            "created_at": record["created_at"],
            "snapshot": record["snapshot"],
            # What actually reached the provider, per attempt. Differs from the
            # plan when a reference was missing and the prompt was stripped.
            "executions": get_executions(db, task_id),
        }

    @app.get("/api/fingerprints/{fingerprint}", dependencies=[Depends(require_admin)])
    def fingerprint_lookup(fingerprint: str, request: Request):
        """Every task built from exactly these inputs — the reproducibility check."""
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise HTTPException(status_code=422,
                                detail="fingerprint must be 64 lowercase hex chars")
        matches = find_by_fingerprint(request.app.state.db, fingerprint)
        return {"input_fingerprint": fingerprint, "count": len(matches),
                "tasks": matches}

    @app.get("/api/tasks/{task_id}/lineage", dependencies=[Depends(require_admin)])
    def task_lineage(task_id: str, request: Request):
        """Shared automatic-work budget for this task's lineage.

        Automatic re-generation and correction draw from ONE allowance per root, so
        this is where an operator sees whether the system stopped trying because
        the budget ran out rather than because something broke.
        """
        return request.app.state.service.lineage_for(task_id)

    @app.get("/api/tasks")
    def list_tasks(request: Request,
                   sku: str | None = None,
                   status: str | None = Query(None, pattern="^(" + "|".join(STATUSES) + ")$"),
                   output_type: str | None = Query(None, pattern="^(" + "|".join(OUTPUT_TYPES) + ")$"),  # noqa: B008
                   batch_id: str | None = None,
                   qa_state: str | None = Query(None, pattern="^(" + "|".join(QA_STATES) + ")$"),  # noqa: B008
                   review_state: str | None = Query(None, pattern="^(" + "|".join(REVIEW_STATES) + ")$"),  # noqa: B008
                   limit: int = Query(100, ge=1, le=500),
                   offset: int = Query(0, ge=0)):
        tasks = request.app.state.service.list_tasks(
            sku=sku, status=status, output_type=output_type, batch_id=batch_id,
            qa_state=qa_state, review_state=review_state,
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
                           detail_nails: str = Form(""),
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
                detail_references=details,
                detail_nails=[item.strip() for item in detail_nails.split(",")
                              if item.strip()],
                owner_override=owner_override,
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
        # Same path the worker uses: stores the verdict and reopens human review,
        # so a re-run cannot leave a stale approval attached to a new verdict.
        service.finish_qa(task_id, qa_doc)
        refreshed = service.get_task(task_id)
        return {**qa_doc, "qa_state": refreshed["qa_state"],
                "review_state": refreshed["review_state"]}

    @app.post("/api/tasks/{task_id}/review", dependencies=[Depends(require_admin)])
    def review_task(task_id: str, body: ReviewRequest, request: Request):
        """Record the human verdict for a successful task's heuristic QA result.

        Returns 409 `qa_not_ready` while automatic QA is still running: that
        window is now an explicit state rather than a race the caller has to
        guess at.
        """
        service: TaskService = request.app.state.service
        task = service.record_review(
            task_id, approved=body.approved, note=body.note,
            reviewer=body.reviewer or _reviewer_from(request),
        )
        return {
            "task_id": task_id,
            "approved": body.approved,
            "qa_state": task["qa_state"],
            "review_state": task["review_state"],
            "needs_human_review": bool(task["qa"]["needs_human_review"]) if task["qa"] else True,
        }

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
        """Cheap connectivity + auth probe: GET {base_url}/models, no image cost.

        This endpoint echoes part of the response body, which makes it the
        read-capable half of an SSRF if the URL is not constrained. The stored
        URL was validated on write, but it is re-validated here: DNS may have
        changed since, and `allow_private_api_hosts` may have been tightened.
        """
        import httpx

        profiles: ProfileService = request.app.state.profiles
        row = profiles._get_row(profile_id)  # noqa: SLF001 - server needs the raw key once
        assert_safe_request_url(
            row["base_url"], allow_private=config.allow_private_api_hosts
        )
        try:
            response = httpx.get(
                f"{row['base_url']}/models",
                headers={"Authorization": f"Bearer {row['api_key']}"},
                timeout=15,
                # No redirects: a 302 to 169.254.169.254 would bypass the check
                # above, since only the first URL is ours to validate.
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "stage": "transport", "detail": redact(str(exc))[:200]}
        if response.status_code != 200:
            return {"ok": False, "stage": "auth_or_endpoint",
                    "http_status": response.status_code,
                    "detail": redact(response.text[:200])}
        model_present = None
        try:
            ids = [m.get("id") for m in response.json().get("data", [])]
            model_present = row["model"] in ids
        except (ValueError, AttributeError):
            ids = []
        return {"ok": True, "http_status": 200, "models_listed": len(ids),
                "configured_model_present": model_present}

    # ---------------- hand models (fixed 4 tones x 4 views) ----------------

    from .prompts import MATRIX_TONES, MATRIX_VIEWS

    def _hand_model_setting(conn, tone: str, view: str) -> str | None:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (f"hand_model_{tone}_{view}",),
        ).fetchone()
        return row["value"] if row else None

    def _check_cell(tone: str, view: str) -> None:
        if tone not in MATRIX_TONES:
            raise HTTPException(status_code=422, detail=f"tone must be one of {MATRIX_TONES}")
        if view not in MATRIX_VIEWS:
            raise HTTPException(status_code=422, detail=f"view must be one of {MATRIX_VIEWS}")

    @app.get("/api/settings/hand-models")
    def hand_models(request: Request):
        conn = request.app.state.db.conn()
        out: dict = {}
        for tone in MATRIX_TONES:
            out[tone] = {}
            for view in MATRIX_VIEWS:
                path = _hand_model_setting(conn, tone, view)
                out[tone][view] = {"configured": bool(path and Path(path).is_file())}
        return {"hand_models": out, "tones": list(MATRIX_TONES), "views": list(MATRIX_VIEWS)}

    @app.post("/api/settings/hand-models/{tone}/{view}",
              dependencies=[Depends(require_admin)])
    async def upload_hand_model(tone: str, view: str, request: Request,
                                file: UploadFile = File(...)):  # noqa: B008 - FastAPI idiom
        _check_cell(tone, view)
        data = await file.read(config.max_upload_mb * 1024 * 1024 + 1)
        fmt, ext = _validated_upload(data)
        # Content-addressed: this used to write a fixed `{tone}-{view}.png`, so
        # re-uploading silently replaced the bytes that past matrix cells had been
        # generated from. Now a new upload lands at a new path and the old one
        # keeps serving the old bytes, so snapshots stay truthful.
        ref = store_bytes(request.app.state.db, config, data,
                          kind="hand_model", ext=ext)
        from .db import transaction, utcnow
        conn = request.app.state.db.conn()
        with transaction(conn):
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                " updated_at = excluded.updated_at",
                (f"hand_model_{tone}_{view}", str(ref.path), utcnow()),
            )
        return {"tone": tone, "view": view, "stored": ref.path.name,
                "digest": ref.digest, "format": fmt, "bytes": len(data)}

    @app.get("/api/settings/hand-models/{tone}/{view}/image")
    def hand_model_image(tone: str, view: str, request: Request):
        _check_cell(tone, view)
        path_text = _hand_model_setting(request.app.state.db.conn(), tone, view)
        if not path_text or not Path(path_text).is_file():
            raise HTTPException(status_code=404, detail="hand model not configured")
        path = Path(path_text).resolve()
        # Two valid roots now: the content-addressed store (new uploads) and the
        # legacy upload dir (hand models registered before the store existed).
        allowed_roots = (assets_root(config).resolve(), config.upload_dir.resolve())
        if not any(path.is_relative_to(root) for root in allowed_roots):
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
            return run_export(request.app.state.db, config, skus=body.skus or None)
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
