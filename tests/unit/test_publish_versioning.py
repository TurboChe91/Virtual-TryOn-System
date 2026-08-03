"""Versioned, resumable publish.

The defect: the old flow ran `DELETE FROM tryon_assets` and then one INSERT per
cell as separate HTTP calls, so a failure in between left the customer-facing
manifest EMPTY while R2 still held the images — the D1/R2 drift the Worker's own
docs describe. It also overwrote R2 keys that the Worker caches immutable for a
year, so a republished cell could serve the previous image for months.
"""

from __future__ import annotations

import pytest
from PIL import Image

from lunelle.cloudflare import CloudflareError
from lunelle.providers.mock import MockImageProvider
from lunelle.publish import (
    PublishError,
    publish_history,
    publish_style,
    unfinished_publishes,
)
from tests.integration.test_worker_flows import run_worker_until_settled
from tests.unit.test_publish import (
    FakeClient,
    approve_all_grids,
    make_style_with_cells,
)


def _publishable(config, db, service, tones=("light",), views=("p2_open_hands",)):
    style = make_style_with_cells(config, db, service, list(tones), list(views))
    return style


class TestVersioning:
    def test_first_publish_is_version_one(self, config, db, service):
        style = _publishable(config, db, service)
        out = publish_style(db, service, FakeClient(), style["style_id"])
        assert out["version"] == 1

    def test_republish_increments_and_never_overwrites(self, config, db, service):
        """The Worker caches images immutable for a year; a republished cell must
        not be served from that cache."""
        style = _publishable(config, db, service)
        first_client = FakeClient()
        first = publish_style(db, service, first_client, style["style_id"])
        second_client = FakeClient()
        second = publish_style(db, service, second_client, style["style_id"])

        assert (first["version"], second["version"]) == (1, 2)
        first_versioned = [k for k, _ in first_client.uploads if "-v1.webp" in k]
        second_versioned = [k for k, _ in second_client.uploads if "-v2.webp" in k]
        assert first_versioned and second_versioned
        # No versioned key is reused between publishes.
        assert not set(first_versioned) & set(second_versioned)

    def test_manifest_row_points_at_the_versioned_key(self, config, db, service):
        style = _publishable(config, db, service)
        client = FakeClient()
        publish_style(db, service, client, style["style_id"])
        upserts = [p for sql, p in client.queries
                   if "INSERT INTO tryon_assets" in sql]
        assert upserts
        assert all("-v1.webp" in params[4] for params in upserts)

    def test_conventional_key_is_written_for_the_static_endpoint(
        self, config, db, service
    ):
        """/api/tryon/result builds its key by convention and would 404 otherwise."""
        style = _publishable(config, db, service)
        client = FakeClient()
        publish_style(db, service, client, style["style_id"])
        keys = [k for k, _ in client.uploads]
        assert "tryon/results/007-light-01.webp" in keys
        assert "tryon/results/007-light-01-v1.webp" in keys

    def test_version_is_stamped_on_the_published_task(self, config, db, service):
        style = _publishable(config, db, service)
        out = publish_style(db, service, FakeClient(), style["style_id"])
        task_id = out["uploaded_cells"][0]["task_id"]
        task = service.get_task(task_id, with_details=False)
        assert task["published_version"] == out["version"]
        assert task["review_state"] == "published"


class TestNoEmptyManifestWindow:
    def test_no_blanket_delete_before_the_upserts(self, config, db, service):
        style = _publishable(config, db, service)
        client = FakeClient()
        publish_style(db, service, client, style["style_id"])
        statements = [sql for sql, _ in client.queries]
        # The old flow's first asset statement was a style-wide DELETE.
        assert not any("DELETE FROM tryon_assets WHERE style_id" in sql
                       for sql in statements)
        assert any("ON CONFLICT" in sql and "tryon_assets" in sql
                   for sql in statements)

    def test_upsert_replaces_a_cell_in_place(self, config, db, service):
        """An existing row for the same cell is updated, not removed and recreated."""
        style = _publishable(config, db, service)
        client = FakeClient(existing_rows=[{"id": "007-light-01-result"}])
        publish_style(db, service, client, style["style_id"])
        upserts = [(sql, p) for sql, p in client.queries
                   if "INSERT INTO tryon_assets" in sql]
        assert len(upserts) == 1
        assert upserts[0][1][0] == "007-light-01-result"
        # It was upserted, so nothing had to be deleted for it.
        deletes = [p for sql, p in client.queries if sql.startswith("DELETE")]
        assert all(params[0] != "007-light-01-result" for params in deletes)

    def test_stale_rows_are_removed_last(self, config, db, service):
        """Cells this style no longer publishes are dropped after the new rows
        land, so a failure there leaves an extra serviceable cell rather than a
        gap."""
        style = _publishable(config, db, service)
        client = FakeClient(existing_rows=[
            {"id": "007-light-01-result"},   # still published
            {"id": "007-tan-03-result"},     # no longer published
        ])
        out = publish_style(db, service, client, style["style_id"])
        assert out["removed_rows"] == ["007-tan-03-result"]
        statements = [sql for sql, _ in client.queries]
        last_upsert = max(i for i, sql in enumerate(statements)
                          if "INSERT INTO tryon_assets" in sql)
        first_delete = min(i for i, sql in enumerate(statements)
                           if sql.startswith("DELETE FROM tryon_assets WHERE id"))
        assert first_delete > last_upsert, "stale cleanup must run after the upserts"


