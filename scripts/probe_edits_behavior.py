#!/usr/bin/env python
"""Controlled probe of the gpt-image-2 edits endpoint.

Three questions, each answered by comparison rather than by a single 200:

A. Is `mask` actually read, or silently ignored? The vendor docs list every
   accepted edits parameter and `mask` is NOT among them, so a 200 proves only
   that the field was tolerated. Same image + same prompt, with and without the
   mask; if the outside-mask region changes equally in both, the mask did nothing.

B. What does `input_fidelity=high` buy? Documented as "提高对输入参考图的跟随程度",
   which is exactly the hand-fidelity problem. Measured against the real hand model.

C. Which sizes does edits actually accept? A 1024x1024 request came back as
   1254x1254, so the requested size is not honoured as-is and must be mapped.

Every call is paid. Run with --questions to select a subset.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image

from lunelle.config import load_config

NAIL_COLORS = {
    "nail-01": (255, 0, 0), "nail-02": (0, 255, 0), "nail-03": (0, 0, 255),
    "nail-04": (255, 255, 0), "nail-05": (255, 0, 255), "nail-06": (0, 255, 255),
    "nail-07": (255, 128, 0), "nail-08": (128, 0, 255), "nail-09": (255, 0, 128),
    "nail-10": (0, 255, 128),
}
ANNOTATION_DIR = Path(
    os.environ.get("LUNELLE_ANNOTATION_DIR", "/Users/turboche/Desktop/标注")
)
# Scratch dir for probe artifacts; inspected by hand, never read back by code.
OUT = Path(tempfile.gettempdir()) / "edits_probe"


def channel() -> tuple[str, str, str]:
    config = load_config()
    conn = sqlite3.connect(config.data_dir / "lunelle.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT name, base_url, api_key, model FROM api_profiles"
        " WHERE is_active = 1 AND kind = 'image' LIMIT 1"
    ).fetchone()
    if row is None:
        return config.image_api_base_url, config.image_api_key, config.image_model
    p = dict(row)
    return p["base_url"].rstrip("/"), p["api_key"], p["model"]


def hand_model(tone: str = "light") -> Path:
    config = load_config()
    conn = sqlite3.connect(config.data_dir / "lunelle.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (f"hand_model_{tone}_p2_open_hands",),
    ).fetchone()
    if row is None:
        raise SystemExit(f"no hand model for {tone}")
    return Path(dict(row)["value"])


def derive_mask(annotation: Path, nail_id: str, size: tuple[int, int]) -> Image.Image:
    colour = NAIL_COLORS[nail_id]
    with Image.open(annotation) as src:
        rgb = src.convert("RGB")
        pixels = rgb.load()
        w, h = rgb.size
    if (w, h) != size:
        raise SystemExit(f"annotation {w}x{h} != base {size}")
    mask = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    mp = mask.load()
    hits = 0
    for y in range(h):
        for x in range(w):
            if pixels[x, y] == colour:
                mp[x, y] = (0, 0, 0, 0)
                hits += 1
    return mask, hits


def png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def call_edits(base_url, key, model, *, image_png: bytes, prompt: str,
               mask_png: bytes | None = None, size: str | None = None,
               fidelity: str | None = None, timeout: int = 600):
    files = {"image": ("base.png", image_png, "image/png")}
    if mask_png is not None:
        files["mask"] = ("mask.png", mask_png, "image/png")
    data = {"model": model, "prompt": prompt, "n": "1",
            "response_format": "b64_json", "quality": "high"}
    if size:
        data["size"] = size
    if fidelity:
        data["input_fidelity"] = fidelity
    try:
        r = httpx.post(f"{base_url}/images/edits",
                       headers={"Authorization": f"Bearer {key}"},
                       files=files, data=data, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    item = r.json().get("data", [{}])[0]
    b64 = item.get("b64_json")
    if not b64:
        url = item.get("url")
        if url:
            try:
                got = httpx.get(url, timeout=300)
                return Image.open(io.BytesIO(got.content)).convert("RGB"), None
            except Exception as exc:  # noqa: BLE001
                return None, f"url fetch failed: {exc}"
        return None, f"no image in response: {list(item)}"
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB"), None


def diff_stats(a: Image.Image, b: Image.Image, mask: Image.Image | None,
               step: int = 4) -> dict:
    """Mean/max abs diff inside vs outside the transparent mask region."""
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    ap, bp = a.load(), b.load()
    mp = mask.load() if mask is not None else None
    w, h = a.size
    ins, outs = [], []
    for y in range(0, h, step):
        for x in range(0, w, step):
            d = max(abs(ap[x, y][i] - bp[x, y][i]) for i in range(3))
            if mp is not None and mp[x, y][3] == 0:
                ins.append(d)
            else:
                outs.append(d)
    def sm(v):
        return {"mean": round(sum(v) / len(v), 2), "max": max(v), "n": len(v)} if v else None
    return {"inside": sm(ins), "outside": sm(outs)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default="ABC", help="subset of A,B,C")
    ap.add_argument("--tone", default="light")
    ap.add_argument("--nail", default="nail-03")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    base_url, key, model = channel()
    print(f"channel: {base_url} / {model}\n")

    base_path = hand_model(args.tone)
    with Image.open(base_path) as im:
        base_rgb = im.convert("RGB")
        base_size = base_rgb.size
    print(f"hand model: {base_size[0]}x{base_size[1]}  {base_path.name[:16]}...")

    mask_img, hits = derive_mask(ANNOTATION_DIR / args.tone / "p2.png", args.nail, base_size)
    print(f"{args.nail} mask: {hits} px ({hits / (base_size[0] * base_size[1]) * 100:.2f}%)\n")
    results: dict = {}

    # Everything is sent at a documented edits size so size is never the variable.
    SEND = (1536, 1024)
    base_sent = base_rgb.resize(SEND, Image.LANCZOS)
    mask_sent = mask_img.resize(SEND, Image.NEAREST)
    base_png = png_bytes(base_sent.convert("RGBA"))
    mask_png = png_bytes(mask_sent)

    PROMPT = (
        "Repaint only the single fingernail that is already indicated; give it a "
        "deep emerald green glossy base with one small gold star at its centre. "
        "Keep the hands, fingers, skin, background and lighting exactly as they are."
    )

    if "A" in args.questions:
        print("=== A. is `mask` read at all? (2 calls, identical except the mask) ===")
        with_mask, err1 = call_edits(base_url, key, model, image_png=base_png,
                                     prompt=PROMPT, mask_png=mask_png,
                                     size="1536x1024", fidelity="high")
        print(f"  with mask   : {'ok ' + str(with_mask.size) if with_mask else err1}")
        without, err2 = call_edits(base_url, key, model, image_png=base_png,
                                   prompt=PROMPT, size="1536x1024", fidelity="high")
        print(f"  without mask: {'ok ' + str(without.size) if without else err2}")
        if with_mask and without:
            with_mask.save(OUT / "A-with-mask.png")
            without.save(OUT / "A-without-mask.png")
            sw = diff_stats(base_sent, with_mask, mask_sent)
            so = diff_stats(base_sent, without, mask_sent)
            print(f"\n  vs base, WITH mask   : inside={sw['inside']}  outside={sw['outside']}")
            print(f"  vs base, WITHOUT mask: inside={so['inside']}  outside={so['outside']}")
            ratio_w = sw["outside"]["mean"] / max(0.01, sw["inside"]["mean"])
            ratio_o = so["outside"]["mean"] / max(0.01, so["inside"]["mean"])
            print(f"\n  outside/inside change ratio  with={ratio_w:.2f}  without={ratio_o:.2f}")
            print("  -> if the two ratios are similar, the mask changed nothing.")
            results["A"] = {"with": sw, "without": so}
        print()

    if "B" in args.questions:
        print("=== B. does input_fidelity=high improve hand fidelity? (2 calls) ===")
        hi, e1 = call_edits(base_url, key, model, image_png=base_png, prompt=PROMPT,
                            size="1536x1024", fidelity="high")
        lo, e2 = call_edits(base_url, key, model, image_png=base_png, prompt=PROMPT,
                            size="1536x1024")
        print(f"  input_fidelity=high: {'ok' if hi else e1}")
        print(f"  (omitted)          : {'ok' if lo else e2}")
        if hi and lo:
            hi.save(OUT / "B-fidelity-high.png")
            lo.save(OUT / "B-fidelity-default.png")
            dh = diff_stats(base_sent, hi, mask_sent)
            dl = diff_stats(base_sent, lo, mask_sent)
            print(f"\n  high   : inside={dh['inside']}  outside={dh['outside']}")
            print(f"  default: inside={dl['inside']}  outside={dl['outside']}")
            print("  -> lower OUTSIDE mean = hand better preserved.")
            results["B"] = {"high": dh, "default": dl}
        print()

    if "C" in args.questions:
        print("=== C. which sizes does edits actually honour? ===")
        sizes = ["1024x1024", "1536x1024", "1448x1086"]
        table = {}
        for s in sizes:
            img, err = call_edits(base_url, key, model, image_png=base_png,
                                  prompt="Slightly warm the overall colour temperature.",
                                  size=s, fidelity="high", timeout=400)
            got = f"{img.width}x{img.height}" if img else f"ERR {err[:80]}"
            table[s] = got
            print(f"  requested {s:12} -> {got}")
        results["C"] = table
        print()

    (OUT / "results.json").write_text(json.dumps(results, indent=2))
    print(f"artifacts + results.json in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
