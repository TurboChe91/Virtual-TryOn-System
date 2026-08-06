#!/usr/bin/env python
"""Does a colour position-map fix nail order, without bleeding into the output?

Current mechanism is one sentence of prose -- "LEFT upper SCREEN LEFT-to-RIGHT =
nail-05, nail-04, ..." -- which asks the model to work out handedness and count
fingers. It gets it wrong.

The colour map replaces that with a visual anchor: the cyan nail is nail-06, so
there is nothing to count. The risk is the opposite failure -- the model treats
ten saturated colour patches as the design and returns a rainbow hand.

Arms A and B differ ONLY by whether the colour map is attached, so any difference
in order accuracy is attributable to it. Cost: one call per arm.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image

from lunelle.config import load_config
from lunelle.nailslots import NAIL_ANATOMY, NAIL_COLORS

ANNOTATION_DIR = Path(
    os.environ.get("LUNELLE_ANNOTATION_DIR", "/Users/turboche/Desktop/标注")
)
OUT = Path(tempfile.gettempdir()) / "order_probe"

#: Human-readable colour names: the model reads the prompt as text, so "cyan"
#: anchors better than "(0,255,255)".
COLOR_NAMES = {
    "nail-01": "RED", "nail-02": "GREEN", "nail-03": "BLUE",
    "nail-04": "YELLOW", "nail-05": "MAGENTA", "nail-06": "CYAN",
    "nail-07": "ORANGE", "nail-08": "PURPLE", "nail-09": "PINK",
    "nail-10": "SPRING GREEN",
}


def channel() -> tuple[str, str, str]:
    config = load_config()
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT base_url, api_key, model FROM api_profiles"
        " WHERE is_active = 1 AND kind = 'image' LIMIT 1"
    ).fetchone()
    if row is None:
        return config.image_api_base_url, config.image_api_key, config.image_model
    p = dict(row)
    return p["base_url"].rstrip("/"), p["api_key"], p["model"]


def style_row(sku_like: str) -> dict:
    config = load_config()
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT style_id, sku, plan_image_path, identity_text FROM styles"
        " WHERE sku LIKE ? LIMIT 1", (sku_like,)).fetchone()
    if row is None:
        raise SystemExit(f"no style matching {sku_like!r}")
    return dict(row)


def hand_model(tone: str, view: str) -> Path:
    config = load_config()
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?",
                       (f"hand_model_{tone}_{view}",)).fetchone()
    if row is None:
        raise SystemExit(f"no hand model for {tone}/{view}")
    return Path(dict(row)["value"])


def as_png(path: Path) -> bytes:
    with Image.open(path) as image:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


def colour_legend(visible: list[str]) -> str:
    lines = []
    for nail_id in visible:
        hand, finger = NAIL_ANATOMY[nail_id]
        lines.append(f"  {COLOR_NAMES[nail_id]:13} = {nail_id} ({hand} {finger})")
    return "\n".join(lines)


BASE_PROMPT = """Photorealistic virtual nail try-on photo for e-commerce.

INPUT AUTHORITY — do not mix these roles:
- Image 1 is the design authority: a 2x5 plan of the ten press-on nails. It is the
  ONLY authority for nail-art colours, motifs, decorations, finish, and each nail's
  length and silhouette. Never reproduce its grid layout or background.
- Image 2 is the immutable base hand photo. It is authority only for hand pose,
  hand geometry, crop, skin tone, background and lighting. Never copy its manicure.
{map_role}
Both hands shown from the BACKS of the hands, fingers open and gently fanned exactly
like Image 2; all ten fingernails clearly visible, thumbs angled inward near the
lower centre, soft cream fabric background and soft studio lighting. Match Image 2
hand pose, geometry, skin tone, crop and lighting exactly.

{placement}

NAIL SET IDENTITY (each nail wears this exact design — do not swap, duplicate, omit,
homogenise, simplify, recolour, or invent):
{identity}

Hard constraints:
- Apply only the listed nail designs; length and silhouette come from Image 1 per nail.
- Natural press-on attachment: cuticle shadows, glossy topcoat, curved highlights.
- Preserve the base photo's true hand and finger proportions. Never stretch or
  elongate the hand to fill the frame.
- No text, labels, boxes, arrows, grid lines, logos, or watermarks."""

PROSE_PLACEMENT = """PLACEMENT (screen order):
Authoritative labelled-image mapping: LEFT upper SCREEN LEFT-to-RIGHT = nail-05,
nail-04, nail-03, nail-02 with nail-01 the lower-centre-left thumb; RIGHT upper
SCREEN LEFT-to-RIGHT = nail-07, nail-08, nail-09, nail-10 with nail-06 the
lower-centre-right thumb. Do not infer or mirror."""

MAP_PLACEMENT = """PLACEMENT (read it off Image 3, do not count fingers):
Image 3 is the SAME hand photo as Image 2 with each nail painted a flat identifier
colour. Each colour marks WHICH nail design belongs on that finger:

{legend}

