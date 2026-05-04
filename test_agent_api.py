"""End-to-end tests for the AI-agent API surface: /eval, /schema, /peek.

No live server required — uses FastAPI TestClient against the imported app.
Run with: python test_agent_api.py

Also unit-tests _find_text_ref_issues (the pre-write text-cell guardrail) by
importing the helper directly.
"""

import os
import sys
from pathlib import Path

# OSS mode bypasses the JWT requirement on require_user. Set BEFORE the import
# so cloud_config picks it up at module load.
os.environ.setdefault("SAAS_MODE", "false")

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient

import main as gridos_main
from main import app, _find_text_ref_issues, _split_eval_result

client = TestClient(app)


# --- helpers ---


def _reset():
    """Clear the workbook between tests so seeded state from earlier tests
    doesn't leak in. Kernel pool persists across requests in OSS mode, so the
    same singleton answers every TestClient call."""
    gridos_main.kernel.clear_unlocked()
    # Drop extra sheets created by tests so each test starts from a single
    # active sheet. clear_unlocked only wipes cell contents.
    for s in list(gridos_main.kernel.sheets.keys()):
        if s != gridos_main.kernel.active_sheet:
            try:
                gridos_main.kernel.delete_sheet(s)
            except Exception:
                pass


def _seed_cell(a1, value, sheet=None):
    payload = {"cell": a1, "value": str(value)}
    if sheet:
        payload["sheet"] = sheet
    res = client.post("/grid/cell", json=payload)
    assert res.status_code == 200, res.text


# --- /eval ---


