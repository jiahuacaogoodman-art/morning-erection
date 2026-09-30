"""Extractor modes real sites need (list, table by selector, ready, login by selector), run in
real Chromium on static pages shaped like the ones probed on the web (saucedemo, TodoMVC,
the-internet /tables)."""
import pytest
from playwright.sync_api import sync_playwright

from wakecore_ui_runtime.browser import extractor as ex

INVENTORY = """<div class="inventory_list">
  <div class="inventory_item"><div class="inventory_item_name">Sauce Labs Backpack</div>
    <div class="price" data-cents="2999">$29.99</div><button data-test="remove-sauce-labs-backpack">Remove</button></div>
  <div class="inventory_item"><div class="inventory_item_name">Sauce Labs Bike Light</div>
    <div class="price" data-cents="999">$9.99</div><button data-test="add-to-cart-bike-light">Add to cart</button></div>
</div>"""
CARDS = {"list": {"item": ".inventory_item"}, "ready": ".inventory_list", "key_field": "name", "columns": [
    {"field": "name", "selector": ".inventory_item_name"},
    {"field": "cents", "selector": ".price", "attr": "data-cents", "type": "int"},
    {"field": "in_cart", "selector": "button[data-test^=remove]", "type": "exists"}],
    "login": {"selector": "#login-button"}}

TABLE = """<table id="table1"><thead><tr><th><span>Last Name</span></th><th>Email</th><th>Due</th></tr></thead>
<tbody><tr><td>Smith</td><td>jsmith@gmail.com</td><td>$50.00</td></tr>
<tr><td>Bach</td><td>fbach@yahoo.com</td><td>$51.00</td></tr></tbody></table>"""
BY_SELECTOR = {"table": {"selector": "#table1"}, "key_field": "email", "columns": [
    {"field": "last", "headers": ["Last Name"]}, {"field": "email", "headers": ["Email"]},
    {"field": "due", "headers": ["Due"]}]}


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b.new_page()
        b.close()


def read(page, html, cfg):
    page.set_content(html)
    cfg = ex.validate(cfg)
    return ex.convert(cfg, page.evaluate(ex.EXTRACT_JS, cfg))


def test_list_mode_reads_cards_attributes_and_presence(page):
    assert read(page, INVENTORY, CARDS) == {
        "Sauce Labs Backpack": {"cents": 2999, "in_cart": True},
        "Sauce Labs Bike Light": {"cents": 999, "in_cart": False}}


def test_empty_list_is_an_empty_result_only_once_rendered(page):
    assert read(page, '<div class="inventory_list"></div>', CARDS) == {}
    page.set_content("<p>loading</p>")
    assert page.query_selector(CARDS["ready"]) is None     # the observer reports not_ready here


def test_list_item_itself_and_class_state(page):
    html = """<ul class="todo-list"><li data-testid="todo-item" class="completed"><input class="toggle" type="checkbox" checked>
      <label data-testid="todo-title">交房租</label></li><li data-testid="todo-item" class=""><input class="toggle" type="checkbox">
      <label data-testid="todo-title">买菜</label></li></ul>"""
    cfg = {"list": {"item": "li[data-testid=todo-item]"}, "ready": ".todo-list", "key_field": "title", "columns": [
        {"field": "title", "selector": "label[data-testid=todo-title]"},
        {"field": "state", "selector": "", "attr": "class"},
        {"field": "done", "selector": ".toggle:checked", "type": "exists"}]}
    assert read(page, html, cfg) == {"交房租": {"state": "completed", "done": True}, "买菜": {"state": "", "done": False}}


def test_table_found_by_selector_keyed_by_a_column(page):
    assert read(page, TABLE, BY_SELECTOR) == {
        "jsmith@gmail.com": {"last": "Smith", "due": "$50.00"}, "fbach@yahoo.com": {"last": "Bach", "due": "$51.00"}}


def test_a_selector_that_is_not_a_table_is_schema_invalid(page):
    with pytest.raises(ex.ExtractorError, match="table_not_found"):
        read(page, '<div id="table1"></div>', BY_SELECTOR)


