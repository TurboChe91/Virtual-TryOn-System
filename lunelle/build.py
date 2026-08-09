"""Which code produced this image — established at import, not guessed afterwards.

The failure this exists to prevent happened on 2026-08-06. Four matrix cells were
rendered for $0.76 by a worker process started at 13:08 UTC; the view-plan code
they were meant to exercise was written at 15:30 UTC and committed at 17:16 UTC.
Python does not hot-reload, so the process sent the OLD Image 1 while the prompt —
frozen at queue time by the NEW code — described a view-plan that was never sent.
Nothing in the database could tell that run apart from a correct one:
`prompt_version` read `mx-2+8176b704`, exactly as a real mx-2 render would. The
only evidence was `ps -o lstart` against file mtimes, a chain that exists outside
the process and disappears when the process does.

Recording the git SHA at execution time would NOT have caught it. The commit
landed at 17:16 and the renders happened at 17:42, so `git rev-parse HEAD` would
have returned the new SHA and stamped the invalid run as a valid mx-2 render — the
wrong answer, recorded authoritatively. What matters is not what the repository
says now but which bytes this interpreter actually loaded.

Hence `runtime_tree_sha256`: a digest over every file that can change generation
behaviour, computed when this module is imported and never recomputed for the
life of the process. It covers

- **`.py`** — all of them, not an audited subset. This codebase imports lazily
  (`worker.py` imports `.llm` inside a method), so a module not yet loaded can
  still be loaded later from disk. Any of them can change an output.
- **`.json`** — `contracts/matrix_views.json` is the authority for per-view screen
  order; `contracts/hero_pose_contract.json` for the hero pose. `screen_slots`
  feeds both the compiled view-plan and the prose mapping note, so editing it
  changes what gets rendered without touching a line of Python.
- **`.sql`** — migrations define the columns generation reads. `schema_migrations`
  records which ones were APPLIED, which is a different fact from which ones are
  on disk; an edited-but-unapplied migration is exactly the drift worth seeing.

`web/*.html` is excluded: the admin UI cannot reach the provider.

Interpreter and library versions ride alongside in `dependency_sha256`, because
Pillow renders every view-plan and mask and httpx carries every request — an
upgrade under a running process changes output with no source change at all.

**Known gap.** `planview._font()` resolves a system font by absolute path
(`/System/Library/Fonts/Helvetica.ttc`, ...). Those files are outside the tree, so
a different host with a different font compiles visibly different view-plan
captions at an identical `runtime_tree_sha256`. Closing that belongs with C5,
where canonical geometry and render determinism are the subject. It is a real hole
and is deliberately left open rather than half-covered here.

Two identifiers, two jobs:

- `runtime_tree_sha256` is the authority. Content-addressed, so it is immune to
  the mtime and process-time reasoning the incident had to rely on.
- `git_sha` is a human-readable label for correlating with history. It can be
  absent (installed wheel, no `.git`) or stale relative to the tree, and is never
  the thing anything decides on.

The digest describes the tree AT IMPORT. Editing a file afterwards does not change
it — that is the point, and it is also why `detect_drift()` exists: it re-reads the
tree and reports what no longer matches, which is what lets the worker refuse to
claim work rather than bill for a stale build.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

logger = logging.getLogger(__name__)

#: Manifest schema version for the `build` object written into task_executions.
#: C2 extends the manifest with request/response detail and bumps this.
MANIFEST_VERSION = 1

#: File types that can change what a generation produces. See the module
#: docstring for why each is here and why `.html` is not.
RUNTIME_SUFFIXES = (".py", ".json", ".sql")

#: Never part of the digest: caches and compiled artefacts, which vary between
#: hosts without any source difference.
EXCLUDED_DIRS = frozenset({"__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"})

#: Installed distributions that participate in generation. Pillow renders every
#: view-plan, mask and QA measurement; httpx carries every provider request;
#: pydantic validates the StyleSpec the prompt is built from. Dev tooling (ruff,
#: mypy, pytest) is excluded on purpose — it cannot alter an output, and including
#: it would report drift on a lint upgrade.
RUNTIME_DEPENDENCIES = ("pillow", "httpx", "pydantic")

#: Set by CI or a container build where `.git` is absent (see the Dockerfile's
#: LUNELLE_BUILD_GIT_SHA arg). Read only as a fallback, never over a live repo.
GIT_SHA_ENV = "LUNELLE_BUILD_GIT_SHA"

#: Opt out of the drift guard for local development, where editing code under a
#: running server is normal. Refused in production by Config.validate_for_serve.
ALLOW_DRIFT_ENV = "LUNELLE_ALLOW_CODE_DRIFT"

#: Seconds a drift verdict is reused. The worker polls every second per thread;
#: re-hashing ~46 files each time would be wasted I/O, and drift that matters is
#: an operator editing files, which does not need sub-second detection.
DRIFT_CACHE_TTL_S = 5.0

_PACKAGE_ROOT = Path(__file__).resolve().parent


def runtime_files(root: Path | None = None) -> list[Path]:
    """Every file that participates in the digest, sorted by relative POSIX path.

    Sorted so the digest is a property of the tree's contents and not of the order
    the filesystem happened to yield entries in — otherwise the same checkout
    could hash differently on two machines.
    """
    base = root or _PACKAGE_ROOT
    found: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix not in RUNTIME_SUFFIXES:
            continue
        if EXCLUDED_DIRS & set(path.relative_to(base).parts):
            continue
        found.append(path)
    return sorted(found, key=lambda p: p.relative_to(base).as_posix())


def file_digests(root: Path | None = None) -> dict[str, str]:
    """sha256 per file, keyed by relative POSIX path.

    Kept per-file rather than only in aggregate so drift can name what changed. An
    operator told "the code changed" has to go find it; one told
    "lunelle/worker.py changed" already knows what happened.
    """
    base = root or _PACKAGE_ROOT
    return {
        path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in runtime_files(base)
    }


def tree_digest(digests: dict[str, str]) -> str:
    """Aggregate digest over per-file digests.

    Paths are hashed alongside contents, so renaming a file changes the digest even
    when no byte of its content did. Each record is length-prefixed: without it,
    `("ab", "c")` and `("a", "bc")` would concatenate identically and two different
    trees could collide.
    """
    accumulator = hashlib.sha256()
    for relative_path, digest in sorted(digests.items()):
        record = f"{relative_path}\0{digest}".encode()
        accumulator.update(str(len(record)).encode())
        accumulator.update(b"\0")
        accumulator.update(record)
    return accumulator.hexdigest()


def dependency_versions() -> dict[str, str]:
    """Interpreter and generation-relevant library versions.

    A Pillow upgrade under a running process changes rendered output with no source
    change, so this belongs in build identity even though it is not code in the
    tree. Absent packages are reported as "absent" rather than skipped: "pillow is
    not installed" is a fact about this build, not a gap in the record.
    """
    versions = {"python": sys.version.split()[0]}
    for name in RUNTIME_DEPENDENCIES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "absent"
    return versions


def _dependency_digest(versions: dict[str, str]) -> str:
    joined = ";".join(f"{name}={version}" for name, version in sorted(versions.items()))
    return hashlib.sha256(joined.encode()).hexdigest()


def _git(*args: str, cwd: Path) -> str | None:
    """Run one read-only git command, or return None if git cannot answer.

    Timed out and never raising: this runs at import, and a missing binary, an
    absent repository or a hung filesystem must degrade the label rather than stop
    the process from starting. The authority is the tree digest, which needs no git.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            ["git", *args],  # noqa: S607 - resolved via PATH by design; git is not vendored
            cwd=cwd, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def git_state(root: Path | None = None) -> tuple[str | None, bool, str | None]:
    """(git_sha, dirty, diff_sha256) for the checkout, best effort.

    `diff_sha256` covers tracked modifications AND the list of untracked runtime
    files, because an untracked module that gets imported is code that ran and is
    in no commit. Untracked paths are hashed by name only: their contents are
    already in `runtime_tree_sha256`, and this field exists to identify the
    working-tree state, not to duplicate it.

    Falls back to LUNELLE_BUILD_GIT_SHA when there is no repository, which is the
    container case — the Dockerfile copies `lunelle/` but not `.git`.
    """
    base = root or _PACKAGE_ROOT
    repo = base.parent
    sha = _git("rev-parse", "HEAD", cwd=repo)
    if sha is None:
        env_sha = os.environ.get(GIT_SHA_ENV, "").strip()
        return (env_sha or None), False, None

    diff = _git("diff", "HEAD", cwd=repo) or ""
    untracked_raw = _git("ls-files", "--others", "--exclude-standard", cwd=repo) or ""
    untracked = sorted(
        line for line in untracked_raw.splitlines()
        if line.strip() and Path(line).suffix in RUNTIME_SUFFIXES
    )
    if not diff and not untracked:
        return sha, False, None
    payload = diff + "\n--untracked--\n" + "\n".join(untracked)
    return sha, True, hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class DriftReport:
    """What no longer matches the build this process started with."""

    drifted: bool
    changed: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    dependencies_changed: bool = False
    current_tree_sha256: str = ""

    def summary(self) -> str:
        if not self.drifted:
            return "no drift"
        parts = []
        for label, paths in (("changed", self.changed), ("added", self.added),
                             ("removed", self.removed)):
            if paths:
                shown = ", ".join(paths[:5])
                more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
                parts.append(f"{label}: {shown}{more}")
        if self.dependencies_changed:
            parts.append("installed dependency versions changed")
        return "; ".join(parts)


