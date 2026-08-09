"""Plan cell detection and view-plan compilation.

Why this exists: nail order was conveyed as prose ("LEFT upper SCREEN LEFT-to-RIGHT
= nail-05, nail-04, ..."), which asks the model to infer handedness and count
fingers. Masked per-nail editing was measured as the alternative and does not work
on this channel — asked to repaint nail-01 (left thumb, mask centroid x=44.7%,
y=71.6%) the model painted the right index finger (x=56.1%, y=42.8%). So position
has to be carried by the image the model copies from.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from lunelle.nailslots import NAIL_ANATOMY
from lunelle.planview import (
    PLAN_CELLS,
    PlanError,
    build_spatial_view_plan,
    build_view_plan,
    cell_box,
    compile_view_plan,
    crop_cell,
    detect_cells,
    view_plan_digest,
)
from lunelle.prompts import (
    MATRIX_VIEWS,
    hero_view_plan_spec,
    matrix_view_plan_rows,
    matrix_view_plan_spec,
)


def synthetic_plan(path, *, cols=5, rows=2, uneven=False, margin=40,
                   size=(1000, 500), colours=None):
    """Ten nail-ish blobs on white, optionally unevenly spaced like a real photo."""
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    width, height = size
    usable = width - 2 * margin
    for row in range(rows):
        for col in range(cols):
            # Uneven mode shrinks each successive nail and adds drift, mimicking a
            # real plan where nails are neither centred nor equally sized.
            shrink = 1.0 - (0.09 * col if uneven else 0)
            slot = usable / cols
            cx = margin + slot * col + slot / 2 + (18 * col if uneven else 0)
            cy = height * (0.27 + 0.46 * row)
            rx = slot * 0.30 * shrink
            ry = height * 0.17
            index = row * cols + col + 1
            fill = (colours or {}).get(f"nail-{index:02d}", (40, 90 + index * 12, 60))
            draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=fill)
    image.save(path)
    return path


class TestCellDetection:
    def test_finds_ten_cells_on_an_even_plan(self, tmp_path):
        plan = synthetic_plan(tmp_path / "even.png")
        cells = detect_cells(plan)
        assert sorted(cells) == sorted(PLAN_CELLS)

    def test_finds_ten_cells_when_spacing_is_uneven(self, tmp_path):
        """The real defect: a uniform fifths split cut nails in half.

        On the first real plan the nails spanned 19%-84% of the width with column
        widths from 121px to 208px, so equal slicing left two tiles empty.
        """
        plan = synthetic_plan(tmp_path / "uneven.png", uneven=True)
        cells = detect_cells(plan)
        assert sorted(cells) == sorted(PLAN_CELLS)
        widths = [box[2] - box[0] for box in cells.values()]
        assert max(widths) - min(widths) > 5, "fixture should be genuinely uneven"

    def test_grid_order_is_row_major(self, tmp_path):
        plan = synthetic_plan(tmp_path / "order.png")
        cells = detect_cells(plan)
        # Top row left to right is nail-01..05.
        top = [cells[f"nail-{i:02d}"] for i in range(1, 6)]
        assert [b[0] for b in top] == sorted(b[0] for b in top)
        # nail-06 sits below nail-01.
        assert cells["nail-06"][1] > cells["nail-01"][1]

    def test_boxes_do_not_overlap_horizontally_within_a_row(self, tmp_path):
        plan = synthetic_plan(tmp_path / "nooverlap.png", uneven=True)
        cells = detect_cells(plan)
        for row_start in (1, 6):
            boxes = [cells[f"nail-{i:02d}"] for i in range(row_start, row_start + 5)]
            boxes.sort(key=lambda b: b[0])
            for left, right in zip(boxes, boxes[1:], strict=False):
                assert left[2] <= right[0] + 2, "cells must not bleed into each other"

    def test_boxes_stay_inside_the_image(self, tmp_path):
        plan = synthetic_plan(tmp_path / "bounds.png")
        with Image.open(plan) as image:
            width, height = image.size
        for box in detect_cells(plan).values():
            assert 0 <= box[0] < box[2] <= width
            assert 0 <= box[1] < box[3] <= height

    def test_wrong_row_count_is_refused(self, tmp_path):
        plan = synthetic_plan(tmp_path / "onerow.png", rows=1)
        with pytest.raises(PlanError, match="expected 2 nail rows"):
            detect_cells(plan)

    def test_wrong_column_count_is_refused(self, tmp_path):
        # A partial detection would mis-assign identities, the exact failure this
        # module exists to prevent, so it must raise rather than return 8 cells.
        plan = synthetic_plan(tmp_path / "fourcol.png", cols=4)
        with pytest.raises(PlanError, match="expected 5 nails"):
            detect_cells(plan)

    def test_blank_plan_is_refused(self, tmp_path):
        path = tmp_path / "blank.png"
        Image.new("RGB", (600, 300), (255, 255, 255)).save(path)
        with pytest.raises(PlanError):
            detect_cells(path)

    def test_cell_box_rejects_an_unknown_nail(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        with pytest.raises(PlanError, match="unknown nail id"):
            cell_box(plan, "nail-11")


class TestCropCell:
    def test_crop_is_enlarged_for_legibility(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        cell = crop_cell(plan, "nail-03", upscale_to=640)
        assert cell.width >= 640

    def test_crop_isolates_one_nail(self, tmp_path):
        """Each nail a distinct hue; a crop must contain its own and no neighbour's."""
        colours = {f"nail-{i:02d}": c for i, c in enumerate(
            [(200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0), (200, 0, 200),
             (0, 200, 200), (120, 60, 0), (60, 0, 120), (200, 100, 0), (0, 120, 60)],
            start=1)}
        plan = synthetic_plan(tmp_path / "hues.png", colours=colours)
        cells = detect_cells(plan)
        for nail_id in colours:
            cell = crop_cell(plan, nail_id, upscale_to=200, cells=cells)
            present = set()
            pixels = cell.convert("RGB").load()
            for y in range(0, cell.height, 4):
                for x in range(0, cell.width, 4):
                    px = pixels[x, y]
                    for other, colour in colours.items():
                        if all(abs(px[i] - colour[i]) < 40 for i in range(3)):
                            present.add(other)
            assert present <= {nail_id}, f"{nail_id} crop also contained {present - {nail_id}}"

    def test_passing_cells_avoids_redetection(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        cells = detect_cells(plan)
        assert crop_cell(plan, "nail-01", cells=cells).width > 0

    def test_unknown_nail_in_supplied_cells_is_refused(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        with pytest.raises(PlanError, match="was not detected"):
            crop_cell(plan, "nail-07", cells={"nail-01": (0, 0, 10, 10)})


class TestViewPlanRows:
    @pytest.mark.parametrize("view", MATRIX_VIEWS)
    def test_every_view_yields_rows(self, view):
        rows = matrix_view_plan_rows(view)
        assert rows, f"{view} produced no view-plan rows"

    @pytest.mark.parametrize("view", MATRIX_VIEWS)
    def test_rows_match_the_view_s_visible_nails(self, view):
        from lunelle.prompts import matrix_visible_nails

        flat = [n for _, row in matrix_view_plan_rows(view) for n in row]
        assert sorted(flat) == sorted(matrix_visible_nails(view))
        assert len(flat) == len(set(flat)), "a nail must not repeat"

    def test_two_hand_views_have_two_rows_and_single_hand_views_one(self):
        assert len(matrix_view_plan_rows("p2_open_hands")) == 2
        assert len(matrix_view_plan_rows("p4_thumb_visible")) == 2
        assert len(matrix_view_plan_rows("p5_left_hand")) == 1
        assert len(matrix_view_plan_rows("p3_right_hand")) == 1

    def test_thumb_is_last_in_its_row(self):
        for view in MATRIX_VIEWS:
            for _, row in matrix_view_plan_rows(view):
                thumbs = [n for n in row if NAIL_ANATOMY[n][1] == "thumb"]
                assert len(thumbs) == 1
                assert row[-1] == thumbs[0], "thumb sits below the finger row"

    def test_p2_and_p4_left_hands_run_opposite_ways(self):
        """Not a bug: backs-of-hands vs curled fists genuinely reverse screen order.

        Both directions were verified from mask pixel centroids. A "fix" that made
        them agree would break one of the two views.
        """
        p2 = matrix_view_plan_rows("p2_open_hands")[0][1][:4]
        p4 = matrix_view_plan_rows("p4_thumb_visible")[0][1][:4]
        assert p2 == ["nail-05", "nail-04", "nail-03", "nail-02"]
        assert p4 == ["nail-02", "nail-03", "nail-04", "nail-05"]

    def test_unknown_view_yields_none(self):
        assert matrix_view_plan_rows("p9_nope") is None


class TestBuildViewPlan:
    def test_compiles_all_four_views(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        for view in MATRIX_VIEWS:
            data = build_view_plan(plan, matrix_view_plan_rows(view),
                                   anatomy=NAIL_ANATOMY)
            assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_output_is_deterministic(self, tmp_path):
        """Same plan and rows must give byte-identical output.

        The view-plan is a task input, so a nondeterministic compiler would change
        the input fingerprint on every queue and defeat idempotent deduplication.
        """
        plan = synthetic_plan(tmp_path / "p.png")
        rows = matrix_view_plan_rows("p2_open_hands")
        first = build_view_plan(plan, rows, anatomy=NAIL_ANATOMY)
        second = build_view_plan(plan, rows, anatomy=NAIL_ANATOMY)
        assert view_plan_digest(first) == view_plan_digest(second)


class TestBuildSpatialViewPlan:
    @pytest.mark.parametrize("view", MATRIX_VIEWS)
    def test_compiles_every_matrix_pose_map(self, tmp_path, view):
        plan = synthetic_plan(tmp_path / f"{view}.png")
        spec = matrix_view_plan_spec(view)
        data = build_spatial_view_plan(
            plan,
            visible_nails=spec["visible_nails"],
            pose_map=spec["pose_map"],
            anatomy=NAIL_ANATOMY,
            title=spec["title"],
        )
        assert data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_compiles_hero_to_landscape_contract_canvas(self, tmp_path):
        import io

        plan = synthetic_plan(tmp_path / "hero.png")
        spec = hero_view_plan_spec()
        data = build_spatial_view_plan(
            plan,
            visible_nails=spec["visible_nails"],
            pose_map=spec["pose_map"],
            anatomy=NAIL_ANATOMY,
            title=spec["title"],
        )
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (1536, 1024)

    def test_is_deterministic(self, tmp_path):
        plan = synthetic_plan(tmp_path / "stable.png")
        spec = hero_view_plan_spec()
        first = build_spatial_view_plan(
            plan, visible_nails=spec["visible_nails"], pose_map=spec["pose_map"],
            anatomy=NAIL_ANATOMY, title=spec["title"],
        )
        second = build_spatial_view_plan(
            plan, visible_nails=spec["visible_nails"], pose_map=spec["pose_map"],
            anatomy=NAIL_ANATOMY, title=spec["title"],
        )
        assert view_plan_digest(first) == view_plan_digest(second)

    def test_rejects_an_incomplete_pose_map(self, tmp_path):
        plan = synthetic_plan(tmp_path / "bad-map.png")
        with pytest.raises(PlanError, match="pose_map mismatch"):
            build_spatial_view_plan(
                plan,
                visible_nails=["nail-01", "nail-02"],
                pose_map={"nail-01": {"x": 0.2, "y": 0.3}},
                anatomy=NAIL_ANATOMY,
                title="bad",
            )

    def test_different_views_give_different_images(self, tmp_path):
        # p2 and p4 differ only in ordering; if the compiler ignored order they
        # would collide, and the whole mechanism would be a no-op.
        plan = synthetic_plan(tmp_path / "p.png")
        p2 = build_view_plan(plan, matrix_view_plan_rows("p2_open_hands"),
                             anatomy=NAIL_ANATOMY)
        p4 = build_view_plan(plan, matrix_view_plan_rows("p4_thumb_visible"),
                             anatomy=NAIL_ANATOMY)
        assert view_plan_digest(p2) != view_plan_digest(p4)

    def test_single_hand_view_is_shorter(self, tmp_path):
        import io

        plan = synthetic_plan(tmp_path / "p.png")
        with Image.open(io.BytesIO(build_view_plan(
                plan, matrix_view_plan_rows("p2_open_hands"),
                anatomy=NAIL_ANATOMY))) as both:
            both_h = both.height
        with Image.open(io.BytesIO(build_view_plan(
                plan, matrix_view_plan_rows("p5_left_hand"),
                anatomy=NAIL_ANATOMY))) as one:
            assert one.height < both_h

    def test_duplicate_nail_is_refused(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        rows = [("BAD", ["nail-01", "nail-01"])]
        with pytest.raises(PlanError, match="repeats nails"):
            build_view_plan(plan, rows, anatomy=NAIL_ANATOMY)

    def test_unknown_nail_is_refused(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        with pytest.raises(PlanError, match="unknown nails"):
            build_view_plan(plan, [("BAD", ["nail-99"])], anatomy=NAIL_ANATOMY)

    def test_empty_rows_are_refused(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        with pytest.raises(PlanError, match="at least one nail"):
            build_view_plan(plan, [], anatomy=NAIL_ANATOMY)

    def test_labels_name_the_anatomy_not_just_the_id(self, tmp_path):
        """A bare "nail-05" tells the model nothing about where it goes."""
        plan = synthetic_plan(tmp_path / "p.png")
        rows = matrix_view_plan_rows("p5_left_hand")
        assert build_view_plan(plan, rows, anatomy=NAIL_ANATOMY)
        # Anatomy must cover every nail the compiler will label, or it would KeyError.
        for _, row in rows:
            for nail_id in row:
                side, digit = NAIL_ANATOMY[nail_id]
                assert side in {"left", "right"}
                assert digit

    def test_nail_art_is_copied_not_recoloured(self, tmp_path):
        """Layout only. A compiler that altered the art would corrupt the very
        contract it exists to carry."""
        colours = {"nail-01": (200, 0, 0), "nail-06": (0, 0, 200)}
        plan = synthetic_plan(tmp_path / "p.png", colours=colours)
        import io

        data = build_view_plan(plan, matrix_view_plan_rows("p2_open_hands"),
                              anatomy=NAIL_ANATOMY)
        with Image.open(io.BytesIO(data)) as compiled:
            pixels = compiled.convert("RGB").load()
            found = set()
            for y in range(0, compiled.height, 3):
                for x in range(0, compiled.width, 3):
                    px = pixels[x, y]
                    for nail_id, colour in colours.items():
                        if all(abs(px[i] - colour[i]) < 45 for i in range(3)):
                            found.add(nail_id)
        assert found == set(colours), f"original nail colours missing: {set(colours) - found}"


class TestCompileViewPlan:
    def test_compiles_and_caches(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        cache = tmp_path / "cache"
        rows = matrix_view_plan_rows("p2_open_hands")
        first = compile_view_plan(plan, "p2_open_hands", rows=rows,
                                 anatomy=NAIL_ANATOMY, cache_dir=cache)
        assert first.is_file()
        stamp = first.stat().st_mtime_ns
        second = compile_view_plan(plan, "p2_open_hands", rows=rows,
                                  anatomy=NAIL_ANATOMY, cache_dir=cache)
        assert second == first
        assert second.stat().st_mtime_ns == stamp, "cache hit must not rewrite"

    def test_views_get_separate_cache_entries(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        cache = tmp_path / "cache"
        paths = {
            view: compile_view_plan(plan, view, rows=matrix_view_plan_rows(view),
                                    anatomy=NAIL_ANATOMY, cache_dir=cache)
            for view in MATRIX_VIEWS
        }
        assert len({p.name for p in paths.values()}) == len(MATRIX_VIEWS)

    def test_edited_plan_recompiles(self, tmp_path):
        """Content-keyed, not mtime-keyed: a stale view-plan would be an input lie."""
        cache = tmp_path / "cache"
        plan = synthetic_plan(tmp_path / "p.png")
        rows = matrix_view_plan_rows("p3_right_hand")
        before = compile_view_plan(plan, "p3_right_hand", rows=rows,
                                   anatomy=NAIL_ANATOMY, cache_dir=cache)
        synthetic_plan(plan, colours={"nail-07": (255, 0, 0)})
        after = compile_view_plan(plan, "p3_right_hand", rows=rows,
                                  anatomy=NAIL_ANATOMY, cache_dir=cache)
        assert after != before, "edited plan must not serve the old compile"

    def test_no_temp_files_survive(self, tmp_path):
        plan = synthetic_plan(tmp_path / "p.png")
        cache = tmp_path / "cache"
        compile_view_plan(plan, "p5_left_hand", rows=matrix_view_plan_rows("p5_left_hand"),
                          anatomy=NAIL_ANATOMY, cache_dir=cache)
        assert not list(cache.glob("*.tmp"))

    def test_undetectable_plan_raises(self, tmp_path):
        """The worker catches this and falls back to the raw plan."""
        path = tmp_path / "blank.png"
        Image.new("RGB", (600, 300), (255, 255, 255)).save(path)
        with pytest.raises(PlanError):
            compile_view_plan(path, "p2_open_hands",
                              rows=matrix_view_plan_rows("p2_open_hands"),
                              anatomy=NAIL_ANATOMY, cache_dir=tmp_path / "c")