def test_duplicate_keys_are_schema_invalid_not_merged(page):
    html = INVENTORY.replace("Sauce Labs Bike Light", "Sauce Labs Backpack")
    with pytest.raises(ex.ExtractorError, match="duplicate"):
        read(page, html, CARDS)


def test_a_changed_attribute_that_no_longer_parses_is_schema_invalid(page):
    with pytest.raises(ex.ExtractorError, match="not_an_int:cents"):
        read(page, INVENTORY.replace('data-cents="999"', 'data-cents="9.99"'), CARDS)


def test_login_wall_by_selector(page):
    page.set_content('<input id="user-name"><input type="password"><input id="login-button" type="submit">')
    assert ex.is_login(CARDS, "https://www.saucedemo.com/", "Swag Labs", page)
    page.set_content(INVENTORY)
    assert not ex.is_login(CARDS, "https://www.saucedemo.com/inventory.html", "Swag Labs", page)
    assert ex.ready_selector(CARDS) == ".inventory_list, #login-button"


@pytest.mark.parametrize("cfg,msg", [
    ({"list": {"item": ".x"}, "key_field": "a", "columns": [{"field": "a", "selector": ""}]}, "ready is required"),
    ({**CARDS, "table": {"selector": "#t"}}, "exactly one of table / list"),
    ({**BY_SELECTOR, "columns": [{"field": "email", "headers": ["Email"], "type": "exists"}]}, "list-mode type"),
    ({**CARDS, "columns": [{"field": "name", "headers": ["Name"]}]}, "unknown fields for list mode"),
    ({**CARDS, "key_field": "nope"}, "key_field must be one of the columns"),
    ({**CARDS, "login": {"selector": ""}}, "extractor.login"),
])
def test_invalid_configs_are_rejected(cfg, msg):
    with pytest.raises(ex.ExtractorError, match=msg):
        ex.validate(cfg)


# ------------------------------------------------------------------ v1 conversions in real Chromium

def test_v1_regex_number_and_transform_on_the_real_dom(page):
    html = """<table id="t"><tr><th>Item</th><th>Price</th><th>Status</th><th>Stock</th></tr>
      <tr><td>sku-001 Backpack</td><td>¥1,299.00 (incl. VAT)</td><td>IN STOCK</td><td>3 left</td></tr>
      <tr><td>sku-002 Light</td><td>¥ 99.5</td><td>Sold Out</td><td>—</td></tr></table>"""
    cfg = {"table": {"selector": "#t"}, "key_field": "sku", "columns": [
        {"field": "sku", "headers": ["Item"], "transform": "upper", "pattern": r"SKU-\d+"},
        {"field": "price", "headers": ["Price"], "type": "number", "pattern": r"([\d,]+(\.\d+)?)", "group": 1},
        {"field": "status", "headers": ["Status"], "transform": "lower"},
        {"field": "stock", "headers": ["Stock"], "type": "int", "pattern": r"\d+", "default": 0}]}
    assert read(page, html, cfg) == {
        "SKU-001": {"price": 1299.0, "status": "in stock", "stock": 3},
        "SKU-002": {"price": 99.5, "status": "sold out", "stock": 0}}


def test_v1_without_a_default_an_unexpected_cell_is_schema_invalid(page):
    html = """<table id="t"><tr><th>Item</th><th>Stock</th></tr><tr><td>a</td><td>ask us</td></tr></table>"""
    cfg = {"table": {"selector": "#t"}, "key_field": "item", "columns": [
        {"field": "item", "headers": ["Item"]}, {"field": "stock", "headers": ["Stock"], "type": "int", "pattern": r"\d+"}]}
    with pytest.raises(ex.ExtractorError, match="pattern_no_match:stock"):
        read(page, html, cfg)


def test_a_table_without_thead_does_not_read_its_header_row_as_a_record(page):
    html = """<table id="t"><tr><th>Item</th><th>Qty</th></tr><tr><td>a</td><td>1</td></tr></table>"""
    cfg = {"table": {"selector": "#t"}, "key_field": "item", "columns": [
        {"field": "item", "headers": ["Item"]}, {"field": "qty", "headers": ["Qty"], "type": "int"}]}
    assert read(page, html, cfg) == {"a": {"qty": 1}}
