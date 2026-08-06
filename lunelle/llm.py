"""Vision-LLM helpers: identity extraction and style recognition from images.

The chat channel resolves from the active kind='llm' profile first, then the
LUNELLE_TEXT_* environment settings. Responses are parsed defensively: models
wrap JSON in prose/fences at will, and vocab fields are validated upstream by
StyleSpec, so anything invalid degrades to defaults instead of failing hard.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx

from .config import Config
from .db import Database
from .logging_setup import redact
from .profiles import ProfileService
from .urlguard import assert_safe_request_url

logger = logging.getLogger(__name__)

ChatFn = Callable[[str, str, list[Path]], str]


class LLMUnavailable(Exception):
    """No LLM channel configured (neither an active llm profile nor env text settings)."""


class _Channel(Protocol):  # pragma: no cover - typing aid
    base_url: str
    api_key: str
    model: str


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def build_llm_chat(config: Config, db: Database) -> ChatFn:
    """Return chat(system, user, images) -> str, or raise LLMUnavailable."""
    row = ProfileService(db).active_row(kind="llm")
    if row is not None:
        base_url, api_key, model = row["base_url"], row["api_key"], row["model"]
    elif config.text_model and config.text_api_key and config.text_api_base_url:
        base_url, api_key, model = config.text_api_base_url, config.text_api_key, config.text_model
    else:
        raise LLMUnavailable(
            "no LLM channel: activate an llm profile in settings or set LUNELLE_TEXT_* in .env"
        )

    def chat(system: str, user: str, images: list[Path]) -> str:
        # Re-validated per call: the profile is operator-editable and DNS can
        # change between write and use.
        assert_safe_request_url(
            base_url, allow_private=config.allow_private_api_hosts
        )
        content: list[dict] = [{"type": "text", "text": user}]
        for image in images:
            content.append({"type": "image_url", "image_url": {"url": _data_url(image)}})
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "max_tokens": 2000,
        }
        try:
            with httpx.Client(timeout=120) as client:
                response = client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                    follow_redirects=False,
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LLM request failed: {redact(str(exc))}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"LLM returned HTTP {response.status_code}: {redact(response.text[:300])}"
            )
        try:
            return response.json()["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("LLM returned an unexpected response shape") from exc

    return chat


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of an LLM reply (fences/prose tolerated)."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)
    try:
        doc = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM reply is not valid JSON: {text[:200]}") from exc
    if not isinstance(doc, dict):
        raise ValueError("LLM reply JSON is not an object")
    return doc


IDENTITY_SYSTEM = (
    "You are a senior press-on-nail product analyst. You describe nail designs "
    "precisely, in English, for an image-generation identity contract. You report "
    "only what is visibly present, you count decorations one by one, and you state "
    "left/right and top/bottom from the viewer's perspective of the flat plan image. "
    "An identity file is a manufacturing contract: a wrong direction or count is "
    "reproduced faithfully by the image model and ruins the product."
)

#: Written against observed failures, not in the abstract. On the first real style
#: the model reversed the wave direction on nail-01 and nail-03 and undercounted
#: nail-03's rhinestones; the image model then executed those errors exactly, which
#: is indistinguishable from a generation fault. Hence the explicit per-cell
#: addressing, the count-out-loud rule, and the mirror warning.
IDENTITY_USER = """This image is a 2x5 plan grid of ten press-on nails, laid flat.

Address the cells by POSITION, not by reading order intuition:
  TOP row,    left to right = nail-01, nail-02, nail-03, nail-04, nail-05
  BOTTOM row, left to right = nail-06, nail-07, nail-08, nail-09, nail-10

