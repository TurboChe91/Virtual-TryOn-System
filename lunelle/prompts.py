"""Prompt generation for the two output types of one style.

All prompt engineering lives here and nowhere else. The rules encode what the
predecessor Lunelle projects proved out over hundreds of real generations:

- one cohesive set identity, described by visible features (colors, motifs with
  counts, finishes), never by vague style labels;
- explicit layout contracts (rows/columns for the grid, finger count for the
  wearing shot) plus "do not swap / duplicate / omit / homogenize / invent";
- scene lock for worn shots: cream draped-fabric background, soft studio light,
  natural press-on attachment with cuticle shadows and glossy curved highlights;
- hard negatives: no text/labels/watermarks, no extra fingers, no flat stickers.

PROMPT_VERSION must be bumped whenever the wording below changes, so stored
tasks stay reproducible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources

from .models import OUTPUT_GRID, OUTPUT_HERO, OUTPUT_WEARING
from .schemas import StyleSpec

# pv-2: dropped the quoted style name from the identity block — the model
#        occasionally typeset it as a title inside the image (no-text violation).
# pv-3: reference blocks extracted as removable constants (text-only fallback
#        must not claim an Image 1 exists); grid prompt gains an optional
#        uploaded-style-reference block.
PROMPT_VERSION = "pv-3"

SKIN_TONE_PHRASES = {
    "light": "light skin tone",
    "medium": "medium brown skin tone",
    "tan": "warm tan skin tone",
    "deep": "deep brown skin tone",
}

BASE_NEGATIVE = (
    "text, letters, numbers, labels, boxes, grid lines, logo, watermark, signature, "
    "extra fingers, six fingers, missing fingers, fused fingers, deformed hand, "
    "broken anatomy, distorted knuckles, second pair of hands, "
    "cartoon, illustration, anime, painting, 3d render look, plastic skin, "
    "flat stickers, printed decals, blurry, lowres, jpeg artifacts"
)


@dataclass(frozen=True)
class PromptBundle:
    grid_prompt: str
    wearing_prompt: str
    hero_prompt: str
    negative_prompt: str
    quality_requirements: dict
    prompt_version: str


def _join(items: list[str]) -> str:
    return ", ".join(items)


def build_identity_block(spec: StyleSpec) -> str:
    """The set identity — the single source of design truth in both prompts."""
    # Never mention the style's display name here: quoted names get typeset
    # into the image as a title (observed with Seedream 4.5, task tk_237dd10fd4).
    lines = [
        f"Base colors: {_join(spec.base_colors)}.",
    ]
    if spec.accent_colors:
        lines.append(f"Accent colors: {_join(spec.accent_colors)}.")
    if spec.elements:
        lines.append(
            f"Design elements across the set: {_join(spec.elements)}. "
            "Distribute them tastefully across the ten nails as one cohesive design; "
            "not every nail needs every element."
        )
    else:
        lines.append("Clean solid-color design with no extra decorations.")
    lines.append(f"Finish: {_join(spec.texture)}.")
    lines.append(
        f"Every nail is {spec.length} length, {spec.shape} shape — one uniform shape and "
        "length for the whole set."
    )
    lines.append(f"Overall look: {spec.visual_style}.")
    if spec.notes:
        lines.append(f"Extra requirements: {spec.notes}")
    if spec.avoid:
        lines.append(
            f"STRICTLY FORBIDDEN in this design: {_join(spec.avoid)}. Never include them."
        )
    lines.append(
        "Closed palette: use only the colors listed above plus neutral shadows; "
        "do not introduce new colors, motifs, or decorations."
    )
    return "\n".join(lines)


WEARING_REFERENCE_BLOCK = """INPUT AUTHORITY:
- Image 1 is the product design plan for this press-on nail set. It is the ONLY authority for nail-art colors, motifs, decorations, finish, and nail shape/length. Its grid layout, spacing, and studio background are packaging only — NEVER reproduce the grid layout, floating nails, or plain background in the output; the nails must appear naturally worn on fingers.

"""

GRID_REFERENCE_BLOCK = """INPUT AUTHORITY:
- Image 1 is a customer-supplied style reference photo. It is the ONLY authority for nail-art colors, motifs, decorations, and finish — reproduce that design faithfully across the ten nails. Ignore its background, layout, hands, and any text; never render them.