@dataclass(frozen=True)
class BuildIdentity:
    """The identity of the code this process loaded. Computed once, at import."""

    runtime_tree_sha256: str
    git_sha: str | None
    git_dirty: bool
    diff_sha256: str | None
    dependency_sha256: str
    dependency_versions: dict[str, str]
    process_id: int
    #: When this module was imported, which is when the digest was taken. Not the
    #: OS process start: what matters is when the code was frozen, and any import
    #: before this one cannot have read a different tree.
    loaded_at: str
    file_count: int
    #: Per-file digests, kept in memory so drift can name what changed. Never
    #: written to the manifest — 46 entries per execution row is noise.
    file_digests: dict[str, str] = field(repr=False, default_factory=dict)
    #: The tree this identity was taken from. Carried so drift detection re-reads
    #: the SAME tree: an identity that did not know its own root could only be
    #: re-checked against the package root, which silently compares one tree's
    #: baseline to another tree's contents.
    root: Path = field(repr=False, default=_PACKAGE_ROOT)

    @property
    def build_id(self) -> str:
        """Stable label for "this code", suitable for GROUP BY on its own.

        Excludes pid and thread: two processes running identical code share a
        build_id, which is what makes "every execution on this build" answerable.
        Process identity lives in its own columns.

        Includes the dependency digest, so the guarantee "same build_id means same
        generation behaviour" actually holds — a Pillow upgrade changes rendering
        with no source change. The alternative was a code-only id plus a separate
        column to remember to group by, and a grouping key you have to remember to
        combine is how the conflation this module exists to prevent comes back.
        """
        git_part = (self.git_sha or "nogit")[:7]
        if self.git_dirty:
            git_part += "+dirty"
        return f"{git_part}-{self.runtime_tree_sha256[:8]}-d{self.dependency_sha256[:6]}"

    def as_manifest(self, *, worker_instance: str | None = None,
                    drift: DriftReport | None = None) -> dict:
        """The `build` object embedded in a task_executions manifest."""
        return {
            "manifest_version": MANIFEST_VERSION,
            "build_id": self.build_id,
            "runtime_tree_sha256": self.runtime_tree_sha256,
            "git_sha": self.git_sha,
            "git_dirty": self.git_dirty,
            "diff_sha256": self.diff_sha256,
            "dependency_sha256": self.dependency_sha256,
            "dependency_versions": dict(sorted(self.dependency_versions.items())),
            "runtime_file_count": self.file_count,
            "process_id": self.process_id,
            "loaded_at": self.loaded_at,
            "worker_instance": worker_instance,
            "runtime_drift_detected": bool(drift and drift.drifted),
            "runtime_drift_summary": drift.summary() if drift and drift.drifted else None,
        }


