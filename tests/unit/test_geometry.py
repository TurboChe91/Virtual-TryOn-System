"""Request size must follow the reference photo's aspect ratio.

The bug these cover: matrix cells were requested at 2048x2048 while the base hand
photo was 1448x1086 (4:3). `size` is a hard API constraint and "match the crop" is
only prose, so the model stretched the hand vertically by 1.33x on every cell.
"""

from pathlib import Path

import pytest
from PIL import Image

from lunelle.geometry import (
    MIN_PIXELS,
    QUANTUM,
    legal_size_for_ratio,
    size_from_reference,
)


class TestLegalSizeForRatio:
    @pytest.mark.parametrize("width,height", [
        (1448, 1086),   # the hand models actually uploaded (4:3)
        (1672, 941),    # P2 native per the roadmap (16:9)
        (2048, 2048),   # square
        (1086, 1448),   # portrait
        (4000, 1000),   # extreme landscape
        (100, 99),      # near-square, far below the floor
        (3, 2),         # tiny; must still scale up correctly
    ])
    def test_preserves_ratio_within_one_quantum_step(self, width, height):
        w, h = legal_size_for_ratio(width, height)
        source = width / height
        got = w / h
        # A quantum snap can shift the ratio slightly; the stretch it replaces was
        # 33%, so anything under 1% is a non-issue.
        assert abs(got - source) / source < 0.01, f"{width}x{height} -> {w}x{h}"

    @pytest.mark.parametrize("width,height", [
        (1448, 1086), (1672, 941), (2048, 2048), (1086, 1448), (100, 99), (3, 2),
    ])
    def test_always_clears_the_provider_floor(self, width, height):
        # Seedream 4.5: "image size must be at least 3686400 pixels" (real HTTP 400).
        w, h = legal_size_for_ratio(width, height)
        assert w * h >= MIN_PIXELS

    @pytest.mark.parametrize("width,height", [
        (1448, 1086), (1672, 941), (2048, 2048), (100, 99), (3, 2),
    ])
    def test_both_sides_are_quantum_aligned(self, width, height):
        w, h = legal_size_for_ratio(width, height)
        assert w % QUANTUM == 0 and h % QUANTUM == 0

    def test_the_real_uploaded_hand_model_maps_to_a_verified_size(self):
        # 2224x1664 was accepted by the live endpoint and echoed back unchanged.
        assert legal_size_for_ratio(1448, 1086) == (2224, 1664)

    def test_square_reference_stays_square(self):
        w, h = legal_size_for_ratio(2048, 2048)
        assert w == h

    def test_landscape_stays_landscape_and_portrait_stays_portrait(self):
        w, h = legal_size_for_ratio(1448, 1086)
        assert w > h
        w, h = legal_size_for_ratio(1086, 1448)
        assert h > w

    def test_result_is_the_smallest_legal_size_not_an_oversized_one(self):
        # Guards against "just scale to 4K": shrinking either side by one quantum
        # must drop below the floor, i.e. we are at the boundary.
        w, h = legal_size_for_ratio(1448, 1086)
        assert (w - QUANTUM) * h < MIN_PIXELS or w * (h - QUANTUM) < MIN_PIXELS

    @pytest.mark.parametrize("width,height", [(0, 100), (100, 0), (-1, 10)])
    def test_nonpositive_dimensions_are_rejected(self, width, height):
        with pytest.raises(ValueError, match="must be positive"):
            legal_size_for_ratio(width, height)


class TestSizeFromReference:
    def test_reads_the_ratio_off_a_real_file(self, tmp_path):
        path = tmp_path / "hand.png"
        Image.new("RGB", (1448, 1086), (200, 170, 150)).save(path)
        assert size_from_reference(path) == (2224, 1664)

    def test_missing_file_yields_no_opinion_rather_than_raising(self, tmp_path):
        # The caller falls back to its configured size; a paid task must not die
        # over unreadable image metadata.
        assert size_from_reference(tmp_path / "nope.png") is None

    def test_non_image_file_yields_no_opinion(self, tmp_path):
        path = tmp_path / "not-an-image.png"
        path.write_text("this is not a PNG")
        assert size_from_reference(path) is None

    def test_directory_yields_no_opinion(self, tmp_path):
        assert size_from_reference(Path(tmp_path)) is None