"""


def strip_reference_block(prompt: str) -> str:
    """Remove the Image-1 authority block for text-only sends (no reference available)."""
    stripped = prompt.replace(WEARING_REFERENCE_BLOCK, "").replace(GRID_REFERENCE_BLOCK, "")
    return stripped.replace(HERO_REFERENCE_BLOCK, "")


def build_grid_prompt(spec: StyleSpec, with_reference: bool = False) -> str:
    reference_block = GRID_REFERENCE_BLOCK if with_reference else ""
    return f"""{reference_block}Professional e-commerce product photograph of one complete press-on nail set: exactly 10 false nails, arranged in a strict grid of 2 rows and 5 columns on a seamless soft cream studio background.

LAYOUT CONTRACT:
- exactly 10 nails total: 5 in the top row, 5 in the bottom row;
- evenly spaced, generous margins, no nail overlapping or touching another, none cropped by the frame;
- every nail upright with the free edge pointing up and the cuticle end pointing down, photographed straight-on from above at identical scale;
- square 1:1 canvas.

SET IDENTITY (all 10 nails belong to ONE cohesive set — do not swap, duplicate, omit, homogenize into plain blanks, or invent extra designs):
{build_identity_block(spec)}

Material realism: dimensional decorations with true depth — pearls round, crystals faceted, metal pieces reflective, with tiny contact shadows; glossy curved highlights along each nail's curvature; crisp macro focus.

No hands, no fingers, no skin, no body parts anywhere in the image.
No text, letters, numbers, labels, boxes, grid lines, logos, or watermarks."""


def build_wearing_prompt(spec: StyleSpec, with_reference: bool) -> str:
    reference_block = WEARING_REFERENCE_BLOCK if with_reference else ""
    tone = SKIN_TONE_PHRASES[spec.skin_tone]
    return f"""Photorealistic Shopify listing photo of a real human hand wearing a press-on nail set.

{reference_block}SCENE LOCK:
- one elegant adult hand, {tone}, natural anatomy, exactly five fingers with exactly one nail per finger, full wrist visible inside the frame;
- fingers gently curled toward the camera so all five nail faces are clearly readable;
- background: matte cream draped-fabric curtain with soft vertical folds, no props, no clutter;
- lighting: soft diffused professional studio light, natural skin texture, gentle contact shadows, no hard flash, no plastic over-smoothing.

NAIL SET IDENTITY (the five visible nails wear this exact design — do not swap, duplicate, omit, homogenize, simplify, recolor, or invent):
{build_identity_block(spec)}

Natural press-on attachment: each nail sits on the real nail bed with subtle cuticle shadow, curved glossy highlight following the nail's arc, and correct perspective along each finger's axis. Preserve true material depth: pearls round, crystals faceted, metal reflective, raised elements casting tiny shadows — never flat stickers or printed decals.

No text, letters, numbers, labels, boxes, logos, or watermarks."""


# ---- hero (contract-locked Shopify listing shot) ---------------------------
#
# Ported from the predecessor pipeline's run_imagegen_direct.py: the ten-slot
# screen-order contract proved to be the single most important defence against
# slot-binding errors (swapped/duplicated nail identities). The contract JSON
# is versioned by content hash inside the task's prompt_version.

_HERO_CONTRACT_TEXT = (
    resources.files("lunelle").joinpath("contracts/hero_pose_contract.json").read_text("utf-8")
)
HERO_CONTRACT = json.loads(_HERO_CONTRACT_TEXT)
HERO_CONTRACT_SHA = hashlib.sha256(_HERO_CONTRACT_TEXT.encode()).hexdigest()[:8]
HERO_PROMPT_VERSION = f"hv-1+{HERO_CONTRACT_SHA}"

HERO_REFERENCE_BLOCK = """INPUT AUTHORITY — do not mix these roles:
- Image 1 is the nail set plan. It is the ONLY authority for nail-art colors, textures, motifs, exact counts, 3D decorations, and each individual nail's length and silhouette. Its layout, spacing, labels, and background are packaging only; never render them.
- Image 2 is the photography reference. It is the ONLY authority for hand pose, hand geometry, background, crop, lighting, and skin appearance. NEVER copy its manicure design, nail length, or nail silhouette.

