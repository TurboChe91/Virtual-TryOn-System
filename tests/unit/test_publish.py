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
from lunelle.qa import store_qa_result
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
    return {
        "passed": passed, "score": 100 if passed else 10,
        "issues": [] if passed else ["bad"], "checks": {},
        "recommended_action": "approve" if passed else "regenerate",
        "needs_human_review": not passed,
    }


def make_style_with_cells(config, db, service, tones, views, qa_fail=()):
    """Cells settle via the worker; a deterministic QA verdict is then stamped
    on each (the mock render's heuristic verdict is arbitrary)."""
    style = make_style(service)
    service.set_tryon_id(style["style_id"], "007")
    plan = service.create_matrix_generation(style["style_id"], tones=tones, views=views)
    run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
    for created in plan.created:
        cell = (created["tone"], created["view"])
        store_qa_result(db, created["task_id"], qa_verdict(cell not in qa_fail))
    return style


class TestCollect:
    def test_latest_qa_verdict_wins(self, config, db, service):
        style = make_style_with_cells(
            config, db, service, ["light"], ["p2_open_hands", "p5_left_hand"],
            qa_fail={("light", "p5_left_hand")},
        )
        ready, excluded = collect_publishable_cells(db, style["style_id"])
        assert ("light", "p2_open_hands") in ready
        assert ("light", "p5_left_hand") not in ready
        assert excluded == [{"tone": "light", "view": "p5_left_hand", "reason": "qa_failed"}]


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

    def test_view_code_contract(self):
        assert VIEW_CODE == {
            "p2_open_hands": "01", "p3_right_hand": "02",
            "p4_thumb_visible": "03", "p5_left_hand": "04",
        }
