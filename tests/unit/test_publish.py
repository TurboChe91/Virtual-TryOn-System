"""Unit tests for the try-on publish pipeline (fake Cloudflare client)."""

from __future__ import annotations

import pytest
from PIL import Image

from lunelle.providers.mock import MockImageProvider
from lunelle.publish import (
    VIEW_CODE,
    PublishError,
    collect_publishable_cells,
    publish_style,
)
from tests.conftest import satisfy_matrix_dependencies
from tests.integration.test_worker_flows import make_style, run_worker_until_settled


class FakeClient:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []
        self.queries: list[tuple[str, list]] = []

    def r2_put(self, key, data, content_type):
        assert data[:4] == b"RIFF" and b"WEBP" in data[:16]  # really webp
        self.uploads.append((key, content_type))

    def d1_query(self, sql, params=None):
        self.queries.append((sql, params or []))
        return []


def qa_verdict(passed: bool) -> dict:
    # needs_human_review is always true: automatic QA never self-approves. Only
    # record_review() clears it.
    return {
        "passed": passed, "score": 100 if passed else 10,
        "issues": [] if passed else ["bad"], "checks": {},
        "recommended_action": "human_review" if passed else "regenerate",
        "needs_human_review": True,
    }


def make_style_with_cells(config, db, service, tones, views, qa_fail=(), *,
                          approve=True):
    """Cells settle via the worker, then a deterministic QA verdict is stamped on
    each (the mock render's own heuristic verdict is arbitrary) and — unless the
    test wants an unreviewed asset — a human approval is recorded, because
    nothing reaches publish without one."""
    style = make_style(service)
    service.set_tryon_id(style["style_id"], "007")
    satisfy_matrix_dependencies(db, config, service, style["style_id"])
    plan = service.create_matrix_generation(style["style_id"], tones=tones, views=views)
    run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
    for created in plan.created:
        cell = (created["tone"], created["view"])
        passed = cell not in qa_fail
        service.finish_qa(created["task_id"], qa_verdict(passed))
        if passed and approve:
            service.record_review(created["task_id"], approved=True, note="test approval")
    return style


def approve_all_grids(db, service, style_id):
    """Approve the style's grid assets so the publish cover icon is eligible."""
    for task in service.list_tasks(output_type="grid"):
        if task["style_id"] != style_id or task["status"] != "success":
            continue
        service.finish_qa(task["task_id"], qa_verdict(True))
        service.record_review(task["task_id"], approved=True, note="test approval")


class TestCollect:
    def test_latest_qa_verdict_wins(self, config, db, service):
        style = make_style_with_cells(
            config, db, service, ["light"], ["p2_open_hands", "p5_left_hand"],
            qa_fail={("light", "p5_left_hand")},
        )
        ready, excluded = collect_publishable_cells(db, style["style_id"])
        assert ("light", "p2_open_hands") in ready
        assert ("light", "p5_left_hand") not in ready
        assert excluded == [{"tone": "light", "view": "p5_left_hand",
                             "reason": "automatic QA did not pass"}]

    def test_unreviewed_cell_is_excluded(self, config, db, service):
        """QA passing is not enough: without a human verdict the cell is blocked."""
        style = make_style_with_cells(
            config, db, service, ["light"], ["p2_open_hands"], approve=False,
        )
        ready, excluded = collect_publishable_cells(db, style["style_id"])
        assert ready == {}
        assert excluded == [{"tone": "light", "view": "p2_open_hands",
                             "reason": "awaiting human review"}]

    def test_cell_with_no_qa_row_is_excluded(self, config, db, service):
        """The original defect: an absent QA row read as NULL -> falsy -> allowed."""
        style = make_style(service)
        service.set_tryon_id(style["style_id"], "007")
        satisfy_matrix_dependencies(db, config, service, style["style_id"])
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        # Erase every QA row, leaving a successful task with no verdict at all.
        conn = db.conn()
        conn.execute("DELETE FROM qa_results")
        conn.execute("UPDATE tasks SET qa_state = 'error', review_state = 'generated'")

        ready, excluded = collect_publishable_cells(db, style["style_id"])
        assert ready == {}
        assert excluded and "no verdict" in excluded[0]["reason"]
        with pytest.raises(PublishError, match="no publishable"):
            publish_style(db, service, FakeClient(), style["style_id"])
        assert plan.created  # the cell existed; it was blocked, not missing

    def test_rejected_cell_is_excluded(self, config, db, service):
        style = make_style_with_cells(
            config, db, service, ["light"], ["p2_open_hands"], approve=False)
        task_id = service.list_tasks(output_type="matrix_cell")[0]["task_id"]
        service.record_review(task_id, approved=False, note="wrong thumb")
        ready, excluded = collect_publishable_cells(db, style["style_id"])
        assert ready == {}
        assert excluded[0]["reason"] == "rejected by human review"


class TestPublish:
    def test_requires_tryon_id(self, config, db, service):
        style = make_style(service)
        with pytest.raises(PublishError, match="3-digit"):
            publish_style(db, service, FakeClient(), style["style_id"])

    def test_full_publish_writes_r2_and_d1(self, config, db, service):
        style = make_style_with_cells(
            config, db, service, ["light", "deep"], ["p2_open_hands", "p5_left_hand"],
        )
        plan_path = config.upload_dir / style["style_id"] / "plan.png"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (300, 200), (200, 180, 160)).save(plan_path)
        service.set_plan_image(style["style_id"], plan_path)
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        approve_all_grids(db, service, style["style_id"])

        client = FakeClient()
        out = publish_style(db, service, client, style["style_id"])

        keys = [k for k, _ in client.uploads]
        assert "tryon/results/007-light-01.webp" in keys
        assert "tryon/results/007-deep-04.webp" in keys
        assert "tryon/icons/007-light-icon.webp" in keys
        assert "tryon/plans/007-plan.webp" in keys
        assert len(out["uploaded_cells"]) == 4
        assert out["manifest_url"].endswith("/v1/styles/007")

        sql_all = " ".join(sql for sql, _ in client.queries)
        assert "INSERT INTO tryon_styles" in sql_all
        assert "DELETE FROM tryon_assets" in sql_all
        insert_assets = [p for sql, p in client.queries if "INSERT INTO tryon_assets" in sql]
        assert len(insert_assets) == 4
        assert all(p[0].startswith("007-") for p in insert_assets)

        # Everything that reached production is stamped published.
        for cell in out["uploaded_cells"]:
            assert service.get_task(cell["task_id"])["review_state"] == "published"

    def test_unapproved_grid_yields_no_cover_icon(self, config, db, service):
        """The cover icon is customer-facing, so it goes through the same gate."""
        style = make_style_with_cells(config, db, service, ["light"], ["p2_open_hands"])
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        # Grid deliberately left unreviewed.
        client = FakeClient()
        out = publish_style(db, service, client, style["style_id"])
        assert out["cover_key"] is None
        assert not [k for k, _ in client.uploads if "icons/" in k]

    def test_view_code_contract(self):
        assert VIEW_CODE == {
            "p2_open_hands": "01", "p3_right_hand": "02",
            "p4_thumb_visible": "03", "p5_left_hand": "04",
        }
