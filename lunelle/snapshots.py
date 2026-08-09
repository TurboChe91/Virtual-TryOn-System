"""Immutable input snapshots and the input fingerprint.

A task row records what to generate, but almost every input it depends on lives
somewhere mutable: the style spec and identity text can be edited, the active API
profile can be switched, contracts and prompt templates change with a deploy, and
hand models used to be overwritten in place. So "what produced this image" was
not recoverable after the fact, and re-running a task could not reproduce it.

A snapshot freezes all of it at creation time, as data rather than as references:

- the resolved style spec and identity text, copied not pointed at
- prompt, negative prompt, prompt version, and the content hash of every contract
  that fed the prompt
- provider, model, image size, reference mode, watermark policy
- the API profile's identity and key FINGERPRINT (never the key)
- every input asset by content digest (see lunelle/assets.py)

`input_fingerprint` is sha256 over that canonical structure. Two tasks with the
same fingerprint had the same inputs, full stop — which is what makes
reproducibility checkable instead of assumed. It deliberately excludes anything
that is not an input: task id, timestamps, batch, note, retry counts.

Snapshots are append-only. The row is written in the same transaction as the task
insert, and nothing updates it afterwards; runtime-resolved references (the grid a
wearing shot actually used, the hand model actually found) are recorded separately
as an execution record, because those are observations, not the plan.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .config import Config
from .db import Database, transaction, utcnow

logger = logging.getLogger(__name__)

#: 2 adds what makes a snapshot executable rather than merely descriptive: inputs
#: tagged by role and resolvable by digest, a channel fingerprint to compare against
#: at execution time, and explicit `deferred`/`unresolved` lists. Version 1
#: snapshots are readable but NOT executable — their `input_assets` say `kind`
#: rather than `role`, so which image was Image 1 is not recoverable from them. The
#: worker refuses them instead of guessing (see worker._load_execution_plan).
SNAPSHOT_VERSION = 2

#: Keys excluded from the fingerprint: identity, bookkeeping, and anything that
#: varies between two runs of the same inputs.
_NON_INPUT_KEYS = frozenset({
    "task_id", "batch_id", "created_at", "note", "snapshot_version",
})


def canonical_json(payload: dict) -> str:
    """Stable serialization: sorted keys, no incidental whitespace.

    The fingerprint is only meaningful if the same inputs always serialize
    identically, so key order and separators are pinned rather than left to
    default formatting.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def compute_fingerprint(inputs: dict) -> str:
    """sha256 over the input-bearing part of a snapshot."""
    filtered = {k: v for k, v in inputs.items() if k not in _NON_INPUT_KEYS}
    return hashlib.sha256(canonical_json(filtered).encode()).hexdigest()


