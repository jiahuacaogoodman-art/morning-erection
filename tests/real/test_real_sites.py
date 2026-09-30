"""Scenarios on real public test sites (see conftest for how to run).

  R0  model endpoint             two tiny calls: the endpoint speaks Responses + the computer tool +
                                 previous_response_id (OpenAI or an OpenAI-compatible relay)
  R1  the-internet /tables       eye only: a real table without labels, read by selector
  R1b books.toscrape             eye only: list mode on a real catalogue
  R2  TodoMVC                    full chain with the real model: a todo appears -> wake -> plan
                                 -> approval -> Computer Use ticks it -> re-observed -> CONFIRMED
  R3  saucedemo                  login in the user's profile, full chain: add one item to the cart
  R3b saucedemo logout           the user logs out -> AUTH_REQUIRED, no model call
"""
import os
import shutil
import subprocess
import sys
import time

import pytest
from conftest import KEY_FILE, ROOT, RealWorld, base_url, has_key, model_args, needs_key, sidecar_env

from harness import TENANT
from wakecore.kernel.domain.enums import ActionStatus, SourceHealth, TaskLifecycle
from wakecore.kernel.replay import replay_task

THE_INTERNET = "https://the-internet.herokuapp.com"
TODOMVC = "https://demo.playwright.dev"
SAUCE = "https://www.saucedemo.com"

TABLE = {"table": {"selector": "#table1"}, "key_field": "email", "columns": [
    {"field": "last", "headers": ["Last Name"]}, {"field": "first", "headers": ["First Name"]},
    {"field": "email", "headers": ["Email"]}, {"field": "due", "headers": ["Due"]},
    {"field": "site", "headers": ["Web Site"]}]}
EMAILS = ["jsmith@gmail.com", "fbach@yahoo.com", "jdoe@hotmail.com", "tconway@earthlink.net"]

TODOS = {"list": {"item": "li[data-testid=todo-item]"}, "ready": "input.new-todo", "key_field": "title",
         "columns": [{"field": "title", "selector": "label[data-testid=todo-title]"},
                     {"field": "done", "selector": "input.toggle:checked", "type": "exists"}]}
INVENTORY = {"list": {"item": ".inventory_item"}, "ready": ".inventory_list", "key_field": "name",
             "columns": [{"field": "name", "selector": ".inventory_item_name"},
                         {"field": "price", "selector": ".inventory_item_price"},
                         {"field": "in_cart", "selector": "button[data-test^=remove]", "type": "exists"}],
             "login": {"selector": "#login-button"}}


def keep_artifacts(w: RealWorld, report, name: str) -> str:
    src = w.state / "artifacts"
    dst = report.dir / name
    if src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    return str(dst)


def model_usage(summary: dict) -> dict:
    acts = [a["act"] for a in summary["attempts"] if a.get("act")]
    return {"model_calls": sum(a.get("model_calls", 0) for a in acts),
            "input_tokens": sum((a.get("usage") or {}).get("input_tokens", 0) for a in acts),
            "output_tokens": sum((a.get("usage") or {}).get("output_tokens", 0) for a in acts)}


# ------------------------------------------------------------------ R1: eye only, real table

def test_r1_the_internet_table_is_watched_without_any_model(make_real, report):
    w = make_real(origin=THE_INTERNET, session="internet_eye", with_key=False)
    cond = {"field": "due", "op": "eq", "value": "$0.00", "kind": "paid_off"}
    entry = {"scenario": "R1 the-internet /tables 纯观察", "url": THE_INTERNET + "/tables"}
    try:
        t0 = time.time()
        w.setup(task_id="table_watch", url=THE_INTERNET + "/tables", extractor=TABLE, record_ids=EMAILS,
                conditions=[cond], complete_when=[cond], watch_fields=["due"], on_met="notify")
        w.h.run()
        for _ in range(2):
            w.h.cycle()
        entry.update(seconds=round(time.time() - t0, 1), fetches=w.web.fetch_count, health=w.binding().health.value,
                     health_reason=w.binding().health_reason,
                     model_calls=len(w.h.k.model.calls), inbox=len(w.h.inbox()), events=len(w.h.events()))
        assert w.binding().health is SourceHealth.HEALTHY
        assert w.web.fetch_count == 3 and w.h.k.model.calls == [] and w.h.inbox() == []
        rec = w.client.observe(session_ref="internet_eye", url=THE_INTERNET + "/tables",
                               origins=[THE_INTERNET], extractor=TABLE)
        entry["sample"] = rec["records"]["jsmith@gmail.com"]
        assert rec["records"]["jsmith@gmail.com"]["last"] == "Smith"
        entry["verdict"] = "PASS"
    except BaseException as e:
        entry.update(verdict="FAIL", error=repr(e)[:500])
        raise
    finally:
        report.add(entry)


