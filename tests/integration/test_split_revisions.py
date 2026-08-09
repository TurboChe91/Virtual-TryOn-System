"""Automatic split is reviewable evidence; manual override is a new revision."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from lunelle.errors import ConflictError
from lunelle.planview import detect_cells
from lunelle.splits import SplitService
from tests.conftest import write_test_plan

from .test_worker_flows import make_style


def _edge_plan(path):
    image = Image.new("RGB", (500, 250), "white")
    draw = ImageDraw.Draw(image)
    for row in range(2):
        for column in range(5):
            left = column * 100 if column == 0 else 20 + column * 95
            top = 15 + row * 125
            draw.ellipse((left, top, left + 55, top + 80), fill=(30, 90, 120))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


class TestCropRevisions:
    def test_clear_plan_auto_approves_and_stores_ten_crops(self, config, db, service):
        style = make_style(service, name="Split", description="blue almond nails")
        plan = write_test_plan(config.upload_dir / "split.png")
        service.set_plan_image(style["style_id"], plan)

        splitter = SplitService(db, config)
        revision = splitter.analyze(style["style_id"], created_by="test")

        assert revision["source"] == "auto"
        assert revision["review_state"] == "auto_approved"
        assert revision["gate_status"] == "pass"
        assert revision["preview_url"].startswith("/api/assets/")
        assert revision["contact_sheet_url"].startswith("/api/assets/")
        assert len(revision["boxes"]) == 10
        for number in range(1, 11):
            assert splitter.nail_crop(
                revision["crop_revision_id"], f"nail-{number:02d}"
            ).is_file()

    def test_suspicious_auto_split_waits_for_review(self, config, db, service):
        style = make_style(service, name="Edge", description="black square nails")
        plan = _edge_plan(config.upload_dir / "edge.png")
        service.set_plan_image(style["style_id"], plan)
        splitter = SplitService(db, config)

        revision = splitter.analyze(style["style_id"], created_by="test")
        assert revision["review_state"] == "waiting_review"
        assert revision["gate_status"] == "review_required"
        assert any(reason.startswith("touches_plan_edge")
                   for reason in revision["gate_reasons"])
        with pytest.raises(ConflictError, match="manual split override"):
            splitter.selected(style["style_id"], require_human_approval=False)

    def test_manual_override_is_immutable_and_becomes_selected(
        self, config, db, service,
    ):
        style = make_style(service, name="Manual", description="red oval nails")
        plan = _edge_plan(config.upload_dir / "manual.png")
        service.set_plan_image(style["style_id"], plan)
        splitter = SplitService(db, config)
        automatic = splitter.analyze(style["style_id"], created_by="auto")

        boxes = detect_cells(plan)
        manual = splitter.create_manual(
            style["style_id"], boxes, created_by="operator:test"
        )
        selected_boxes, selected_revision = splitter.selected(
            style["style_id"], require_human_approval=False
        )

        assert manual["source"] == "manual"
        assert manual["review_state"] == "approved"
        assert selected_revision["crop_revision_id"] == manual["crop_revision_id"]
        assert selected_boxes == boxes
        history = splitter.list(style["style_id"])
        assert [item["crop_revision_id"] for item in history] == [
            manual["crop_revision_id"], automatic["crop_revision_id"],
        ]


class TestSplitApi:
    def test_preview_and_manual_override_are_operable_from_the_admin_api(self, config):
        from fastapi.testclient import TestClient

        from lunelle.server import create_app

        plan_path = write_test_plan(config.upload_dir / "api-plan.png")
        with TestClient(create_app(config, start_worker=False)) as client:
            created = client.post(
                "/api/styles", json={"name": "API Split", "description": "gold nails"}
            )
            assert created.status_code == 201
            style_id = created.json()["style"]["style_id"]
            uploaded = client.post(
                f"/api/styles/{style_id}/reference-image?kind=plan",
                files={"file": ("plan.png", plan_path.read_bytes(), "image/png")},
            )
            assert uploaded.status_code == 200
            automatic = uploaded.json()["split"]
            assert automatic["review_state"] == "auto_approved"

            preview = client.get(automatic["preview_url"])
            assert preview.status_code == 200
            assert preview.headers["content-type"] == "image/png"

            body = {
                "boxes": [
                    {"nail_id": nail_id, "bbox": bbox}
                    for nail_id, bbox in sorted(automatic["boxes"].items())
                ]
            }
            manual = client.post(
                f"/api/styles/{style_id}/splits/manual", json=body
            )
            assert manual.status_code == 201
            assert manual.json()["review_state"] == "approved"
            history = client.get(f"/api/styles/{style_id}/splits").json()["revisions"]
            assert [item["source"] for item in history] == ["manual", "auto"]
