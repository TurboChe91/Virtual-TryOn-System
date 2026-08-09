"""Every execution must say which code produced it, and a stale process must stop.

On 2026-08-06 four matrix cells cost $0.76 on a worker whose code predated the
feature they were meant to test, and `task_executions` recorded nothing that could
show it: `prompt_version` read `mx-2+8176b704`, exactly as a correct render would.
These tests assert the two halves of the fix — the build identity reaches the row,
and a process whose tree has moved refuses to spend.
"""

from __future__ import annotations

import json
import time

import pytest

from lunelle import build
from lunelle.db import transaction, utcnow
from lunelle.providers.mock import MockImageProvider
from lunelle.snapshots import get_executions
from lunelle.worker import Worker
from tests.conftest import satisfy_matrix_dependencies

from .test_worker_flows import make_style, run_worker_until_settled


@pytest.fixture
def client(config):
    from fastapi.testclient import TestClient

    from lunelle.server import create_app

    with TestClient(create_app(config, start_worker=False)) as test_client:
        yield test_client


class TestExecutionCarriesBuildIdentity:
    def test_columns_are_populated(self, config, db, service):
        style = make_style(service, name="Build Provenance")
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        row = db.conn().execute(
            "SELECT * FROM task_executions LIMIT 1"
        ).fetchone()
        assert row is not None, "an execution row must exist"
        assert row["build_id"] == build.IDENTITY.build_id
        assert row["runtime_tree_sha"] == build.IDENTITY.runtime_tree_sha256
        assert row["git_sha"] == build.IDENTITY.git_sha
        assert row["git_dirty"] in (0, 1)
        assert row["dependency_sha256"] == build.IDENTITY.dependency_sha256
        assert row["process_id"] == build.IDENTITY.process_id
        assert row["runtime_drift_detected"] == 0

    def test_worker_instance_names_the_thread(self, config, db, service):
        """Concurrent renders inside one process must be tellable apart."""
        style = make_style(service, name="Thread Named")
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        row = db.conn().execute(
            "SELECT worker_instance FROM task_executions LIMIT 1"
        ).fetchone()
        assert row["worker_instance"].startswith("lunelle-worker-")

    def test_json_payload_also_carries_the_build(self, config, db, service):
        """The columns are for querying; the JSON keeps the whole record."""
        style = make_style(service, name="Json Build")
        plan = service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        execution = get_executions(db, plan.created[0]["task_id"])[0]["execution"]
        assert execution["build"]["build_id"] == build.IDENTITY.build_id
        assert execution["build"]["manifest_version"] == build.MANIFEST_VERSION
        assert "dependency_versions" in execution["build"]
        assert execution["build"]["runtime_drift_detected"] is False
        assert execution["build"]["runtime_tree_sha256"] == \
               build.IDENTITY.runtime_tree_sha256

    def test_build_id_is_queryable(self, config, db, service):
        """"Which cells ran on this build" was the unanswerable question."""
        style = make_style(service, name="Queryable")
        service.create_generation(style["style_id"], ["grid"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        count = db.conn().execute(
            "SELECT COUNT(*) AS n FROM task_executions WHERE build_id = ?",
            (build.IDENTITY.build_id,),
        ).fetchone()["n"]
        assert count == 1

    def test_matrix_cell_records_build_identity(self, config, db, service):
        """The output type the incident actually involved."""
        style = make_style(service, name="Matrix Build")
        satisfy_matrix_dependencies(db, config, service, style["style_id"])
        plan = service.create_matrix_generation(
            style["style_id"], tones=["light"], views=["p2_open_hands"])
        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        row = db.conn().execute(
            "SELECT build_id, runtime_tree_sha FROM task_executions WHERE task_id = ?",
            (plan.created[0]["task_id"],),
        ).fetchone()
        assert row["build_id"] == build.IDENTITY.build_id
        assert row["runtime_tree_sha"] == build.IDENTITY.runtime_tree_sha256

    def test_failed_attempt_still_records_its_build(self, config, db, service):
        """A provider error must not lose the code identity of the attempt."""
        style = make_style(service, name="Failing Build")
        plan = service.create_generation(style["style_id"], ["grid"])
        provider = MockImageProvider(
            allowed=True,
            fail_with=__import__("lunelle.providers", fromlist=["ProviderError"])
            .ProviderError("server_error", "boom", retryable=False),
        )
        run_worker_until_settled(config, db, service, provider)

        task = service.get_task(plan.created[0]["task_id"])
        assert task["status"] == "failed"
        row = db.conn().execute(
            "SELECT build_id FROM task_executions WHERE task_id = ?",
            (plan.created[0]["task_id"],),
        ).fetchone()
        assert row is not None, "execution provenance is recorded before the call"
        assert row["build_id"] == build.IDENTITY.build_id


class TestDriftHaltsTheWorker:
    """The behavioural half: a stale process must not reach the provider.

    Simulated by making detect_drift report drift, which is what an operator
    editing a file under a running server produces. The digest itself is covered in
    tests/unit/test_build_identity.py; what matters here is that the worker stops
    claiming and spends nothing.
    """

    def _drifting(self, monkeypatch, *, changed=("lunelle/worker.py",)):
        from lunelle import worker as worker_module

        report = build.DriftReport(
            drifted=True, changed=tuple(changed), current_tree_sha256="f" * 64)
        monkeypatch.setattr(worker_module, "detect_drift", lambda **kwargs: report)
        return report

    def test_drifted_worker_claims_nothing_and_spends_nothing(
            self, config, db, service, monkeypatch):
        style = make_style(service, name="Drift Halt")
        plan = service.create_generation(style["style_id"], ["grid"])
        self._drifting(monkeypatch)
        monkeypatch.delenv(build.ALLOW_DRIFT_ENV, raising=False)

        provider = MockImageProvider(allowed=True)
        worker = Worker(config, db, service, provider)
        worker.start()
        try:
            time.sleep(1.5)  # several poll cycles
        finally:
            worker.stop(timeout=5)

        assert provider.calls == 0, "a stale build must not reach the provider"
        task = service.get_task(plan.created[0]["task_id"])
        assert task["status"] == "pending", "the task stays claimable for a fresh process"
        reservations = db.conn().execute(
            "SELECT COUNT(*) AS n FROM spend_reservations"
        ).fetchone()["n"]
        assert reservations == 0, "no budget may be claimed by a halted worker"

    def test_halt_is_sticky_even_if_the_file_is_reverted(
            self, config, db, service, monkeypatch):
        """Reverting does not un-load the modules already imported from the old tree.

        A process that has seen drift is a mixture of old and new code, and no
        record can describe a mixture. Only a restart resolves it.
        """
        from lunelle import worker as worker_module

        style = make_style(service, name="Sticky Halt")
        service.create_generation(style["style_id"], ["grid"])
        reports = iter([
            build.DriftReport(drifted=True, changed=("lunelle/prompts.py",),
                              current_tree_sha256="f" * 64),
        ])
        clean = build.DriftReport(drifted=False, current_tree_sha256="a" * 64)
        monkeypatch.setattr(worker_module, "detect_drift",
                            lambda **kwargs: next(reports, clean))
        monkeypatch.delenv(build.ALLOW_DRIFT_ENV, raising=False)

        provider = MockImageProvider(allowed=True)
        worker = Worker(config, db, service, provider)
        worker.start()
        try:
            time.sleep(1.5)
        finally:
            worker.stop(timeout=5)
        assert provider.calls == 0, "the halt must not lift when drift disappears"

    def test_escape_hatch_continues_but_marks_the_row(
            self, config, db, service, monkeypatch):
        """LUNELLE_ALLOW_CODE_DRIFT=1 keeps development working — and tells the truth.

        The row records runtime_drift_detected=1, so a paid image produced under a
        drifted tree is never indistinguishable from a clean one.
        """
        style = make_style(service, name="Drift Allowed")
        plan = service.create_generation(style["style_id"], ["grid"])
        self._drifting(monkeypatch)
        monkeypatch.setenv(build.ALLOW_DRIFT_ENV, "1")

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        task = service.get_task(plan.created[0]["task_id"])
        assert task["status"] == "success"
        row = db.conn().execute(
            "SELECT runtime_drift_detected FROM task_executions WHERE task_id = ?",
            (plan.created[0]["task_id"],),
        ).fetchone()
        assert row["runtime_drift_detected"] == 1
        execution = get_executions(db, plan.created[0]["task_id"])[0]["execution"]
        assert execution["build"]["runtime_drift_detected"] is True
        assert "worker.py" in execution["build"]["runtime_drift_summary"]

    def test_production_refuses_the_escape_hatch(self, tmp_path, monkeypatch):
        """A stale build billing real money is not a development convenience."""
        from tests.conftest import make_config

        monkeypatch.setenv(build.ALLOW_DRIFT_ENV, "1")
        production = make_config(
            tmp_path, env="production", image_provider="openai-compat",
            image_api_base_url="https://api.example.com/v1",
            image_api_key="sk-real-looking-key", image_model="gpt-image-2",
            admin_token="tok", daily_budget_usd=10.0, grid_size=(1024, 1024),
        )
        problems = production.validate_for_serve()
        assert any("LUNELLE_ALLOW_CODE_DRIFT" in problem for problem in problems)

    def test_clean_tree_does_not_halt(self, config, db, service, monkeypatch):
        """The guard must not be a liveness bug: a clean build keeps working."""
        style = make_style(service, name="No Drift")
        plan = service.create_generation(style["style_id"], ["grid"])
        monkeypatch.delenv(build.ALLOW_DRIFT_ENV, raising=False)
        build.reset_drift_cache()

        run_worker_until_settled(config, db, service, MockImageProvider(allowed=True))

        assert service.get_task(plan.created[0]["task_id"])["status"] == "success"


class TestReadinessSurfacesTheBuild:
    def test_health_reports_build_identity_without_a_token(self, client):
        """Checkable before a paid run, by a probe with no admin token."""
        body = client.get("/health").json()
        assert body["build"]["build_id"] == build.IDENTITY.build_id
        assert body["build"]["runtime_tree_sha256"] == build.IDENTITY.runtime_tree_sha256

    def test_health_leaks_no_secrets(self, client):
        rendered = json.dumps(client.get("/health").json())
        assert "sk-" not in rendered
        assert "api_key" not in rendered

    def test_ready_includes_the_build_check(self, client):
        body = client.get("/ready").json()
        assert "runtime_build_current" in body["checks"]
        assert body["build"]["build_id"] == build.IDENTITY.build_id


class TestHistoricalRowsStayNull:
    def test_migration_does_not_invent_a_build_for_past_executions(self, db):
        """A pre-C1 row's code identity is not knowable; NULL is the honest value.

        Backfilling today's SHA would manufacture exactly the false certainty this
        work removes — the four cells from 2026-08-06 would read as a clean build.
        """
        conn = db.conn()
        with transaction(conn):
            conn.execute(
                "INSERT INTO styles (style_id, sku, name, spec_json, source_type,"
                " source_input_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("st_hist", "SKU-HIST", "Hist", "{}", "structured", "{}",
                 utcnow(), utcnow()),
            )
            conn.execute(
                "INSERT INTO tasks (task_id, style_id, sku, output_type, prompt,"
                " prompt_version, provider, model, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("tk_hist", "st_hist", "SKU-HIST", "grid", "p", "v1", "mock",
                 "m", "success", utcnow(), utcnow()),
            )
            conn.execute(
                "INSERT INTO task_executions (task_id, attempt_no, execution_json,"
                " created_at) VALUES (?,?,?,?)",
                ("tk_hist", 1, json.dumps({"model": "m"}), utcnow()),
            )
        row = conn.execute(
            "SELECT build_id, runtime_tree_sha, runtime_drift_detected"
            " FROM task_executions WHERE task_id = 'tk_hist'"
        ).fetchone()
        assert row["build_id"] is None
        assert row["runtime_tree_sha"] is None
        assert row["runtime_drift_detected"] is None
