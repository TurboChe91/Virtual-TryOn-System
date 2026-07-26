from __future__ import annotations

import os

import pytest
from PIL import Image, ImageDraw

from lunelle.config import ConfigError, load_config
from lunelle.qa import detect_grid_layout, run_qa
from tests.conftest import make_config


def draw_grid_image(path, cols=5, rows=2, size=800, bg=(245, 240, 232), fg=(120, 60, 60)):
    image = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(image)
    cell_w, cell_h = size // cols, size // rows
    for row in range(rows):
        for col in range(cols):
            cx, cy = col * cell_w + cell_w // 2, row * cell_h + cell_h // 2
            rx, ry = int(cell_w * 0.22), int(cell_h * 0.34)
            draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=fg)
    image.save(path)
    return path


class TestGridDetection:
    def test_detects_2x5(self, tmp_path):
        path = draw_grid_image(tmp_path / "grid.png")
        layout = detect_grid_layout(path)
        assert layout["detected_columns"] == 5
        assert layout["detected_rows"] == 2
        assert layout["estimated_nails"] == 10

    def test_detects_wrong_layout(self, tmp_path):
        path = draw_grid_image(tmp_path / "grid3x2.png", cols=3, rows=2)
        layout = detect_grid_layout(path)
        assert layout["estimated_nails"] == 6


class TestRunQa:
    def make_big_grid(self, tmp_path, size=600):
        # Low-amplitude noise (< foreground threshold 24) inflates the PNG past
        # the min-file-size gate without disturbing grid detection.
        path = tmp_path / "grid.png"
        draw_grid_image(path, size=size)
        import random

        rng = random.Random(42)
        image = Image.open(path).convert("RGB")
        pixels = image.load()
        for x in range(size):
            for y in range(size):
                p = pixels[x, y]
                d = rng.randint(-8, 8)
                pixels[x, y] = (max(0, min(255, p[0] + d)),
                                max(0, min(255, p[1] - d)),
                                max(0, min(255, p[2] + rng.randint(-8, 8))))
        image.save(path)
        assert path.stat().st_size > 30 * 1024, "test image must exceed QA min size"
        return path

    def test_missing_file_hard_fails(self, tmp_path):
        doc = run_qa(output_type="grid", image_path=tmp_path / "nope.png",
                     expected_size=(512, 512), min_side=256)
        assert doc["passed"] is False
        assert doc["recommended_action"] == "regenerate"
        assert doc["score"] == 0

    def test_good_grid_passes_hard_checks(self, tmp_path):
        path = self.make_big_grid(tmp_path)
        doc = run_qa(output_type="grid", image_path=path,
                     expected_size=(600, 600), min_side=256)
        assert doc["checks"]["grid_nail_count"]["estimated_nails"] == 10
        assert doc["checks"]["aspect_ratio"]["passed"] is True
        assert doc["needs_human_review"] is True  # automation never fully approves
        assert doc["recommended_action"] in ("human_review",)

    def test_wrong_aspect_ratio_fails(self, tmp_path):
        path = tmp_path / "wide.png"
        Image.new("RGB", (800, 400), (200, 100, 100)).save(path)
        doc = run_qa(output_type="wearing", image_path=path,
                     expected_size=(512, 512), min_side=256)
        assert doc["checks"]["aspect_ratio"]["passed"] is False
        assert doc["passed"] is False

    def test_low_resolution_fails(self, tmp_path):
        path = tmp_path / "small.png"
        Image.new("RGB", (200, 200), (10, 10, 10)).save(path)
        doc = run_qa(output_type="wearing", image_path=path,
                     expected_size=(512, 512), min_side=512)
        assert doc["checks"]["min_resolution"]["passed"] is False

    def test_unreadable_file_fails(self, tmp_path):
        path = tmp_path / "bad.png"
        path.write_bytes(b"this is not an image at all" * 10)
        doc = run_qa(output_type="grid", image_path=path,
                     expected_size=(512, 512), min_side=256)
        assert doc["passed"] is False
        assert doc["checks"]["file_readable"]["passed"] is False

    def test_color_consistency_reported(self, tmp_path):
        grid = self.make_big_grid(tmp_path)
        doc = run_qa(output_type="wearing", image_path=grid,
                     expected_size=(600, 600), min_side=256, grid_image_path=grid)
        # same image vs itself: colors must match
        check = doc["checks"]["color_consistency"]
        assert check["passed"] is True


class TestConfig:
    def _with_env(self, monkeypatch, **env):
        for key in list(os.environ):
            if key.startswith("LUNELLE_"):
                monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return load_config("/dev/null")

    def test_defaults(self, monkeypatch):
        config = self._with_env(monkeypatch)
        assert config.env == "development"
        assert config.port == 8300
        assert config.grid_size == (2048, 2048)

    def test_invalid_env_rejected(self, monkeypatch):
        with pytest.raises(ConfigError):
            self._with_env(monkeypatch, LUNELLE_ENV="staging")

    def test_invalid_size_rejected(self, monkeypatch):
        with pytest.raises(ConfigError):
            self._with_env(monkeypatch, LUNELLE_GRID_IMAGE_SIZE="huge")

    def test_invalid_port_rejected(self, monkeypatch):
        with pytest.raises(ConfigError):
            self._with_env(monkeypatch, LUNELLE_PORT="99999")

    def test_pricing_override(self, monkeypatch):
        config = self._with_env(monkeypatch, LUNELLE_PRICING_JSON='{"m1": 0.5}')
        assert config.price_for("m1") == 0.5

    def test_bad_pricing_rejected(self, monkeypatch):
        with pytest.raises(ConfigError):
            self._with_env(monkeypatch, LUNELLE_PRICING_JSON="not json")

    def test_serve_validation_missing_key(self, tmp_path):
        config = make_config(tmp_path, image_provider="openai-compat", image_api_key="")
        problems = config.validate_for_serve()
        assert any("LUNELLE_IMAGE_API_KEY" in p for p in problems)

    def test_serve_validation_mock_refused_in_production(self, tmp_path):
        config = make_config(tmp_path, env="production", image_provider="mock")
        assert any("mock" in p for p in config.validate_for_serve())

    def test_serve_validation_debug_refused_in_production(self, tmp_path):
        config = make_config(tmp_path, env="production", debug=True,
                             image_provider="openai-compat",
                             image_api_base_url="https://x.example/v1",
                             image_api_key="sk-x", image_model="m")
        assert any("LUNELLE_DEBUG" in p for p in config.validate_for_serve())

    def test_key_fingerprint_not_key(self, tmp_path):
        config = make_config(tmp_path, image_api_key="sk-something-secret")
        fp = config.key_fingerprint()
        assert len(fp) == 8 and "secret" not in fp
