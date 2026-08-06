"""The uploaded design image must become the design authority.

The defect: both creation paths stored every upload as `reference_image_path`,
leaving `plan_image_path` empty. Matrix cells then fell back to "the most recent
successful grid" — copying an image this system generated earlier instead of the
operator's design — and did so silently, so the resulting cells were
indistinguishable from faithful ones without reading the logs.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from lunelle.providers.mock import MockImageProvider
from tests.conftest import satisfy_matrix_dependencies

from .test_worker_flows import make_style, run_worker_until_settled


def plan_png(size=(400, 200)) -> bytes:
    """Something shaped like a 2x5 set plan."""
    image = Image.new("RGB", size, (245, 242, 238))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TestUploadLandsInTheRightField:
    def test_kind_plan_sets_the_plan_image(self, client, admin_headers):
        created = client.post("/api/styles", json={"name": "P", "description": "black"},
                              headers=admin_headers).json()
        style_id = created["style"]["style_id"]
        response = client.post(
            f"/api/styles/{style_id}/reference-image?kind=plan",
            files={"file": ("plan.png", plan_png(), "image/png")},
            headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["kind"] == "plan"
        style = client.get(f"/api/styles/{style_id}").json()["style"]
        assert style["plan_image_path"]
        assert not style["reference_image_path"]

    def test_kind_reference_still_sets_the_reference(self, client, admin_headers):
        created = client.post("/api/styles", json={"name": "R", "description": "black"},
                              headers=admin_headers).json()
        style_id = created["style"]["style_id"]
        client.post(f"/api/styles/{style_id}/reference-image?kind=reference",
                    files={"file": ("ref.png", plan_png(), "image/png")},
                    headers=admin_headers)
        style = client.get(f"/api/styles/{style_id}").json()["style"]
        assert style["reference_image_path"]
        assert not style["plan_image_path"]

    def test_upload_response_reports_identity_derivation(self, client, admin_headers):
        """Whether identity was written is part of the result, not a silent side effect."""
        created = client.post("/api/styles", json={"name": "I", "description": "black"},
                              headers=admin_headers).json()
        style_id = created["style"]["style_id"]
        body = client.post(f"/api/styles/{style_id}/reference-image?kind=plan",
                           files={"file": ("plan.png", plan_png(), "image/png")},
                           headers=admin_headers).json()
        assert "identity_derived" in body
        # No LLM channel in tests, so it reports the failure rather than claiming success.
        assert body["identity_derived"] is False
        assert body["identity_error"]


class TestMatrixRefusesAGeneratedGridAsAuthority:
    def test_cell_blocks_when_only_a_grid_exists(self, config, db, service):
        """A grid is a generated image; basing sixteen cells on it bakes in its drift."""
        style = make_style(service, name="GridOnly", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"])
        # Remove the plan that the fixture supplies, leaving a successful grid.
        db.conn().execute("UPDATE styles SET plan_image_path = NULL WHERE style_id = ?",
                          (style["style_id"],))
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        task = service.get_task(plan.created[0]["task_id"])
        assert task["status"] == "failed"
        assert task["error_code"] == "dependency_missing"
        assert "plan image" in task["error_message"]
        assert provider.calls == 0, "a blocked cell must not be billed"

    def test_cell_proceeds_with_an_uploaded_plan(self, config, db, service):
        style = make_style(service, name="HasPlan", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"])
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        task = service.get_task(plan.created[0]["task_id"], with_details=False)
        assert task["status"] == "success"
        assert provider.calls == 1

    def test_blocked_cell_is_not_exportable(self, config, db, service):
        style = make_style(service, name="Blocked", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"])
        db.conn().execute("UPDATE styles SET plan_image_path = NULL WHERE style_id = ?",
                          (style["style_id"],))
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        task = service.get_task(plan.created[0]["task_id"], with_details=False)
        assert task["review_state"] == "generated"


class TestImage1IsTheCompiledViewPlan:
    """Nail placement rides on the image, not on prose.

    Prose asked the model to infer handedness and count fingers. Masked per-nail
    editing was measured as the alternative and does not work on this channel: told
    to repaint nail-01 (left thumb, mask centroid x=44.7%, y=71.6%) the model
    painted the right index finger (x=56.1%, y=42.8%).
    """

    def test_cell_receives_a_compiled_view_plan_not_the_raw_plan(
            self, config, db, service):
        style = make_style(service, name="ViewPlan", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"])
        service.create_matrix_generation(style["style_id"], tones=["light"],
                                         views=["p2_open_hands"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        assert provider.calls == 1
        references = provider.requested_references[0]
        assert len(references) == 2, "Image 1 = view-plan, Image 2 = hand model"
        image1 = references[0]
        style_row = service.get_style(style["style_id"])
        assert image1 != Path(style_row["plan_image_path"]), \
            "the raw 2x5 plan carries no screen order"
        assert image1.name.startswith("viewplan-")
        assert "p2_open_hands" in image1.name, "the view-plan must match the cell's view"
        assert image1.is_file()

    def test_each_view_gets_its_own_view_plan(self, config, db, service):
        """p2 and p4 disagree on left-hand screen order, so one image cannot serve both."""
        style = make_style(service, name="PerView", description="red square nails")
        views = ["p2_open_hands", "p4_thumb_visible"]
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=views)
        service.create_matrix_generation(style["style_id"], tones=["light"], views=views)
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        first_images = [refs[0] for refs in provider.requested_references]
        assert len(first_images) == 2
        assert len({p.name for p in first_images}) == 2

    def test_view_plan_is_reused_across_cells_of_the_same_view(
            self, config, db, service):
        """Four tones share one view, so the compile is cached, not repeated."""
        style = make_style(service, name="Reuse", description="red square nails")
        tones = ["light", "deep"]
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=tones, views=["p3_right_hand"])
        service.create_matrix_generation(style["style_id"], tones=tones,
                                         views=["p3_right_hand"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        first_images = {refs[0] for refs in provider.requested_references}
        assert len(first_images) == 1, "same view must reuse one compiled view-plan"

    def test_undetectable_plan_falls_back_to_the_raw_plan(self, config, db, service):
        """A flat plan photo is an operator input problem, not a reason to block.

        The cell still has correct art and the QA judge already reads nail order, so
        degrading beats refusing here — unlike a missing hand model, which makes the
        output unusable in the matrix.
        """
        style = make_style(service, name="Flat", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p5_left_hand"])
        style_row = service.get_style(style["style_id"])
        Image.new("RGB", (300, 150), (255, 255, 255)).save(style_row["plan_image_path"])

        service.create_matrix_generation(style["style_id"], tones=["light"],
                                         views=["p5_left_hand"])
        provider = MockImageProvider(allowed=True)
        run_worker_until_settled(config, db, service, provider)

        assert provider.calls == 1, "an undetectable plan must not block the cell"
        image1 = provider.requested_references[0][0]
        assert image1 == Path(style_row["plan_image_path"])


class TestSnapshotAgreesWithTheWorker:
    def test_snapshot_does_not_freeze_a_grid_as_the_authority(self, config, db, service):
        """Queue time and execution must agree on what the authority is.

        If the snapshot accepted a grid the worker refuses, it would record an
        input that was never used — a false provenance record.
        """
        from lunelle.snapshots import get_snapshot

        style = make_style(service, name="SnapGrid", description="red square nails")
        satisfy_matrix_dependencies(db, config, service, style["style_id"],
                                    tones=["light"], views=["p2_open_hands"])
        db.conn().execute("UPDATE styles SET plan_image_path = NULL WHERE style_id = ?",
                          (style["style_id"],))
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        record = get_snapshot(db, plan.created[0]["task_id"])
        assets = record["snapshot"]["input_assets"]
        paths = " ".join(str(a.get("path") or a.get("original_path") or "") for a in assets)
        assert "-grid-" not in paths, "a generated grid must not be frozen as the authority"


@pytest.fixture
def client(config):
    from fastapi.testclient import TestClient

    from lunelle.server import create_app

    with TestClient(create_app(config, start_worker=False)) as test_client:
        yield test_client


@pytest.fixture
def admin_headers(config):
    return {"X-Admin-Token": config.admin_token} if config.admin_token else {}
