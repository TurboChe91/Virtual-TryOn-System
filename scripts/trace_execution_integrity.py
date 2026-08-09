#!/usr/bin/env python3
"""Trace one mock matrix cell's inputs through every stage, by SHA256.

Proves the C3 invariant on a real run rather than by inspection:

    queue  ==  snapshot  ==  worker  ==  provider  ==  QA

Every stage is measured independently — the provider's digests come from re-hashing
the files it was actually handed, and QA's from re-hashing the files handed to the
judge. Nothing is copied forward from the snapshot, so agreement here is evidence
rather than tautology.

Mock provider, temp directory, no network, no cost. Run:

    .venv/bin/python scripts/trace_execution_integrity.py
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lunelle.assets import digest_of_file  # noqa: E402
from lunelle.db import Database, migrate  # noqa: E402
from lunelle.providers.mock import MockImageProvider  # noqa: E402
from lunelle.snapshots import get_executions, get_snapshot  # noqa: E402
from lunelle.tasks import TaskService  # noqa: E402
from lunelle.worker import Worker  # noqa: E402

TONE, VIEW = "light", "p2_open_hands"

#: Captured by monkeypatching the vision judge: the images QA actually reads.
QA_IMAGES: list[Path] = []


def short(digest: str) -> str:
    return digest[:12]


def make_environment(root: Path):
    """A working config, database and service in a temp directory."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from tests.conftest import make_config, satisfy_matrix_dependencies

    config = make_config(root)
    db = Database(config.db_path)
    migrate(db.conn())
    service = TaskService(db, config)
    return config, db, service, satisfy_matrix_dependencies


