#!/usr/bin/env python
"""Probe per-nail (masked) editing before building it into the worker.

Two things have to hold for a 10-step chain to be viable, and neither is obvious
from a single-call test:

1. Does each step actually repaint the target nail and leave the others alone?
   Position stops being a counting problem only if the mask really constrains
   where the edit lands.

2. Does quality survive re-encoding ten times? Every step feeds the previous
   output back in, so any per-step degradation compounds. This is the risk that
   does not exist in single-shot generation, and the reason to measure a chain
   rather than one call.

Also measured: does cropping the plan to just this nail help fidelity? A full 2x5
plan asks the model to find the right cell AND copy it; a single-nail crop removes
the first job.

Run with --steps to limit the chain while iterating.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image

from lunelle.config import load_config
from lunelle.nailslots import NAIL_ANATOMY

OUT = Path(tempfile.gettempdir()) / "per_nail_probe"
ANNOTATION_DIR = Path(
    os.environ.get("LUNELLE_ANNOTATION_DIR", "/Users/turboche/Desktop/标注")
)

#: Where each nail sits in the 2x5 plan: top row is nail-01..05 left to right,
#: bottom row nail-06..10. This is the plan's own grid order, NOT screen order on
#: a hand -- the two are different things and conflating them is how nail order
#: went wrong in the first place.
PLAN_GRID = {f"nail-{i:02d}": (i - 1) % 5 for i in range(1, 11)}
PLAN_ROW = {f"nail-{i:02d}": 0 if i <= 5 else 1 for i in range(1, 11)}


def channel():
    config = load_config()
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT base_url, api_key, model FROM api_profiles"
        " WHERE is_active = 1 AND kind = 'image' LIMIT 1").fetchone()
    if row is None:
        return config.image_api_base_url, config.image_api_key, config.image_model
    p = dict(row)
    return p["base_url"].rstrip("/"), p["api_key"], p["model"]


def load_slots(tone: str, view: str):
    config = load_config()
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    model = conn.execute(
        "SELECT hand_model_id, base_digest, width, height FROM hand_models"
        " WHERE tone = ? AND view = ? AND retired_at IS NULL", (tone, view)).fetchone()
    if model is None:
        raise SystemExit(f"no hand model for {tone}/{view}")
    model = dict(model)
    base = conn.execute("SELECT path FROM assets WHERE digest = ?",
                        (model["base_digest"],)).fetchone()
    slots = []
    for row in conn.execute(
            "SELECT s.nail_id, s.mask_digest, s.bbox_x, s.bbox_y, s.bbox_w, s.bbox_h,"
            " a.path FROM hand_model_slots s JOIN assets a ON a.digest = s.mask_digest"
            " WHERE s.hand_model_id = ? ORDER BY s.nail_id", (model["hand_model_id"],)):
        slots.append(dict(row))
    return model, Path(dict(base)["path"]), slots


def style_row(sku_like: str):
    config = load_config()
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT sku, plan_image_path, identity_text FROM styles WHERE sku LIKE ? LIMIT 1",
        (sku_like,)).fetchone()
    if row is None:
        raise SystemExit(f"no style matching {sku_like!r}")
    return dict(row)


def identity_line(identity: str, nail_id: str) -> str:
    for line in identity.splitlines():
        if line.strip().startswith(nail_id):
            return line.split(":", 1)[1].strip()
    return ""


def crop_plan_cell(plan: Path, nail_id: str) -> bytes:
    """Just this nail's cell from the 2x5 plan, so the model has one job."""
    with Image.open(plan) as image:
        rgb = image.convert("RGB")
        w, h = rgb.size
        cell_w, cell_h = w / 5, h / 2
        col, row = PLAN_GRID[nail_id], PLAN_ROW[nail_id]
        box = (int(col * cell_w), int(row * cell_h),
               int((col + 1) * cell_w), int((row + 1) * cell_h))
        cell = rgb.crop(box)
    buffer = io.BytesIO()
    cell.save(buffer, format="PNG")
    return buffer.getvalue()


def as_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def mask_png_resized(mask_path: Path, size: tuple[int, int]) -> bytes:
    with Image.open(mask_path) as mask:
        rgba = mask.convert("RGBA").resize(size, Image.NEAREST)
    buffer = io.BytesIO()
    rgba.save(buffer, format="PNG")
    return buffer.getvalue()


PROMPT = """Repaint ONLY the single fingernail inside the editable region of the mask.

That nail must show exactly this design:
{design}

The first attached image is the current photo. The second is an enlarged reference
of the exact nail art to apply. Reproduce that reference faithfully: base colour,
motif, decoration counts and placement.

Do not alter anything outside the editable region: keep the hand, fingers, skin,
every other nail, the background and the lighting pixel-identical. No text, labels,
or watermarks."""


