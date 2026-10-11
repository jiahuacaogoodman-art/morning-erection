"""Render the canonical vector and compare opaque silhouettes with the untouched reference."""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--iteration", type=int, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[5]
out = root / "docs/design/morning/outputs/fit_iterations"
out.mkdir(parents=True, exist_ok=True)
source = Image.open(root / "apps/pulse/public/morning-sunrise.png").convert("RGBA")
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1254, "height": 1254}, device_scale_factor=1)
    page.goto((root / "apps/pulse/public/morning-logo.svg").as_uri())
    page.locator("svg").evaluate('(el)=>{el.setAttribute("width","1254");el.setAttribute("height","1254")}')
    page.screenshot(path=str(out / f"{args.iteration:02}-vector.png"), omit_background=True)
    browser.close()
render = Image.open(out / f"{args.iteration:02}-vector.png").convert("RGBA")
a = np.asarray(source)
b = np.asarray(render)
A = a[:, :, 3] > 160
B = b[:, :, 3] > 160
result = {
    "iteration": args.iteration,
    "iou": int((A & B).sum()) / int((A | B).sum()),
    "src_only_px": int((A & ~B).sum()),
    "render_only_px": int((B & ~A).sum()),
    "alpha_threshold": 160,
    "smoothness": "circular arc and cubic Bezier boundaries; no pixel-grid tracing",
}
(out / f"{args.iteration:02}-metrics.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
bg = Image.new("RGBA", source.size, "#fffefb")
left = Image.alpha_composite(bg, source).convert("RGB")
right = Image.alpha_composite(bg, render).convert("RGB")
comparison = Image.new("RGB", (2508, 1254), "#fffefb")
comparison.paste(left, (0, 0))
comparison.paste(right, (1254, 0))
comparison.resize((1254, 627)).save(out / f"{args.iteration:02}-compare.png")
over = np.asarray(left).copy()
over[B] = (over[B] * 0.55 + np.array([0, 190, 210]) * 0.45).astype("uint8")
Image.fromarray(over).save(out / f"{args.iteration:02}-overlay.png")
