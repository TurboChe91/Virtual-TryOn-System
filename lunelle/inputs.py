"""Task inputs, frozen by role and digest at queue time and verified before use.

The defect this closes: the snapshot was write-only. Its only reader was the admin
display endpoint, and the helper written to resolve it back to files
(`snapshots.snapshot_asset_paths`) had no production caller at all. The worker
re-derived its inputs at execution time from whatever the style row, `app_settings`
and the viewplan cache directory happened to say — so "what this image was
generated from" had four independent answers for one cell:

    snapshot input_assets[0]  the raw plan, labelled kind='hand_model'
    snapshot prompt           "Image 1 is the VIEW PLAN ... copy each tile"
    actually sent             the raw plan (old worker, resolved live)
    QA's Image 2              the raw plan again, re-read from style.plan_image_path

The four cells of 2026-08-06 got a prompt describing captions with an image that
had none — a combination that existed in no version of the code, so their output
could not be evidence for or against anything.

Three properties make that unrepresentable rather than merely unlikely:

1. **Role, not position.** An input is `view_plan` or `base_hand`, never "the first
   one". Position was implicit in a list whose two ends were built by different
   functions; the kind field said `hand_model` for a plan and `reference` for
   everything, so neither end described what it held.
2. **Digest, not path.** Inputs resolve through the content-addressed store, where
   the filename IS the sha256. Overwriting is impossible by construction, so a
   re-uploaded hand model cannot change what a queued task will send.
3. **Verified, not trusted.** Bytes are re-hashed on load. A digest that no longer
   matches its file is a blocked task, never a substituted input — the store makes
   that near-impossible, but "near-impossible" is not what a paid call should rest
   on, and legacy rows can still point outside the store.

Missing or mismatched inputs raise `InputUnavailable`, which the worker turns into
`dependency_missing` with no provider call. There is deliberately no fallback: a
fallback is how a cell rendered against the wrong Image 1 and still looked
successful.

One input genuinely cannot be frozen: a wearing shot copies the grid generated
later in the same batch, which does not exist when the wearing task is queued.
Claiming a digest for it would be a guess, so it is declared in `deferred` and
resolved at execution time, and the manifest records what was actually used. The
list of deferred roles is closed and small on purpose — everything else must be
frozen.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .assets import AssetRef, digest_of_file, register_locked
from .db import Database
from .prompts import strip_reference_block

logger = logging.getLogger(__name__)

#: What an input IS, independent of where it sits in the request. The provider
#: receives them in this list's order, but the worker and QA address them by role.
ROLES = (
    # Matrix Image 1: the plan recompiled into this view's screen order.
    "view_plan",
    # Image 1 when no view-plan applies, or when compilation failed.
    "design_plan",
    # Matrix Image 2: the immutable base hand photo.
    "base_hand",
    # Hero Image 2: pose, hand geometry, crop, lighting, and skin authority.
    "photography_reference",
    # Operator's uploaded photo reference.
    "style_reference",
    # Correction: the previous candidate, edited in place.
    "correction_base",
    # Correction: per-nail detail crops.
    "correction_detail",
    # Wearing: the grid it copies. Deferred — see below.
    "grid",
)

# Inputs a correction must inherit from the source snapshot, in the order in
# which that snapshot froze them. They follow correction_base in a correction
# request; keeping this list role-based prevents authority drift across versions.
AUTHORITY_ROLES = frozenset({
    "view_plan",
    "design_plan",
    "base_hand",
    "photography_reference",
    # Backward compatibility: Hero snapshots created before the dedicated role
    # used style_reference for their photography authority. Grid corrections may
    # also legitimately inherit an uploaded style reference under this role.
    "style_reference",
    "grid",
})

#: Roles whose asset cannot exist when the task is queued. A wearing shot waits on
#: a grid generated later in the same batch. Kept explicit and closed: anything not
#: listed here MUST be frozen, and the worker refuses a snapshot that defers
#: something else.
DEFERRABLE_ROLES = frozenset({"grid"})


class InputUnavailable(Exception):
    """A frozen input cannot be loaded, so the task must not call the provider."""


@dataclass(frozen=True)
class FrozenInput:
    """One input, named by role and identified by content."""

    role: str
    digest: str
    path: str
    byte_size: int
    mime_type: str
    #: Pixel size where known. Recorded for auditing; not used for resolution.
    width: int | None = None
    height: int | None = None
    #: Provenance for a derived asset (a view-plan records the plan it came from
    #: and the contract that ordered it), so a compiled input is reproducible.
    derived_from: dict | None = None

    def as_dict(self) -> dict:
        doc: dict = {
            "role": self.role,
            "digest": self.digest,
            "path": self.path,
            "byte_size": self.byte_size,
            "mime_type": self.mime_type,
        }
        if self.width and self.height:
            doc["size"] = [self.width, self.height]
        if self.derived_from:
            doc["derived_from"] = dict(sorted(self.derived_from.items()))
        return doc


@dataclass
class ResolvedInputs:
    """Inputs loaded from a snapshot, verified, in provider order.

    Carried from the worker into QA unchanged. QA re-deriving its own view of the
    inputs is what let it judge a candidate against an image the provider never
    saw, so there is one resolution per execution and both readers share it.
    """

    entries: list[tuple[str, Path]] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        return [path for _, path in self.entries]

    @property
    def roles(self) -> list[str]:
        return [role for role, _ in self.entries]

    def first(self, *roles: str) -> Path | None:
        """The first input matching any of `roles`, in the order given."""
        for wanted in roles:
            for role, path in self.entries:
                if role == wanted:
                    return path
        return None

    def append(self, role: str, path: Path) -> None:
        self.entries.append((role, path))


# ---------------------------------------------------------------------------
# Freezing, at queue time
# ---------------------------------------------------------------------------


def _pixel_size(path: Path) -> tuple[int | None, int | None]:
    """(width, height), or (None, None) for anything unreadable.

    Recorded for auditing only. A failure here must not block queueing: the digest
    is what identifies the input, and image metadata is commentary on it.
    """
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:  # noqa: BLE001 - unreadable/missing/not-an-image: no opinion
        return None, None


def freeze(ref: AssetRef, *, role: str, derived_from: dict | None = None) -> FrozenInput:
    """Describe a stored asset as a frozen input under `role`."""
    if role not in ROLES:
        raise ValueError(f"unknown input role {role!r}; expected one of {ROLES}")
    width, height = _pixel_size(ref.path)
    return FrozenInput(
        role=role, digest=ref.digest, path=str(ref.path),
        byte_size=ref.byte_size, mime_type=ref.mime_type,
        width=width, height=height, derived_from=derived_from,
    )


def freeze_file_locked(conn, path: Path, *, role: str, kind: str,
                       derived_from: dict | None = None) -> FrozenInput | None:
    """Register a file already on disk and freeze it, inside the caller's transaction.

    Used for inputs whose bytes are already content-addressed or already immutable
    in practice. Returns None when the file is absent, which the caller records as
    an unresolved input rather than silently omitting.
    """
    ref = register_locked(conn, path, kind=kind)
    if ref is None:
        return None
    return freeze(ref, role=role, derived_from=derived_from)


# ---------------------------------------------------------------------------
# Loading, at execution time
# ---------------------------------------------------------------------------


def load_one(db: Database, entry: dict) -> Path:
    """Resolve one frozen input to a verified file, or raise InputUnavailable.

    Digest first, recorded path as a fallback for assets registered before the
    store existed. Either way the bytes are re-hashed: resolution that trusts a
    path is how an overwritten file silently becomes a different input.
    """
    role = entry.get("role", "?")
    digest = entry.get("digest")
    if not digest:
        raise InputUnavailable(
            f"input {role!r} has no digest in the snapshot, so what it was cannot "
            f"be established; re-queue this task"
        )

    candidates: list[Path] = []
    record = db.conn().execute(
        "SELECT path FROM assets WHERE digest = ?", (digest,)
    ).fetchone()
    if record is not None:
        candidates.append(Path(record["path"]))
    recorded = entry.get("path")
    if recorded and Path(recorded) not in candidates:
        candidates.append(Path(recorded))

    for candidate in candidates:
        if not candidate.is_file():
            continue
        actual = digest_of_file(candidate)
        if actual == digest:
            return candidate
        # Same name, different bytes. Inside the store this should be impossible;
        # outside it (a legacy path) it is exactly the overwrite the digest exists
        # to catch. Never fall through to the next candidate on a mismatch without
        # saying so.
        logger.error(
            "input %s: %s has digest %s but the snapshot froze %s; refusing to use it",
            role, candidate, actual[:12], digest[:12],
        )
    raise InputUnavailable(
        f"input {role!r} (digest {digest[:12]}) could not be loaded: "
        f"{'no file matches that digest' if candidates else 'the asset is not registered'}. "
        f"The frozen bytes are gone, so this task cannot be reproduced as queued; "
        f"re-queue it to freeze current inputs."
    )


def load_all(db: Database, entries: list[dict]) -> ResolvedInputs:
    """Resolve every frozen input, in order. Raises on the first failure.

    All-or-nothing on purpose: a partially resolved input set is what produces a
    prompt that describes images the provider never received.
    """
    resolved = ResolvedInputs()
    for entry in entries:
        resolved.append(entry.get("role", "?"), load_one(db, entry))
    return resolved


# ---------------------------------------------------------------------------
# The provider-call boundary
# ---------------------------------------------------------------------------


def channel_fingerprint(profile: dict | None) -> str:
    """Identify the channel a task was queued against, without storing its secret.

    Covers profile id, base URL and key. A profile row is edited in place, so the
    same profile_id can point somewhere else with a different key tomorrow; queued
    tasks would then silently go to a channel their snapshot never described.
    Comparing this at execution time turns that into a blocked task.

    Fingerprints rather than values: a snapshot is long-lived, exported, and read by
    anyone debugging a task, so the key must not be in it — and the base URL is
    hashed alongside so one comparison covers the whole channel.
    """
    if not profile:
        return "env-fallback"
    parts = "|".join([
        str(profile.get("profile_id") or ""),
        str(profile.get("base_url") or ""),
        str(profile.get("key_fingerprint") or ""),
    ])
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


#: The only permitted transformations between a snapshot's prompt and the prompt
#: actually sent. Closed registry, and each entry is a pure function of the frozen
#: prompt, so the comparison below can RECOMPUTE the expected result rather than
#: trust the worker's claim about what it did.
#:
#: `strip_reference_block` exists for the one honest divergence: a wearing shot's
#: grid is generated later in the same batch, and if it is not there the prompt must
#: not claim an Image 1 exists. That is a different thing from "the prompt drifted",
#: and collapsing the two would reopen the gap this module closes.
PROMPT_TRANSFORMS: dict[str, Callable[[str], str]] = {
    "none": lambda text: text,
    "strip_reference_block": strip_reference_block,
}


def request_manifest(*, prompt: str, negative_prompt: str, size: tuple[int, int],
                     model: str, provider: str, resolved: ResolvedInputs,
                     channel: str, prompt_transform: str = "none") -> dict:
    """What is about to be sent, measured from the bytes themselves.

    Built by re-hashing each file at the call boundary rather than by copying the
    snapshot's digests forward. Copying them would make the comparison below
    tautological: it would prove the snapshot equals itself.
    """
    images = []
    for index, (role, path) in enumerate(resolved.entries, start=1):
        images.append({
            "index": index,
            "role": role,
            "sha256": digest_of_file(path),
            "byte_size": path.stat().st_size,
            "path": str(path),
        })
    if prompt_transform not in PROMPT_TRANSFORMS:
        raise ValueError(
            f"unknown prompt transform {prompt_transform!r}; "
            f"expected one of {sorted(PROMPT_TRANSFORMS)}"
        )
    return {
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "negative_prompt_sha256": hashlib.sha256(negative_prompt.encode()).hexdigest(),
        "size_requested": [size[0], size[1]],
        "model": model,
        "provider": provider,
        "channel_fingerprint": channel,
        # Declared, not assumed. Verified by recomputation below.
        "prompt_transform": prompt_transform,
        "input_images": images,
    }


def compare_to_snapshot(snapshot: dict, manifest: dict) -> list[str]:
    """Differences between what was planned and what is about to be sent.

    An empty list is the precondition for calling the provider. Every field checked
    here was, at some point, able to drift from the snapshot unnoticed:

    - prompt: frozen in `tasks.prompt` AND in the snapshot, and the worker read the
      column. They were never compared, and `prompt_differs_from_snapshot` sat at
      None because no caller ever filled it.
    - size: recomputed at execution time from the current `app_settings` hand model.
    - model: frozen; the channel it was sent to was resolved live.
    - images: the whole point. Role and digest, in order.

    Frozen inputs come first and are compared exactly, by role AND digest. Anything
    beyond them must be a role the snapshot DECLARED deferrable — the snapshot's own
    declaration is the authority, not the worker's account of what it resolved, so a
    worker appending an undeclared image is a mismatch rather than an exception.
    """
    problems: list[str] = []

    # Apply the DECLARED transform to the snapshot's own prompt and hash the result.
    # Recomputing means a worker cannot excuse an arbitrary prompt by naming a
    # transform: only the exact output of that transform on the frozen text passes.
    transform_name = manifest.get("prompt_transform", "none")
    transform = PROMPT_TRANSFORMS.get(transform_name)
    if transform is None:
        problems.append(f"prompt transform {transform_name!r} is not a permitted transform")
        transform = PROMPT_TRANSFORMS["none"]
    expected_prompt = transform(snapshot.get("prompt") or "")
    planned_prompt_sha = hashlib.sha256(expected_prompt.encode()).hexdigest()
    if manifest["prompt_sha256"] != planned_prompt_sha:
        detail = (f" after {transform_name}" if transform_name != "none" else "")
        problems.append(
            f"prompt differs from the snapshot{detail} "
            f"(sending {manifest['prompt_sha256'][:12]}, "
            f"snapshot froze {planned_prompt_sha[:12]})"
        )

    planned_size = list(snapshot.get("size") or [])
    if planned_size and manifest["size_requested"] != planned_size:
        problems.append(
            f"size differs from the snapshot "
            f"(sending {manifest['size_requested']}, snapshot froze {planned_size})"
        )

    planned_model = ((snapshot.get("channel") or {}).get("model")) or ""
    if planned_model and manifest["model"] != planned_model:
        problems.append(
            f"model differs from the snapshot "
            f"(sending {manifest['model']!r}, snapshot froze {planned_model!r})"
        )

    planned_inputs = list(snapshot.get("input_assets") or [])
    declared_deferred = [
        entry.get("role", "?") for entry in (snapshot.get("deferred_inputs") or [])
    ]
    sent = manifest["input_images"]

    if len(sent) < len(planned_inputs):
        problems.append(
            f"fewer inputs than the snapshot froze "
            f"(sending {len(sent)}, snapshot froze {len(planned_inputs)})"
        )
    for index, (planned, actual) in enumerate(
            zip(planned_inputs, sent, strict=False), start=1):
        planned_role = planned.get("role", "?")
        if planned_role != actual["role"]:
            problems.append(
                f"input {index} role differs from the snapshot "
                f"(sending {actual['role']!r}, snapshot froze {planned_role!r})"
            )
            continue
        if planned.get("digest") != actual["sha256"]:
            problems.append(
                f"input {index} ({planned_role}) content differs from the snapshot "
                f"(sending {actual['sha256'][:12]}, "
                f"snapshot froze {str(planned.get('digest'))[:12]})"
            )

    # Anything past the frozen prefix must have been declared deferrable. Fewer
    # deferred images than declared is fine — a wearing shot with no grid yet
    # renders text-only — but an image the snapshot never mentioned is not.
    remaining = declared_deferred[:]
    for index, actual in enumerate(sent[len(planned_inputs):],
                                   start=len(planned_inputs) + 1):
        if actual["role"] in remaining:
            remaining.remove(actual["role"])
            continue
        problems.append(
            f"input {index} ({actual['role']}) is not in the snapshot: it was "
            f"neither frozen nor declared deferrable"
        )
    return problems