def test_r1b_books_catalogue_list_mode(make_real, report):
    origin = "https://books.toscrape.com"
    w = make_real(origin=origin, session="books_eye", with_key=False)
    ex = {"list": {"item": "article.product_pod"}, "ready": "ol.row", "key_field": "title", "columns": [
        {"field": "title", "selector": "h3 a", "attr": "title"}, {"field": "price", "selector": ".price_color"},
        {"field": "in_stock", "selector": ".instock.availability", "type": "exists"}]}
    entry = {"scenario": "R1b books.toscrape 列表模式", "url": origin}
    try:
        r1 = w.client.observe(session_ref="books_eye", url=origin + "/", origins=[origin], extractor=ex)
        r2 = w.client.observe(session_ref="books_eye", url=origin + "/", origins=[origin], extractor=ex)
        entry.update(outcome=r1["outcome"], records=len(r1.get("records") or {}),
                     stable_digest=r1.get("content_digest") == r2.get("content_digest"),
                     sample=next(iter((r1.get("records") or {}).items()), None))
        assert r1["outcome"] == "SUCCESS" and len(r1["records"]) == 20 and entry["stable_digest"]
        entry["verdict"] = "PASS"
    except BaseException as e:
        entry.update(verdict="FAIL", error=repr(e)[:500])
        raise
    finally:
        report.add(entry)


# ------------------------------------------------------------------ R2: TodoMVC full chain

def add_todo(title: str):
    def human(page):
        page.wait_for_selector("input.new-todo")
        page.fill("input.new-todo", title)
        page.press("input.new-todo", "Enter")
        page.wait_for_selector(f"label[data-testid=todo-title]:text-is('{title}')")
        page.wait_for_timeout(500)     # let localStorage flush before the profile closes
    return human


@needs_key
def test_r2_todomvc_new_todo_wakes_the_task_and_computer_use_ticks_it(make_real, report):
    app = TODOMVC + "/todomvc/#/"
    w = make_real(origin=TODOMVC, session="todo_user", with_key=True)
    target, other = "交房租", "买菜"
    not_done = {"field": "done", "op": "eq", "value": False, "kind": "todo_added"}
    done = {"field": "done", "op": "eq", "value": True}
    entry = {"scenario": "R2 TodoMVC 全链路（真实 Computer Use）", "url": app}
    try:
        w.as_user(app, add_todo(other))
        w.setup(task_id="todo_watch", url=app, extractor=TODOS, record_ids=[target], conditions=[not_done],
                complete_when=[done], watch_fields=["done"],
                scope_extra={"missing_targets": "absent", "state_location": "client"},
                purpose="待办里出现「交房租」就帮我勾掉，确认已完成后结束")
        w.h.run()                                              # baseline: the todo does not exist yet
        assert w.binding().health is SourceHealth.HEALTHY and w.browser_actions() == []
        w.script_plan(goal=f"在这个待办事项应用里，把「{target}」这一项勾选为已完成。只做这一件事，"
                           f"不要修改、删除或新增其他待办。完成后回复 DONE。",
                      start_url=app, record=target, conditions=[done])
        w.as_user(app, add_todo(target))                       # the user writes a new todo by hand
        w.h.cycle()
        [action] = w.browser_actions()
        entry["woke"] = action.status.value
        assert action.status is ActionStatus.WAITING_APPROVAL, action.resolution
        w.approve(action)
        t0 = time.time()
        w.h.run()                                              # the real model operates the page
        entry["act_seconds"] = round(time.time() - t0, 1)
        summary = w.act_summary(action)
        entry.update(result=summary, **model_usage(summary))
        entry["artifacts"] = keep_artifacts(w, report, "r2_todomvc")
        assert w.current(action).status is ActionStatus.CONFIRMED, summary
        page = w.client.observe(session_ref="todo_user", url=app, origins=[TODOMVC], extractor=TODOS)
        entry["page_after"] = page.get("records")
        assert page["records"] == {other: {"done": False}, target: {"done": True}}   # nothing else touched
        w.h.cycle()
        assert w.h.runtime("todo_watch").lifecycle is TaskLifecycle.COMPLETED
        w.side.stop()                                          # replay must not need (or touch) the page
        entry["replay_mismatches"] = replay_task(w.h.store, TENANT, "todo_watch").summary()["mismatches"]
        assert entry["replay_mismatches"] == []
        entry["verdict"] = "PASS"
    except BaseException as e:
        entry.update(verdict="FAIL", error=repr(e)[:800])
        raise
    finally:
        report.add(entry)


# ------------------------------------------------------------------ R3: saucedemo

def sauce_login(page):
    page.fill("#user-name", "standard_user")          # public demo credentials, typed by the "human"
    page.fill("#password", "secret_sauce")
    page.click("#login-button")


def sauce_logout(page):
    page.click("#react-burger-menu-btn")
    page.click("#logout_sidebar_link")
    page.wait_for_selector("#login-button")


SAUCE_TASK = dict(url=SAUCE + "/inventory.html", extractor=INVENTORY, record_ids=["Sauce Labs Backpack"],
                  watch_fields=["in_cart"], scope_extra={"state_location": "client"})