"""


def _hero_view_contract() -> str:
    slots = HERO_CONTRACT["screen_slots"]
    return "\n".join(
        [
            f"Pose: {HERO_CONTRACT['pose']}",
            "LEFT-side hand upper fingers, absolute SCREEN LEFT-to-RIGHT = "
            + ", ".join(slots["left_upper_fingers_left_to_right"]) + ".",
            f"LEFT thumb = {slots['left_thumb']['nail_id']} at {slots['left_thumb']['position']}.",
            "RIGHT-side hand upper fingers, absolute SCREEN LEFT-to-RIGHT = "
            + ", ".join(slots["right_upper_fingers_left_to_right"]) + ".",
            f"RIGHT thumb = {slots['right_thumb']['nail_id']} at {slots['right_thumb']['position']}.",
            "Do not infer order from anatomy. Do not mirror or reverse either hand.",
            f"Background: {HERO_CONTRACT['background']}",
            f"Lighting: {HERO_CONTRACT['lighting']}",
            f"Framing: {HERO_CONTRACT['framing']}",
        ]
    )


def build_hero_prompt(spec: StyleSpec, identity_text: str | None, with_reference: bool) -> str:
    """Two-hand Shopify listing hero. identity_text (per-nail identities, the
    predecessor identity.txt format) beats the set-level spec block when given."""
    reference_block = HERO_REFERENCE_BLOCK if with_reference else ""
    identity_block = (identity_text or "").strip() or build_identity_block(spec)
    return f"""{reference_block}Photorealistic Shopify listing hero for a press-on nail set: two complete hands wearing the full ten-nail set.

HIGHEST-PRIORITY VIEW CONTRACT:
{_hero_view_contract()}

STYLE IDENTITY ONLY — nail art plus per-nail shape; never pose, screen order, or scene:
{identity_block}

Hard constraints:
- Use every visible nail identity exactly once in the contract slots; do not swap, duplicate, omit, homogenize, or invent.
- Nail length and silhouette follow the plan per nail; never homogenize the ten nails into one shared shape.
- Preserve true material depth: natural press-on attachment, cuticle shadows, glossy curved highlights, and dimensional metal/gems/pearls where the style calls for them.
- Keep complete hands and wrists inside the frame with a generous cream-curtain margin.
- No text, labels, boxes, grid lines, logos, or watermarks in the output."""


# ---- try-on matrix (4 tones x 4 views per style) ----------------------------
#
# Ported from the predecessor run_imagegen_matrix.py. Image 1 = design authority
# (plan upload or the latest grid), Image 2 = the configured hand model for the
# cell's skin tone (pose/skin/background authority). Views and their scene
# contracts come from contracts/matrix_views.json (content-hashed into the
# prompt version).

_MATRIX_CONTRACT_TEXT = (
    resources.files("lunelle").joinpath("contracts/matrix_views.json").read_text("utf-8")
)
MATRIX_CONTRACT = json.loads(_MATRIX_CONTRACT_TEXT)
MATRIX_TONES = tuple(MATRIX_CONTRACT["tones"])
MATRIX_VIEWS = tuple(MATRIX_CONTRACT["views"])
MATRIX_PROMPT_VERSION = (
    "mx-1+" + hashlib.sha256(_MATRIX_CONTRACT_TEXT.encode()).hexdigest()[:8]
)

MATRIX_REFERENCE_BLOCK = """INPUT AUTHORITY — do not mix these roles:
- Image 1 is the design authority for this press-on nail set. It is the ONLY authority for nail-art colors, motifs, decorations, finish, and per-nail length and silhouette. Its layout and background are packaging only; never reproduce them.
- Image 2 is the immutable base hand photo. It is authority only for hand pose, hand geometry, crop, skin tone, background, and lighting. Never copy its manicure, nail length, or nail silhouette.

"""


def build_matrix_prompt(spec: StyleSpec, identity_text: str | None,
                        tone: str, view: str, with_reference: bool) -> str:
    view_doc = MATRIX_CONTRACT["views"][view]
    identity_block = (identity_text or "").strip() or build_identity_block(spec)
    reference_block = MATRIX_REFERENCE_BLOCK if with_reference else ""
    return f"""{reference_block}Photorealistic virtual nail try-on photo for e-commerce.

{view_doc["scene"]}
Skin: {MATRIX_CONTRACT["tones"][tone]}. Background and lighting must match the base hand photo.

