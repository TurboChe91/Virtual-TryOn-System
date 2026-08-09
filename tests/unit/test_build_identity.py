"""Build identity: the digest, and the drift it exists to catch.

The incident these cover: on 2026-08-06 four matrix cells were rendered by a worker
process started two hours before the code under test existed. Python does not
hot-reload, so the process sent the old Image 1 while the prompt described a
view-plan it never sent. Nothing in the database distinguished that run from a
correct one, and a git SHA read at execution time would have made it worse -- the
commit landed 26 minutes BEFORE the renders, so it would have certified the run.

Hence the central test here: git_sha and runtime_tree_sha256 must be able to
disagree, and the tree digest must be the one that tracks what actually loaded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lunelle import build


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture
def tree(tmp_path) -> Path:
    """A miniature package tree: code, a contract, a migration, and a UI file."""
    return write_tree(tmp_path / "pkg", {
        "worker.py": "def run(): pass\n",
        "prompts.py": "VERSION = 'mx-2'\n",
        "contracts/matrix_views.json": '{"views": {}}\n',
        "migrations/0001_init.sql": "CREATE TABLE t (id INTEGER);\n",
        "web/index.html": "<html>admin ui</html>\n",
    })


class TestRuntimeFileSelection:
    def test_includes_py_json_and_sql(self, tree):
        names = {p.name for p in build.runtime_files(tree)}
        assert names == {"worker.py", "prompts.py", "matrix_views.json", "0001_init.sql"}

    def test_excludes_html_because_the_ui_cannot_reach_the_provider(self, tree):
        assert "index.html" not in {p.name for p in build.runtime_files(tree)}

    def test_excludes_caches(self, tree):
        write_tree(tree, {"__pycache__/worker.cpython-312.pyc": "compiled"})
        (tree / ".mypy_cache").mkdir(exist_ok=True)
        (tree / ".mypy_cache" / "cached.json").write_text("{}", encoding="utf-8")
        paths = {p.relative_to(tree).as_posix() for p in build.runtime_files(tree)}
        assert not [p for p in paths if "__pycache__" in p or ".mypy_cache" in p]

    def test_order_is_by_relative_path_not_filesystem_order(self, tree):
        ordered = [p.relative_to(tree).as_posix() for p in build.runtime_files(tree)]
        assert ordered == sorted(ordered)

    def test_the_real_package_covers_contracts_and_migrations(self):
        """The two contract files and every migration must be in scope.

        matrix_views.json's screen_slots is the authority for per-view nail order --
        editing it changes what is rendered without touching any Python.
        """
        names = {p.name for p in build.runtime_files()}
        assert "matrix_views.json" in names
        assert "hero_pose_contract.json" in names
        assert "0012_build_identity.sql" in names
        assert "planview.py" in names


class TestTreeDigest:
    def test_same_content_same_digest(self, tree, tmp_path):
        twin = write_tree(tmp_path / "twin", {
            "worker.py": "def run(): pass\n",
            "prompts.py": "VERSION = 'mx-2'\n",
            "contracts/matrix_views.json": '{"views": {}}\n',
            "migrations/0001_init.sql": "CREATE TABLE t (id INTEGER);\n",
            "web/index.html": "<html>different ui entirely</html>\n",
        })
        assert build.tree_digest(build.file_digests(tree)) == \
               build.tree_digest(build.file_digests(twin))

    def test_editing_a_py_file_changes_the_digest(self, tree):
        before = build.tree_digest(build.file_digests(tree))
        (tree / "worker.py").write_text("def run(): return 1\n", encoding="utf-8")
        assert build.tree_digest(build.file_digests(tree)) != before

    def test_editing_a_contract_changes_the_digest(self, tree):
        """A screen_slots edit reorders nails with no Python change."""
        before = build.tree_digest(build.file_digests(tree))
        (tree / "contracts/matrix_views.json").write_text(
            '{"views": {"p2_open_hands": {}}}\n', encoding="utf-8")
        assert build.tree_digest(build.file_digests(tree)) != before

    def test_editing_a_migration_changes_the_digest(self, tree):
        before = build.tree_digest(build.file_digests(tree))
        (tree / "migrations/0001_init.sql").write_text(
            "CREATE TABLE t (id INTEGER, extra TEXT);\n", encoding="utf-8")
        assert build.tree_digest(build.file_digests(tree)) != before

    def test_editing_the_ui_does_not_change_the_digest(self, tree):
        before = build.tree_digest(build.file_digests(tree))
        (tree / "web/index.html").write_text("<html>redesigned</html>\n", encoding="utf-8")
        assert build.tree_digest(build.file_digests(tree)) == before

    def test_renaming_a_file_changes_the_digest(self, tree):
        """Paths are hashed with contents: a rename is a different tree."""
        before = build.tree_digest(build.file_digests(tree))
        (tree / "worker.py").rename(tree / "worker_v2.py")
        assert build.tree_digest(build.file_digests(tree)) != before

    def test_content_cannot_shift_across_the_path_boundary(self, tmp_path):
        """Length-prefixing: ("ab","c") and ("a","bc") must not collide."""
        left = write_tree(tmp_path / "l", {"ab.py": "c"})
        right = write_tree(tmp_path / "r", {"a.py": "bc"})
        assert build.tree_digest(build.file_digests(left)) != \
               build.tree_digest(build.file_digests(right))

    def test_adding_a_file_changes_the_digest_before_it_is_imported(self, tree):
        """Lazy imports mean an unloaded module can still be loaded later."""
        before = build.tree_digest(build.file_digests(tree))
        write_tree(tree, {"newmodule.py": "X = 1\n"})
        assert build.tree_digest(build.file_digests(tree)) != before


class TestDriftDetection:
    """The guard that would have stopped the 2026-08-06 run before it billed."""

    def test_unchanged_tree_reports_no_drift(self, tree):
        identity = build.compute_identity(tree)
        assert not build.detect_drift(identity=identity).drifted

    def test_a_changed_file_is_named(self, tree):
        identity = build.compute_identity(tree)
        (tree / "worker.py").write_text("def run(): return 'new'\n", encoding="utf-8")
        report = build.detect_drift(identity=identity)
        assert report.drifted
        assert "worker.py" in report.changed
        assert "worker.py" in report.summary()

    def test_drift_is_detected_even_when_mtime_is_preserved(self, tree):
        """Content-addressed on purpose.

        The incident's evidence chain was `ps -o lstart` against file mtimes. An
        editor or `cp -p` that preserves mtime would defeat that reasoning; a
        digest does not care.
        """
        identity = build.compute_identity(tree)
        target = tree / "prompts.py"
        original_stat = target.stat()
        target.write_text("VERSION = 'mx-3'\n", encoding="utf-8")
        import os as _os
        _os.utime(target, (original_stat.st_atime, original_stat.st_mtime))
        assert target.stat().st_mtime == original_stat.st_mtime
        assert build.detect_drift(identity=identity).drifted

    def test_added_and_removed_files_drift(self, tree):
        identity = build.compute_identity(tree)
        write_tree(tree, {"extra.py": "Y = 2\n"})
        (tree / "prompts.py").unlink()
        report = build.detect_drift(identity=identity)
        assert "extra.py" in report.added
        assert "prompts.py" in report.removed

    def test_ui_edits_do_not_halt_the_worker(self, tree):
        identity = build.compute_identity(tree)
        (tree / "web/index.html").write_text("<html>new</html>\n", encoding="utf-8")
        assert not build.detect_drift(identity=identity).drifted

    def test_summary_truncates_a_large_change_set(self, tree):
        identity = build.compute_identity(tree)
        write_tree(tree, {f"mod{i}.py": f"Z = {i}\n" for i in range(9)})
        summary = build.detect_drift(identity=identity).summary()
        assert "more" in summary

    def test_dependency_change_counts_as_drift(self, tree, monkeypatch):
        """A Pillow upgrade changes rendered output with no source change."""
        identity = build.compute_identity(tree)
        monkeypatch.setattr(
            build, "dependency_versions",
            lambda: {**identity.dependency_versions, "pillow": "999.0.0"},
        )
        report = build.detect_drift(identity=identity)
        assert report.drifted
        assert report.dependencies_changed
        assert "dependency" in report.summary()

    def test_cache_is_reused_then_refreshed(self, monkeypatch):
        calls = {"n": 0}
        real = build.file_digests

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(build, "file_digests", counting)
        build.reset_drift_cache()
        build.detect_drift()
        build.detect_drift()
        assert calls["n"] == 1, "the cached verdict must serve the second call"
        build.reset_drift_cache()
        build.detect_drift()
        assert calls["n"] == 2

    def test_a_read_error_is_not_reported_as_drift(self, tree, monkeypatch):
        """A transient FS error must not halt a healthy worker."""
        identity = build.compute_identity(tree)

        def exploding(*args, **kwargs):
            raise OSError("transient")

        monkeypatch.setattr(build, "file_digests", exploding)
        assert not build.detect_drift(identity=identity).drifted


class TestBuildIdentity:
    def test_git_sha_and_tree_sha_can_disagree(self, tree):
        """THE regression test for 2026-08-06.

        The commit landed at 17:16 UTC; the renders happened at 17:42. An
        execution-time `git rev-parse HEAD` would have returned the NEW sha and
        stamped the stale run as a valid mx-2 render. The tree digest is taken from
        the bytes that were loaded, so it diverges instead of agreeing.
        """
        identity = build.compute_identity(tree)
        # The operator commits; git's answer moves. The loaded identity does not.
        (tree / "worker.py").write_text("def run(): return 'committed later'\n",
                                       encoding="utf-8")
        report = build.detect_drift(identity=identity)
        assert report.drifted, "a stale process must be detectable"
        assert report.current_tree_sha256 != identity.runtime_tree_sha256
        # And the identity itself is unchanged: it describes what loaded, not disk.
        assert build.compute_identity(tree).runtime_tree_sha256 == \
               report.current_tree_sha256

    def test_identity_is_frozen_at_import(self):
        """IDENTITY must not follow the disk, or it cannot witness drift."""
        before = build.IDENTITY.runtime_tree_sha256
        build.detect_drift(force=True)
        assert build.IDENTITY.runtime_tree_sha256 == before

    def test_build_id_groups_identical_code(self, tree, tmp_path):
        twin = write_tree(tmp_path / "twin", dict.fromkeys([], ""))
        for path in build.runtime_files(tree):
            target = twin / path.relative_to(tree)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        assert build.compute_identity(tree).build_id == build.compute_identity(twin).build_id

    def test_build_id_separates_different_code(self, tree):
        before = build.compute_identity(tree).build_id
        (tree / "worker.py").write_text("def run(): return 2\n", encoding="utf-8")
        assert build.compute_identity(tree).build_id != before

    def test_build_id_reflects_dependency_versions(self, tree, monkeypatch):
        """Same source, different Pillow, must not share a grouping key."""
        before = build.compute_identity(tree).build_id
        monkeypatch.setattr(build, "dependency_versions",
                            lambda: {"python": "3.12.0", "pillow": "999.0.0"})
        assert build.compute_identity(tree).build_id != before

    def test_missing_git_degrades_to_a_label_not_an_error(self, tree, monkeypatch):
        """An installed wheel has no .git. The tree digest still identifies it."""
        monkeypatch.setattr(build, "_git", lambda *a, **k: None)
        monkeypatch.delenv(build.GIT_SHA_ENV, raising=False)
        identity = build.compute_identity(tree)
        assert identity.git_sha is None
        assert identity.build_id.startswith("nogit-")
        assert len(identity.runtime_tree_sha256) == 64

    def test_env_sha_is_the_container_fallback(self, tree, monkeypatch):
        monkeypatch.setattr(build, "_git", lambda *a, **k: None)
        monkeypatch.setenv(build.GIT_SHA_ENV, "abc1234def5678")
        assert build.compute_identity(tree).git_sha == "abc1234def5678"

    def test_a_live_repo_beats_the_env_override(self, tree, monkeypatch):
        """A stale CI variable must never mask the real checkout."""
        monkeypatch.setenv(build.GIT_SHA_ENV, "0000000stale")
        monkeypatch.setattr(build, "_git",
                            lambda *a, **k: "realsha123456" if a[0] == "rev-parse" else "")
        assert build.compute_identity(tree).git_sha == "realsha123456"

    def test_dirty_tree_is_flagged_with_a_diff_digest(self, tree, monkeypatch):
        def fake_git(*args, **kwargs):
            if args[0] == "rev-parse":
                return "deadbeefcafe"
            if args[0] == "diff":
                return "diff --git a/worker.py b/worker.py\n+changed\n"
            return ""

        monkeypatch.setattr(build, "_git", fake_git)
        identity = build.compute_identity(tree)
        assert identity.git_dirty
        assert identity.diff_sha256 and len(identity.diff_sha256) == 64
        assert "+dirty" in identity.build_id

    def test_untracked_runtime_file_makes_the_tree_dirty(self, tree, monkeypatch):
        """An untracked module that gets imported is code in no commit."""
        def fake_git(*args, **kwargs):
            if args[0] == "rev-parse":
                return "deadbeefcafe"
            if args[0] == "diff":
                return ""
            return "scripts/probe.py\nnotes.txt\n"

        monkeypatch.setattr(build, "_git", fake_git)
        identity = build.compute_identity(tree)
        assert identity.git_dirty, "an untracked .py must count as dirty"
        assert identity.diff_sha256

    def test_untracked_non_runtime_file_does_not(self, tree, monkeypatch):
        def fake_git(*args, **kwargs):
            if args[0] == "rev-parse":
                return "deadbeefcafe"
            if args[0] == "diff":
                return ""
            return "notes.txt\nREADME.md\n"

        monkeypatch.setattr(build, "_git", fake_git)
        assert not build.compute_identity(tree).git_dirty


class TestManifest:
    def test_manifest_carries_the_fields_the_incident_needed(self, tree):
        manifest = build.compute_identity(tree).as_manifest(
            worker_instance="lunelle-worker-1")
        for key in ("build_id", "runtime_tree_sha256", "git_sha", "git_dirty",
                    "dependency_sha256", "dependency_versions", "process_id",
                    "loaded_at", "worker_instance", "runtime_drift_detected",
                    "manifest_version"):
            assert key in manifest, f"manifest is missing {key}"
        assert manifest["worker_instance"] == "lunelle-worker-1"
        assert manifest["runtime_drift_detected"] is False

    def test_manifest_records_drift_when_present(self, tree):
        identity = build.compute_identity(tree)
        (tree / "worker.py").write_text("changed\n", encoding="utf-8")
        report = build.detect_drift(identity=identity)
        manifest = identity.as_manifest(drift=report)
        assert manifest["runtime_drift_detected"] is True
        assert "worker.py" in manifest["runtime_drift_summary"]

    def test_manifest_omits_per_file_digests(self, tree):
        """46 file digests per execution row is noise; drift needs them, not audit."""
        manifest = build.compute_identity(tree).as_manifest()
        assert "file_digests" not in manifest
        assert manifest["runtime_file_count"] == len(build.runtime_files(tree))

    def test_stamp_uses_the_process_identity(self):
        assert build.stamp()["build_id"] == build.IDENTITY.build_id

    def test_describe_carries_no_secrets(self):
        rendered = repr(build.describe())
        assert "api_key" not in rendered
        assert "sk-" not in rendered


class TestDriftEscapeHatch:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv(build.ALLOW_DRIFT_ENV, raising=False)
        assert not build.drift_allowed()

    def test_opt_in_is_explicit(self, monkeypatch):
        monkeypatch.setenv(build.ALLOW_DRIFT_ENV, "1")
        assert build.drift_allowed()

    @pytest.mark.parametrize("value", ["0", "", "true", "yes"])
    def test_only_the_literal_one_enables_it(self, monkeypatch, value):
        monkeypatch.setenv(build.ALLOW_DRIFT_ENV, value)
        assert not build.drift_allowed()
