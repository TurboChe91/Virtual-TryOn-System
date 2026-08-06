"""A matrix cell must be requested in its base hand photo's shape.

Regression for a defect that hit 100% of real cells: the request was hardcoded to
`wearing_size` (2048x2048) while the uploaded hand models were 1448x1086 (4:3), so
the provider stretched every hand vertically by 1.33x. The prompt said "match Image
2 crop", but `size` is a hard API parameter and prose loses to it.
"""

from __future__ import annotations

from PIL import Image

from lunelle.geometry import MIN_PIXELS, legal_size_for_ratio
from lunelle.providers.mock import MockImageProvider
from lunelle.snapshots import get_snapshot
from tests.conftest import satisfy_matrix_dependencies

from .test_worker_flows import make_style, run_worker_until_settled

# 4:3, the ratio of the hand models actually in production.
LANDSCAPE_HAND = (1448, 1086)


class TestCellSizeFollowsTheHandModel:
    def test_request_matches_the_hand_model_ratio_not_the_square_config(
            self, config, db, service):
        assert config.wearing_size[0] == config.wearing_size[1], "fixture should be square"
        style = make_style(service, name="Ratio", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"],
                                    hand_size=LANDSCAPE_HAND)
        service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])

        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        assert provider.calls == 1
        requested = provider.requested_sizes[0]
        assert requested == legal_size_for_ratio(*LANDSCAPE_HAND)
        # The actual defect, stated directly: not square, and landscape like the base.
        assert requested[0] != requested[1]
        assert requested[0] > requested[1]

    def test_ratio_error_stays_under_one_percent(self, config, db, service):
        style = make_style(service, name="Drift", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"],
                                    hand_size=LANDSCAPE_HAND)
        service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        w, h = provider.requested_sizes[0]
        source = LANDSCAPE_HAND[0] / LANDSCAPE_HAND[1]
        assert abs(w / h - source) / source < 0.01

    def test_the_written_output_has_the_hand_model_shape(self, config, db, service):
        style = make_style(service, name="Output", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"],
                                    hand_size=LANDSCAPE_HAND)
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        task = service.get_task(plan.created[0]["task_id"], with_details=False)
        assert task["status"] == "success"
        with Image.open(task["output_path"]) as image:
            assert image.width > image.height
            assert image.width * image.height >= MIN_PIXELS

    def test_portrait_hand_model_yields_a_portrait_request(self, config, db, service):
        style = make_style(service, name="Portrait", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"],
                                    hand_size=(1086, 1448))
        service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        w, h = provider.requested_sizes[0]
        assert h > w, "a portrait base must not be requested as landscape"

    def test_every_request_clears_the_provider_floor(self, config, db, service):
        # A tiny hand model must still produce a legal request, not a 256px one.
        style = make_style(service, name="Tiny", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"],
                                    hand_size=(120, 90))
        service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        w, h = provider.requested_sizes[0]
        assert w * h >= MIN_PIXELS

    def test_grid_and_wearing_sizes_are_untouched(self, config, db, service):
        # The fix must be scoped to cells; grid stays square per its own contract.
        style = make_style(service, name="Scope", description="red square nails")
        service.create_generation(style["style_id"], output_types=["grid", "wearing"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        assert set(provider.requested_sizes) == {
            tuple(config.grid_size), tuple(config.wearing_size)}


class TestJudgeReceivesTheBaseHandPhoto:
    def test_worker_forwards_the_hand_model_and_this_view_s_nails(
            self, config, db, service, monkeypatch):
        """The judge could not see the stretch because the base photo was never
        sent. Assert the worker now supplies it, plus the per-view nail list."""
        captured: dict = {}

        def fake_verdict(chat, images, identity, output_type, *, visible_nails=None):
            captured["images"] = list(images)
            captured["visible_nails"] = visible_nails
            captured["output_type"] = output_type
            return {"passed": True, "issues": [], "correction": ""}

        # `_maybe_llm_qa` imports from .llm at call time, so patching the module
        # attribute is what actually intercepts it.
        monkeypatch.setattr("lunelle.llm.auto_qa_verdict", fake_verdict)
        monkeypatch.setattr("lunelle.llm.build_llm_chat",
                            lambda config, db: (lambda s, u, i: ""))

        style = make_style(service, name="Judge", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p3_right_hand"],
                                    hand_size=LANDSCAPE_HAND)
        service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p3_right_hand"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        assert captured, "llm qa never ran"
        assert captured["output_type"] == "matrix_cell"
        # p3 is a single-hand view: 5 nails, not 10.
        assert captured["visible_nails"] is not None
        assert len(captured["visible_nails"]) == 5
        # candidate + design authority + base hand photo
        assert len(captured["images"]) == 3
        with Image.open(captured["images"][2]) as base:
            assert (base.width, base.height) == LANDSCAPE_HAND


class TestSnapshotRecordsTheSizeActuallyUsed:
    def test_snapshot_size_matches_the_provider_request(self, config, db, service):
        """The snapshot is the audit record; if it disagreed with the real request
        it would silently misreport what was generated."""
        style = make_style(service, name="Snap", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"],
                                    hand_size=LANDSCAPE_HAND)
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        record = get_snapshot(db, plan.created[0]["task_id"])
        assert record is not None
        assert tuple(record["snapshot"]["size"]) == provider.requested_sizes[0]
