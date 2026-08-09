"""Integration tests for the contract-locked hero output type."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from lunelle.prompts import HERO_PROMPT_VERSION
from lunelle.providers.mock import MockImageProvider
from lunelle.schemas import StyleCreateRequest
from lunelle.snapshots import get_snapshot
from lunelle.styles import build_style_spec

from .test_worker_flows import make_style, run_worker_until_settled


def _write_plan_image(config, style_id: str) -> Path:
    path = config.upload_dir / style_id / "plan-test.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    for row in range(2):
        for column in range(5):
            left = 42 + column * 120
            top = 35 + row * 175
            draw.rounded_rectangle(
                (left, top, left + 70 + column * 2, top + 125),
                radius=25,
                fill=(80 + column * 20, 35 + row * 80, 120 + column * 12),
            )
    image.save(path)
    return path


def _write_photo(config, style_id: str) -> Path:
    path = config.upload_dir / style_id / "photo-test.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (768, 512), (225, 194, 174)).save(path)
    return path


def _satisfy_hero_inputs(config, service, style_id: str) -> None:
    service.set_plan_image(style_id, _write_plan_image(config, style_id))
    service.set_reference_image(style_id, _write_photo(config, style_id))


class TestHeroGeneration:
    def test_hero_with_uploaded_plan_succeeds(self, config, db, service):
        style = make_style(service)
        _satisfy_hero_inputs(config, service, style["style_id"])
        service.set_identity_text(style["style_id"], "- nail-01: milky white almond with one pearl.")

        plan = service.create_generation(style["style_id"], ["hero"])
        assert len(plan.created) == 1
        task_id = plan.created[0]["task_id"]

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        task = service.get_task(task_id)
        assert task["status"] == "success"
        assert task["output_type"] == "hero"
        assert task["prompt_version"] == HERO_PROMPT_VERSION
        assert task["metadata"]["generation_mode"] == "precision"
        assert Path(task["output_path"]).is_file()
        # Compiled Hero View Plan is Image 1 and photography reference is Image 2.
        assert task["metadata"]["reference_used"] is True
        roles = [
            entry["role"]
            for entry in get_snapshot(db, task_id)["snapshot"]["input_assets"]
        ]
        assert roles == ["view_plan", "photography_reference"]
        assert task["qa"] is not None
        # Contract text made it into the stored prompt.
        assert "SCREEN LEFT-to-RIGHT" in task["prompt"]
        # Identity text overrides the spec-derived block.
        assert "nail-01: milky white almond" in task["prompt"]

    def test_hero_without_plan_is_blocked_instead_of_copying_a_generated_grid(
        self, config, db, service,
    ):
        style = make_style(service, name="Hero Chain", description="red square nails glossy")
        service.set_reference_image(
            style["style_id"], _write_photo(config, style["style_id"])
        )
        plan = service.create_generation(style["style_id"], ["grid", "hero"])
        assert len(plan.created) == 2
        by_type = {c["output_type"]: c for c in plan.created}
        hero = service.get_task(by_type["hero"]["task_id"], with_details=False)
        assert hero["wait_for_task_id"] is None

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        hero = service.get_task(by_type["hero"]["task_id"])
        assert hero["status"] == "failed"
        assert hero["error_code"] == "dependency_missing"
        assert "Hero requires an uploaded 2x5 plan" in hero["error_message"]
        assert provider.calls == 1, "only the independent grid may call the provider"

    def test_hero_size_uses_dedicated_config(self, config, db, service):
        style = make_style(service, name="Hero Size", description="green oval nails")
        _satisfy_hero_inputs(config, service, style["style_id"])
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

    def test_correction_preserves_both_hero_authorities_before_the_edit_base(
        self, config, db, service,
    ):
        style = make_style(service, name="Hero Fix", description="gold almond nails")
        _satisfy_hero_inputs(config, service, style["style_id"])
        created = service.create_generation(style["style_id"], ["hero"]).created[0]
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        correction = service.create_correction(
            created["task_id"], correction_text="Only correct nail-04.",
            detail_nails=["nail-04"],
        )
        entries = get_snapshot(db, correction["task_id"])["snapshot"]["input_assets"]
        roles = [entry["role"] for entry in entries]
        assert roles == [
            "view_plan", "photography_reference", "correction_base",
            "correction_detail",
        ]
        assert entries[-1]["derived_from"]["nail_id"] == "nail-04"
        assert "In attachment order they are: nail-04" in correction["prompt"]
