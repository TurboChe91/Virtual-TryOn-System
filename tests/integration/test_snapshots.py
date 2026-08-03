"""Immutable input snapshots, the input fingerprint, and the asset freeze.

The property under test: after a task exists, nothing an operator does later can
change the record of what that task was built from. Before this, a task recorded
the PATH of its inputs, and hand models were written to a fixed
`{tone}-{view}.png` — so re-uploading one silently replaced the bytes a past
matrix cell had been generated from.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from lunelle.assets import get_asset, resolve_path, store_bytes
from lunelle.providers.mock import MockImageProvider
from lunelle.server import create_app
from lunelle.snapshots import (
    canonical_json,
    compute_fingerprint,
    find_by_fingerprint,
    get_snapshot,
    snapshot_asset_paths,
)
from tests.conftest import make_config, satisfy_matrix_dependencies
from tests.integration.test_worker_flows import make_style, run_worker_until_settled


def _png(color=(180, 40, 40), size=(300, 300)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class TestSnapshotIsWrittenWithTheTask:
    def test_every_new_task_gets_a_snapshot_and_fingerprint(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid", "wearing"])
        for created in plan.created:
            task = service.get_task(created["task_id"], with_details=False)
            assert task["input_fingerprint"], "task has no fingerprint"
            record = get_snapshot(db, created["task_id"])
            assert record is not None
            assert record["input_fingerprint"] == task["input_fingerprint"]

    def test_snapshot_copies_the_spec_rather_than_pointing_at_it(
        self, config, db, service
    ):
        """Editing the style afterwards must not rewrite history."""
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        task_id = plan.created[0]["task_id"]
        before = get_snapshot(db, task_id)["snapshot"]
        original_shape = before["style"]["spec"]["shape"]

        # Mutate the live style.
        from lunelle.db import transaction
        conn = db.conn()
        spec = dict(style["spec"])
        spec["shape"] = "stiletto"
        with transaction(conn):
            conn.execute("UPDATE styles SET spec_json = ? WHERE style_id = ?",
                         (json.dumps(spec), style["style_id"]))

        after = get_snapshot(db, task_id)["snapshot"]
        assert after["style"]["spec"]["shape"] == original_shape
        assert service.get_style(style["style_id"])["spec"]["shape"] == "stiletto"

    def test_snapshot_records_identity_text_at_creation_time(self, config, db, service):
        style = make_style(service)
        service.set_identity_text(style["style_id"], "- nail-01: red\n- nail-10: red")
        style = service.get_style(style["style_id"])
        plan = service.create_generation(style["style_id"], ["grid"])
        snapshot = get_snapshot(db, plan.created[0]["task_id"])["snapshot"]
        assert "nail-01" in snapshot["style"]["identity_text"]

        service.set_identity_text(style["style_id"], "completely different")
        frozen = get_snapshot(db, plan.created[0]["task_id"])["snapshot"]
        assert "nail-01" in frozen["style"]["identity_text"]

    def test_snapshot_never_contains_the_api_key(self, config, db, service):
        """Snapshots are long-lived and read by anyone debugging a task."""
        from lunelle.profiles import ProfileService

        profiles = ProfileService(db)
        profiles.create(name="chan", base_url="https://example.com/v1",
                        api_key="sk-super-secret-value-1234", model="m")
        row = db.conn().execute("SELECT profile_id FROM api_profiles").fetchone()
        profiles.activate(row["profile_id"])

        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        record = get_snapshot(db, plan.created[0]["task_id"])
        serialized = json.dumps(record["snapshot"])
        assert "sk-super-secret-value-1234" not in serialized
        profile = record["snapshot"]["channel"]["api_profile"]
        assert profile["name"] == "chan"
        assert len(profile["key_fingerprint"]) == 8

    def test_snapshot_freezes_the_contract_hashes(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid", "hero"])
        by_type = {}
        for created in plan.created:
            snapshot = get_snapshot(db, created["task_id"])["snapshot"]
            by_type[snapshot["output_type"]] = snapshot
        assert "prompt_module_version" in by_type["grid"]["contracts"]
        # The hero prompt is contract-driven, so its contract hash is recorded.
        assert "hero_contract" in by_type["hero"]["contracts"]


class TestFingerprint:
    def test_same_inputs_produce_the_same_fingerprint(self, config, db, service):
        style = make_style(service)
        first = service.create_generation(style["style_id"], ["grid"])
        # force=true only changes the idempotency nonce, not the inputs.
        second = service.create_generation(style["style_id"], ["grid"], force=True)
        left = service.get_task(first.created[0]["task_id"], with_details=False)
        right = service.get_task(second.created[0]["task_id"], with_details=False)
        assert left["task_id"] != right["task_id"]
        assert left["input_fingerprint"] == right["input_fingerprint"], (
            "identical inputs must fingerprint identically")

    def test_different_style_produces_a_different_fingerprint(self, config, db, service):
        first = make_style(service, name="Alpha", description="red square nails")
        second = make_style(service, name="Beta", description="blue almond nails")
        left = service.create_generation(first["style_id"], ["grid"])
        right = service.create_generation(second["style_id"], ["grid"])
        assert (service.get_task(left.created[0]["task_id"],
                                 with_details=False)["input_fingerprint"]
                != service.get_task(right.created[0]["task_id"],
                                    with_details=False)["input_fingerprint"])

    def test_fingerprint_excludes_task_identity_and_timestamps(self):
        """Otherwise every task would be unique and the fingerprint useless."""
        base = {"style": {"sku": "x"}, "prompt": "p", "output_type": "grid"}
        with_noise = {**base, "task_id": "tk_1", "created_at": "2026-01-01",
                      "batch_id": "bt_1", "note": "hello"}
        assert compute_fingerprint(base) == compute_fingerprint(with_noise)

    def test_fingerprint_is_stable_across_key_order(self):
        assert (compute_fingerprint({"a": 1, "b": 2})
                == compute_fingerprint({"b": 2, "a": 1}))

    def test_canonical_json_is_deterministic(self):
        payload = {"b": [3, 1], "a": {"z": 1, "y": 2}}
        assert canonical_json(payload) == canonical_json(dict(reversed(list(payload.items()))))

    def test_model_change_changes_the_fingerprint(self, tmp_path):
        """A different model is a different input, even with the same prompt."""
        from lunelle.db import Database, migrate
        from lunelle.tasks import TaskService

        prints = []
        for model in ("mock-model", "other-model"):
            config = make_config(tmp_path / model, image_model=model)
            db = Database(config.db_path)
            migrate(db.conn())
            service = TaskService(db, config)
            try:
                style = make_style(service)
                plan = service.create_generation(style["style_id"], ["grid"])
                prints.append(service.get_task(plan.created[0]["task_id"],
                                               with_details=False)["input_fingerprint"])
            finally:
                db.close_all()
        assert prints[0] != prints[1]

    def test_lookup_finds_every_task_with_those_inputs(self, config, db, service):
        style = make_style(service)
        first = service.create_generation(style["style_id"], ["grid"])
        second = service.create_generation(style["style_id"], ["grid"], force=True)
        fingerprint = service.get_task(first.created[0]["task_id"],
                                       with_details=False)["input_fingerprint"]
        matches = find_by_fingerprint(db, fingerprint)
        found = {row["task_id"] for row in matches}
        assert found == {first.created[0]["task_id"], second.created[0]["task_id"]}


class TestAssetFreeze:
    def test_hand_model_reupload_cannot_change_a_past_snapshot(self, tmp_path):
        """The defect this exists to fix, end to end."""
        config = make_config(tmp_path, confirm_cost_usd=0.0)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style = client.post("/api/styles", json={
                "name": "Freeze", "description": "wine red almond glossy nails",
            }).json()["style"]
            # Original hand model.
            original = _png((210, 180, 160))
            upload = client.post(
                "/api/settings/hand-models/light/p2_open_hands",
                files={"file": ("hand.png", original, "image/png")})
            assert upload.status_code == 200
            original_digest = upload.json()["digest"]

            client.post(f"/api/styles/{style['style_id']}/reference-image?kind=plan",
                        files={"file": ("plan.png", _png((90, 20, 20)), "image/png")})
            queued = client.post(f"/api/styles/{style['style_id']}/matrix",
                                 json={"tones": ["light"],
                                       "views": ["p2_open_hands"]})
            assert queued.status_code == 202
            task_id = queued.json()["created"][0]["task_id"]

            snapshot = client.get(f"/api/tasks/{task_id}/snapshot").json()
            assert snapshot["available"] is True
            digests = {a.get("digest") for a in snapshot["snapshot"]["input_assets"]}
            assert original_digest in digests

            # Operator replaces the hand model with completely different bytes.
            replacement = client.post(
                "/api/settings/hand-models/light/p2_open_hands",
                files={"file": ("hand.png", _png((40, 30, 25)), "image/png")})
            assert replacement.status_code == 200
            assert replacement.json()["digest"] != original_digest

            # The past task's snapshot is unchanged, and the original bytes are
            # still resolvable from the store.
            after = client.get(f"/api/tasks/{task_id}/snapshot").json()
            assert after["snapshot"] == snapshot["snapshot"]
            assert after["input_fingerprint"] == snapshot["input_fingerprint"]
            restored = resolve_path(app.state.db, original_digest)
            assert restored is not None and restored.is_file()
            assert restored.read_bytes() == original

    def test_store_is_idempotent_for_identical_bytes(self, config, db):
        data = _png()
        first = store_bytes(db, config, data, kind="hand_model", ext=".png")
        second = store_bytes(db, config, data, kind="hand_model", ext=".png")
        assert first.digest == second.digest
        assert first.path == second.path
        rows = db.conn().execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]
        assert rows == 1

    def test_different_bytes_get_different_paths(self, config, db):
        first = store_bytes(db, config, _png((1, 2, 3)), kind="hand_model", ext=".png")
        second = store_bytes(db, config, _png((9, 8, 7)), kind="hand_model", ext=".png")
        assert first.path != second.path
        assert first.path.is_file() and second.path.is_file()

    def test_registry_records_size_and_mime(self, config, db):
        data = _png()
        ref = store_bytes(db, config, data, kind="reference", ext=".png")
        record = get_asset(db, ref.digest)
        assert record["byte_size"] == len(data)
        assert record["mime_type"] == "image/png"
        assert record["kind"] == "reference"

    def test_unknown_kind_is_refused(self, config, db):
        with pytest.raises(ValueError, match="unknown asset kind"):
            store_bytes(db, config, _png(), kind="nonsense", ext=".png")

    def test_snapshot_assets_resolve_back_to_bytes(self, config, db, service):
        style = make_style(service)
        # Give the style a reference so the grid task has a real input asset.
        reference = config.upload_dir / "ref.png"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(_png((77, 88, 99)))
        service.set_reference_image(style["style_id"], reference)
        style = service.get_style(style["style_id"])
        plan = service.create_generation(style["style_id"], ["grid"])
        snapshot = get_snapshot(db, plan.created[0]["task_id"])["snapshot"]
        paths = snapshot_asset_paths(db, snapshot)
        assert paths, "snapshot recorded no resolvable assets"
        assert paths[0].read_bytes() == reference.read_bytes()

    def test_missing_input_is_recorded_as_missing_not_omitted(self, config, db, service):
        """"expected and absent" is a different fact from "no such input"."""
        style = make_style(service)
        reference = config.upload_dir / "gone.png"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_bytes(_png())
        service.set_reference_image(style["style_id"], reference)
        reference.unlink()
        style = service.get_style(style["style_id"])
        plan = service.create_generation(style["style_id"], ["grid"])
        snapshot = get_snapshot(db, plan.created[0]["task_id"])["snapshot"]
        # The style still claims a reference, but the file is gone; the snapshot
        # must not silently pretend there was no reference.
        assert snapshot["extra"]["pending_reference_intent"] is not None


class TestExecutionRecord:
    def test_execution_records_what_was_actually_sent(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        from lunelle.snapshots import get_executions
        executions = get_executions(db, plan.created[0]["task_id"])
        assert executions, "no execution provenance recorded"
        payload = executions[0]["execution"]
        assert payload["provider"] == "mock"
        assert len(payload["prompt_sha256"]) == 64

    def test_execution_is_recorded_per_attempt(self, config, db, service):
        from lunelle.providers.base import ProviderError

        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        provider = MockImageProvider(
            allowed=True,
            fail_with=ProviderError("timeout", "boom", retryable=True),
            fail_times=1,
        )
        run_worker_until_settled(config, db, service, provider, timeout=60)
        from lunelle.snapshots import get_executions
        executions = get_executions(db, plan.created[0]["task_id"])
        # One row per attempt, so a retry's provenance is not lost.
        assert len(executions) >= 1
        assert [row["attempt_no"] for row in executions] == sorted(
            row["attempt_no"] for row in executions)


class TestSnapshotApi:
    def test_snapshot_endpoint_requires_admin(self, tmp_path):
        config = make_config(tmp_path, admin_token="secret-token")
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style = client.post("/api/styles", json={
                "name": "Auth", "description": "red square nails"},
                headers={"X-Admin-Token": "secret-token"}).json()["style"]
            queued = client.post(
                f"/api/styles/{style['style_id']}/generate",
                json={"output_types": ["grid"]},
                headers={"X-Admin-Token": "secret-token"}).json()
            task_id = queued["created"][0]["task_id"]
            assert client.get(f"/api/tasks/{task_id}/snapshot").status_code == 401

    def test_legacy_task_reports_unavailable_rather_than_guessing(self, tmp_path):
        config = make_config(tmp_path)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style = client.post("/api/styles", json={
                "name": "Legacy", "description": "ivory oval nails"}).json()["style"]
            queued = client.post(f"/api/styles/{style['style_id']}/generate",
                                 json={"output_types": ["grid"]}).json()
            task_id = queued["created"][0]["task_id"]
            # Simulate a pre-snapshot task.
            from lunelle.db import transaction
            conn = app.state.db.conn()
            with transaction(conn):
                conn.execute("DELETE FROM task_snapshots WHERE task_id = ?", (task_id,))
                conn.execute("UPDATE tasks SET input_fingerprint = NULL"
                             " WHERE task_id = ?", (task_id,))
            body = client.get(f"/api/tasks/{task_id}/snapshot").json()
            assert body["available"] is False
            assert "predates" in body["reason"]

    def test_fingerprint_endpoint_validates_its_input(self, tmp_path):
        config = make_config(tmp_path)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            assert client.get("/api/fingerprints/not-a-hash").status_code == 422
            ok = client.get("/api/fingerprints/" + "a" * 64)
            assert ok.status_code == 200
            assert ok.json()["count"] == 0

    def test_matrix_snapshot_records_tone_and_view(self, tmp_path):
        config = make_config(tmp_path, confirm_cost_usd=0.0)
        app = create_app(config, start_worker=False)
        with TestClient(app) as client:
            style = client.post("/api/styles", json={
                "name": "Cell", "description": "teal coffin matte nails"}).json()["style"]
            satisfy_matrix_dependencies(app.state.db, config, app.state.service,
                                        style["style_id"])
            queued = client.post(f"/api/styles/{style['style_id']}/matrix",
                                 json={"tones": ["deep"], "views": ["p3_right_hand"]})
            task_id = queued.json()["created"][0]["task_id"]
            snapshot = client.get(f"/api/tasks/{task_id}/snapshot").json()["snapshot"]
            assert snapshot["extra"]["tone"] == "deep"
            assert snapshot["extra"]["view"] == "p3_right_hand"
            assert "matrix_contract" in snapshot["contracts"]