def test_eval_happy_path():
    _reset()
    _seed_cell("A1", 10)
    _seed_cell("A2", 20)
    res = client.post("/eval", json={"formulas": [
        {"cell": "A3", "formula": "=A1+A2"},
        {"cell": "A4", "formula": "=SUM(A1:A2)"},
    ]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sheet"]
    by_cell = {r["cell"]: r for r in body["results"]}
    assert by_cell["A3"]["result"] == 30
    assert by_cell["A3"]["error"] is None
    assert by_cell["A4"]["result"] == 30


def test_eval_no_persistence():
    """The crown jewel: /eval evaluates against current state but mustn't
    write anything. After /eval, A3 should still be empty in the kernel."""
    _reset()
    _seed_cell("A1", 5)
    res = client.post("/eval", json={"formulas": [
        {"cell": "A3", "formula": "=A1*100"},
    ]})
    assert res.status_code == 200
    assert res.json()["results"][0]["result"] == 500
    grid = client.get("/debug/grid").json()
    assert "A3" not in grid, f"A3 should not exist after dry-run; grid={grid}"


def test_eval_div_by_zero():
    _reset()
    _seed_cell("A1", 10)
    _seed_cell("A2", 0)
    res = client.post("/eval", json={"formulas": [
        {"cell": "B1", "formula": "=A1/A2"},
    ]})
    assert res.status_code == 200
    out = res.json()["results"][0]
    assert out["result"] is None
    assert out["error"] == "#DIV/0!", f"unexpected error: {out['error']}"


def test_eval_parse_error_when_no_equals():
    _reset()
    # Leading '=' is normalized in. A truly broken formula triggers parse-error.
    res = client.post("/eval", json={"formulas": [
        {"cell": "A1", "formula": "=SUM("},
    ]})
    assert res.status_code == 200
    out = res.json()["results"][0]
    assert out["error"] is not None
    assert out["error"].startswith("#")


def test_eval_invalid_a1():
    _reset()
    res = client.post("/eval", json={"formulas": [
        {"cell": "not-a-cell", "formula": "=1+1"},
    ]})
    assert res.status_code == 200
    out = res.json()["results"][0]
    assert out["error"] is not None
    assert "REF" in out["error"]


def test_eval_unknown_sheet_404s():
    _reset()
    res = client.post("/eval", json={
        "formulas": [{"cell": "A1", "formula": "=1+1"}],
        "sheet": "NoSuchSheet",
    })
    assert res.status_code == 404


def test_eval_oversized_request():
    _reset()
    big = [{"cell": "A1", "formula": "=1"}] * 600  # over the 500 cap
    res = client.post("/eval", json={"formulas": big})
    assert res.status_code == 413


def test_eval_empty_list():
    _reset()
    res = client.post("/eval", json={"formulas": []})
    assert res.status_code == 200
    assert res.json()["results"] == []


def test_split_eval_result_unit():
    """Direct unit-test of the error-vs-value splitter."""
    assert _split_eval_result(42) == (42, None)
    assert _split_eval_result("hello") == ("hello", None)
    assert _split_eval_result("#DIV/0!") == (None, "#DIV/0!")
    assert _split_eval_result("#PARSE_ERROR!") == (None, "#PARSE_ERROR!")
    assert _split_eval_result("#REF!") == (None, "#REF!")
    assert _split_eval_result(None) == (None, None)


# --- /schema ---


def test_schema_empty_workbook():
    _reset()
    res = client.get("/schema")
    assert res.status_code == 200
    body = res.json()
    assert "sheets" in body
    assert body["sheets"][0]["rows"] == 0
    assert body["sheets"][0]["cols"] == []


def test_schema_with_headers_and_data():
    _reset()
    _seed_cell("A1", "Month")
    _seed_cell("B1", "Revenue")
    _seed_cell("C1", "Growth")
    _seed_cell("A2", "Jan")
    _seed_cell("B2", 1000)
    _seed_cell("C2", "=B2*1.1")
    _seed_cell("A3", "Feb")
    _seed_cell("B3", 1100)
    _seed_cell("C3", "=B3*1.1")

    res = client.get("/schema")
    assert res.status_code == 200
    body = res.json()
    sheet0 = body["sheets"][0]
    assert sheet0["rows"] == 3
    by_col = {c["col"]: c for c in sheet0["cols"]}
    assert by_col["A"]["header"] == "Month"
    assert by_col["A"]["type"] == "text"
    assert by_col["B"]["header"] == "Revenue"
    assert by_col["B"]["type"] == "number"
    assert by_col["C"]["header"] == "Growth"
    assert by_col["C"]["type"] == "formula"
    assert sheet0["occupied_bounds"]["first"] == "A1"
    assert sheet0["occupied_bounds"]["last"] == "C3"


# --- /peek ---


def test_peek_csv_format():
    _reset()
    _seed_cell("A1", "x")
    _seed_cell("B1", "y")
    _seed_cell("A2", 1)
    _seed_cell("B2", 2)
    res = client.get("/peek", params={"range": "A1:B2"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert res.text == "x,y\n1,2"


def test_peek_tsv_format():
    _reset()
    _seed_cell("A1", "a")
    _seed_cell("B1", "b")
    res = client.get("/peek", params={"range": "A1:B1", "format": "tsv"})
    assert res.status_code == 200
    assert "tab-separated" in res.headers["content-type"]
    assert res.text == "a\tb"


def test_peek_json_format():
    _reset()
    _seed_cell("A1", 10)
    _seed_cell("B1", 20)
    res = client.get("/peek", params={"range": "A1:B1", "format": "json"})
    assert res.status_code == 200
    body = res.json()
    assert body["range"] == "A1:B1"
    assert body["rows"] == [[10, 20]]


def test_peek_csv_quotes_fields_with_commas():
    _reset()
    _seed_cell("A1", "Smith, John")
    _seed_cell("B1", "ok")
    res = client.get("/peek", params={"range": "A1:B1"})
    assert res.status_code == 200
    assert res.text == '"Smith, John",ok'


def test_peek_empty_cells_render_as_empty_string():
    _reset()
    _seed_cell("A1", 1)
    # B1, A2, B2 all empty
    res = client.get("/peek", params={"range": "A1:B2"})
    assert res.status_code == 200
    # csv: row 1 is "1,", row 2 is ","
    assert res.text == "1,\n,"


def test_peek_bad_range_400s():
    _reset()
    res = client.get("/peek", params={"range": "not-a-range"})
    assert res.status_code == 400


def test_peek_oversized_range_413s():
    _reset()
    # 200 rows × 6 cols = 1200 cells, over the 1000 cap.
    res = client.get("/peek", params={"range": "A1:F200"})
    assert res.status_code == 413


def test_peek_unknown_sheet_404s():
    _reset()
    res = client.get("/peek", params={"range": "A1:B2", "sheet": "NoSuchSheet"})
    assert res.status_code == 404


# --- _find_text_ref_issues (Phase 2.1 wire-up) ---


def test_text_ref_helper_blocks_label_column_deref():
    _reset()
    _seed_cell("A1", "Q1")  # label column — text
    _seed_cell("B1", 100)
    state = gridos_main.kernel._sheet_state(None)
    # Agent proposes =A1*2 in C1 — referencing the label column. Should flag.
    issues = _find_text_ref_issues([{"cell": "C1", "value": "=A1*2"}], state)
    assert len(issues) == 1
    assert issues[0]["cell"] == "C1"
    assert "A1" in issues[0]["bad_refs"]


def test_text_ref_helper_allows_numeric_deref():
    _reset()
    _seed_cell("B1", 100)
    state = gridos_main.kernel._sheet_state(None)
    issues = _find_text_ref_issues([{"cell": "C1", "value": "=B1*2"}], state)
    assert issues == []


def test_text_ref_helper_skips_iferror_wrap():
    _reset()
    _seed_cell("A1", "label")
    state = gridos_main.kernel._sheet_state(None)
    # IFERROR explicitly handles non-numeric — guard should not flag.
    issues = _find_text_ref_issues(
        [{"cell": "C1", "value": "=IFERROR(A1*2, 0)"}], state
    )
    assert issues == []


def test_text_ref_helper_skips_self_overwritten_cell():
    _reset()
    _seed_cell("A1", "old label")
    state = gridos_main.kernel._sheet_state(None)
    # Same preview is overwriting A1 with a number AND writing a formula
    # that references A1. The agent has self-corrected — don't flag.
    issues = _find_text_ref_issues([
        {"cell": "A1", "value": 100},
        {"cell": "C1", "value": "=A1*2"},
    ], state)
    assert issues == []


# --- runner ---


_TESTS = [v for k, v in dict(globals()).items() if k.startswith("test_")]


def main():
    failed = []
    for t in _TESTS:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed.append(t.__name__)
        except Exception as e:
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
            failed.append(t.__name__)
    print()
    print(f"{len(_TESTS) - len(failed)} / {len(_TESTS)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