def compute_identity(root: Path | None = None) -> BuildIdentity:
    """Take a full build identity from the tree as it is right now.

    Called once at import for the process singleton; tests call it against a
    temporary tree to exercise the digest without touching the real package.
    """
    base = root or _PACKAGE_ROOT
    digests = file_digests(base)
    versions = dependency_versions()
    git_sha, dirty, diff_sha = git_state(base)
    return BuildIdentity(
        runtime_tree_sha256=tree_digest(digests),
        git_sha=git_sha,
        git_dirty=dirty,
        diff_sha256=diff_sha,
        dependency_sha256=_dependency_digest(versions),
        dependency_versions=versions,
        process_id=os.getpid(),
        loaded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        file_count=len(digests),
        file_digests=digests,
        root=base,
    )


#: The identity of THIS process's code. Frozen at import on purpose: recomputing
#: it later would describe the tree as it is then, which is precisely the mistake
#: that made the 2026-08-06 run unattributable.
IDENTITY = compute_identity()

_drift_lock = threading.Lock()
_drift_cached: tuple[float, DriftReport] | None = None


def _compare(identity: BuildIdentity, current: dict[str, str],
             current_versions: dict[str, str]) -> DriftReport:
    baseline = identity.file_digests
    changed = tuple(sorted(
        path for path, digest in current.items()
        if path in baseline and baseline[path] != digest
    ))
    added = tuple(sorted(set(current) - set(baseline)))
    removed = tuple(sorted(set(baseline) - set(current)))
    deps_changed = current_versions != identity.dependency_versions
    return DriftReport(
        drifted=bool(changed or added or removed or deps_changed),
        changed=changed, added=added, removed=removed,
        dependencies_changed=deps_changed,
        current_tree_sha256=tree_digest(current),
    )


