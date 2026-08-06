"""The imported Nail Slot assets, checked against the database.

These run only when the assets have actually been imported, so a fresh clone
without the annotation set does not fail. What they guard is the invariant that
survives import: every view holds exactly the nails its contract says are
visible, bound to the right finger, with masks that are real and distinct.
"""

from __future__ import annotations

import sqlite3

import pytest

from lunelle.config import load_config
from lunelle.nailslots import NAIL_ANATOMY, NAIL_COLORS
from lunelle.prompts import MATRIX_CONTRACT, MATRIX_TONES, MATRIX_VIEWS


@pytest.fixture(scope="module")
def real_db():
    config = load_config()
    if not config.db_path.is_file():
        pytest.skip("no local database")
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        count = conn.execute("SELECT count(*) FROM hand_models").fetchone()[0]
    except sqlite3.OperationalError:
        pytest.skip("migration 0010 not applied here")
    if not count:
        pytest.skip("no hand models imported")
    return conn


class TestImportedAssets:
    def test_every_tone_and_view_is_present(self, real_db):
        rows = real_db.execute(
            "SELECT tone, view FROM hand_models WHERE retired_at IS NULL").fetchall()
        got = {(r["tone"], r["view"]) for r in rows}
        expected = {(t, v) for t in MATRIX_TONES for v in MATRIX_VIEWS}
        assert got == expected, f"missing {sorted(expected - got)}"

    def test_each_model_holds_exactly_its_contract_nails(self, real_db):
        for row in real_db.execute(
                "SELECT hand_model_id, view FROM hand_models WHERE retired_at IS NULL"):
            slots = real_db.execute(
                "SELECT nail_id FROM hand_model_slots WHERE hand_model_id = ?",
                (row["hand_model_id"],)).fetchall()
            got = {s["nail_id"] for s in slots}
            expected = set(MATRIX_CONTRACT["views"][row["view"]]["visible_nails"])
            assert got == expected, f"{row['hand_model_id']}: {sorted(got ^ expected)}"

    def test_anatomy_matches_the_code_contract(self, real_db):
        for row in real_db.execute(
                "SELECT nail_id, hand, finger FROM hand_model_slots"):
            assert (row["hand"], row["finger"]) == NAIL_ANATOMY[row["nail_id"]]

    def test_single_hand_views_only_carry_that_hand(self, real_db):
        for view, hand in (("p3_right_hand", "right"), ("p5_left_hand", "left")):
            rows = real_db.execute(
                "SELECT DISTINCT s.hand FROM hand_model_slots s"
                " JOIN hand_models m USING (hand_model_id) WHERE m.view = ?",
                (view,)).fetchall()
            assert [r["hand"] for r in rows] == [hand]

    def test_masks_are_distinct_per_slot(self, real_db):
        total = real_db.execute("SELECT count(*) FROM hand_model_slots").fetchone()[0]
        distinct = real_db.execute(
            "SELECT count(DISTINCT mask_digest) FROM hand_model_slots").fetchone()[0]
        # Two identical masks would mean two nails share a region.
        assert distinct == total

    def test_every_mask_file_exists_on_disk(self, real_db):
        from lunelle.assets import resolve_path
        from lunelle.db import Database

        config = load_config()
        db = Database(config.db_path)
        missing = []
        for row in real_db.execute("SELECT mask_digest FROM hand_model_slots"):
            path = resolve_path(db, row["mask_digest"])
            if path is None or not path.is_file():
                missing.append(row["mask_digest"][:12])
        assert not missing, f"{len(missing)} masks missing on disk"

    def test_bboxes_are_inside_the_photo(self, real_db):
        for row in real_db.execute(
                "SELECT m.width, m.height, s.nail_id, s.bbox_x, s.bbox_y,"
                " s.bbox_w, s.bbox_h FROM hand_model_slots s"
                " JOIN hand_models m USING (hand_model_id)"):
            assert row["bbox_x"] >= 0 and row["bbox_y"] >= 0
            assert row["bbox_x"] + row["bbox_w"] <= row["width"]
            assert row["bbox_y"] + row["bbox_h"] <= row["height"]

    def test_regions_do_not_overlap_within_a_model(self, real_db):
        """Two nails claiming the same pixel would make identity ambiguous."""
        import io

        from PIL import Image

        from lunelle.assets import resolve_path
        from lunelle.db import Database

        config = load_config()
        db = Database(config.db_path)
        model = real_db.execute(
            "SELECT hand_model_id FROM hand_models WHERE view = 'p2_open_hands'"
            " AND retired_at IS NULL LIMIT 1").fetchone()
        claimed: dict[tuple[int, int], str] = {}
        for row in real_db.execute(
                "SELECT nail_id, mask_digest FROM hand_model_slots"
                " WHERE hand_model_id = ?", (model["hand_model_id"],)):
            path = resolve_path(db, row["mask_digest"])
            with Image.open(io.BytesIO(path.read_bytes())) as mask:
                rgba = mask.convert("RGBA")
                pixels = rgba.load()
                w, h = rgba.size
            for y in range(0, h, 3):
                for x in range(0, w, 3):
                    if pixels[x, y][3] == 255:
                        assert (x, y) not in claimed, (
                            f"{row['nail_id']} overlaps {claimed.get((x, y))} at {x},{y}")
                        claimed[(x, y)] = row["nail_id"]
        assert claimed, "no editable pixels found in any mask"

    def test_screen_order_derived_from_pixels_matches_the_prompt_contract(self, real_db):
        """The contract's screen order is prose; the masks are ground truth.

        p2 and p4 order their left hands in opposite directions, which looked like
        a bug until measured — backs-of-hands vs curled fists genuinely reverse
        screen order. This asserts the prose agrees with the pixels.
        """
        for view in ("p2_open_hands", "p4_thumb_visible"):
            model = real_db.execute(
                "SELECT hand_model_id FROM hand_models WHERE view = ? AND tone = 'light'"
                " AND retired_at IS NULL", (view,)).fetchone()
            rows = real_db.execute(
                "SELECT nail_id, hand, finger, bbox_x, bbox_w FROM hand_model_slots"
                " WHERE hand_model_id = ?", (model["hand_model_id"],)).fetchall()
            centres = {r["nail_id"]: r["bbox_x"] + r["bbox_w"] / 2 for r in rows}
            fingers = {r["nail_id"]: (r["hand"], r["finger"]) for r in rows}
            slots = MATRIX_CONTRACT["views"][view]["screen_slots"]
            for key in ("left_upper_fingers_left_to_right",
                        "right_upper_fingers_left_to_right"):
                declared = slots[key]
                measured = sorted(declared, key=lambda n: centres[n])
                assert declared == measured, (
                    f"{view}/{key}: contract says {declared} but pixels say {measured}")
                assert all(fingers[n][1] != "thumb" for n in declared)


class TestColourMappingIsCodeSide:
    def test_mapping_is_not_stored_in_the_database(self, real_db):
        """The colour->id contract must live in code, so a change is reviewable.

        If it were an uploaded asset or a settings row, swapping it would silently
        remap every nail with no diff to review.
        """
        rows = real_db.execute(
            "SELECT key FROM app_settings WHERE key LIKE '%colour%'"
            " OR key LIKE '%color%' OR key LIKE '%nail_map%'").fetchall()
        assert not rows, f"colour mapping leaked into settings: {[r['key'] for r in rows]}"
        assert len(NAIL_COLORS) == 10
