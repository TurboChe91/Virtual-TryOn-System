"""Integration tests for the contract-locked hero output type."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from lunelle.prompts import HERO_PROMPT_VERSION
from lunelle.providers.mock import MockImageProvider
from lunelle.schemas import StyleCreateRequest
from lunelle.styles import build_style_spec

from .test_worker_flows import make_style, run_worker_until_settled


def _write_plan_image(config, style_id: str) -> Path:
    path = config.upload_dir / style_id / "plan-test.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (512, 256), (240, 235, 228)).save(path)
    return path


class TestHeroGeneration:
    def test_hero_with_uploaded_plan_succeeds(self, config, db, service):
        style = make_style(service)
        service.set_plan_image(style["style_id"], _write_plan_image(config, style["style_id"]))
        service.set_identity_text(style["style_id"], "- nail-01: milky white almond with one pearl.")

        plan = service.create_generation(style["style_id"], ["hero"])
        assert len(plan.created) == 1
        task_id = plan.created[0]["task_id"]

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        task = service.get_task(task_id)
        assert task["status"] == "success"
        assert task["output_type"] == "hero"
        assert task["prompt_version"] == HERO_PROMPT_VERSION
        assert Path(task["output_path"]).is_file()
        # Uploaded plan is Image 1, so the reference really was attached.
        assert task["metadata"]["reference_used"] is True
        assert task["qa"] is not None
        # Contract text made it into the stored prompt.
        assert "SCREEN LEFT-to-RIGHT" in task["prompt"]
        # Identity text overrides the spec-derived block.
        assert "nail-01: milky white almond" in task["prompt"]

    def test_hero_without_plan_waits_for_grid(self, config, db, service):
        style = make_style(service, name="Hero Chain", description="red square nails glossy")
        plan = service.create_generation(style["style_id"], ["grid", "hero"])
        assert len(plan.created) == 2
        by_type = {c["output_type"]: c for c in plan.created}
        hero = service.get_task(by_type["hero"]["task_id"], with_details=False)
        assert hero["wait_for_task_id"] == by_type["grid"]["task_id"]

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        hero = service.get_task(by_type["hero"]["task_id"])
        assert hero["status"] == "success"
        # The grid ran first and served as the hero's Image 1.
        assert hero["metadata"]["reference_used"] is True

    def test_hero_size_uses_dedicated_config(self, config, db, service):
        style = make_style(service, name="Hero Size", description="green oval nails")
        service.set_plan_image(style["style_id"], _write_plan_image(config, style["style_id"]))
        plan = service.create_generation(style["style_id"], ["hero"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        task = service.get_task(plan.created[0]["task_id"])
        with Image.open(task["output_path"]) as image:
            assert image.size == config.hero_size

    def test_generate_rejects_unknown_type(self, service):
        outcome = build_style_spec(
            StyleCreateRequest(name="X", description="blue nails"), service.taken_skus()
        )
        style = service.create_style(
            outcome.spec, source_type=outcome.source_type, source_input={},
            parser=outcome.parser, warnings=outcome.warnings,
        )
        try:
            service.create_generation(style["style_id"], ["matrix_cell"])
        except ValueError as exc:
            assert "matrix_cell" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("matrix_cell must not be generatable yet")