def build_snapshot(
    *,
    style: dict,
    spec: dict,
    output_type: str,
    prompt: str,
    negative_prompt: str,
    prompt_version: str,
    contract_hashes: dict,
    provider: str,
    model: str,
    size: tuple[int, int],
    reference_mode: str,
    disable_watermark: bool,
    api_profile: dict | None,
    input_assets: list[dict],
    extra: dict | None = None,
    channel_fingerprint: str = "",
    deferred_inputs: list[dict] | None = None,
    unresolved_inputs: list[dict] | None = None,
) -> dict:
    """Assemble the frozen input record. Pure — no I/O, no clock."""
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "style": {
            "style_id": style.get("style_id"),
            "sku": style.get("sku"),
            "name": style.get("name"),
            # Copied, not referenced: editing the style later must not rewrite
            # what a past task was generated from.
            "spec": spec,
            "identity_text": (style.get("identity_text") or "") or None,
        },
        "output_type": output_type,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "prompt_version": prompt_version,
        # Content hashes of the JSON contracts that shaped the prompt, so a
        # contract edit is visible as a different fingerprint.
        "contracts": dict(sorted(contract_hashes.items())),
        "channel": {
            "provider": provider,
            "model": model,
            "reference_mode": reference_mode,
            "disable_watermark": bool(disable_watermark),
            # Identity and key fingerprint only. The key itself must never enter
            # a snapshot: snapshots are long-lived, exported, and read by anyone
            # debugging a task.
            "api_profile": api_profile,
            # One value covering profile id + base URL + key. A profile row is
            # edited in place, so the same profile_id can point at a different
            # endpoint with a different key tomorrow; the worker compares this and
            # blocks rather than sending a queued task down a channel its snapshot
            # never described.
            "channel_fingerprint": channel_fingerprint,
        },
        "size": [size[0], size[1]],
        # Inputs by ROLE and content digest, in the order the provider receives
        # them. Role rather than position: which image is the view-plan and which
        # is the base hand used to be implicit in list order, and the `kind` field
        # said 'hand_model' for a plan and 'reference' for everything.
        "input_assets": input_assets,
        # Inputs that cannot exist yet — a wearing shot's grid is generated later in
        # the same batch. Declared, not guessed: claiming a digest for a file that
        # does not exist would make the snapshot a prediction.
        "deferred_inputs": deferred_inputs or [],
        # Inputs that were expected and absent at queue time. The task is queued
        # anyway and the worker blocks it as dependency_missing without a provider
        # call, which keeps the failure visible in the task list.
        "unresolved_inputs": unresolved_inputs or [],
    }
    if extra:
        snapshot["extra"] = dict(sorted(extra.items()))
    return snapshot


def profile_snapshot(row) -> dict | None:
    """Public description of an API profile for a snapshot (no secret)."""
    if row is None:
        return None
    api_key = row["api_key"] if "api_key" in row.keys() else ""
    base_url = row["base_url"] or ""
    return {
        "profile_id": row["profile_id"],
        "name": row["name"],
        "base_url": base_url,
        "model": row["model"],
        "reference_mode": row["reference_mode"],
        # sha256 prefix: enough to tell two keys apart, useless as a credential.
        "key_fingerprint": hashlib.sha256(api_key.encode()).hexdigest()[:8],
        # The base URL is stored in the clear above for debugging, and fingerprinted
        # here so the execution-time comparison is one uniform check across all
        # three channel fields rather than a string compare on one of them.
        "base_url_fingerprint": hashlib.sha256(base_url.encode()).hexdigest()[:8],
    }


def store_snapshot_locked(conn, task_id: str, snapshot: dict,
                          fingerprint: str, *, replace: bool = False) -> None:
    """Insert the snapshot inside the caller's transaction.

    Same transaction as the task insert on purpose: a task must never exist
    without the record of what it was built from.

    `replace=True` is for explicit re-queue only (see TaskService.refreeze_snapshot).
    A task blocked for a missing asset froze "this input was absent", and the worker
    now executes only from the snapshot, so without a re-freeze the operator could
    never recover it by uploading the asset. It is deliberately not the default:
    overwriting a snapshot while a task is claimable would destroy the immutability
    the worker depends on.
    """
    if replace:
        conn.execute(
            "INSERT INTO task_snapshots (task_id, input_fingerprint, snapshot_version,"
            " snapshot_json, created_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(task_id) DO UPDATE SET"
            "   input_fingerprint = excluded.input_fingerprint,"
            "   snapshot_version = excluded.snapshot_version,"
            "   snapshot_json = excluded.snapshot_json,"
            "   created_at = excluded.created_at",
            (task_id, fingerprint, SNAPSHOT_VERSION,
             canonical_json(snapshot), utcnow()),
        )
        return
    conn.execute(
        "INSERT INTO task_snapshots (task_id, input_fingerprint, snapshot_version,"
        " snapshot_json, created_at) VALUES (?,?,?,?,?)",
        (task_id, fingerprint, SNAPSHOT_VERSION,
         canonical_json(snapshot), utcnow()),
    )


