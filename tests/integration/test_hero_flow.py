"""Integration tests for the contract-locked hero output type."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw

from lunelle.assets import digest_of_file
from lunelle.prompts import HERO_PROMPT_VERSION
from lunelle.providers.base import GenerationResult
from lunelle.providers.mock import MockImageProvider
from lunelle.schemas import StyleCreateRequest
from lunelle.snapshots import get_snapshot
from lunelle.styles import build_style_spec
from lunelle.worker import _image_request_extra

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
    def test_hero_qa_receives_photography_reference_as_image_three(
        self, config, db, service, monkeypatch,
    ):
        captured: list[Path] = []

        def fake_verdict(chat, images, identity, output_type, *, visible_nails=None):
            captured.extend(images)
            return {
                "passed": False,
                "issues": ["nail-04 motif is wrong"],
                "nails": {"nail-04": {"status": "fail", "issue": "wrong motif"}},
                "hand_geometry": {"status": "pass", "issues": []},
                "set_counts": {"status": "pass", "issues": []},
                "target_nails": ["nail-04"],
                "correction": "Only correct nail-04. Preserve all other nails.",
            }

        monkeypatch.setattr("lunelle.llm.auto_qa_verdict", fake_verdict)
        monkeypatch.setattr("lunelle.llm.build_llm_chat",
                            lambda config, db: (lambda s, u, i: "{}"))
        style = make_style(service, name="Hero Judge", description="silver oval nails")
        _satisfy_hero_inputs(config, service, style["style_id"])
        task_id = service.create_generation(style["style_id"], ["hero"]).created[0]["task_id"]
        snapshot = get_snapshot(db, task_id)["snapshot"]
        photo_digest = next(
            entry["digest"] for entry in snapshot["input_assets"]
            if entry["role"] == "photography_reference"
        )
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        assert len(captured) == 3
        assert digest_of_file(captured[2]) == photo_digest
        task = service.get_task(task_id)
        verdict = task["qa_advisory"]["checks"]["llm_identity_qa"]
        assert verdict["target_nails"] == ["nail-04"]
        # Hero defaults to Precision, so advisory failure waits for the explicit
        # one-click operator action instead of silently starting an edit loop.
        assert service.descendants_of(task_id) == []

    def test_hero_with_uploaded_plan_succeeds(self, config, db, service):
        style = make_style(service)
        _satisfy_hero_inputs(config, service, style["style_id"])
        service.set_identity_text(style["style_id"], "- nail-01: milky white almond with one pearl.")

        plan = service.create_generation(style["style_id"], ["hero"])
        assert len(plan.created) == 1
        task_id = plan.created[0]["task_id"]

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

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
        assert _image_request_extra({"output_type": "hero", "model": "gpt-image-2"}) == {
            "quality": "high",
            "output_format": "jpeg",
            "output_compression": "90",
        }

    def test_jpeg_provider_result_is_persisted_as_real_png(self, config, db, service):
        class JpegHeroProvider(MockImageProvider):
            def generate(self, request):
                result = super().generate(request)
                with Image.open(io.BytesIO(result.image_bytes)) as image:
                    jpeg = io.BytesIO()
                    image.convert("RGB").save(jpeg, format="JPEG", quality=90)
                return GenerationResult(
                    image_bytes=jpeg.getvalue(),
                    external_request_id=result.external_request_id,
                    actual_cost_usd=result.actual_cost_usd,
                    reference_used=result.reference_used,
                    response_meta={**result.response_meta, "source_format": "jpeg"},
                )

        style = make_style(service, name="JPEG Hero", description="red almond nails")
        _satisfy_hero_inputs(config, service, style["style_id"])
        task_id = service.create_generation(style["style_id"], ["hero"]).created[0]["task_id"]

        run_worker_until_settled(config, db, service, JpegHeroProvider(allowed=True))

        task = service.get_task(task_id)
        output = Path(task["output_path"])
        assert task["status"] == "success"
        assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(output) as image:
            assert image.format == "PNG"
            assert image.size == config.hero_size
        assert task["metadata"]["stored_image_format"] == "png"
        assert task["metadata"]["image_sha256"] == digest_of_file(output)

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

    def test_correction_puts_edit_base_before_both_hero_authorities(
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
            "correction_base", "view_plan", "photography_reference",
            "correction_detail",
        ]
        assert entries[-1]["derived_from"]["nail_id"] == "nail-04"
        assert "Image 1 is the PREVIOUS CANDIDATE" in correction["prompt"]
        assert "Image 2 is the HERO VIEW PLAN" in correction["prompt"]
        assert "Image 3 is the photography reference" in correction["prompt"]
        assert "Image 1 is the HERO VIEW PLAN" not in correction["prompt"]
        assert "In attachment order they are: nail-04" in correction["prompt"]
        assert correction["prompt_version"].endswith("+cr-3")
