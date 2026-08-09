#!/usr/bin/env python
"""Measure which base colour landed on each finger, per matrix cell.

The emerald identity file has a clean invariant: odd nails are deep emerald
green, even nails are brushed champagne gold. So "did the design reach the right
finger" reduces to a question that can be measured instead of eyeballed -- sample
each nail's mask region and classify it green or gold.

This does NOT check motifs, counts or wave direction. It checks base colour
placement, which is the specific thing the view-plan change is supposed to fix
and the thing prose ordering got wrong.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "lunelle.db"

# From the identity file: odd = emerald green, even = champagne gold.
EXPECTED = {f"nail-{i:02d}": ("green" if i % 2 else "gold") for i in range(1, 11)}


def classify(rgb: tuple[float, float, float]) -> str:
    """Green vs gold from mean RGB. Emerald is g-dominant and dark; champagne
    gold is r>=g>b and bright. The gap between them is wide, so a mean over the
    mask interior is enough -- no per-pixel voting needed."""
    r, g, b = rgb
    if g > b and g >= r and (g - b) > 12:
        return "green"
    if r >= g > b and (r + g) / 2 > 110:
        return "gold"
    return "green" if g > r else "gold"


def slots(conn: sqlite3.Connection, tone: str, view: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    hm = conn.execute(
        "select * from hand_models where tone=? and view=? and retired_at is null"
        " order by revision desc limit 1",
        (tone, view),
    ).fetchone()
    if hm is None:
        raise SystemExit(f"no hand model for tone={tone} view={view}")
    rows = conn.execute(
        "select s.*, a.path mask_path from hand_model_slots s"
        " join assets a on a.digest = s.mask_digest"
        " where s.hand_model_id = ? order by s.nail_id",
        (hm["hand_model_id"],),
    ).fetchall()
    return [dict(r) | {"model_w": hm["width"], "model_h": hm["height"]} for r in rows]


def measure(image_path: Path, slot_rows: list[dict]) -> dict[str, str]:
    img = Image.open(image_path).convert("RGB")
    out: dict[str, str] = {}
    for s in slot_rows:
        # The nail region lives in the alpha channel; RGB is all zero, so
        # convert("L") would flatten every mask to empty.
        raw = Image.open(s["mask_path"])
        mask = raw.split()[-1] if raw.mode == "RGBA" else raw.convert("L")
        # The output is generated at the provider's own size, so scale the mask
        # to it rather than assuming the base photo's dimensions.
        if mask.size != img.size:
            mask = mask.resize(img.size, Image.NEAREST)
        # Masks are opaque-on-nail (measured polarity, see migration 0010 notes).
        px = img.load()
        mp = mask.load()
        acc = [0.0, 0.0, 0.0]
        n = 0
        w, h = img.size
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                if mp[x, y] > 128:
                    p = px[x, y]
                    acc[0] += p[0]
                    acc[1] += p[1]
                    acc[2] += p[2]
                    n += 1
        if n == 0:
            out[s["nail_id"]] = "empty"
            continue
        mean = (acc[0] / n, acc[1] / n, acc[2] / n)
        out[s["nail_id"]] = classify(mean)
    return out


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    task_ids = sys.argv[1:]
    if not task_ids:
        raise SystemExit("usage: check_nail_placement.py <task_id> [task_id ...]")

    for tid in task_ids:
        t = conn.execute(
            "select task_id, prompt_version, output_path, review_state,"
            " json_extract(metadata_json,'$.tone') tone,"
            " json_extract(metadata_json,'$.view') view"
            " from tasks where task_id=?",
            (tid,),
        ).fetchone()
        if t is None or not t["output_path"]:
            print(f"{tid}: no output")
            continue
        rows = slots(conn, t["tone"], t["view"])
        got = measure(Path(t["output_path"]), rows)
        by_nail = {r["nail_id"]: r for r in rows}

        hits = sum(1 for k, v in got.items() if v == EXPECTED[k])
        print(f"\n=== {tid}  {t['prompt_version']}  tone={t['tone']} "
              f"{t['review_state'] or ''} ===")
        print(f"{'nail':9}{'finger':16}{'expect':8}{'got':8}")
        for nid in sorted(got):
            r = by_nail[nid]
            mark = "" if got[nid] == EXPECTED[nid] else "   <-- WRONG"
            print(f"{nid:9}{r['hand'] + ' ' + r['finger']:16}"
                  f"{EXPECTED[nid]:8}{got[nid]:8}{mark}")
        print(f"base-colour placement: {hits}/10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