def get_snapshot(db: Database, task_id: str) -> dict | None:
    row = db.conn().execute(
        "SELECT * FROM task_snapshots WHERE task_id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return None
    doc = dict(row)
    doc["snapshot"] = json.loads(doc.pop("snapshot_json"))
    return doc


def record_execution(db: Database, task_id: str, attempt_no: int, *,
                     resolved_assets: list[dict], prompt_sent: str,
                     model: str, provider: str,
                     build: dict | None = None,
                     request: dict | None = None,
                     snapshot_fingerprint: str | None = None,
                     matches_snapshot: bool | None = None) -> None:
    """Record what was ACTUALLY sent, as distinct from what was planned.

    These differ in practice: a reference can be missing at execution time and the
    prompt then has its reference block stripped, so the bytes that reached the
    provider are not what the snapshot planned. Auditing a bad image needs the
    former; reproducing a task needs the latter. Keeping both, separately, is the
    only way to have each.

    `build` is the identity of the code that ran (see lunelle/build.py). Without it
    a run by a stale process is indistinguishable from a correct one, which is how
    four cells were rendered on 2026-08-06 against code that had not been loaded.
    It is promoted to columns as well as kept in the JSON so "every execution on
    this build" is a query rather than a scan.

    `request` is the measured description of what is being sent: every image's
    sha256 and role, the prompt hash, the requested size, the model and the channel
    fingerprint (see inputs.request_manifest). It is written BEFORE the call, so a
    crash mid-flight still leaves a record of what went out.

    `matches_snapshot` is the verdict of comparing the two. It replaces
    `prompt_differs_from_snapshot`, which was declared but never filled by any
    caller and so read as None on every row ever written.
    """
    payload: dict = {
        "resolved_assets": resolved_assets,
        "prompt_sha256": hashlib.sha256(prompt_sent.encode()).hexdigest(),
        "model": model,
        "provider": provider,
    }
    if build:
        payload["build"] = build
    if request:
        payload["request"] = request
    if snapshot_fingerprint:
        payload["snapshot_fingerprint"] = snapshot_fingerprint
    if matches_snapshot is not None:
        payload["matches_snapshot"] = matches_snapshot
    conn = db.conn()
    with transaction(conn):
        conn.execute(
            "INSERT INTO task_executions (task_id, attempt_no, execution_json,"
            " created_at, build_id, runtime_tree_sha, git_sha, git_dirty,"
            " diff_sha256, dependency_sha256, process_id, worker_instance,"
            " runtime_drift_detected, prompt_sha256, size_requested,"
            " channel_fingerprint, snapshot_fingerprint, matches_snapshot,"
            " input_digests)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(task_id, attempt_no) DO UPDATE SET"
            "   execution_json = excluded.execution_json,"
            "   created_at = excluded.created_at,"
            "   build_id = excluded.build_id,"
            "   runtime_tree_sha = excluded.runtime_tree_sha,"
            "   git_sha = excluded.git_sha,"
            "   git_dirty = excluded.git_dirty,"
            "   diff_sha256 = excluded.diff_sha256,"
            "   dependency_sha256 = excluded.dependency_sha256,"
            "   process_id = excluded.process_id,"
            "   worker_instance = excluded.worker_instance,"
            "   runtime_drift_detected = excluded.runtime_drift_detected,"
            "   prompt_sha256 = excluded.prompt_sha256,"
            "   size_requested = excluded.size_requested,"
            "   channel_fingerprint = excluded.channel_fingerprint,"
            "   snapshot_fingerprint = excluded.snapshot_fingerprint,"
            "   matches_snapshot = excluded.matches_snapshot,"
            "   input_digests = excluded.input_digests",
            (task_id, attempt_no, canonical_json(payload), utcnow(),
             (build or {}).get("build_id"),
             (build or {}).get("runtime_tree_sha256"),
             (build or {}).get("git_sha"),
             _as_int_flag((build or {}).get("git_dirty")),
             (build or {}).get("diff_sha256"),
             (build or {}).get("dependency_sha256"),
             (build or {}).get("process_id"),
             (build or {}).get("worker_instance"),
             _as_int_flag((build or {}).get("runtime_drift_detected")),
             payload["prompt_sha256"],
             _size_text((request or {}).get("size_requested")),
             (request or {}).get("channel_fingerprint"),
             snapshot_fingerprint,
             _as_int_flag(matches_snapshot),
             _input_digest_text((request or {}).get("input_images"))),
        )


def _as_int_flag(value) -> int | None:
    """Booleans as 0/1 for SQLite, preserving None as "not recorded"."""
    return None if value is None else (1 if value else 0)


def _size_text(size) -> str | None:
    """"2224x1664" — greppable, and comparable without parsing JSON."""
    if not size or len(size) != 2:
        return None
    return f"{size[0]}x{size[1]}"


def _input_digest_text(images) -> str | None:
    """"role:digest12 role:digest12" in send order.

    A flat column so "which cells were sent this exact view-plan" is a LIKE away.
    Roles are included because a digest alone does not say what it was used AS, and
    that conflation is what let a plan be sent where a view-plan was promised.
    """
    if not images:
        return None
    return " ".join(f"{img['role']}:{img['sha256'][:12]}" for img in images)


def get_executions(db: Database, task_id: str) -> list[dict]:
    rows = db.conn().execute(
        "SELECT * FROM task_executions WHERE task_id = ? ORDER BY attempt_no",
        (task_id,),
    ).fetchall()
    out = []
    for row in rows:
        doc = dict(row)
        doc["execution"] = json.loads(doc.pop("execution_json"))
        out.append(doc)
    return out


def find_by_fingerprint(db: Database, fingerprint: str, limit: int = 20) -> list[dict]:
    """Tasks that shared exactly these inputs — the reproducibility question."""
    rows = db.conn().execute(
        "SELECT s.task_id, s.created_at, t.status, t.output_type, t.sku,"
        " t.output_path, t.review_state"
        " FROM task_snapshots s JOIN tasks t ON t.task_id = s.task_id"
        " WHERE s.input_fingerprint = ?"
        " ORDER BY s.created_at DESC LIMIT ?",
        (fingerprint, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def snapshot_asset_paths(db: Database, snapshot: dict) -> list[Path]:
    """Resolve a snapshot's input assets back to on-disk paths, by digest.

    Digest first, recorded path second: the digest is what makes the resolution
    correct even if the original path was later overwritten.
    """
    from .assets import resolve_path

    paths: list[Path] = []
    for entry in snapshot.get("input_assets", []):
        digest = entry.get("digest")
        if digest:
            resolved = resolve_path(db, digest)
            if resolved is not None:
                paths.append(resolved)
                continue
        recorded = entry.get("path")
        if recorded and Path(recorded).is_file():
            paths.append(Path(recorded))
    return paths


def contract_hashes_for(output_type: str) -> dict:
    """Content hashes of the contracts that shape this output type's prompt.

    Imported lazily: prompts.py reads contract files at import time, and snapshots
    are also used by tooling that has no reason to load them.
    """
    from .prompts import (
        HERO_CONTRACT_SHA,
        MATRIX_PROMPT_VERSION,
        PROMPT_VERSION,
    )

    hashes = {"prompt_module_version": PROMPT_VERSION}
    if output_type == "hero":
        hashes["hero_contract"] = HERO_CONTRACT_SHA
    elif output_type == "matrix_cell":
        hashes["matrix_contract"] = MATRIX_PROMPT_VERSION
    return hashes


def config_fingerprint_fields(config: Config) -> dict:
    """Config values that change generation output, for the snapshot's extras.

    Deliberately narrow: only settings that affect the produced image. Including
    operational settings (timeouts, concurrency, log level) would make the
    fingerprint change on unrelated redeploys and destroy its usefulness.
    """
    return {
        "qa_min_side": config.qa_min_side,
        "grid_size": list(config.grid_size),
        "wearing_size": list(config.wearing_size),
        "hero_size": list(config.hero_size),
    }
