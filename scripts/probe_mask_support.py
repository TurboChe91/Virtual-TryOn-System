#!/usr/bin/env python
"""Probe whether the configured channel supports mask-based /images/edits.

This answers one architectural question with facts instead of assumption: can we
do real inpainting (lock the hand, repaint only one nail), or must the colour
annotation be used the weaker way (as a mapping picture inside the prompt)?

It derives a real mask from the annotation set, so a success here means the exact
production path works, not merely that some mask was accepted.

Mask convention (OpenAI /images/edits): the mask is a PNG with an alpha channel;
TRANSPARENT pixels are the region the model may repaint, opaque pixels must be
preserved. Image and mask must have identical dimensions.

Usage:
    python scripts/probe_mask_support.py            # dry run, writes artifacts only
    python scripts/probe_mask_support.py --live      # makes ONE paid API call
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image

from lunelle.config import load_config

#: Annotation colour -> nail id. Lives in code, not in uploaded assets: it is a
#: versioned contract, and a mismatch must be a code change, not a silent asset swap.
NAIL_COLORS = {
    "nail-01": (255, 0, 0),
    "nail-02": (0, 255, 0),
    "nail-03": (0, 0, 255),
    "nail-04": (255, 255, 0),
    "nail-05": (255, 0, 255),
    "nail-06": (0, 255, 255),
    "nail-07": (255, 128, 0),
    "nail-08": (128, 0, 255),
    "nail-09": (255, 0, 128),
    "nail-10": (0, 255, 128),
}

ANNOTATION_DIR = Path(
    os.environ.get("LUNELLE_ANNOTATION_DIR", "/Users/turboche/Desktop/标注")
)
# Scratch dir for probe artifacts; inspected by hand, never read back by code.
OUT_DIR = Path(tempfile.gettempdir()) / "mask_probe"


def derive_mask(annotation: Path, nail_id: str, size: tuple[int, int]) -> Image.Image:
    """Transparent exactly where `nail_id`'s colour is, opaque everywhere else."""
    colour = NAIL_COLORS[nail_id]
    with Image.open(annotation) as src:
        rgb = src.convert("RGB")
        if rgb.size != size:
            raise SystemExit(f"annotation {rgb.size} != base {size}")
        pixels = rgb.load()
        width, height = rgb.size
        mask = Image.new("RGBA", (width, height), (0, 0, 0, 255))
        mp = mask.load()
        hits = 0
        for y in range(height):
            for x in range(width):
                if pixels[x, y] == colour:
                    mp[x, y] = (0, 0, 0, 0)
                    hits += 1
    if hits == 0:
        raise SystemExit(f"{nail_id} colour {colour} not present in {annotation}")
    print(f"  mask for {nail_id}: {hits} transparent px ({hits / (width * height) * 100:.2f}%)")
    return mask


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="make one real paid call")
    parser.add_argument("--nail", default="nail-03")
    parser.add_argument("--tone", default="light")
    args = parser.parse_args()

    config = load_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # The clean base is what Studio already holds for this tone+view.
    import sqlite3

    conn = sqlite3.connect(config.data_dir / "lunelle.db")
    conn.row_factory = sqlite3.Row

    # Probe the channel the worker would actually use: the active profile wins over
    # .env. Probing .env while production runs a different relay would prove nothing.
    base_url, api_key, model = config.image_api_base_url, config.image_api_key, config.image_model
    profile = conn.execute(
        "SELECT name, base_url, api_key, model FROM api_profiles"
        " WHERE is_active = 1 AND kind = 'image' LIMIT 1"
    ).fetchone()
    if profile is not None:
        p = dict(profile)
        base_url, api_key, model = p["base_url"].rstrip("/"), p["api_key"], p["model"]
        print(f"using ACTIVE PROFILE {p['name']!r}: {base_url} / {model}")
    else:
        print(f"no active image profile; using .env: {base_url} / {model}")

    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (f"hand_model_{args.tone}_p2_open_hands",),
    ).fetchone()
    if row is None:
        raise SystemExit(f"no hand model for {args.tone}/p2_open_hands")
    base_path = Path(dict(row)["value"])
    with Image.open(base_path) as base:
        base_size = base.size
    print(f"base: {base_path.name} {base_size[0]}x{base_size[1]}")

    annotation = ANNOTATION_DIR / args.tone / "p2.png"
    mask = derive_mask(annotation, args.nail, base_size)
    mask_path = OUT_DIR / f"mask-{args.tone}-p2-{args.nail}.png"
    mask.save(mask_path)

    # The base must be sent as PNG with an alpha channel for edits.
    base_png = OUT_DIR / f"base-{args.tone}-p2.png"
    with Image.open(base_path) as base:
        base.convert("RGBA").save(base_png)

    print(f"artifacts: {base_png}  {mask_path}")
    print(f"endpoint: {base_url}/images/edits")
    print(f"model: {model}")
    if not args.live:
        print("\ndry run — pass --live to make one paid call")
        return 0

    prompt = (
        "Repaint ONLY the single nail inside the editable region: deep emerald green "
        "glossy base with one small gold star at its centre. Do not alter the hand, "
        "fingers, skin, background, lighting, or any other nail."
    )
    files = {
        "image": (base_png.name, base_png.read_bytes(), "image/png"),
        "mask": (mask_path.name, mask_path.read_bytes(), "image/png"),
    }
    data = {"model": model, "prompt": prompt, "n": "1"}
    print("\ncalling /images/edits with mask ...")
    try:
        response = httpx.post(
            f"{base_url}/images/edits",
            headers={"Authorization": f"Bearer {api_key}"},
            files=files,
            data=data,
            timeout=600,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"TRANSPORT ERROR {type(exc).__name__}: {str(exc)[:300]}")
        return 1

    print(f"HTTP {response.status_code}")
    if response.status_code != 200:
        print("body:", response.text[:800])
        return 1

    payload = response.json()
    item = payload.get("data", [{}])[0]
    b64 = item.get("b64_json")
    if not b64:
        print("no b64_json; keys:", list(item))
        print("body head:", response.text[:400])
        return 1
    out = OUT_DIR / f"edited-{args.tone}-p2-{args.nail}.png"
    out.write_bytes(base64.b64decode(b64))
    with Image.open(out) as edited:
        print(f"returned image: {edited.width}x{edited.height}")
    (OUT_DIR / "response-meta.json").write_text(
        json.dumps({k: v for k, v in payload.items() if k != "data"}, indent=2)
    )
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