class TestLedgerAndRecovery:
    def test_plan_is_recorded_before_any_remote_call(self, config, db, service):
        """A publish that dies on its first call must still be visible."""
        style = _publishable(config, db, service)
        client = FakeClient(fail_on="tryon/results")
        with pytest.raises(CloudflareError):
            publish_style(db, service, client, style["style_id"])

        history = publish_history(db, style["style_id"])
        assert len(history) == 1
        assert history[0]["state"] == "failed"
        assert history[0]["version"] == 1
        # The intended end state survives the failure.
        assert history[0]["plan"]["cells"], "plan recorded no cells"
        assert "simulated R2 failure" in history[0]["error"]

    def test_failure_during_d1_records_what_reached_r2(self, config, db, service):
        style = _publishable(config, db, service)
        client = FakeClient(fail_on="INSERT INTO tryon_styles")
        with pytest.raises(CloudflareError):
            publish_style(db, service, client, style["style_id"])

        record = publish_history(db, style["style_id"])[0]
        assert record["state"] == "failed"
        # R2 finished, so recovery knows the objects exist.
        assert record["progress"]["uploaded_keys"]
        assert any("-v1.webp" in key for key in record["progress"]["uploaded_keys"])

    def test_unfinished_publishes_are_listed(self, config, db, service):
        style = _publishable(config, db, service)
        with pytest.raises(CloudflareError):
            publish_style(db, service, FakeClient(fail_on="tryon/results"),
                          style["style_id"])
        pending = unfinished_publishes(db)
        assert len(pending) == 1
        assert pending[0]["style_id"] == style["style_id"]
        assert pending[0]["state"] == "failed"

    def test_rerunning_after_a_failure_completes_the_publish(self, config, db, service):
        """Idempotence is what makes the re-run safe: versioned keys never
        overwrite and rows upsert on a stable id."""
        style = _publishable(config, db, service)
        with pytest.raises(CloudflareError):
            publish_style(db, service, FakeClient(fail_on="INSERT INTO tryon_styles"),
                          style["style_id"])

        healthy = FakeClient()
        out = publish_style(db, service, healthy, style["style_id"])
        assert out["version"] == 2  # a fresh version, not a reused one
        history = publish_history(db, style["style_id"])
        assert [record["state"] for record in history] == ["committed", "failed"]
        assert unfinished_publishes(db) == [] or all(
            record["state"] == "failed" for record in unfinished_publishes(db))

    def test_committed_publish_records_its_progress(self, config, db, service):
        style = _publishable(config, db, service)
        publish_style(db, service, FakeClient(), style["style_id"])
        record = publish_history(db, style["style_id"])[0]
        assert record["state"] == "committed"
        assert record["committed_at"] is not None
        assert record["progress"]["committed_rows"] == ["007-light-01-result"]

    def test_versions_are_unique_per_style(self, config, db, service):
        style = _publishable(config, db, service)
        publish_style(db, service, FakeClient(), style["style_id"])
        publish_style(db, service, FakeClient(), style["style_id"])
        rows = db.conn().execute(
            "SELECT version FROM publish_versions WHERE style_id = ? ORDER BY version",
            (style["style_id"],),
        ).fetchall()
        assert [row["version"] for row in rows] == [1, 2]

    def test_refuses_to_build_a_key_from_an_unexpected_tone(self, config, db, service):
        """Defence in depth: tones are a fixed vocabulary, and an R2 key is a path."""
        from lunelle.db import transaction

        style = _publishable(config, db, service)
        conn = db.conn()
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET metadata_json = json_set(metadata_json, '$.tone',"
                " '../../etc/passwd') WHERE output_type = 'matrix_cell'")
        client = FakeClient()
        with pytest.raises(PublishError, match="refusing to build an R2 key"):
            publish_style(db, service, client, style["style_id"])
        # Refused before anything was sent, so no traversal-shaped key was written.
        assert client.uploads == []
        assert client.queries == []


class TestGateStillApplies:
    def test_unapproved_cells_are_still_refused(self, config, db, service):
        """Versioning must not have loosened the publish gate."""
        style = make_style_with_cells(config, db, service, ["light"],
                                     ["p2_open_hands"], approve=False)
        with pytest.raises(PublishError, match="no publishable"):
            publish_style(db, service, FakeClient(), style["style_id"])
        # Nothing was recorded, because nothing was attempted.
        assert publish_history(db, style["style_id"]) == []

    def test_cover_icon_still_needs_an_approved_grid(self, config, db, service):
        style = make_style_with_cells(config, db, service, ["light"], ["p2_open_hands"])
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        # Grid deliberately unreviewed.
        out = publish_style(db, service, FakeClient(), style["style_id"])
        assert out["cover_key"] is None

    def test_approved_grid_becomes_the_cover(self, config, db, service):
        style = make_style_with_cells(config, db, service, ["light"], ["p2_open_hands"])
        plan_path = config.upload_dir / "plan.png"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (300, 200), (200, 180, 160)).save(plan_path)
        service.set_plan_image(style["style_id"], plan_path)
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))
        approve_all_grids(db, service, style["style_id"])
        out = publish_style(db, service, FakeClient(), style["style_id"])
        assert out["cover_key"] == "tryon/icons/007-light-icon.webp"
        assert out["plan_key"] == "tryon/plans/007-plan.webp"