@needs_key
def test_r3_saucedemo_logged_in_profile_adds_one_item_to_the_cart(make_real, report):
    w = make_real(origin=SAUCE, session="sauce_user", with_key=True)
    item = "Sauce Labs Backpack"
    missing = {"field": "in_cart", "op": "eq", "value": False, "kind": "not_in_cart"}
    inside = {"field": "in_cart", "op": "eq", "value": True}
    entry = {"scenario": "R3 saucedemo 登录态 + 加购（真实 Computer Use）", "url": SAUCE}
    try:
        w.as_user(SAUCE + "/", sauce_login, done_url_contains="/inventory.html")
        w.setup(task_id="cart_watch", conditions=[missing], complete_when=[inside],
                first_snapshot="notify_existing_results", purpose="购物车里没有背包就加进去，确认后结束", **SAUCE_TASK)
        w.script_plan(goal=f"在商品列表页把「{item}」加入购物车。只加这一件商品，不要打开购物车、不要结账。完成后回复 DONE。",
                      start_url=SAUCE + "/inventory.html", record=item, conditions=[inside])
        w.h.run()
        assert w.binding().health is SourceHealth.HEALTHY
        [action] = w.browser_actions()
        entry["woke"] = action.status.value
        assert action.status is ActionStatus.WAITING_APPROVAL, action.resolution
        w.approve(action)
        t0 = time.time()
        w.h.run()
        entry["act_seconds"] = round(time.time() - t0, 1)
        summary = w.act_summary(action)
        entry.update(result=summary, **model_usage(summary))
        entry["artifacts"] = keep_artifacts(w, report, "r3_saucedemo")
        assert w.current(action).status is ActionStatus.CONFIRMED, summary
        page = w.client.observe(session_ref="sauce_user", url=SAUCE + "/inventory.html", origins=[SAUCE],
                                extractor=INVENTORY)
        in_cart = sorted(k for k, v in page["records"].items() if v["in_cart"])
        entry["in_cart_after"] = in_cart
        assert in_cart == [item]                                     # exactly one item, the right one
        w.h.cycle()
        assert w.h.runtime("cart_watch").lifecycle is TaskLifecycle.COMPLETED
        w.side.stop()
        entry["replay_mismatches"] = replay_task(w.h.store, TENANT, "cart_watch").summary()["mismatches"]
        assert entry["replay_mismatches"] == []
        entry["verdict"] = "PASS"
    except BaseException as e:
        entry.update(verdict="FAIL", error=repr(e)[:800])
        raise
    finally:
        report.add(entry)


def test_r3b_saucedemo_logout_blocks_the_source_without_the_model(make_real, report):
    w = make_real(origin=SAUCE, session="sauce_eye", with_key=False)
    cond = {"field": "in_cart", "op": "eq", "value": True, "kind": "in_cart"}
    entry = {"scenario": "R3b saucedemo 退出登录 → AUTH_REQUIRED", "url": SAUCE}
    try:
        w.as_user(SAUCE + "/", sauce_login, done_url_contains="/inventory.html")
        w.setup(task_id="cart_eye", conditions=[cond], complete_when=[cond], on_met="notify", **SAUCE_TASK)
        w.h.run()
        entry["before"] = w.binding().health.value
        assert w.binding().health is SourceHealth.HEALTHY
        w.as_user(SAUCE + "/inventory.html", sauce_logout, done_url_contains="saucedemo.com")
        w.h.cycle()
        entry.update(after=w.binding().health.value, model_calls=len(w.h.k.model.calls))
        assert w.binding().health is SourceHealth.AUTH_REQUIRED and w.h.k.model.calls == []
        entry["verdict"] = "PASS"
    except BaseException as e:
        entry.update(verdict="FAIL", error=repr(e)[:500])
        raise
    finally:
        report.add(entry)


def test_key_presence_is_reported(report):
    report.add({"scenario": "环境", "verdict": "INFO", "openai_key_file_filled": has_key(),
                "model": os.environ.get("WAKECORE_REAL_MODEL") or "runtime default (gpt-5.6-sol)",
                "variant": os.environ.get("WAKECORE_REAL_VARIANT") or "ga",
                "endpoint": "custom (.secrets/openai.base_url or WAKECORE_REAL_BASE_URL)" if base_url()
                else "api.openai.com"})
    if not has_key():
        pytest.skip("key file empty: R2/R3 skipped")


@needs_key
def test_r0_the_model_endpoint_speaks_the_protocol(report):
    """Before spending a browser run: does this endpoint (OpenAI or a relay) do what the loop needs?"""
    p = subprocess.run([sys.executable, "-m", "wakecore_ui_runtime.server", "--check-model",
                        "--openai-key-file", str(KEY_FILE), *model_args()],
                       cwd=ROOT, env=sidecar_env(), capture_output=True, text=True, timeout=300)
    out = p.stdout.strip().splitlines()
    report.add({"scenario": "R0 模型端点连通性", "verdict": "PASS" if p.returncode == 0 else "FAIL", "check": out,
                "stderr": p.stderr[-500:]})
    assert p.returncode == 0, "\n".join(out) + p.stderr[-500:]
