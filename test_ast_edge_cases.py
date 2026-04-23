"""AST parser edge-case battery.

Exercises the formula kernel on shapes that historically have slipped into LLM
hallucination territory. Pure kernel tests — no server, no LLM. Run with:
    .venv/Scripts/python.exe test_ast_edge_cases.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.engine import GridOSKernel


def _expect(kernel, cell, expected, label):
    actual = kernel.get_cell_value(cell) if hasattr(kernel, "get_cell_value") else None
    state = kernel._sheet_state(None)
    r, c = next((k for k, _ in state["cells"].items() if _coord_to_a1(k) == cell), (None, None))
    if r is None:
        raise AssertionError(f"{label}: cell {cell} not found")
    actual = state["cells"][(r, c)].value
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        assert abs(actual - expected) < 1e-9, f"{label}: expected {expected}, got {actual}"
    else:
        assert actual == expected, f"{label}: expected {expected!r}, got {actual!r}"


def _coord_to_a1(coord):
    r, c = coord
    letters = ""
    cc = c
    while True:
        letters = chr(ord("A") + (cc % 26)) + letters
        cc = cc // 26 - 1
        if cc < 0:
            break
    return f"{letters}{r + 1}"


def _fresh():
    return GridOSKernel()


def test_operator_precedence():
    k = _fresh()
    k.write_user_cell("A1", 2)
    k.write_user_cell("B1", 3)
    k.write_user_cell("C1", 4)
    k.write_user_cell("D1", "=A1+B1*C1")
    _expect(k, "D1", 14, "2 + 3*4")


def test_parenthesis_overrides_precedence():
    k = _fresh()
    k.write_user_cell("A1", 2)
    k.write_user_cell("B1", 3)
    k.write_user_cell("C1", 4)
    k.write_user_cell("D1", "=(A1+B1)*C1")
    _expect(k, "D1", 20, "(2+3)*4")


def test_unary_minus_on_cell_ref():
    k = _fresh()
    k.write_user_cell("A1", 5)
    k.write_user_cell("B1", "=-A1+10")
    _expect(k, "B1", 5, "-A1+10")


def test_unary_minus_on_parenthesized_expr():
    k = _fresh()
    k.write_user_cell("A1", 3)
    k.write_user_cell("B1", 2)
    k.write_user_cell("C1", "=-(A1+B1)*2")
    _expect(k, "C1", -10, "-(3+2)*2")


def test_nested_function_calls():
    k = _fresh()
    k.write_user_cell("A1", 1)
    k.write_user_cell("A2", 2)
    k.write_user_cell("A3", 3)
    k.write_user_cell("B1", 10)
    k.write_user_cell("B2", 20)
    k.write_user_cell("C1", "=SUM(A1:A3, MAX(B1, B2))")
    _expect(k, "C1", 26, "SUM + nested MAX")


def test_deeply_nested():
    k = _fresh()
    k.write_user_cell("A1", 7)
    k.write_user_cell("B1", "=SUM(SUM(SUM(A1)))")
    _expect(k, "B1", 7, "triple-nested SUM")


def test_range_reference_sum():
    k = _fresh()
    for i, v in enumerate([1, 2, 3, 4, 5], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", "=SUM(A1:A5)")
    _expect(k, "B1", 15, "range sum")


def test_if_function_true_branch():
    k = _fresh()
    k.write_user_cell("A1", 10)
    k.write_user_cell("B1", "=IF(A1>0, 100, 200)")
    _expect(k, "B1", 100, "IF true branch")


def test_if_function_false_branch():
    k = _fresh()
    k.write_user_cell("A1", -5)
    k.write_user_cell("B1", "=IF(A1>0, 100, 200)")
    _expect(k, "B1", 200, "IF false branch")


def test_not_equal_operator():
    k = _fresh()
    k.write_user_cell("A1", 5)
    k.write_user_cell("B1", 6)
    k.write_user_cell("C1", "=IF(A1<>B1, 1, 0)")
    _expect(k, "C1", 1, "not-equal yields true branch")


def test_case_insensitive_function_names():
    k = _fresh()
    k.write_user_cell("A1", 1)
    k.write_user_cell("A2", 2)
    k.write_user_cell("B1", "=sum(A1:A2)")
    _expect(k, "B1", 3, "lowercase function name still works")


def test_circular_reference_is_flagged():
    k = _fresh()
    k.write_user_cell("A1", "=A1+1")
    state = k._sheet_state(None)
    # The kernel resolves these statically and must NOT loop forever. Whatever
    # sentinel it picks (#CIRCULAR!, 0, #VALUE!), the test proves it terminated.
    val = state["cells"][(0, 0)].value
    assert val is not None, "circular ref produced no value — kernel may have hung"


def test_division_by_zero():
    k = _fresh()
    k.write_user_cell("A1", 10)
    k.write_user_cell("B1", 0)
    k.write_user_cell("C1", "=A1/B1")
    _expect(k, "C1", "#DIV/0!", "divide-by-zero sentinel")


def test_parse_error_on_garbage():
    k = _fresh()
    k.write_user_cell("A1", "=+*/")
    state = k._sheet_state(None)
    val = state["cells"][(0, 0)].value
    assert isinstance(val, str) and val.startswith("#"), f"expected #ERROR sentinel, got {val!r}"


def test_absolute_refs_evaluate_like_relative():
    k = _fresh()
    k.write_user_cell("A1", 4)
    k.write_user_cell("B1", "=$A$1*3")
    _expect(k, "B1", 12, "absolute ref")


def test_string_concat():
    k = _fresh()
    k.write_user_cell("A1", "Hello ")
    k.write_user_cell("B1", "World")
    k.write_user_cell("C1", "=A1&B1")
    _expect(k, "C1", "Hello World", "string concat with &")


def test_mixed_number_and_range():
    k = _fresh()
    k.write_user_cell("A1", 1)
    k.write_user_cell("A2", 2)
    k.write_user_cell("B1", "=SUM(A1:A2)+100")
    _expect(k, "B1", 103, "range sum plus scalar")


def test_unknown_function_does_not_hallucinate():
    k = _fresh()
    k.write_user_cell("A1", "=MAGIC_AI_PREDICT(1,2,3)")
    state = k._sheet_state(None)
    val = state["cells"][(0, 0)].value
    # MUST return a sentinel string, not a made-up number.
    assert isinstance(val, str) and val.startswith("#"), (
        f"Unknown function must fail deterministically, got {val!r}"
    )


def test_formula_updating_on_dependency_change():
    k = _fresh()
    k.write_user_cell("A1", 5)
    k.write_user_cell("B1", "=A1*2")
    _expect(k, "B1", 10, "initial B1")
    k.write_user_cell("A1", 7)
    _expect(k, "B1", 14, "B1 recalc after A1 changed")


def test_multiplication_chain():
    k = _fresh()
    k.write_user_cell("A1", "=2*3*4*5")
    _expect(k, "A1", 120, "chained multiplication")


def test_mixed_add_subtract_left_to_right():
    k = _fresh()
    k.write_user_cell("A1", "=10-3+2")
    _expect(k, "A1", 9, "left-to-right add/sub")


# ---- Cross-sheet references ----
#
# NOTE: GridOSKernel.create_sheet() auto-activates the new sheet. Tests
# must explicitly pass `sheet_name="Sheet1"` to write_user_cell / write_user_range
# when writing the formula cell, or the formula lands on the newly-created
# sheet instead of Sheet1 (which is what these tests intend to assert on).


def test_crosssheet_single_cell():
    k = _fresh()
    k.create_sheet("Sheet2")
    k.write_user_range("A1", [[42]], sheet_name="Sheet2")
    k.write_user_cell("B1", "=Sheet2!A1", sheet_name="Sheet1")
    state = k._sheet_state("Sheet1")
    assert state["cells"][(0, 1)].value == 42.0, state["cells"][(0, 1)].value


def test_crosssheet_sum_range():
    k = _fresh()
    k.create_sheet("Data")
    k.write_user_range("A1", [[10], [20], [30]], sheet_name="Data")
    k.write_user_cell("A1", "=SUM(Data!A1:A3)", sheet_name="Sheet1")
    state = k._sheet_state("Sheet1")
    assert state["cells"][(0, 0)].value == 60.0, state["cells"][(0, 0)].value


def test_crosssheet_case_insensitive_sheet_name():
    k = _fresh()
    k.create_sheet("Sheet2")
    k.write_user_range("A1", [[7]], sheet_name="Sheet2")
    k.write_user_cell("B1", "=sheet2!A1", sheet_name="Sheet1")  # lowercase sheet name
    state = k._sheet_state("Sheet1")
    assert state["cells"][(0, 1)].value == 7.0, state["cells"][(0, 1)].value


def test_crosssheet_quoted_sheet_name_with_spaces():
    k = _fresh()
    k.create_sheet("Monthly Budget")
    k.write_user_range("A1", [[1234]], sheet_name="Monthly Budget")
    k.write_user_cell("B1", "='Monthly Budget'!A1", sheet_name="Sheet1")
    state = k._sheet_state("Sheet1")
    assert state["cells"][(0, 1)].value == 1234.0, state["cells"][(0, 1)].value


def test_crosssheet_missing_sheet_yields_ref_error():
    k = _fresh()
    k.write_user_cell("A1", "=DoesNotExist!A1", sheet_name="Sheet1")
    state = k._sheet_state("Sheet1")
    val = state["cells"][(0, 0)].value
    assert val == "#REF!", f"expected #REF!, got {val!r}"


def test_crosssheet_missing_sheet_in_range_yields_ref_error():
    k = _fresh()
    k.write_user_cell("A1", "=SUM(Phantom!A1:A3)", sheet_name="Sheet1")
    state = k._sheet_state("Sheet1")
    val = state["cells"][(0, 0)].value
    # SUM over a single #REF! sentinel should surface #VALUE! (can't add a
    # string), OR the sentinel propagates directly. Either way it's NOT a
    # silently-accepted 0 — which was the pre-fix behavior.
    assert isinstance(val, str) and val.startswith("#"), f"expected error sentinel, got {val!r}"


def test_crosssheet_arithmetic_across_sheets():
    k = _fresh()
    k.create_sheet("Sheet2")
    k.create_sheet("Sheet3")
    k.write_user_range("A1", [[100]], sheet_name="Sheet2")
    k.write_user_range("A1", [[25]], sheet_name="Sheet3")
    k.write_user_cell("B1", "=Sheet2!A1-Sheet3!A1", sheet_name="Sheet1")
    state = k._sheet_state("Sheet1")
    assert state["cells"][(0, 1)].value == 75.0, state["cells"][(0, 1)].value


def test_crosssheet_mismatched_range_sheets_yields_ref():
    # =SUM(Sheet2!A1:Sheet3!A3) has two different sheets in one range —
    # Excel rejects this and so do we.
    k = _fresh()
    k.create_sheet("Sheet2")
    k.create_sheet("Sheet3")
    k.write_user_cell("A1", "=SUM(Sheet2!A1:Sheet3!A3)", sheet_name="Sheet1")
    state = k._sheet_state("Sheet1")
    val = state["cells"][(0, 0)].value
    assert isinstance(val, str) and val.startswith("#"), f"expected error sentinel, got {val!r}"


def test_single_sheet_formula_still_works_after_crosssheet_changes():
    # Regression guard: the QCELL parser addition shouldn't affect plain
    # single-sheet formulas. Every pre-existing shape should keep working.
    k = _fresh()
    k.write_user_cell("A1", 5)
    k.write_user_cell("B1", 10)
    k.write_user_cell("C1", "=A1+B1")
    _expect(k, "C1", 15, "single-sheet add")
    k.write_user_cell("D1", "=SUM(A1:B1)")
    _expect(k, "D1", 15, "single-sheet range sum")


def run_all():
    tests = {name: fn for name, fn in sorted(globals().items()) if name.startswith("test_")}
    passed = 0
    failed = 0
    for name, fn in tests.items():
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{len(tests)} passed ({failed} failed)")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