Work one cell at a time. For each cell, before writing its line, do this silently:
1. Locate the cell by its row and column position above.
2. Name the base: colour, material and finish (e.g. "deep emerald green shimmery
   glossy", "brushed champagne gold matte-metallic").
3. Name the dominant motif, if any, and where it sits (centre / upper third / along
   one edge). Common motifs here: oval bezel-set cabochon, cross, four-petal cross,
   eight-pointed star charm, organic wave metal accent.
4. COUNT each kind of small decoration individually — spherical studs, round
   rhinestones, star studs. Count them, do not estimate. If you cannot resolve an
   exact count, write "several" rather than inventing a number.
5. For anything with a direction (waves, half-and-half colour splits, off-centre
   placement), state which EDGE it runs along or which SIDE is which, as seen in
   this flat image: left, right, top, bottom. Do not mirror it. A wave along the
   right edge must never be described as along the left.

Then output the identity file in EXACTLY this format, one line per nail:

- nail-01: <base colour+material+finish>, <motif and placement>, <counted
  decorations with their placement>. Shape hint: <length + silhouette>.
- nail-02: ...
(continue through nail-10)

Rules:
- Describe only what is visible in that cell. Never copy a neighbour's features.
- Near-twins are common in these sets and are the easiest thing to get wrong. When
  two nails are near-identical, say "Near twin to nail-XX" and state the ONE
  distinguishing detail (usually a mirrored accent or a different count).
- Never use vague style words ("elegant", "luxurious") — only concrete features.
- End with one line starting "SET-WIDE:" locking the shared palette and materials.
Output only the identity text, no preamble."""

STYLE_SYSTEM = (
    "You are a senior press-on-nail merchandiser. You convert a nail design photo "
    "into a structured product spec for an e-commerce catalog. Answer with JSON only."
)

STYLE_USER = """Analyze this nail design image and output ONE JSON object with these keys:
{"name": "<short product name in English, max 6 words>",
 "description": "<one-paragraph English description of the design>",
 "base_colors": ["..."], "accent_colors": ["..."],
 "elements": ["<visible motifs/decorations>"], "texture": ["<finishes>"],
 "shape": "<one of: almond|coffin|square|tapered-square|squoval|oval|round|stiletto>",
 "length": "<one of: short|medium|long|extra-long>",
 "visual_style": "<max 12 words>", "avoid": [],
 "notes": "<anything crucial for faithful reproduction, max 60 words>"}
Use only what is visible. JSON only, no commentary."""


AUTO_QA_SYSTEM = (
    "You are the final quality gate for press-on-nail product images. You compare a "
    "generated image against its design authority and output a strict JSON verdict."
)

#: What each output type is SUPPOSED to look like. Without this the judge invented
#: its own contract: it failed matrix cells for being "a hand photo, not a
#: matrix_cell image", when a worn hand photo is exactly what a cell is. An enum
#: name is not a specification — the judge cannot infer one from the string.
_LAYOUT_CONTRACT = {
    "grid": (
        "a flat 2-row x 5-column product grid of exactly 10 press-on nails on a plain "
        "background. NO hands, fingers, or skin may appear."
    ),
    "hero": (
        "a styled marketing photo of hands wearing the set. Hands ARE expected. Judge "
        "the manicure, not the presence of hands."
    ),
    "matrix_cell": (
        "a photo of REAL HANDS wearing the set, reproducing a specific base hand pose. "
        "Hands ARE expected and required — a hand photo is correct, not a defect. This is "
        "NOT a grid: never require a 2x5 layout, and never ask for nails to be detached "
        "or laid flat."
    ),
}

AUTO_QA_USER = """Image 1 is the GENERATED candidate.
Image 2 (if present) is the design authority (plan/reference).
Image 3 (if present) is the immutable base hand photo the candidate had to reproduce.
Design identity (authoritative when present):
{identity}

The candidate is a {output_type} image, which must be: {layout_contract}

{count_rule}

Check the candidate strictly: every visible nail matches its identity (colors, motif
counts, placement); no swapped, duplicated, omitted, or invented designs; no text or
watermarks; sound hand anatomy if hands are shown.
When Image 3 is present, also verify the candidate did not distort it: hand and finger
proportions, pose, crop, and skin tone must match Image 3. Report stretched, squashed,
or elongated hands/fingers as a defect.

Output ONE JSON object only:
{{"passed": true|false,
  "issues": ["<each concrete defect, naming the nail slot>"],
  "correction": "<if failed: the exact correction instruction for a locked-base local edit.
Name ONLY the wrong slots, give set-wide motif COUNT LOCKS
(e.g. 'exactly two gothic opals in the whole image'),
and end with which nails must stay completely untouched. Empty string if passed.>"}}"""


def auto_qa_verdict(chat: ChatFn, images: list[Path], identity: str, output_type: str,
                    *, visible_nails: list[str] | None = None) -> dict:
    """Advisory vision verdict.

    `visible_nails` is the set of nail ids this view can physically show. It must be
    passed for matrix cells: p3/p5 show one hand (5 nails), so judging every view
    against 10 fails the single-hand views on anatomy the contract already dictates.
    """
    if visible_nails:
        count_rule = (
            f"This view shows exactly {len(visible_nails)} nails: "
            f"{', '.join(visible_nails)}. Judge ONLY these. Requiring any other count "
            "is wrong — nails not in this list are legitimately out of frame, which is "
            "not a defect."
        )
    else:
        count_rule = "Exactly 10 nails (nail-01..nail-10) must be present and correct."
    reply = chat(AUTO_QA_SYSTEM,
                 AUTO_QA_USER.format(identity=identity or "(none — judge by coherence)",
                                     output_type=output_type,
                                     layout_contract=_LAYOUT_CONTRACT.get(
                                         output_type, "judged by internal coherence"),
                                     count_rule=count_rule),
                 images)
    doc = extract_json(reply)
    return {
        "passed": bool(doc.get("passed")),
        "issues": [str(item) for item in doc.get("issues", []) if str(item).strip()][:20],
        "correction": str(doc.get("correction") or "").strip()[:2000],
    }


#: One cell at a time: the model sees a single nail with no neighbours to blend in
#: and no grid to lose its place in. Whole-plan reading confused near-twins and
#: mirrored directions; a lone cell removes both failure modes at the cost of ten
#: cheap text calls.
IDENTITY_CELL_USER = """This is ONE press-on nail from a ten-nail set, shown flat and
enlarged. It is {nail_id}.

Describe only this nail, in one line, in this exact format:

- {nail_id}: <base colour + material + finish>, <dominant motif and where it sits>,
  <each kind of small decoration with an exact count and its placement>. Shape hint:
  <length + silhouette>.

Requirements:
- COUNT decorations individually (studs, rhinestones, star charms). Do not estimate.
  If a count is genuinely unresolvable, write "several" rather than guessing.
- For anything directional — a wave accent, a half-and-half colour split, an
  off-centre motif — state the EDGE or SIDE as seen in this image: left, right, top,
  bottom. Do not mirror.
- Concrete features only; no style adjectives.
Output only that single line."""


def identify_nail_identities(chat: ChatFn, image: Path) -> str:
    """Whole-plan identity read: one call, ten nails.

    Cheap and usually right on distinctive sets, but it confuses near-twins and has
    mirrored directional accents in practice. `identify_nail_identities_per_cell`
    trades ten calls for far better accuracy; prefer it when the set has twins.
    """
    reply = chat(IDENTITY_SYSTEM, IDENTITY_USER, [image]).strip()
    if "nail-01" not in reply or "nail-10" not in reply:
        raise ValueError("LLM identity reply is missing nail-01..nail-10 lines")
    return reply


def identify_nail_identities_per_cell(chat: ChatFn, image: Path, *,
                                      crop_cell) -> str:
    """Identity read one cell at a time, then assembled.

    `crop_cell(nail_id) -> Path` supplies the enlarged single-nail crop. Injected
    rather than imported so this module stays free of image handling and the caller
    controls where temporary files live.

    A failed cell becomes an explicit "(not resolved)" line instead of being
    dropped: a missing nail in an identity file reads as "this nail has no design",
    which is a different and more damaging claim than "we could not read it".
    """
    lines = []
    for index in range(1, 11):
        nail_id = f"nail-{index:02d}"
        try:
            cell = crop_cell(nail_id)
            reply = chat(IDENTITY_SYSTEM,
                         IDENTITY_CELL_USER.format(nail_id=nail_id), [cell]).strip()
        except (RuntimeError, ValueError) as exc:
            logger.warning("identity cell %s failed: %s", nail_id, str(exc)[:160])
            lines.append(f"- {nail_id}: (not resolved — describe this nail manually)")
            continue
        line = next((raw.strip() for raw in reply.splitlines()
                     if nail_id in raw and raw.strip()), "")
        if not line:
            line = f"- {nail_id}: {reply.splitlines()[0].strip()}" if reply else \
                   f"- {nail_id}: (not resolved — describe this nail manually)"
        if not line.startswith("-"):
            line = f"- {line}"
        lines.append(line)
    return "\n".join(lines)


def style_fields_from_image(chat: ChatFn, image: Path) -> dict:
    doc = extract_json(chat(STYLE_SYSTEM, STYLE_USER, [image]))
    fields: dict = {}
    for key in ("name", "description", "visual_style", "notes", "shape", "length"):
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()
    for key in ("base_colors", "accent_colors", "elements", "texture", "avoid"):
        value = doc.get(key)
        if isinstance(value, list):
            fields[key] = [str(item) for item in value if str(item).strip()][:10]
    return fields
