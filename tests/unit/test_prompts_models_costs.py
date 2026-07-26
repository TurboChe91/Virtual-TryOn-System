from __future__ import annotations

import pytest

from lunelle import models
from lunelle.logging_setup import redact
from lunelle.prompts import PROMPT_VERSION, build_prompt_bundle
from lunelle.schemas import StyleSpec
from lunelle.tasks import TaskService


@pytest.fixture
def spec():
    return StyleSpec(
        sku="nail-pearl-french",
        name="Pearl French",
        base_colors=["milky white", "soft pink"],
        elements=["pearl", "gold line", "french tip"],
        texture=["glossy", "translucent"],
        shape="almond",
        length="medium",
        visual_style="luxury minimalist",
        avoid=["cartoon style", "oversized decoration"],
        skin_tone="medium",
    )


class TestPrompts:
    def test_grid_prompt_contract(self, spec):
        bundle = build_prompt_bundle(spec, (2048, 2048), (2048, 2048))
        grid = bundle.grid_prompt
        assert "2 rows and 5 columns" in grid
        assert "exactly 10" in grid
        assert "1:1" in grid
        assert "No hands" in grid
        assert "milky white, soft pink" in grid
        assert "pearl, gold line, french tip" in grid
        assert "almond" in grid
        assert "STRICTLY FORBIDDEN" in grid and "cartoon style" in grid
        assert "watermark" in grid

    def test_wearing_prompt_contract(self, spec):
        bundle = build_prompt_bundle(spec, (2048, 2048), (2048, 2048), with_reference=True)
        wearing = bundle.wearing_prompt
        assert "exactly five fingers" in wearing
        assert "medium brown skin tone" in wearing
        assert "Image 1" in wearing  # reference authority block
        assert "cream draped-fabric" in wearing
        assert "do not swap, duplicate, omit, homogenize" in wearing

    def test_wearing_prompt_without_reference(self, spec):
        bundle = build_prompt_bundle(spec, (1024, 1024), (1024, 1024), with_reference=False)
        assert "Image 1" not in bundle.wearing_prompt

    def test_negative_prompt_includes_avoid(self, spec):
        bundle = build_prompt_bundle(spec, (1024, 1024), (1024, 1024))
        assert "watermark" in bundle.negative_prompt
        assert "extra fingers" in bundle.negative_prompt
        assert "cartoon style" in bundle.negative_prompt

    def test_quality_requirements(self, spec):
        bundle = build_prompt_bundle(spec, (2048, 2048), (1536, 1024))
        assert bundle.quality_requirements["grid"]["nail_count"] == 10
        assert bundle.quality_requirements["grid"]["expected_size"] == "2048x2048"
        assert bundle.quality_requirements["wearing"]["expected_size"] == "1536x1024"
        assert bundle.prompt_version == PROMPT_VERSION


class TestStateMachine:
    @pytest.mark.parametrize("current,new", [
        ("pending", "running"), ("pending", "cancelled"),
        ("running", "success"), ("running", "failed"), ("running", "retrying"),
        ("retrying", "running"), ("retrying", "cancelled"),
        ("failed", "pending"), ("cancelled", "pending"),
    ])
    def test_legal(self, current, new):
        models.check_transition(current, new)

    @pytest.mark.parametrize("current,new", [
        ("success", "running"), ("success", "pending"), ("pending", "success"),
        ("failed", "running"), ("pending", "failed"), ("cancelled", "running"),
        ("running", "pending"),
    ])
    def test_illegal(self, current, new):
        with pytest.raises(models.IllegalTransition):
            models.check_transition(current, new)


class TestRetryClassification:
    @pytest.mark.parametrize("code", ["timeout", "network", "dns", "rate_limited",
                                       "server_error", "download_failed", "interrupted"])
    def test_retryable(self, code):
        assert models.is_retryable(code)

    @pytest.mark.parametrize("code", ["auth_invalid", "bad_request", "content_policy",
                                       "invalid_response", "file_write_failed", "disk_full",
                                       "config_error", None])
    def test_non_retryable(self, code):
        assert not models.is_retryable(code)


class TestIdempotencyKey:
    def test_stable(self):
        a = TaskService.idempotency_key("st_1", "grid", "pv-1", "prompt text", "")
        b = TaskService.idempotency_key("st_1", "grid", "pv-1", "prompt text", "")
        assert a == b

    def test_changes_with_inputs(self):
        base = TaskService.idempotency_key("st_1", "grid", "pv-1", "prompt", "")
        assert TaskService.idempotency_key("st_1", "wearing", "pv-1", "prompt", "") != base
        assert TaskService.idempotency_key("st_1", "grid", "pv-2", "prompt", "") != base
        assert TaskService.idempotency_key("st_1", "grid", "pv-1", "other", "") != base
        assert TaskService.idempotency_key("st_1", "grid", "pv-1", "prompt", "nonce") != base


class TestFileNaming:
    def test_output_path_pattern(self, service, config):
        task = {"sku": "nail-x", "output_type": "grid", "task_id": "tk_abc"}
        path = service.output_file_for(task, 3)
        assert path == config.output_dir / "nail-x" / "nail-x-grid-tk_abc-a3.png"

    def test_paths_differ_per_attempt_and_task(self, service):
        t1 = {"sku": "s", "output_type": "grid", "task_id": "tk_1"}
        t2 = {"sku": "s", "output_type": "grid", "task_id": "tk_2"}
        assert service.output_file_for(t1, 1) != service.output_file_for(t1, 2)
        assert service.output_file_for(t1, 1) != service.output_file_for(t2, 1)


class TestCosts:
    def test_known_model_price(self, config):
        assert config.price_for("gpt-image-1") == 0.17

    def test_unknown_model_fallback(self, config):
        assert config.price_for("who-knows") == 0.05


class TestRedaction:
    def test_sk_key_redacted(self):
        out = redact("calling with sk-Zz9test0FAKEFAKEfakefake1234567890abcd")
        assert "sk-Zz9test0FAKEFAKEfakefake1234567890abcd" in out and "<redacted>" in out
        assert "PDeghp6nocz" not in out

    def test_bearer_redacted(self):
        out = redact("Authorization: Bearer abcdef123456789012345")
        assert "abcdef123456789012345" not in out

    def test_plain_text_untouched(self):
        assert redact("hello world") == "hello world"