Put each nail's design on the finger whose Image 3 patch carries its colour."""

MAP_ROLE = """- Image 3 is a POSITION MAP ONLY. Its flat colours are identifiers, not design.
  NEVER render these identifier colours, and never let them tint the manicure. The
  output must show ONLY the Image 1 nail art on a hand identical to Image 2.
"""


def call(base_url, key, model, *, prompt: str, images: list[bytes],
         size: str, timeout: int = 900):
    files = [("image", (f"img{i}.png", data, "image/png"))
             for i, data in enumerate(images)]
    data = {"model": model, "prompt": prompt, "n": "1", "size": size,
            "quality": "high", "response_format": "b64_json",
            "input_fidelity": "high"}
    try:
        r = httpx.post(f"{base_url}/images/edits",
                       headers={"Authorization": f"Bearer {key}"},
                       files=files, data=data, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    item = r.json().get("data", [{}])[0]
    if item.get("b64_json"):
        return Image.open(io.BytesIO(base64.b64decode(item["b64_json"]))), None
    if item.get("url"):
        got = httpx.get(item["url"], timeout=300)
        return Image.open(io.BytesIO(got.content)), None
    return None, f"no image in response: {list(item)}"


def colour_bleed(image: Image.Image) -> dict:
    """How much of the output is close to an identifier colour.

    A rainbow hand is the failure mode this arm risks, and it needs to be measured
    rather than eyeballed.
    """
    rgb = image.convert("RGB")
    small = rgb.resize((rgb.width // 3, rgb.height // 3))
    pixels = small.load()
    w, h = small.size
    hits: dict[str, int] = {}
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y]
            if max(r, g, b) < 90 or max(r, g, b) - min(r, g, b) < 110:
                continue  # not a vivid flat colour
            for nail_id, (cr, cg, cb) in NAIL_COLORS.items():
                if abs(r - cr) < 60 and abs(g - cg) < 60 and abs(b - cb) < 60:
                    hits[nail_id] = hits.get(nail_id, 0) + 1
                    break
    total = w * h
    return {"total_px": sum(hits.values()),
            "pct": round(sum(hits.values()) / total * 100, 3),
            "by_colour": dict(sorted(hits.items(), key=lambda kv: -kv[1])[:5])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sku", default="nail-emerald%")
    parser.add_argument("--tone", default="light")
    parser.add_argument("--view", default="p2_open_hands")
    parser.add_argument("--arms", default="AB", help="A=prose only, B=colour map")
    parser.add_argument("--size", default="1536x1024")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    base_url, key, model = channel()
    style = style_row(args.sku)
    print(f"channel : {base_url} / {model}")
    print(f"style   : {style['sku']}")
    plan = Path(style["plan_image_path"] or "")
    if not plan.is_file():
        raise SystemExit("style has no plan image")
    identity = (style["identity_text"] or "").strip()
    if not identity:
        raise SystemExit("style has no identity text; order cannot be judged")
    print(f"plan    : {plan.name}   identity: {len(identity)} chars")

    hand = hand_model(args.tone, args.view)
    annotation = ANNOTATION_DIR / args.tone / f"{args.view.split('_')[0]}.png"
    visible = [n for n in NAIL_COLORS if n in identity or True][:10]
    print(f"hand    : {hand.name[:20]}...   map: {annotation.name}\n")

    plan_png, hand_png = as_png(plan), as_png(hand)

    if "A" in args.arms:
        print("=== ARM A: prose screen-order only (current production behaviour) ===")
        prompt = BASE_PROMPT.format(map_role="", placement=PROSE_PLACEMENT,
                                    identity=identity)
        image, err = call(base_url, key, model, prompt=prompt,
                          images=[plan_png, hand_png], size=args.size)
        if image is None:
            print(f"  FAILED: {err}\n")
        else:
            path = OUT / "A-prose-only.png"
            image.save(path)
            print(f"  ok {image.size} -> {path}")
            print(f"  colour bleed: {colour_bleed(image)}\n")

    if "B" in args.arms:
        print("=== ARM B: colour position map as Image 3 ===")
        prompt = BASE_PROMPT.format(
            map_role=MAP_ROLE,
            placement=MAP_PLACEMENT.format(legend=colour_legend(visible)),
            identity=identity)
        (OUT / "B-prompt.txt").write_text(prompt)
        image, err = call(base_url, key, model, prompt=prompt,
                          images=[plan_png, hand_png, as_png(annotation)],
                          size=args.size)
        if image is None:
            print(f"  FAILED: {err}\n")
        else:
            path = OUT / "B-colour-map.png"
            image.save(path)
            bleed = colour_bleed(image)
            print(f"  ok {image.size} -> {path}")
            print(f"  colour bleed: {bleed}")
            verdict = ("CLEAN — identifier colours did not reach the output"
                       if bleed["pct"] < 0.15 else
                       "BLED — identifier colours appear in the output")
            print(f"  -> {verdict}\n")

    print(f"artifacts in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
