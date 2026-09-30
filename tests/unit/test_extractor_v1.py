"""Extractor v1 conversions: regex + group, number, transform, explicit defaults."""
import pytest

ex = pytest.importorskip("wakecore_ui_runtime.browser.extractor")

BASE = {"table": {"label": "Listings"}, "key_field": "id", "columns": [{"field": "id", "headers": ["ID"]}]}


def cfg(*cols, key=None):
    c = {**BASE, "columns": [key or BASE["columns"][0], *cols]}
    return ex.validate(c)


def rows(*cells):
    return {"rows": [{"key": k, "cells": c} for k, c in cells]}


def test_pattern_group_and_number():
    c = cfg({"field": "price", "headers": ["Price"], "type": "number", "pattern": r"¥\s*([\d,.]+)", "group": 1},
            {"field": "stock", "headers": ["Stock"], "type": "int", "pattern": r"\d+"})
    out = ex.convert(c, rows(("A1", {"id": "A1", "price": "¥ 1,299.50 incl. tax", "stock": "only 3 left"})))
    assert out == {"A1": {"price": 1299.5, "stock": 3}}


@pytest.mark.parametrize("text,value", [("12", 12), ("-3.25", -3.25), ("1,000", 1000), ("1,234,567.8", 1234567.8)])
def test_numbers(text, value):
    c = cfg({"field": "n", "headers": ["N"], "type": "number"})
    assert ex.convert(c, rows(("k", {"id": "k", "n": text})))["k"]["n"] == value


@pytest.mark.parametrize("text", ["12abc", "1,00", "", "1.2.3", "∞"])
def test_bad_numbers_are_errors_not_guesses(text):
    c = cfg({"field": "n", "headers": ["N"], "type": "number"})
    with pytest.raises(ex.ExtractorError, match="not_a_number:n|cell_missing:n"):
        ex.convert(c, rows(("k", {"id": "k", "n": text})))


def test_transform_applies_before_pattern_and_to_the_key():
    c = cfg({"field": "state", "headers": ["State"], "transform": "lower", "pattern": "(open|closed)", "group": 1},
            key={"field": "id", "headers": ["ID"], "transform": "upper", "pattern": r"[A-Z]+-\d+"})
    out = ex.convert(c, rows(("ref: abc-12 (new)", {"id": "ref: abc-12 (new)", "state": "Now OPEN"})))
    assert out == {"ABC-12": {"state": "open"}}


def test_a_key_that_does_not_match_is_a_missing_key():
    c = cfg(key={"field": "id", "headers": ["ID"], "pattern": r"\d+"})
    with pytest.raises(ex.ExtractorError, match="row_key_missing_or_duplicate"):
        ex.convert(c, rows(("none here", {"id": "none here"})))


def test_no_match_without_default_is_an_error():
    c = cfg({"field": "s", "headers": ["S"], "pattern": r"\d+"})
    with pytest.raises(ex.ExtractorError, match="pattern_no_match:s"):
        ex.convert(c, rows(("k", {"id": "k", "s": "sold out"})))


def test_declared_defaults_fill_only_what_is_missing_or_unmatched():
    c = cfg({"field": "seats", "headers": ["Seats"], "type": "int", "pattern": r"\d+", "default": 0},
            {"field": "note", "headers": ["Note"], "default": None})
    out = ex.convert(c, rows(("a", {"id": "a", "seats": "full", "note": None}),
                             ("b", {"id": "b", "seats": "4 seats", "note": "x"})))
    assert out == {"a": {"seats": 0, "note": None}, "b": {"seats": 4, "note": "x"}}


def test_a_default_does_not_hide_a_type_error_after_a_match():
    c = cfg({"field": "n", "headers": ["N"], "type": "int", "default": 0})
    assert ex.convert(c, rows(("k", {"id": "k", "n": "seven"})))["k"]["n"] == 0   # unparseable -> declared default
    c = cfg({"field": "n", "headers": ["N"], "type": "int"})
    with pytest.raises(ex.ExtractorError, match="not_an_int:n"):
        ex.convert(c, rows(("k", {"id": "k", "n": "seven"})))


def test_bool_words():
    c = cfg({"field": "ok", "headers": ["OK"], "type": "bool", "true": ["有"], "false": ["无"]})
    out = ex.convert(c, rows(("a", {"id": "a", "ok": "有"}), ("b", {"id": "b", "ok": "无"})))
    assert out == {"a": {"ok": True}, "b": {"ok": False}}


@pytest.mark.parametrize("col,msg", [
    ({"type": "exists"}, "list-mode"),
    ({"transform": "title"}, "transform"),
    ({"pattern": "("}, "does not compile"),
    ({"pattern": "(a)", "group": 2}, "group must be 0..1"),
    ({"group": 1}, "group needs a pattern"),
    ({"true": "yes"}, "list of strings"),
    ({"type": "int", "default": "x"}, "default"),
    ({"type": "number", "default": True}, "default"),
])
def test_validation(col, msg):
    with pytest.raises(ex.ExtractorError, match=msg):
        cfg({"field": "c", "headers": ["C"], **col})


def test_the_key_column_takes_no_default():
    with pytest.raises(ex.ExtractorError, match="default"):
        cfg(key={"field": "id", "headers": ["ID"], "default": "x"})


def test_v03_configs_are_unchanged():
    old = {"table": {"label": "课程"}, "key_field": "code",
           "columns": [{"field": "code", "headers": ["课程号"]}, {"field": "seats", "headers": ["余量"], "type": "int"},
                       {"field": "open", "headers": ["状态"], "type": "bool", "true": ["可选"], "false": ["已满"]}]}
    assert ex.validate(old) == old
    assert ex.convert(old, rows(("P", {"code": "P", "seats": "3", "open": "可选"}))) == {"P": {"seats": 3, "open": True}}


def test_cli(tmp_path, capsys):
    import json
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text(json.dumps(BASE))
    bad.write_text(json.dumps({**BASE, "columns": []}))
    assert ex.main([str(good)]) == 0 and "ok: table mode, 1 columns" in capsys.readouterr().out
    assert ex.main([str(bad)]) == 1
    assert ex.main([]) == 2
