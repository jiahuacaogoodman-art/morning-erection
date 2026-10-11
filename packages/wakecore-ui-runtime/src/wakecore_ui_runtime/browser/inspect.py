"""A deterministic, read-only element picker for generic Pulse website onboarding."""
from typing import Any

from ..policy import in_scope, normalise
from .observer import fail, retry_after
from .playwright import install_guard

PICK_JS = r"""() => {
  const selector = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    for (const attr of ['data-testid','data-test','data-course-id','aria-label']) {
      const val = el.getAttribute(attr);
      if (val && val.length < 100) {
        const s = el.tagName.toLowerCase() + '[' + attr + '=' + JSON.stringify(val) + ']';
        if (document.querySelectorAll(s).length === 1) return s;
      }
    }
    const bits = [];
    for (let n = el; n && n !== document.body && bits.length < 8; n = n.parentElement) {
      const peers = [...n.parentElement.children].filter(x => x.tagName === n.tagName);
      bits.unshift(n.tagName.toLowerCase() + (peers.length > 1 ? ':nth-of-type(' + (peers.indexOf(n)+1) + ')' : ''));
      if (n.parentElement.id) { bits.unshift('#' + CSS.escape(n.parentElement.id)); break; }
    }
    return bits.join(' > ');
  };
  const candidates = [{selector:'body', label:'页面正文', value:document.body.innerText.replace(/\s+/g,' ').slice(0,60),
    numeric:false, context:'关注整页文字变化；页面上的时间等动态内容也会影响结果'}];
  for (const el of document.querySelectorAll('h1,h2,h3,p,td,span,strong,b,a,button,[role=status]')) {
    if (candidates.length >= 80) break;
    if (el.closest('form:has(input[type=password]),[hidden],[aria-hidden=true]')) continue;
    if (el.querySelector('input,textarea,select') || el.children.length > 2) continue;
    const value = el.innerText?.replace(/\s+/g,' ').trim();
    const rect = el.getBoundingClientRect();
    if (!value || value.length > 160 || !rect.width || !rect.height || getComputedStyle(el).visibility === 'hidden') continue;
    const s = selector(el);
    if (!s || candidates.some(x => x.selector === s)) continue;
    const row = el.closest('tr,article,li');
    let label = el.getAttribute('aria-label') || el.getAttribute('data-label');
    if (!label && row?.tagName === 'TR') {
      const index = [...el.parentElement.children].indexOf(el);
      label = row.closest('table')?.querySelectorAll('thead th')[index]?.innerText;
    }
    label ||= el.previousElementSibling?.tagName === 'LABEL' ? el.previousElementSibling.innerText : value;
    candidates.push({selector:s, label:label.slice(0,60), value, numeric:/[-+]?\d[\d,]*(\.\d+)?/.test(value),
      context:row?.innerText?.replace(/\s+/g,' ').slice(0,120) || ''});
  }
  return candidates;
}"""


def inspect_page(worker: Any, faults: dict[str, Any], *, url: str, origins: list[str]) -> dict[str, Any]:
    allowed = normalise(origins)
    if not in_scope(url, allowed):
        return fail("UNAVAILABLE", "origin_outside_scope")
    context = worker.ensure_context(lambda w: install_guard(w, faults))
    guard = worker.guard
    guard.enter("observe", allowed)
    page = guard.own(context.new_page())
    try:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_selector("body", timeout=10000)
        except Exception:
            return fail("AUTH_REQUIRED" if guard.escaped else "UNAVAILABLE",
                        "redirected_off_origin" if guard.escaped else "navigation_failed")
        if guard.escaped or not in_scope(page.url, allowed):
            return fail("AUTH_REQUIRED", "redirected_off_origin")
        if page.locator('input[type="password"]').count() or (response and response.status in (401, 403)):
            return fail("AUTH_REQUIRED", "login_page")
        if response and (response.status == 429 or response.status >= 500):
            return fail("UNAVAILABLE", f"http_{response.status}",
                        retry_after_seconds=retry_after(response.header_value("retry-after")))
        return {"outcome": "SUCCESS", "error_code": None, "records": {}, "title": page.title(),
                "url": page.url, "candidates": page.evaluate(PICK_JS)}
    finally:
        guard.leave()
        guard.disown(page)
        guard.close_strays(context)
