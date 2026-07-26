"""Lunelle Studio command-line interface.

Usage: python -m lunelle.cli <command> [options]

Commands:
  init          create directories + initialize/migrate the database
  serve         run the API server (worker included)
  health        check a running server's /health and /ready
  create-style  create a style from CLI options
  generate      create generation tasks for a style (grid + wearing)
  tasks         list tasks (filters: --sku --status --output-type)
  show          show one task in full detail
  retry         re-queue a failed/cancelled task
  stats         print statistics from the database
  export        run the Shopify asset export
  backup        online backup of the SQLite database
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError, load_config
from .db import Database, migrate
from .logging_setup import setup_logging


def _service(config):
    from .tasks import TaskService

    db = Database(config.db_path)
    migrate(db.conn())
    return db, TaskService(db, config)


def _print(doc) -> None:
    print(json.dumps(doc, ensure_ascii=False, indent=2, default=str))


def cmd_init(config, _args) -> int:
    config.ensure_dirs()
    db = Database(config.db_path)
    applied = migrate(db.conn())
    problems = config.validate_for_serve()
    print(f"data dir: {config.data_dir}")
    print(f"database: {config.db_path}")
    print(f"migrations applied now: {applied or 'none (up to date)'}")
    for directory in config.runtime_dirs():
        print(f"dir ok: {directory}")
    if problems:
        print("\nConfiguration problems (fix before serving):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("configuration: OK")
    return 0


def cmd_serve(config, _args) -> int:
    import uvicorn

    from .server import create_app

    problems = config.validate_for_serve()
    if problems:
        print("Refusing to start; configuration problems:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")
    return 0


def cmd_health(config, args) -> int:
    import httpx

    base = args.url or f"http://{config.host}:{config.port}"
    ok = True
    for endpoint in ("/health", "/ready"):
        try:
            response = httpx.get(base + endpoint, timeout=10)
            body = response.json()
            print(f"{endpoint}: HTTP {response.status_code} {json.dumps(body, ensure_ascii=False)}")
            ok = ok and response.status_code == 200
        except Exception as exc:  # noqa: BLE001 - CLI surface
            print(f"{endpoint}: FAILED ({exc})")
            ok = False
    return 0 if ok else 1


def cmd_create_style(config, args) -> int:
    from .providers import build_chat_fn
    from .schemas import StyleCreateRequest
    from .styles import build_style_spec

    _db, service = _service(config)
    request = StyleCreateRequest(
        sku=args.sku,
        name=args.name,
        description=args.description or "",
        base_colors=args.color or [],
        elements=args.element or [],
        texture=args.texture or [],
        avoid=args.avoid or [],
        shape=args.shape,
        length=args.length,
        skin_tone=args.skin_tone,
        use_llm=args.use_llm,
    )
    chat_fn = build_chat_fn(config) if args.use_llm else None
    outcome = build_style_spec(request, service.taken_skus(), chat_fn=chat_fn)
    style = service.create_style(
        outcome.spec, source_type=outcome.source_type,
        source_input=request.model_dump(), parser=outcome.parser, warnings=outcome.warnings,
    )
    _print({"style_id": style["style_id"], "sku": style["sku"], "spec": style["spec"],
            "warnings": outcome.warnings})
    return 0


def cmd_generate(config, args) -> int:
    _db, service = _service(config)
    style = service.get_style_by_sku(args.sku) if args.sku else service.get_style(args.style_id)
    plan = service.create_generation(
        style["style_id"],
        args.output_types.split(",") if args.output_types else ["grid", "wearing"],
        force=args.force,
        note=args.note or "",
    )
    _print({"batch_id": plan.batch_id, "created": plan.created,
            "reused": plan.reused, "skipped": plan.skipped})
    if args.wait:
        return _wait_for_batch(service, plan, timeout_s=args.wait)
    return 0


def _wait_for_batch(service, plan, timeout_s: int) -> int:
    task_ids = [t["task_id"] for t in plan.created + plan.reused]
    if not task_ids:
        return 0
    print(f"waiting up to {timeout_s}s for {len(task_ids)} task(s)... (worker must be running: serve)")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        docs = [service.get_task(tid, with_details=False) for tid in task_ids]
        states = {d["task_id"]: d["status"] for d in docs}
        if all(s in ("success", "failed", "cancelled") for s in states.values()):
            _print(states)
            return 0 if all(s == "success" for s in states.values()) else 1
        time.sleep(3)
    print("timeout waiting for tasks; check `tasks` output")
    return 1


def cmd_tasks(config, args) -> int:
    _db, service = _service(config)
    rows = service.list_tasks(sku=args.sku, status=args.status,
                              output_type=args.output_type, limit=args.limit)
    for row in rows:
        print(f"{row['task_id']}  {row['status']:9s} {row['output_type']:8s} {row['sku']:24s}"
              f" retry={row['retry_count']}/{row['max_retries']}"
              f" {row['error_code'] or ''}")
    if not rows:
        print("(no tasks)")
    return 0


def cmd_show(config, args) -> int:
    _db, service = _service(config)
    _print(service.get_task(args.task_id))
    return 0


def cmd_retry(config, args) -> int:
    _db, service = _service(config)
    doc = service.manual_retry(args.task_id, note=args.note or "cli retry")
    _print({"task_id": doc["task_id"], "status": doc["status"]})
    return 0


def cmd_stats(config, _args) -> int:
    from .stats import collect_stats

    db, _service_ = _service(config)
    _print(collect_stats(db))
    return 0


def cmd_export(config, args) -> int:
    from .export import ExportError, run_export

    db, _service_ = _service(config)
    try:
        result = run_export(db, config, skus=args.sku or None)
    except ExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    _print({"export_dir": result["export_dir"], "item_count": result["item_count"],
            "skipped": result["skipped"]})
    return 0


def cmd_backup(config, args) -> int:
    from .db import backup, connect

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = Path(args.dest) if args.dest else (config.data_dir / "backups" / f"lunelle-{stamp}.db")
    conn = connect(config.db_path)
    try:
        backup(conn, dest)
    finally:
        conn.close()
    print(f"backup written: {dest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lunelle", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=None, help="path to .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    sub.add_parser("serve")

    p = sub.add_parser("health")
    p.add_argument("--url", default=None)

    p = sub.add_parser("create-style")
    p.add_argument("--name")
    p.add_argument("--sku")
    p.add_argument("--description", "-d")
    p.add_argument("--color", action="append")
    p.add_argument("--element", action="append")
    p.add_argument("--texture", action="append")
    p.add_argument("--avoid", action="append")
    p.add_argument("--shape")
    p.add_argument("--length")
    p.add_argument("--skin-tone")
    p.add_argument("--use-llm", action="store_true")

    p = sub.add_parser("generate")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--sku")
    group.add_argument("--style-id")
    p.add_argument("--output-types", help="comma list: grid,wearing")
    p.add_argument("--force", action="store_true")
    p.add_argument("--note")
    p.add_argument("--wait", type=int, default=0, help="wait up to N seconds for completion")

    p = sub.add_parser("tasks")
    p.add_argument("--sku")
    p.add_argument("--status")
    p.add_argument("--output-type")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("show")
    p.add_argument("task_id")

    p = sub.add_parser("retry")
    p.add_argument("task_id")
    p.add_argument("--note")

    sub.add_parser("stats")

    p = sub.add_parser("export")
    p.add_argument("--sku", action="append")

    p = sub.add_parser("backup")
    p.add_argument("--dest")

    return parser


COMMANDS = {
    "init": cmd_init,
    "serve": cmd_serve,
    "health": cmd_health,
    "create-style": cmd_create_style,
    "generate": cmd_generate,
    "tasks": cmd_tasks,
    "show": cmd_show,
    "retry": cmd_retry,
    "stats": cmd_stats,
    "export": cmd_export,
    "backup": cmd_backup,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.env_file)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command != "serve":
        setup_logging(config.log_dir, "WARNING", console=False)
    try:
        return COMMANDS[args.command](config, args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
