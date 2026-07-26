"""Integration tests for try-on matrix generation (4 tones x 4 views)."""

from __future__ import annotations

import pytest

from lunelle.prompts import MATRIX_PROMPT_VERSION, MATRIX_TONES, MATRIX_VIEWS
from lunelle.providers.mock import MockImageProvider

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
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light", "deep"], views=["p2_open_hands"])
        assert len(plan.created) == 2
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        for created in plan.created:
            task = service.get_task(created["task_id"])
            assert task["status"] == "success"
            assert task["qa"] is not None  # wearing-style heuristics ran
