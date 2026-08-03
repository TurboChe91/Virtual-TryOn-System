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

SNAPSHOT_VERSION = 1

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
        },
        "size": [size[0], size[1]],
        # Inputs by content digest, so an overwritten file cannot change history.
        "input_assets": input_assets,
    }
    if extra:
        snapshot["extra"] = dict(sorted(extra.items()))
    return snapshot


def profile_snapshot(row) -> dict | None:
    """Public description of an API profile for a snapshot (no secret)."""
    if row is None:
        return None
    api_key = row["api_key"] if "api_key" in row.keys() else ""
    return {
        "profile_id": row["profile_id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "model": row["model"],
        "reference_mode": row["reference_mode"],
        # sha256 prefix: enough to tell two keys apart, useless as a credential.
        "key_fingerprint": hashlib.sha256(api_key.encode()).hexdigest()[:8],
    }


def store_snapshot_locked(conn, task_id: str, snapshot: dict,
                          fingerprint: str) -> None:
    """Insert the snapshot inside the caller's transaction.

    Same transaction as the task insert on purpose: a task must never exist
    without the record of what it was built from.
    """
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
                     model: str, provider: str) -> None:
    """Record what was ACTUALLY sent, as distinct from what was planned.

    These differ in practice: a reference can be missing at execution time and the
    prompt then has its reference block stripped, so the bytes that reached the
    provider are not what the snapshot planned. Auditing a bad image needs the
    former; reproducing a task needs the latter. Keeping both, separately, is the
    only way to have each.
    """
    payload = {
        "resolved_assets": resolved_assets,
        "prompt_sha256": hashlib.sha256(prompt_sent.encode()).hexdigest(),
        "prompt_differs_from_snapshot": None,  # filled by the caller if known
        "model": model,
        "provider": provider,
    }
    conn = db.conn()
    with transaction(conn):
        conn.execute(
            "INSERT INTO task_executions (task_id, attempt_no, execution_json,"
            " created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(task_id, attempt_no) DO UPDATE SET"
            "   execution_json = excluded.execution_json,"
            "   created_at = excluded.created_at",
            (task_id, attempt_no, canonical_json(payload), utcnow()),
        )


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
