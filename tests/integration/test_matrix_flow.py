"""Integration tests for try-on matrix generation (4 tones x 4 views)."""

from __future__ import annotations

import pytest

from lunelle.prompts import MATRIX_PROMPT_VERSION, MATRIX_TONES, MATRIX_VIEWS
from lunelle.providers.mock import MockImageProvider
from tests.conftest import satisfy_matrix_dependencies, write_test_image

from .test_worker_flows import make_style, run_worker_until_settled


class TestMatrixGeneration:
    def test_full_matrix_creates_sixteen_cells(self, config, db, service):
        style = make_style(service)
        plan = service.create_matrix_generation(style["style_id"])
        assert len(plan.created) == len(MATRIX_TONES) * len(MATRIX_VIEWS) == 16
        cells = {(c["tone"], c["view"]) for c in plan.created}
        assert len(cells) == 16

        first = service.get_task(plan.created[0]["task_id"], with_details=False)
        assert first["output_type"] == "matrix_cell"
        assert first["prompt_version"] == MATRIX_PROMPT_VERSION
        assert first["metadata"]["tone"] in MATRIX_TONES
        assert first["metadata"]["view"] in MATRIX_VIEWS
        # Tone phrase and view scene actually reached the prompt.
        assert "skin tone" in first["prompt"]
        assert "virtual nail try-on" in first["prompt"]

    def test_matrix_idempotent_and_partial(self, config, db, service):
        style = make_style(service, name="Partial", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"])
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p5_left_hand"])
        assert len(plan.created) == 1

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        again = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p5_left_hand"])
        assert not again.created and len(again.skipped) == 1

    def test_matrix_rejects_unknown_tone(self, service):
        style = make_style(service, name="Bad", description="blue oval nails")
        with pytest.raises(ValueError):
            service.create_matrix_generation(style["style_id"], tones=["neon"])

    def test_matrix_cells_run_and_record_qa(self, config, db, service):
        style = make_style(service, name="RunCells", description="green almond nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"])
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light", "deep"], views=["p2_open_hands"])
        assert len(plan.created) == 2
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        for created in plan.created:
            task = service.get_task(created["task_id"])
            assert task["status"] == "success"
            assert task["qa"] is not None  # wearing-style heuristics ran
            # QA state is explicit, and review opens automatically.
            assert task["qa_state"] == "done"
            assert task["review_state"] == "waiting_human_review"

    def test_matrix_cell_blocks_without_hand_model(self, config, db, service):
        """A cell with no hand model must fail with dependency_missing, never
        silently degrade to a text-only render that looks like a success."""
        style = make_style(service, name="NoHand", description="teal coffin nails")
        # Design authority present, hand model deliberately absent.
        plan_image = write_test_image(config.upload_dir / "plan-nohand.png", (256, 256))
        service.set_plan_image(style["style_id"], plan_image)
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        assert len(plan.created) == 1

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(plan.created[0]["task_id"])
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert "hand model" in task["error_message"]
        # Fail-fast: no provider call, so no cost was incurred.
        assert provider.calls == 0
        assert task["actual_cost_usd"] is None

    def test_matrix_cell_blocks_without_design_authority(self, config, db, service):
        style = make_style(service, name="NoPlan", description="ivory oval nails")
        # Hand model present, design authority (plan/grid) deliberately absent.
        satisfy_matrix_dependencies(db, config, service, style["style_id"])
        service._set_style_column(style["style_id"], "plan_image_path", None)
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)
        task = service.get_task(plan.created[0]["task_id"])
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert "design authority" in task["error_message"]
        assert provider.calls == 0

    def test_blocked_cell_can_be_retried_after_upload(self, config, db, service):
        """dependency_missing is non-retryable automatically but must recover via
        a manual retry once the operator uploads the missing asset."""
        style = make_style(service, name="Recover", description="rose square nails")
        plan_image = write_test_image(config.upload_dir / "plan-recover.png", (256, 256))
        service.set_plan_image(style["style_id"], plan_image)
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        task_id = plan.created[0]["task_id"]

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        assert service.get_task(task_id)["error_code"] == "dependency_missing"
        # No automatic retry was scheduled (retrying would just fail again).
        assert service.get_task(task_id, with_details=False)["retry_count"] == 0

        satisfy_matrix_dependencies(db, config, service, style["style_id"])
        service.manual_retry(task_id, note="hand model uploaded")
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        recovered = service.get_task(task_id)
        assert recovered["status"] == "success"
        assert recovered["qa_state"] == "done"