def call_edit(base_url, key, model, *, image_png, mask_png, ref_png, prompt, size):
    files = [
        ("image", ("current.png", image_png, "image/png")),
        ("image", ("nailref.png", ref_png, "image/png")),
        ("mask", ("mask.png", mask_png, "image/png")),
    ]
    data = {"model": model, "prompt": prompt, "n": "1", "size": size,
            "quality": "high", "response_format": "b64_json",
            "input_fidelity": "high"}
    try:
        r = httpx.post(f"{base_url}/images/edits",
                       headers={"Authorization": f"Bearer {key}"},
                       files=files, data=data, timeout=900)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:240]}"
    item = r.json().get("data", [{}])[0]
    if item.get("b64_json"):
        return Image.open(io.BytesIO(base64.b64decode(item["b64_json"]))), None
    if item.get("url"):
        got = httpx.get(item["url"], timeout=300)
        return Image.open(io.BytesIO(got.content)), None
    return None, "no image in response"


def region_diff(before: Image.Image, after: Image.Image, mask_path: Path) -> dict:
    """Change inside the edited nail vs everywhere else."""
    if before.size != after.size:
        after = after.resize(before.size, Image.LANCZOS)
    with Image.open(mask_path) as m:
        mask = m.convert("RGBA").resize(before.size, Image.NEAREST)
    bp, ap, mp = before.convert("RGB").load(), after.convert("RGB").load(), mask.load()
    w, h = before.size
    ins, outs = [], []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            d = max(abs(bp[x, y][i] - ap[x, y][i]) for i in range(3))
            (ins if mp[x, y][3] == 255 else outs).append(d)
    def sm(v):
        return {"mean": round(sum(v) / len(v), 2), "max": max(v)} if v else None
    return {"inside": sm(ins), "outside": sm(outs)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sku", default="nail-emerald%")
    parser.add_argument("--tone", default="light")
    parser.add_argument("--view", default="p2_open_hands")
    parser.add_argument("--steps", type=int, default=3,
                        help="how many nails to edit (default 3 to probe cheaply)")
    parser.add_argument("--size", default="1536x1024")
    args = parser.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    base_url, key, model = channel()
    style = style_row(args.sku)
    identity = (style["identity_text"] or "").strip()
    plan = Path(style["plan_image_path"] or "")
    if not plan.is_file() or not identity:
        raise SystemExit("style needs both a plan image and identity text")

    hand_model, base_path, slots = load_slots(args.tone, args.view)
    print(f"channel: {base_url} / {model}")
    print(f"style  : {style['sku']}")
    print(f"base   : {base_path.name[:16]}... {hand_model['width']}x{hand_model['height']}")
    print(f"slots  : {len(slots)}, editing {args.steps}\n")

    send_size = tuple(int(v) for v in args.size.split("x"))
    with Image.open(base_path) as image:
        current = image.convert("RGB").resize(send_size, Image.LANCZOS)
    current.save(OUT / "step00-base.png")

    log = []
    for index, slot in enumerate(slots[:args.steps], start=1):
        nail_id = slot["nail_id"]
        hand, finger = NAIL_ANATOMY[nail_id]
        design = identity_line(identity, nail_id)
        mask_png = mask_png_resized(Path(slot["path"]), send_size)
        ref_png = crop_plan_cell(plan, nail_id)
        started = time.monotonic()
        result, err = call_edit(base_url, key, model,
                               image_png=as_png(current), mask_png=mask_png,
                               ref_png=ref_png,
                               prompt=PROMPT.format(design=design), size=args.size)
        elapsed = time.monotonic() - started
        if result is None:
            print(f"  step {index} {nail_id} ({hand} {finger}): FAILED {err}")
            log.append({"nail": nail_id, "error": err})
            continue
        result = result.convert("RGB")
        if result.size != send_size:
            result = result.resize(send_size, Image.LANCZOS)
        stats = region_diff(current, result, Path(slot["path"]))
        ratio = stats["inside"]["mean"] / max(0.01, stats["outside"]["mean"])
        print(f"  step {index} {nail_id} ({hand} {finger}) {elapsed:5.1f}s  "
              f"inside={stats['inside']['mean']:6.2f} outside={stats['outside']['mean']:5.2f} "
              f"ratio={ratio:5.1f}x")
        result.save(OUT / f"step{index:02d}-{nail_id}.png")
        log.append({"nail": nail_id, **stats, "ratio": round(ratio, 1),
                    "seconds": round(elapsed, 1)})
        current = result

    current.save(OUT / "final.png")
    print()
    outs = [e["outside"]["mean"] for e in log if e.get("outside")]
    if outs:
        print(f"outside-mask drift per step: mean {sum(outs)/len(outs):.2f}, worst {max(outs):.2f}")
        print("  (this is what compounds over a 10-step chain)")
    print(f"\nartifacts in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
