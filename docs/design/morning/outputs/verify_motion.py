"""Deterministic rendered checks for the layered mark and its interaction controls."""

import json
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
URL = (ROOT / "docs/design/morning/logo_motion.html").as_uri()


def changes(first, second):
    difference = ImageChops.difference(Image.open(first).convert("RGB"), Image.open(second).convert("RGB"))
    return sum(pixel != (0, 0, 0) for pixel in difference.getdata())


report = {}
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1000, "height": 900}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    for name, query in [("static", "static=1"), ("final", "t=1600"), ("exploded", "qa=1&explode=1")]:
        page.goto(URL + "?" + query)
        page.wait_for_function("window.__p2mReady === true")
        page.locator("#logo-root").screenshot(path=str(OUT / f"{name}.png"))
    report["final_frame_changed_pixels"] = changes(OUT / "static.png", OUT / "final.png")
    assert report["final_frame_changed_pixels"] == 0
    page.goto(URL + "?qa=1")
    page.wait_for_function("window.__p2mReady === true")
    page.wait_for_function("document.querySelector('#logo-root').classList.contains('is-static')")
    page.locator("#logo-root").screenshot(path=str(OUT / "product-held-final.png"))
    report["product_hold_changed_pixels"] = changes(OUT / "static.png", OUT / "product-held-final.png")
    assert report["product_hold_changed_pixels"] == 0
    report["layer_samples"] = {}
    for t in [300, 700, 1100]:
        page.goto(URL + f"?t={t}")
        page.wait_for_function("window.__p2mReady === true")
        report["layer_samples"][t] = page.evaluate(
            "Object.fromEntries([...document.querySelectorAll('#logo-root [data-enter]')].map(e=>[e.dataset.enter,{transform:getComputedStyle(e).transform,opacity:getComputedStyle(e).opacity}]))"
        )
    assert len({value["transform"] for value in report["layer_samples"][700].values()}) >= 4
    page.goto(URL)
    page.wait_for_function("window.__p2mReady === true")
    page.get_by_role("button", name="分层展开", exact=True).click()
    assert "is-exploded" in page.locator("#logo-root").get_attribute("class")
    page.get_by_role("checkbox", name="显示太阳", exact=True).uncheck()
    assert page.locator('#logo-root [data-part="sun"]').evaluate("e=>getComputedStyle(e).display") == "none"
    page.get_by_role("button", name="重播", exact=True).click()
    assert page.get_by_role("checkbox", name="显示太阳", exact=True).is_checked()
    page.get_by_role("button", name="慢动作", exact=True).click()
    assert page.locator("#logo-root").evaluate(
        "e=>e.getAnimations({subtree:true}).filter(a=>a instanceof CSSAnimation).every(a=>a.playbackRate===.3)"
    )
    assert not page.evaluate(
        "[...document.querySelectorAll('[id]')].map(e=>e.id).some((id,i,ids)=>ids.indexOf(id)!==i)"
    )
    page.locator("#live-study").evaluate("e=>e.getAnimations({subtree:true}).forEach(a=>{a.pause();a.playbackRate=1})")
    for t in [0, 3200, 1, 3199]:
        page.locator("#live-study").evaluate("(e,t)=>e.getAnimations({subtree:true}).forEach(a=>a.currentTime=t)", t)
        page.locator("#live-study").screenshot(path=str(OUT / f"loop-{t}.png"))
    report["loop_seam_changed_pixels"] = changes(OUT / "loop-0.png", OUT / "loop-3200.png")
    report["loop_near_seam_changed_pixels"] = changes(OUT / "loop-1.png", OUT / "loop-3199.png")
    assert report["loop_seam_changed_pixels"] == 0
    page.emulate_media(reduced_motion="reduce")
    page.goto(URL + "?qa=1")
    page.wait_for_function("window.__p2mReady === true")
    page.locator("#logo-root").screenshot(path=str(OUT / "reduced-motion.png"))
    report["reduced_motion_changed_pixels"] = changes(OUT / "static.png", OUT / "reduced-motion.png")
    assert report["reduced_motion_changed_pixels"] == 0
    report["reduced_motion_active_animations"] = page.locator("#logo-root").evaluate(
        "e=>e.getAnimations({subtree:true}).length"
    )
    assert report["reduced_motion_active_animations"] == 0
    page.emulate_media(reduced_motion="no-preference")
    page.goto(URL)
    page.add_script_tag(path=str(ROOT / "apps/pulse/node_modules/axe-core/axe.min.js"))
    report["accessibility_violations"] = page.evaluate(
        "async()=> (await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})).violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}))"
    )
    assert not report["accessibility_violations"], report["accessibility_violations"]
    for width, height in [(1280, 900), (390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        page.get_by_role("button", name="分层展开", exact=True).click()
        page.locator("#logo-root").evaluate(
            'e=>e.querySelectorAll("[data-part]").forEach(p=>p.style.transition="none")'
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(OUT / f"preview-{width}.png"), full_page=True)
        page.get_by_role("button", name="分层展开", exact=True).click()
    report["console_errors"] = errors
    assert not errors
    browser.close()
(OUT / "motion-qa.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report, ensure_ascii=False))