def patch_qa_judge() -> None:
    """Record what the LLM judge is shown, without calling anything."""
    import lunelle.llm as llm

    def fake_chat(config, db):
        return lambda system, user, images: "{}"

    def fake_verdict(chat, images, identity, output_type, *, visible_nails=None):
        QA_IMAGES.clear()
        QA_IMAGES.extend(Path(p) for p in images)
        return {"passed": True, "issues": [], "correction": ""}

    llm.build_llm_chat = fake_chat  # type: ignore[assignment]
    llm.auto_qa_verdict = fake_verdict  # type: ignore[assignment]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="lunelle-trace-"))
    try:
        config, db, service, satisfy = make_environment(root)
        patch_qa_judge()

        from lunelle.schemas import StyleCreateRequest
        from lunelle.styles import build_style_spec

        outcome = build_style_spec(
            StyleCreateRequest(name="Trace Cell",
                               description="red square glossy nails with gold accents"),
            service.taken_skus(),
        )
        style = service.create_style(
            outcome.spec, source_type=outcome.source_type, source_input={},
            parser=outcome.parser, warnings=outcome.warnings,
        )
        satisfy(db, config, service, style["style_id"], tones=[TONE], views=[VIEW])

        # ---- stage 1: queue -------------------------------------------------
        plan = service.create_matrix_generation(style["style_id"], tones=[TONE],
                                                views=[VIEW])
        task_id = plan.created[0]["task_id"]
        style_row = service.get_style(style["style_id"])
        raw_plan = Path(style_row["plan_image_path"])
        hand_setting = db.conn().execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (f"hand_model_{TONE}_{VIEW}",),
        ).fetchone()["value"]

        print("=" * 78)
        print(f"MOCK MATRIX CELL  {task_id}   tone={TONE}  view={VIEW}")
        print("=" * 78)
        print("\n[1] QUEUE — operator uploads (sources, not inputs)")
        print(f"    raw plan (2x5, no screen order)  {short(digest_of_file(raw_plan))}  {raw_plan.name}")
        print(f"    hand model on disk               {short(digest_of_file(Path(hand_setting)))}  "
              f"{Path(hand_setting).name}")

        # ---- stage 2: snapshot ----------------------------------------------
        record = get_snapshot(db, task_id)
        snapshot = record["snapshot"]
        frozen = {entry["role"]: entry for entry in snapshot["input_assets"]}
        print(f"\n[2] SNAPSHOT — v{snapshot['snapshot_version']}, "
              f"fingerprint {short(record['input_fingerprint'])}")
        for index, entry in enumerate(snapshot["input_assets"], start=1):
            derived = entry.get("derived_from") or {}
            note = f"  compiled from plan {short(derived['plan_digest'])} for {derived['view']}" \
                if derived.get("plan_digest") else ""
            print(f"    input {index}  role={entry['role']:<11} {short(entry['digest'])}{note}")
        print(f"    prompt sha256                    "
              f"{short(hashlib.sha256(snapshot['prompt'].encode()).hexdigest())}")
        print(f"    size                             {snapshot['size'][0]}x{snapshot['size'][1]}")
        print(f"    channel fingerprint              {snapshot['channel']['channel_fingerprint']}")
        print(f"    deferred / unresolved            {len(snapshot['deferred_inputs'])} / "
              f"{len(snapshot['unresolved_inputs'])}")
        assert frozen["view_plan"]["digest"] != digest_of_file(raw_plan), \
            "the frozen Image 1 must NOT be the raw plan"
        print("    -> Image 1 is a compiled view-plan, not the raw plan  [OK]")

        # ---- stage 3+4: worker and provider ---------------------------------
        provider = MockImageProvider(allowed=True)
        worker = Worker(config, db, service, provider)
        worker.start()
        try:
            import time

            deadline = time.time() + 60
            while time.time() < deadline:
                if service.get_task(task_id, with_details=False)["status"] in (
                        "success", "failed", "cancelled"):
                    break
                time.sleep(0.1)
        finally:
            worker.stop(timeout=10)

        task = service.get_task(task_id)
        execution = get_executions(db, task_id)[0]["execution"]
        manifest = execution["request"]

        print(f"\n[3] WORKER — loaded from snapshot digests (status={task['status']})")
        for image in manifest["input_images"]:
            print(f"    input {image['index']}  role={image['role']:<11} "
                  f"{short(image['sha256'])}  <- store")

        print("\n[4] PROVIDER — re-hashed from the files it was handed")
        sent = [digest_of_file(p) for p in provider.requested_references[0]]
        for index, (path, digest) in enumerate(
                zip(provider.requested_references[0], sent, strict=True), start=1):
            print(f"    image {index}  {short(digest)}  {path.name}")
        print(f"    prompt sha256                    {short(manifest['prompt_sha256'])}"
              f"   (transform: {manifest['prompt_transform']})")
        print(f"    size requested                   "
              f"{provider.requested_sizes[0][0]}x{provider.requested_sizes[0][1]}")
        print(f"    model                            {manifest['model']}")
        print(f"    channel fingerprint              {manifest['channel_fingerprint']}")

        # ---- stage 5: QA ----------------------------------------------------
        print(f"\n[5] QA — images shown to the judge (qa_state={task['qa_state']})")
        for index, path in enumerate(QA_IMAGES, start=1):
            label = {1: "candidate", 2: "design authority", 3: "base hand"}.get(index, "extra")
            print(f"    image {index}  {short(digest_of_file(path))}  {label}")

        return report(task_id, db, snapshot, frozen, manifest, sent, task)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def report(task_id, db, snapshot, frozen, manifest, sent, task) -> int:
    """Assert the four-way equality and print the verdict per role."""
    qa_digests = [digest_of_file(path) for path in QA_IMAGES]
    manifest_by_role = {img["role"]: img["sha256"] for img in manifest["input_images"]}
    # QA order is: candidate, design authority, base hand.
    qa_by_role = {
        "view_plan": qa_digests[1] if len(qa_digests) > 1 else None,
        "base_hand": qa_digests[2] if len(qa_digests) > 2 else None,
    }

    print("\n" + "=" * 78)
    print("EQUALITY  snapshot == worker == provider == QA")
    print("=" * 78)
    print(f"{'role':<12} {'snapshot':<14} {'manifest':<14} {'provider':<14} {'QA':<14} verdict")
    failures = 0
    for index, (role, entry) in enumerate(frozen.items()):
        planned = entry["digest"]
        measured = manifest_by_role.get(role)
        delivered = sent[index] if index < len(sent) else None
        judged = qa_by_role.get(role)
        agree = planned == measured == delivered == judged
        failures += 0 if agree else 1
        print(f"{role:<12} {short(planned):<14} {short(measured or '-'):<14} "
              f"{short(delivered or '-'):<14} {short(judged or '-'):<14} "
              f"{'MATCH' if agree else 'MISMATCH'}")

    prompt_sha = hashlib.sha256(snapshot["prompt"].encode()).hexdigest()
    checks = [
        ("prompt sha256", prompt_sha == manifest["prompt_sha256"],
         f"{short(prompt_sha)} == {short(manifest['prompt_sha256'])}"),
        ("requested size", snapshot["size"] == manifest["size_requested"],
         f"{snapshot['size']} == {manifest['size_requested']}"),
        ("model", snapshot["channel"]["model"] == manifest["model"],
         f"{snapshot['channel']['model']} == {manifest['model']}"),
        ("channel fingerprint",
         snapshot["channel"]["channel_fingerprint"] == manifest["channel_fingerprint"],
         f"{snapshot['channel']['channel_fingerprint']} == {manifest['channel_fingerprint']}"),
        ("candidate judged", len(qa_digests) >= 1
         and qa_digests[0] == digest_of_file(Path(task["output_path"])),
         "QA image 1 is this task's own output"),
    ]
    print()
    for label, ok, detail in checks:
        failures += 0 if ok else 1
        print(f"{'MATCH' if ok else 'MISMATCH':<9} {label:<22} {detail}")

    row = db.conn().execute(
        "SELECT matches_snapshot, snapshot_fingerprint, input_digests, build_id"
        " FROM task_executions WHERE task_id = ?", (task_id,)
    ).fetchone()
    print(f"\nrecorded verdict   matches_snapshot={row['matches_snapshot']}"
          f"  snapshot={short(row['snapshot_fingerprint'])}  build={row['build_id']}")
    print(f"input_digests      {row['input_digests']}")
    if row["matches_snapshot"] != 1:
        failures += 1

    print()
    if failures:
        print(f"FAILED — {failures} disagreement(s)")
        return 1
    print("ALL STAGES AGREE — snapshot == worker == provider == QA, by SHA256")
    return 0


if __name__ == "__main__":
    sys.exit(main())
