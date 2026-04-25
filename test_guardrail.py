"""Coverage for the empty-formula-deps guardrail in main.py.

The guardrail catches a real bug class — agents writing =DIVIDE(C5, B5) where
B5 is empty, producing #DIV/0!. But it has historically over-fired on benign
range aggregations like =SUM(A1:A100) when bookend cells happen to be empty.
These tests pin down both halves of that contract.
"""
import sys
from core.engine import GridOSKernel
import main


def _fresh():
    return GridOSKernel()


def _preview(cell: str, value):
    return {"cell": cell, "value": value}


# ---------- The guardrail SHOULD fire (real bugs) ----------

def test_guardrail_catches_divide_by_empty_standalone_ref():
    k = _fresh()
    k.write_user_cell("A1", 100)
    # B1 is empty — agent writes =A1/B1 expecting B1 to already be populated.
    preview = [_preview("C1", "=A1/B1")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert len(issues) == 1, f"DIVIDE by empty standalone ref must trigger, got {issues}"
    assert "B1" in issues[0]["empty_refs"]


def test_guardrail_catches_minus_with_empty_baseline():
    k = _fresh()
    k.write_user_cell("C4", 200)
    # =MINUS(C4, C3) — C3 empty would yield 200, misleading. Original bug
    # class: %-growth formulas that quietly read 0 from empty cells.
    preview = [_preview("D4", "=MINUS(C4, C3)")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert len(issues) == 1, "standalone empty ref should still trigger"
    assert "C3" in issues[0]["empty_refs"]


def test_guardrail_catches_multiple_empty_refs():
    k = _fresh()
    preview = [_preview("D1", "=A1+B1+C1")]  # all three refs empty
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert len(issues) == 1
    assert set(issues[0]["empty_refs"]) >= {"A1", "B1", "C1"}


# ---------- The guardrail should NOT fire (false-positive cases) ----------

def test_guardrail_allows_sum_over_range_with_empty_bookend():
    """=SUM(A1:A100) where A100 is empty should NOT trigger. This was the
    pilot's #1 failure mode — 4 of 25 questions hit this false positive."""
    k = _fresh()
    for i in range(1, 11):
        k.write_user_cell(f"A{i}", i)
    # A11..A100 are empty; SUM is still legal in Excel.
    preview = [_preview("B1", "=SUM(A1:A100)")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], f"SUM over range with empty cells should NOT trigger, got {issues}"


def test_guardrail_allows_countif_over_range_with_empties():
    k = _fresh()
    k.write_user_cell("A1", "x")
    k.write_user_cell("A2", "y")
    # A3..A50 empty; benchmark patterns use full-column ranges constantly.
    preview = [_preview("C1", '=COUNTIF(A1:A50, "x")')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "COUNTIF over partially-empty range should NOT trigger"


def test_guardrail_allows_sumifs_with_multiple_partial_ranges():
    k = _fresh()
    for i, (cat, val) in enumerate([("a", 10), ("b", 20), ("a", 30)], start=1):
        k.write_user_cell(f"A{i}", cat)
        k.write_user_cell(f"B{i}", val)
    # Range goes to row 100 but only first 3 are populated — typical bench
    # pattern when answer_position is large but actual data is sparse.
    preview = [_preview("D1", '=SUMIFS(B1:B100, A1:A100, "a")')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "SUMIFS over a partially-populated range should NOT trigger"


def test_guardrail_allows_average_over_range_with_text_cells_in_middle():
    """Mixed-type range (numbers + headers) — AVERAGE skips text. Empty cells
    in the range should also be ignored. Pilot's id=290-1 hit this pattern."""
    k = _fresh()
    k.write_user_cell("A1", "Header")  # text — AVERAGE skips
    k.write_user_cell("A2", 50)
    k.write_user_cell("A3", 100)
    # A4..A20 empty
    preview = [_preview("B1", "=AVERAGE(A1:A20)")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "AVERAGE with empties + headers should NOT trigger"


def test_guardrail_skips_cross_sheet_refs():
    """The same-sheet guard ignores cross-sheet refs (verified in earlier
    fix). Belt-and-suspenders test."""
    k = _fresh()
    k.create_sheet("Source")
    preview = [_preview("A1", '=SUM(Source!A1:A100)')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "cross-sheet refs must be invisible to the same-sheet guard"


def test_guardrail_ignores_cell_refs_inside_string_literals():
    """Real V2 pilot bug: =IF(A2=\"T9A\", ...) — the regex pulled T9 out of
    the string literal and flagged it as an empty same-sheet ref. Stripping
    string literals first eliminates this whole class."""
    k = _fresh()
    k.write_user_cell("A2", "@9T")
    # T9 inside the string "T9A" — must not register as a cell ref.
    preview = [_preview("E2", '=IF(OR(A2="@9T", A2="SAL", A2="T9A"), "", A2)')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], (
        f'string literal "T9A" should not yield empty-ref T9, got {issues}'
    )


def test_guardrail_ignores_double_quoted_currency_codes():
    """Same shape: currency codes / SKU patterns inside string literals."""
    k = _fresh()
    k.write_user_cell("A1", 100)
    preview = [_preview("B1", '=IF(A1="USD100", "match", "miss")')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == []


def test_guardrail_still_catches_outside_quotes():
    """Sanity: the string-literal stripper doesn't accidentally hide a real
    bare ref that happens to follow a string."""
    k = _fresh()
    # B5 is empty, and it appears OUTSIDE any string — still a real bug.
    preview = [_preview("D1", '=IF(A1="hello", B5, 0)')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert len(issues) == 1, "B5 outside the string should still trigger"
    assert "B5" in issues[0]["empty_refs"]


def test_guardrail_handles_cross_sheet_with_absolute_refs():
    """Real V2 pilot bug (267-21): VLOOKUP(B2, SH1!B$2:C$8, 2, FALSE) had $
    in the cell ref so the cross-sheet stripper bailed and left SH1 exposed.
    The standalone cell-ref regex then matched SH1 (sheet name!) and flagged
    it as 'empty'. Stripping $ before the cross-sheet pass closes this."""
    k = _fresh()
    k.create_sheet("SH1")
    k.write_user_cell("B2", "key", sheet_name="SH1")
    k.write_user_cell("C2", 100, sheet_name="SH1")
    preview = [_preview(
        "C2",
        '=IFERROR(VLOOKUP(B2, SH1!B$2:C$8, 2, FALSE), "-")'
    )]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], (
        f"cross-sheet ref with absolute markers should not flag the sheet "
        f"name as an empty cell, got {issues}"
    )


def test_guardrail_handles_same_sheet_absolute_refs():
    """=$A$1+$B$1 with both populated should not trigger. The $ stripping
    only kicks in for the cell-ref scan; the engine still understands
    absolutes via _normalize_excel_formula."""
    k = _fresh()
    k.write_user_cell("A1", 10)
    k.write_user_cell("B1", 20)
    preview = [_preview("C1", "=$A$1+$B$1")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "absolute refs to populated cells should not trigger"


def test_guardrail_catches_absolute_ref_to_empty_cell():
    """Belt-and-suspenders: $-stripping doesn't accidentally hide a real
    empty-ref bug just because the agent used absolute notation."""
    k = _fresh()
    k.write_user_cell("A1", 100)
    # $B$1 is empty — bug class is preserved
    preview = [_preview("C1", "=$A$1/$B$1")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert len(issues) == 1
    assert "B1" in issues[0]["empty_refs"]


def test_guardrail_skips_iferror_wrapped_formulas():
    """Real V2 pilot bug (267-21): the agent wrote
        =IFERROR(VLOOKUP(B9, SH1!$B$2:$C$8, 2, FALSE), "-")
    where B9 was empty in the destination sheet. Guardrail flagged B9, but
    the WHOLE POINT of IFERROR is to handle that case — the formula returns
    \"-\" for empty source rows, which is the agent's intent. Don't fire on
    formulas explicitly wrapped in IFERROR/IFNA."""
    k = _fresh()
    # B9 deliberately empty
    preview = [_preview("C9", '=IFERROR(VLOOKUP(B9, SH1!$B$2:$C$8, 2, FALSE), "-")')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], f"IFERROR-wrapped formula should be skipped, got {issues}"


def test_guardrail_skips_ifna_wrapped_formulas():
    k = _fresh()
    preview = [_preview("C9", '=IFNA(VLOOKUP(B9, A1:Z100, 2, FALSE), "missing")')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == []


def test_guardrail_iferror_skip_is_case_insensitive():
    k = _fresh()
    preview = [_preview("D1", '=iferror(B5/C5, 0)')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "lowercase iferror should also be respected"


def test_guardrail_does_not_skip_inner_iferror_with_outer_arithmetic():
    """Subtle case: =A1+IFERROR(B1, 0) where A1 is empty. The IFERROR only
    wraps B1, not the whole formula — A1 being empty is still a bug.
    Our regex matches `^\\s*=\\s*(IFERROR|IFNA)\\s*\\(` which only fires on
    formulas that are FULLY wrapped, so this case still triggers the guard."""
    k = _fresh()
    # A1 empty, B1 empty; IFERROR is inner — outer arithmetic ref to A1
    # should still flag.
    preview = [_preview("C1", "=A1+IFERROR(B1, 0)")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert len(issues) == 1, "inner IFERROR shouldn't shield outer empty refs"
    assert "A1" in issues[0]["empty_refs"]


def test_guardrail_skips_concatenate_with_empty_args():
    """V2 pilot 267-18: agent wrote CONCATENATE("//[", E1, "]"). E1 was
    empty in the destination sheet — Excel returns the literal text with
    an empty in the middle, which is the correct semantic. Don't fire."""
    k = _fresh()
    preview = [_preview("G1", '=CONCATENATE("//[", E1, "]")')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "CONCATENATE with empty arg is benign, not a bug"


def test_guardrail_skips_textjoin_with_empty_args():
    k = _fresh()
    preview = [_preview("D1", '=TEXTJOIN(", ", TRUE, A1, B1, C1)')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == []


def test_guardrail_skips_sumproduct_with_empty_filter_ranges():
    """SUMPRODUCT is the typical home of array filter math like
    SUMPRODUCT((A2:A100=\"x\")*B2:B100). Empty cells inside those filter
    ranges are part of the pattern, not a bug."""
    k = _fresh()
    preview = [_preview("D1", '=SUMPRODUCT((A2:A100="x")*(B2:B100))')]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == []


def test_guardrail_handles_quoted_sheet_name_with_spaces():
    """Real V2 pilot bug (209-30): formula was
        =LEFT('Data to Import'!C2, LEN('Data to Import'!C2) - 3)
    where the destination 'Data to Import'!C2 was populated, but the active
    sheet's C2 was empty. The string-literal stripper ate 'Data to Import'
    as a string, leaving naked !C2 — and the standalone cell-ref regex then
    flagged C2 as empty on the wrong sheet. Fix: strip cross-sheet refs
    BEFORE strings."""
    k = _fresh()
    k.create_sheet("Data to Import")
    # Source sheet has C2 populated; active sheet's C2 is empty
    k.write_user_cell("C2", "abcdef", sheet_name="Data to Import")
    preview = [_preview("A1", "=LEFT('Data to Import'!C2, LEN('Data to Import'!C2) - 3)")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], (
        f"quoted-sheet-name with spaces should fully strip as cross-sheet, "
        f"not be misread as a string literal that exposes the trailing C2; "
        f"got {issues}"
    )


def test_guardrail_handles_quoted_sheet_name_with_absolute_refs():
    """Combined case: 'Data to Import'!$C$2 — quotes AROUND sheet name,
    plus $ absolute markers in the cell ref. Both fixes need to compose."""
    k = _fresh()
    k.create_sheet("Data to Import")
    k.write_user_cell("C2", 100, sheet_name="Data to Import")
    preview = [_preview("A1", "='Data to Import'!$C$2 + 5")]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], f"quoted sheet name with $ should strip cleanly, got {issues}"


def test_guardrail_combines_self_written_with_existing():
    """Multi-intent: intent#1 writes A1, intent#2's formula on B1 references
    A1. Both arrive in the same merged_preview_cells. No false positive."""
    k = _fresh()
    preview = [
        _preview("A1", 100),
        _preview("B1", "=A1*2"),
    ]
    issues = main._find_empty_formula_deps(preview, k._sheet_state(None))
    assert issues == [], "self-written non-empty ref must satisfy the guard"


# ---------- Run-all entry point ----------

def run_all():
    tests = {name: fn for name, fn in sorted(globals().items()) if name.startswith("test_")}
    passed = failed = 0
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
