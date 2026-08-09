"""snapshot == worker == provider == QA, asserted on digests.

Nothing in the suite compared these before. Four independent answers to "what was
Image 1" could coexist for one cell, and did:

    snapshot input_assets[0]  the raw plan, labelled kind='hand_model'
    snapshot prompt           "Image 1 is the VIEW PLAN ... copy each tile"
    actually sent             the raw plan (worker re-resolved it live)
    QA's Image 2              the raw plan again, re-read from style.plan_image_path

Every test here compares SHA256s rather than paths or filenames. A path proves
which file was named; only a digest proves which bytes were sent.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from lunelle.assets import digest_of_file
from lunelle.db import transaction, utcnow
from lunelle.providers.mock import MockImageProvider
from lunelle.snapshots import get_executions, get_snapshot
from tests.conftest import satisfy_matrix_dependencies, write_test_image

from .test_worker_flows import make_style, run_worker_until_settled


def queue_cell(config, db, service, *, name="Integrity", tone="light",
               view="p2_open_hands") -> str:
    style = make_style(service, name=name, description="red square nails")
    satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                tones=[tone], views=[view])
    plan = service.create_matrix_generation(style["style_id"], tones=[tone],
                                            views=[view])
    return plan.created[0]["task_id"]


def snapshot_digests(db, task_id: str) -> dict[str, str]:
    """{role: digest} as frozen at queue time."""
    record = get_snapshot(db, task_id)
    assert record is not None, "every task must have a snapshot"
    return {entry["role"]: entry["digest"]
            for entry in record["snapshot"]["input_assets"]}


class TestTheFourWayEquality:
    """One mock cell, all four views of its inputs, compared by digest."""

    def test_snapshot_equals_what_the_provider_received(self, config, db, service):
        task_id = queue_cell(config, db, service)
        frozen = snapshot_digests(db, task_id)
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        sent = [digest_of_file(path) for path in provider.requested_references[0]]
        assert sent == [frozen["view_plan"], frozen["base_hand"]], (
            "the provider must receive exactly the frozen bytes, in snapshot order"
        )

    def test_manifest_records_the_bytes_that_went_out(self, config, db, service):
        task_id = queue_cell(config, db, service)
        frozen = snapshot_digests(db, task_id)
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        manifest = get_executions(db, task_id)[0]["execution"]["request"]
        by_role = {img["role"]: img["sha256"] for img in manifest["input_images"]}
        assert by_role == frozen
        # And the manifest agrees with the provider's own account of the call.
        assert [img["sha256"] for img in manifest["input_images"]] == \
               [digest_of_file(p) for p in provider.requested_references[0]]

    def test_qa_judges_the_images_the_provider_saw(self, config, db, service,
                                                   monkeypatch):
        """QA used to re-read style.plan_image_path and re-resolve the hand model.

        It therefore scored a cell against images the provider never received —
        tk_47e7d1a54d was passed at 100 while its actual placement was 6/10, because
        the judge was reading the uncaptioned raw plan.
        """
        seen: list[list[Path]] = []

        def fake_verdict(chat, images, identity, output_type, *, visible_nails=None):
            seen.append(list(images))
            return {"passed": True, "issues": [], "correction": ""}

        monkeypatch.setattr("lunelle.llm.auto_qa_verdict", fake_verdict)
        monkeypatch.setattr("lunelle.llm.build_llm_chat",
                            lambda config, db: (lambda s, u, i: "{}"))

        task_id = queue_cell(config, db, service)
        frozen = snapshot_digests(db, task_id)
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        assert seen, "the LLM judge must have run"
        judged = seen[0]
        output_path = Path(service.get_task(task_id)["output_path"])
        assert digest_of_file(judged[0]) == digest_of_file(output_path), \
            "Image 1 to the judge is the candidate"
        assert digest_of_file(judged[1]) == frozen["view_plan"], \
            "Image 2 must be the view-plan the provider received, not the raw plan"
        assert digest_of_file(judged[2]) == frozen["base_hand"], \
            "Image 3 must be the base hand the provider received"

    def test_prompt_size_and_model_all_match_the_snapshot(self, config, db, service):
        task_id = queue_cell(config, db, service)
        record = get_snapshot(db, task_id)["snapshot"]
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        import hashlib

        manifest = get_executions(db, task_id)[0]["execution"]["request"]
        assert manifest["prompt_sha256"] == \
               hashlib.sha256(record["prompt"].encode()).hexdigest()
        assert manifest["size_requested"] == record["size"]
        assert provider.requested_sizes[0] == tuple(record["size"])
        assert manifest["model"] == record["channel"]["model"]

    def test_view_plan_is_compiled_at_queue_time_not_execution(
            self, config, db, service):
        """The digest must exist before any worker runs.

        Compiling at execution time is what let the prompt (frozen at queue time,
        describing captioned tiles) and the image (produced later, by whatever code
        was loaded) come from two different moments.
        """
        task_id = queue_cell(config, db, service)
        frozen = snapshot_digests(db, task_id)
        stored = db.conn().execute(
            "SELECT kind, path FROM assets WHERE digest = ?", (frozen["view_plan"],)
        ).fetchone()
        assert stored is not None, "the view-plan must be in the store before execution"
        assert stored["kind"] == "view_plan"
        assert Path(stored["path"]).is_file()
        assert digest_of_file(Path(stored["path"])) == frozen["view_plan"]

    def test_execution_row_reports_the_match(self, config, db, service):
        task_id = queue_cell(config, db, service)
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        row = db.conn().execute(
            "SELECT matches_snapshot, snapshot_fingerprint, input_digests, prompt_sha256,"
            " size_requested FROM task_executions WHERE task_id = ?", (task_id,)
        ).fetchone()
        assert row["matches_snapshot"] == 1
        assert row["snapshot_fingerprint"] == \
               service.get_task(task_id, with_details=False)["input_fingerprint"]
        frozen = snapshot_digests(db, task_id)
        assert row["input_digests"] == (
            f"view_plan:{frozen['view_plan'][:12]} base_hand:{frozen['base_hand'][:12]}"
        )
        assert row["size_requested"] == "x".join(
            str(v) for v in get_snapshot(db, task_id)["snapshot"]["size"])


class TestCurrentStateCannotReachAQueuedTask:
    """Changing settings after queueing must not change what a queued task sends.

    Each of these mutations used to flow straight into the next execution, because
    the worker re-derived its inputs from current state at execution time.
    """

    def test_replacing_the_plan_image_does_not_change_the_frozen_view_plan(
            self, config, db, service):
        task_id = queue_cell(config, db, service, name="PlanSwap")
        frozen = snapshot_digests(db, task_id)

        style = service.get_style(service.get_task(task_id)["style_id"])
        Image.new("RGB", (500, 250), (10, 20, 30)).save(style["plan_image_path"])

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        assert service.get_task(task_id)["status"] == "success"
        assert digest_of_file(provider.requested_references[0][0]) == frozen["view_plan"], (
            "the cell must send the view-plan compiled from the plan as it was queued"
        )

    def test_replacing_the_hand_model_does_not_change_the_frozen_base(
            self, config, db, service):
        task_id = queue_cell(config, db, service, name="HandSwap")
        frozen = snapshot_digests(db, task_id)

        # Overwrite the settings path the worker used to read at execution time.
        row = db.conn().execute(
            "SELECT value FROM app_settings WHERE key = 'hand_model_light_p2_open_hands'"
        ).fetchone()
        Image.new("RGB", (256, 256), (1, 2, 3)).save(row["value"])

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        assert service.get_task(task_id)["status"] == "success"
        assert digest_of_file(provider.requested_references[0][1]) == frozen["base_hand"]

    def test_pointing_the_setting_at_a_different_file_is_ignored(
            self, config, db, service):
        task_id = queue_cell(config, db, service, name="HandRepoint")
        frozen = snapshot_digests(db, task_id)

        other = write_test_image(config.upload_dir / "someone-elses-hand.png", (256, 256),
                                color=(9, 9, 9))
        with transaction(db.conn()):
            db.conn().execute(
                "UPDATE app_settings SET value = ?, updated_at = ?"
                " WHERE key = 'hand_model_light_p2_open_hands'", (str(other), utcnow()))

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        assert digest_of_file(provider.requested_references[0][1]) == frozen["base_hand"]

    def test_editing_the_prompt_column_blocks_the_call(self, config, db, service):
        """`tasks.prompt` is no longer an input; disagreeing with it must stop the call.

        The worker took the prompt from this column while the snapshot held its own
        copy, and nothing compared them — `prompt_differs_from_snapshot` was declared
        and never filled by any caller.
        """
        task_id = queue_cell(config, db, service, name="PromptEdit")
        with transaction(db.conn()):
            db.conn().execute(
                "UPDATE tasks SET prompt = ? WHERE task_id = ?",
                ("something else entirely", task_id))

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(task_id)
        # The snapshot is authoritative, so the edited column is simply not used and
        # the frozen prompt still goes out.
        assert task["status"] == "success"
        assert provider.calls == 1
        record = get_snapshot(db, task_id)["snapshot"]
        import hashlib

        manifest = get_executions(db, task_id)[0]["execution"]["request"]
        assert manifest["prompt_sha256"] == \
               hashlib.sha256(record["prompt"].encode()).hexdigest()

    def test_deleting_a_frozen_asset_blocks_and_spends_nothing(self, config, db, service):
        task_id = queue_cell(config, db, service, name="AssetGone")
        frozen = snapshot_digests(db, task_id)
        stored = db.conn().execute(
            "SELECT path FROM assets WHERE digest = ?", (frozen["view_plan"],)
        ).fetchone()
        Path(stored["path"]).unlink()

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert provider.calls == 0, "a missing frozen input must not be substituted"
        assert "could not be loaded" in task["error_message"]

    def test_tampering_with_a_frozen_asset_blocks(self, config, db, service):
        """Bytes are re-hashed on load: same name, different content is refused.

        Inside the store this should be impossible (the filename is the hash), which
        is exactly why it must be verified rather than assumed.
        """
        task_id = queue_cell(config, db, service, name="AssetTampered")
        frozen = snapshot_digests(db, task_id)
        stored = db.conn().execute(
            "SELECT path FROM assets WHERE digest = ?", (frozen["base_hand"],)
        ).fetchone()
        Image.new("RGB", (256, 256), (200, 0, 0)).save(stored["path"])

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert provider.calls == 0

    def test_switching_the_api_channel_blocks(self, config, db, service):
        """A queued task must not run on a channel its snapshot never described."""
        from lunelle.profiles import ProfileService

        task_id = queue_cell(config, db, service, name="ChannelSwap")
        profiles = ProfileService(db)
        created = profiles.create(name="other", base_url="https://other.example.com/v1",
                                  api_key="sk-different-key", model="mock-model")
        profiles.activate(created["profile_id"])

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert "channel changed" in task["error_message"]
        assert provider.calls == 0


class TestBlockedTasksStillRecover:
    def test_manual_retry_refreezes_after_the_operator_uploads(self, config, db, service):
        """The blocked-then-recovered path must keep working.

        A cell blocked for a missing hand model froze "this input was absent". Since
        the worker executes only from the snapshot, recovery requires re-planning —
        which is what an explicit retry means, and it happens while the task is not
        claimable.
        """
        style = make_style(service, name="Recovers", description="rose square nails")
        plan_image = write_test_image(config.upload_dir / "plan-recovers.png", (500, 250))
        service.set_plan_image(style["style_id"], plan_image)
        created = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        task_id = created.created[0]["task_id"]

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        assert service.get_task(task_id)["error_code"] == "dependency_missing"
        assert provider.calls == 0
        before = get_snapshot(db, task_id)["input_fingerprint"]

        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"])
        service.manual_retry(task_id, note="hand model uploaded")
        after = get_snapshot(db, task_id)
        assert after["input_fingerprint"] != before, "the retry must re-plan"
        assert not after["snapshot"]["unresolved_inputs"]

        provider2 = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider2)
        task = service.get_task(task_id)
        assert task["status"] == "success"
        frozen = snapshot_digests(db, task_id)
        assert digest_of_file(provider2.requested_references[0][1]) == frozen["base_hand"]

    def test_attempt_history_shows_both_plans(self, config, db, service):
        """Re-planning must not erase which plan an earlier attempt ran against."""
        style = make_style(service, name="TwoPlans", description="rose square nails")
        plan_image = write_test_image(config.upload_dir / "plan-two.png", (500, 250))
        service.set_plan_image(style["style_id"], plan_image)
        created = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        task_id = created.created[0]["task_id"]
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"])
        service.manual_retry(task_id)
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        # The blocked attempt never reached the provider, so it has no manifest; the
        # successful one records the fingerprint it ran against.
        rows = db.conn().execute(
            "SELECT attempt_no, snapshot_fingerprint FROM task_executions"
            " WHERE task_id = ? ORDER BY attempt_no", (task_id,)
        ).fetchall()
        assert rows, "the successful attempt must be recorded"
        assert rows[-1]["snapshot_fingerprint"] == get_snapshot(db, task_id)["input_fingerprint"]


class TestOldSnapshotsAreRefused:
    def test_a_version_1_snapshot_will_not_execute(self, config, db, service):
        """v1 tagged every input `kind` and none `role`, so Image 1 is unrecoverable."""
        task_id = queue_cell(config, db, service, name="OldSnapshot")
        record = get_snapshot(db, task_id)
        old = dict(record["snapshot"], snapshot_version=1)
        # A real v1 row carries the old version in BOTH places.
        with transaction(db.conn()):
            db.conn().execute(
                "UPDATE task_snapshots SET snapshot_version = 1, snapshot_json = ?"
                " WHERE task_id = ?", (json.dumps(old), task_id))

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert "version 1" in task["error_message"]
        assert provider.calls == 0

    def test_a_tampered_version_disagreement_will_not_execute(self, config, db, service):
        """The JSON version and the column must agree; disagreement means edited."""
        task_id = queue_cell(config, db, service, name="VersionSkew")
        with transaction(db.conn()):
            db.conn().execute(
                "UPDATE task_snapshots SET snapshot_version = 1 WHERE task_id = ?",
                (task_id,))

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        assert service.get_task(task_id)["status"] == "failed"
        assert provider.calls == 0

    def test_a_task_without_a_snapshot_will_not_execute(self, config, db, service):
        task_id = queue_cell(config, db, service, name="NoSnapshot")
        with transaction(db.conn()):
            db.conn().execute("DELETE FROM task_snapshots WHERE task_id = ?", (task_id,))

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(task_id)
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert provider.calls == 0
