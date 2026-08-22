"""Integration tests for the SOP v2+ correction loop (locked-base local edit)."""

from __future__ import annotations

import pytest

from lunelle.providers.mock import MockImageProvider
from lunelle.snapshots import get_snapshot
from lunelle.tasks import CORRECTION_BUDGET, ConflictError

from .test_worker_flows import make_style, run_worker_until_settled


def settled_success(service, task_id):
    task = service.get_task(task_id)
    assert task["status"] == "success", task
    return task


class TestCorrectionLoop:
    def test_correction_creates_locked_base_edit(self, config, db, service):
        style = make_style(service)
        plan = service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        v1 = settled_success(service, plan.created[0]["task_id"])

        v2 = service.create_correction(
            v1["task_id"],
            correction_text="Swap slots 1 and 3 on the top row; exactly one sun motif "
                            "in the whole image; the other nine nails stay untouched.",
        )
        assert v2["metadata"]["version"] == 2
        assert v2["metadata"]["correction_of"] == v1["task_id"]
        assert "HIGHEST-PRIORITY ATTEMPT CORRECTION" in v2["prompt"]
        assert "LOCAL EDIT" in v2["prompt"]
        assert "Image 1 is the PREVIOUS CANDIDATE" in v2["prompt"]
        assert v2["prompt_version"].endswith("+cr-3")
        roles = [
            item["role"]
            for item in get_snapshot(db, v2["task_id"])["snapshot"]["input_assets"]
        ]
        assert roles[0] == "correction_base"

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        done = settled_success(service, v2["task_id"])
        # The previous candidate rode along as a reference image.
        assert done["metadata"]["reference_used"] is True

        branched = service.create_correction(
            v1["task_id"], correction_text="Branch again from v1 without overwriting v2."
        )
        assert branched["metadata"]["version"] == 3

    def test_correction_requires_success_and_text(self, config, db, service):
        style = make_style(service, name="Guard", description="green oval nails")
        plan = service.create_generation(style["style_id"], ["grid"])
        with pytest.raises(ConflictError):
            service.create_correction(plan.created[0]["task_id"], correction_text="fix it")
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        with pytest.raises(ValueError):
            service.create_correction(plan.created[0]["task_id"], correction_text="   ")

    def test_budget_blocks_sixth_version(self, config, db, service):
        style = make_style(service, name="Budget", description="blue square nails")
        plan = service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        current = plan.created[0]["task_id"]
        for _ in range(CORRECTION_BUDGET - 1):  # v2..v5
            new = service.create_correction(current, correction_text="tighten one motif only")
            run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
            current = new["task_id"]
        with pytest.raises(ConflictError, match="budget exhausted"):
            service.create_correction(current, correction_text="one more")
        # Owner override lets it through, and is recorded.
        v6 = service.create_correction(current, correction_text="owner approved extra try",
                                       owner_override="owner-2026-07-26")
        assert v6["metadata"]["owner_override"] == "owner-2026-07-26"