def detect_drift(*, force: bool = False, identity: BuildIdentity | None = None) -> DriftReport:
    """Compare the tree on disk against the build this process loaded.

    Answers the question the incident could only answer with `ps` and mtimes: is
    the code on disk still the code running? Content-addressed, so it holds even
    when an editor or `cp -p` preserves mtimes.

    Results are cached for DRIFT_CACHE_TTL_S because every worker thread asks
    before every claim. `force=True` bypasses the cache for tests and for the
    one-shot check at startup.
    """
    global _drift_cached
    base = identity or IDENTITY
    if identity is None and not force:
        with _drift_lock:
            if _drift_cached is not None:
                cached_at, report = _drift_cached
                if (datetime.now(UTC).timestamp() - cached_at) < DRIFT_CACHE_TTL_S:
                    return report
    try:
        report = _compare(base, file_digests(base.root), dependency_versions())
    except OSError:
        # A transient read error is not evidence of drift, and claiming it is would
        # halt a healthy worker. Report clean and let the next check decide.
        logger.warning("could not read the runtime tree for drift detection", exc_info=True)
        report = DriftReport(drifted=False, current_tree_sha256=base.runtime_tree_sha256)
    if identity is None:
        with _drift_lock:
            _drift_cached = (datetime.now(UTC).timestamp(), report)
    return report


def reset_drift_cache() -> None:
    """Drop the cached verdict. For tests that mutate a tree and re-check."""
    global _drift_cached
    with _drift_lock:
        _drift_cached = None


def drift_allowed() -> bool:
    """Whether this process may keep working after drift is detected.

    Development edits code under a running server constantly, so the guard has an
    escape hatch. Config.validate_for_serve refuses it in production, where a
    stale build spends real money on unattributable images.
    """
    return os.environ.get(ALLOW_DRIFT_ENV, "0") == "1"


def stamp(*, worker_instance: str | None = None, drift: DriftReport | None = None) -> dict:
    """The `build` object for one execution record."""
    return IDENTITY.as_manifest(worker_instance=worker_instance, drift=drift)


def describe() -> dict:
    """Build identity for logs and the readiness endpoint (no per-file detail)."""
    return {
        "build_id": IDENTITY.build_id,
        "runtime_tree_sha256": IDENTITY.runtime_tree_sha256,
        "git_sha": IDENTITY.git_sha,
        "git_dirty": IDENTITY.git_dirty,
        "runtime_file_count": IDENTITY.file_count,
        "process_id": IDENTITY.process_id,
        "loaded_at": IDENTITY.loaded_at,
        "dependency_versions": dict(sorted(IDENTITY.dependency_versions.items())),
    }