Visible nails this render: {", ".join(view_doc["visible_nails"])}
{view_doc["mapping_note"]}

NAIL SET IDENTITY (the visible nails wear this exact design — do not swap, duplicate, omit, homogenize, simplify, recolor, or invent):
{identity_block}

Hard constraints:
- Apply only the listed nail designs; nail length and silhouette come from the design authority per nail.
- Natural press-on attachment: cuticle shadows, glossy topcoat, curved highlights; true material depth for pearls, crystals, and metal.
- Preserve the exact base-photo hand count and view scope; never add another hand to a single-hand view.
- No text, labels, boxes, arrows, grid lines, logos, or watermarks."""


# ---- correction (v2+ = locked-base local edit, never a free re-roll) --------
#
# Protocol proven on style-wechat-35-9 (PASS in 3 versions): narrow the scope
# explicitly, attach the previous candidate as a must-preserve edit base,
# optionally attach single-nail detail references, and let the correction text
# carry set-wide COUNT LOCKS ("exactly two gothic opals in the whole image") —
# count locks are what stop collateral damage on untouched nails.

def build_correction_prompt(base_prompt: str, correction_text: str, detail_count: int) -> str:
    detail_note = (
        f" After it come {detail_count} enlarged single-nail detail reference(s); "
        "use each ONLY for its explicitly named correction slot."
        if detail_count else ""
    )
    header = f"""HIGHEST-PRIORITY ATTEMPT CORRECTION — this is a LOCAL EDIT, not a new generation:
{correction_text.strip()}

EDIT BASE: the image attached after the authority image(s) is the PREVIOUS CANDIDATE. It is the edit base that must be preserved: keep its photography, lighting, hands, and every nail that the correction does not explicitly name, pixel-faithful.{detail_note}
Apply ONLY the correction above. Do not re-style, re-pose, re-light, or improve anything else.

"""
    return header + base_prompt


def build_negative_prompt(spec: StyleSpec) -> str:
    if spec.avoid:
        return BASE_NEGATIVE + ", " + ", ".join(item.lower() for item in spec.avoid)
    return BASE_NEGATIVE


def build_quality_requirements(spec: StyleSpec, grid_size: tuple[int, int], wearing_size: tuple[int, int]) -> dict:
    return {
        "grid": {
            "aspect_ratio": "1:1",
            "expected_size": f"{grid_size[0]}x{grid_size[1]}",
            "nail_count": 10,
            "rows": 2,
            "columns": 5,
            "no_hands": True,
            "no_text_or_watermark": True,
        },
        "wearing": {
            "expected_size": f"{wearing_size[0]}x{wearing_size[1]}",
            "visible_nails": 5,
            "finger_count": 5,
            "natural_anatomy": True,
            "no_text_or_watermark": True,
            "style_consistent_with_grid": True,
        },
        "shared": {
            "shape": spec.shape,
            "length": spec.length,
            "base_colors": spec.base_colors,
            "avoid": spec.avoid,
        },
    }


def build_prompt_bundle(
    spec: StyleSpec,
    grid_size: tuple[int, int],
    wearing_size: tuple[int, int],
    with_reference: bool = True,
    with_grid_reference: bool = False,
    identity_text: str | None = None,
    with_hero_reference: bool = True,
) -> PromptBundle:
    return PromptBundle(
        grid_prompt=build_grid_prompt(spec, with_reference=with_grid_reference),
        wearing_prompt=build_wearing_prompt(spec, with_reference=with_reference),
        hero_prompt=build_hero_prompt(spec, identity_text, with_reference=with_hero_reference),
        negative_prompt=build_negative_prompt(spec),
        quality_requirements=build_quality_requirements(spec, grid_size, wearing_size),
        prompt_version=PROMPT_VERSION,
    )


def prompt_for_output_type(bundle: PromptBundle, output_type: str) -> str:
    if output_type == OUTPUT_GRID:
        return bundle.grid_prompt
    if output_type == OUTPUT_WEARING:
        return bundle.wearing_prompt
    if output_type == OUTPUT_HERO:
        return bundle.hero_prompt
    raise ValueError(f"unknown output type {output_type!r}")


def prompt_version_for(output_type: str) -> str:
    """Hero carries its own version (wording + contract hash)."""
    return HERO_PROMPT_VERSION if output_type == OUTPUT_HERO else PROMPT_VERSION
