"""Nail Slot mask derivation.

The headline test here is the mask polarity one. Everything else guards the
derivation contract; that one guards against a plausible-looking "fix" that would
silently break every matrix cell.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from lunelle.nailslots import (
    COLOR_TO_NAIL,
    MASK_EDITABLE_IS_OPAQUE,
    NAIL_ANATOMY,
    NAIL_COLORS,
    SlotError,
    bbox_of,
    build_mask,
    count_blobs,
    derive_slots,
    find_color_regions,
    verify_alignment,
)


def write_annotation(path, size=(40, 30), regions=None):
    """A synthetic annotation: flat background plus solid colour rectangles."""
    image = Image.new("RGB", size, (200, 180, 160))
    pixels = image.load()
    for nail_id, (x0, y0, x1, y1) in (regions or {}).items():
        colour = NAIL_COLORS[nail_id]
        for y in range(y0, y1):
            for x in range(x0, x1):
                pixels[x, y] = colour
    image.save(path)
    return path


def write_base(path, size=(40, 30)):
    Image.new("RGB", size, (200, 180, 160)).save(path)
    return path


class TestColorContract:
    def test_every_nail_has_a_colour(self):
        assert sorted(NAIL_COLORS) == [f"nail-{i:02d}" for i in range(1, 11)]

    def test_colours_are_unique(self):
        # A duplicate would silently merge two nails into one region.
        assert len(COLOR_TO_NAIL) == 10

    def test_colours_are_far_from_skin_and_fabric(self):
        # Fully saturated with channels in {0,128,255}: no natural photo tone
        # lands here, so exact matching cannot pick up background pixels.
        for colour in NAIL_COLORS.values():
            assert all(v in (0, 128, 255) for v in colour)
            assert max(colour) - min(colour) >= 128

    def test_anatomy_covers_every_nail_and_binds_five_per_hand(self):
        assert sorted(NAIL_ANATOMY) == sorted(NAIL_COLORS)
        left = [n for n, (h, _) in NAIL_ANATOMY.items() if h == "left"]
        right = [n for n, (h, _) in NAIL_ANATOMY.items() if h == "right"]
        assert len(left) == 5 and len(right) == 5
        assert NAIL_ANATOMY["nail-01"] == ("left", "thumb")
        assert NAIL_ANATOMY["nail-10"] == ("right", "pinky")

    def test_each_hand_has_each_finger_exactly_once(self):
        for hand in ("left", "right"):
            fingers = [f for _, (h, f) in NAIL_ANATOMY.items() if h == hand]
            assert sorted(fingers) == ["index", "middle", "pinky", "ring", "thumb"]


class TestMaskPolarity:
    """The relay inverts the documented OpenAI convention.

    Measured on the live endpoint with the same base photo and prompt, varying
    only the mask:

        nail transparent (OpenAI docs): nail changed   4.4, rest changed 4.7
        no mask:                        nail changed 192.1, rest changed 3.9
        nail opaque (inverted):         nail changed 177.4, rest changed 5.6

    Under the documented polarity the nail was the region left ALONE. If this is
    ever "corrected" to match the OpenAI docs, masks appear to stop working and
    the cause is very hard to see from the symptom.
    """

    def test_editable_region_is_opaque(self):
        assert MASK_EDITABLE_IS_OPAQUE is True

    def test_target_nail_is_opaque_and_everything_else_transparent(self):
        coords = [(x, y) for y in range(5, 10) for x in range(5, 10)]
        mask = Image.open(io.BytesIO(build_mask(coords, (20, 20)))).convert("RGBA")
        pixels = mask.load()
        assert pixels[7, 7][3] == 255, "the nail being repainted must be OPAQUE"
        assert pixels[0, 0][3] == 0, "everything preserved must be TRANSPARENT"

    def test_mask_alpha_is_strictly_binary(self):
        # A soft edge would make the editable region ambiguous.
        coords = [(x, y) for y in range(5, 10) for x in range(5, 10)]
        mask = Image.open(io.BytesIO(build_mask(coords, (20, 20)))).convert("RGBA")
        assert {p[3] for p in mask.getdata()} <= {0, 255}

    def test_mask_matches_the_source_dimensions(self):
        mask = Image.open(io.BytesIO(build_mask([(1, 1)], (37, 23))))
        assert mask.size == (37, 23)

    def test_opaque_pixel_count_equals_the_region_size(self):
        coords = [(x, y) for y in range(3, 9) for x in range(2, 11)]
        mask = Image.open(io.BytesIO(build_mask(coords, (20, 20)))).convert("RGBA")
        opaque = sum(1 for p in mask.getdata() if p[3] == 255)
        assert opaque == len(coords)


class TestRegionDetection:
    def test_finds_each_colour(self, tmp_path):
        path = write_annotation(tmp_path / "a.png", regions={
            "nail-01": (2, 2, 8, 8), "nail-07": (20, 5, 28, 12)})
        regions = find_color_regions(path)
        assert set(regions) == {"nail-01", "nail-07"}
        assert len(regions["nail-01"]) == 36

    def test_ignores_background(self, tmp_path):
        path = write_annotation(tmp_path / "a.png", regions={"nail-01": (2, 2, 5, 5)})
        assert set(find_color_regions(path)) == {"nail-01"}

    def test_bbox_is_inclusive(self):
        assert bbox_of([(2, 3), (5, 9)]) == (2, 3, 4, 7)

    def test_single_rectangle_is_one_blob(self):
        coords = [(x, y) for y in range(4) for x in range(4)]
        assert count_blobs(coords) == [16]

    def test_two_separated_rectangles_are_two_blobs(self):
        coords = [(x, y) for y in range(3) for x in range(3)]
        coords += [(x, y) for y in range(3) for x in range(10, 13)]
        assert count_blobs(coords) == [9, 9]

    def test_diagonal_touch_is_not_connected(self):
        # 4-connectivity: a diagonal-only join means two separate marks.
        assert count_blobs([(0, 0), (1, 1)]) == [1, 1]


class TestDeriveSlots:
    def test_derives_one_slot_per_colour_with_anatomy(self, tmp_path):
        path = write_annotation(tmp_path / "a.png", regions={
            "nail-01": (2, 2, 8, 8), "nail-06": (20, 5, 28, 12)})
        slots = derive_slots(path)
        assert [s.nail_id for s in slots] == ["nail-01", "nail-06"]
        assert (slots[0].hand, slots[0].finger) == ("left", "thumb")
        assert (slots[1].hand, slots[1].finger) == ("right", "thumb")

    def test_area_and_bbox_describe_the_region(self, tmp_path):
        path = write_annotation(tmp_path / "a.png", regions={"nail-01": (2, 3, 8, 7)})
        slot = derive_slots(path)[0]
        assert slot.area_px == 6 * 4
        assert slot.bbox == (2, 3, 6, 4)

    def test_missing_expected_nail_is_refused(self, tmp_path):
        path = write_annotation(tmp_path / "a.png", regions={"nail-01": (2, 2, 8, 8)})
        with pytest.raises(SlotError, match="do not match the view contract"):
            derive_slots(path, expected_nails=["nail-01", "nail-02"])

    def test_unexpected_extra_nail_is_refused(self, tmp_path):
        path = write_annotation(tmp_path / "a.png", regions={
            "nail-01": (2, 2, 8, 8), "nail-02": (20, 2, 26, 8)})
        with pytest.raises(SlotError, match="do not match the view contract"):
            derive_slots(path, expected_nails=["nail-01"])

    def test_fragmented_region_is_refused(self, tmp_path):
        # Two equal halves for one nail: an annotation mistake, not a nail.
        image = Image.new("RGB", (40, 30), (200, 180, 160))
        pixels = image.load()
        for y in range(4, 10):
            for x in range(2, 8):
                pixels[x, y] = NAIL_COLORS["nail-01"]
            for x in range(25, 31):
                pixels[x, y] = NAIL_COLORS["nail-01"]
        path = tmp_path / "frag.png"
        image.save(path)
        with pytest.raises(SlotError, match="fragmented"):
            derive_slots(path)

    def test_a_few_stray_pixels_are_tolerated(self, tmp_path):
        # Real annotations carry occasional 1px noise; the big region still wins.
        image = Image.new("RGB", (60, 60), (200, 180, 160))
        pixels = image.load()
        for y in range(5, 35):
            for x in range(5, 35):
                pixels[x, y] = NAIL_COLORS["nail-01"]
        pixels[50, 50] = NAIL_COLORS["nail-01"]
        path = tmp_path / "noise.png"
        image.save(path)
        slots = derive_slots(path)
        assert slots[0].area_px == 30 * 30 + 1

    def test_empty_annotation_is_refused(self, tmp_path):
        path = write_annotation(tmp_path / "blank.png")
        with pytest.raises(SlotError, match="no annotation colours"):
            derive_slots(path)

    def test_derivation_is_deterministic(self, tmp_path):
        path = write_annotation(tmp_path / "a.png", regions={"nail-03": (2, 2, 9, 9)})
        first = derive_slots(path)[0]
        second = derive_slots(path)[0]
        assert first.digest == second.digest


class TestAlignment:
    def test_identical_outside_the_regions_passes(self, tmp_path):
        base = write_base(tmp_path / "base.png")
        ann = write_annotation(tmp_path / "ann.png", regions={"nail-01": (2, 2, 8, 8)})
        report = verify_alignment(ann, base)
        assert report["max_diff"] == 0

    def test_size_mismatch_is_refused(self, tmp_path):
        # The real p2 case: a 1672x941 base cannot serve 1448x1086 annotations.
        base = write_base(tmp_path / "base.png", size=(50, 40))
        ann = write_annotation(tmp_path / "ann.png", size=(40, 30),
                               regions={"nail-01": (2, 2, 8, 8)})
        with pytest.raises(SlotError, match="differ in size"):
            verify_alignment(ann, base)

    def test_a_different_photo_is_refused(self, tmp_path):
        base = Image.new("RGB", (40, 30), (100, 100, 100))
        base_path = tmp_path / "other.png"
        base.save(base_path)
        ann = write_annotation(tmp_path / "ann.png", regions={"nail-01": (2, 2, 8, 8)})
        with pytest.raises(SlotError, match="not painted on this photo"):
            verify_alignment(ann, base_path)

    def test_colour_regions_are_excluded_from_the_comparison(self, tmp_path):
        # The annotation differs from the base exactly where the colours are;
        # that difference is the point and must not count as misalignment.
        base = write_base(tmp_path / "base.png")
        ann = write_annotation(tmp_path / "ann.png",
                               regions={n: (2 + i * 3, 2, 4 + i * 3, 6)
                                        for i, n in enumerate(NAIL_COLORS)})
        assert verify_alignment(ann, base)["max_diff"] == 0
