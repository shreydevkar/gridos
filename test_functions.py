"""Coverage for the formula library expansion shipped with the SpreadsheetBench
prep work: counts, conditional aggregations, dates, strings, lookups, misc.

Tests exercise the kernel end-to-end (write_user_cell → recalc → read value)
rather than calling functions directly. That confirms the engine still parses
and dispatches correctly with the new arg-shape for ranges (_RangeValues kept
as a single argument instead of flattened into scalars).
"""
import sys
from core.engine import GridOSKernel


def _fresh():
    return GridOSKernel()


def _val(k, a1, sheet=None):
    state = k._sheet_state(sheet)
    coords_to_a1 = __import__("core.utils", fromlist=["a1_to_coords"]).a1_to_coords
    r, c = coords_to_a1(a1)
    cell = state["cells"].get((r, c))
    return cell.value if cell else None


def _expect(k, a1, want, label):
    got = _val(k, a1)
    if isinstance(want, float) and isinstance(got, (int, float)):
        assert abs(got - want) < 1e-6, f"{label}: expected {want}, got {got}"
    else:
        assert got == want, f"{label}: expected {want!r}, got {got!r}"


# ---------- Mixed-type ranges (the engine refactor) ----------

def test_sum_ignores_text_in_range():
    k = _fresh()
    k.write_user_cell("A1", 10)
    k.write_user_cell("A2", "hello")
    k.write_user_cell("A3", 20)
    k.write_user_cell("B1", "=SUM(A1:A3)")
    _expect(k, "B1", 30, "SUM skips text cells")


def test_sum_ignores_blank_cells():
    k = _fresh()
    k.write_user_cell("A1", 5)
    k.write_user_cell("A3", 10)  # A2 left blank
    k.write_user_cell("B1", "=SUM(A1:A3)")
    _expect(k, "B1", 15, "SUM treats blank as missing, not zero")


def test_average_divides_by_numeric_count_not_total():
    k = _fresh()
    k.write_user_cell("A1", 10)
    k.write_user_cell("A2", "skip me")
    k.write_user_cell("A3", 20)
    k.write_user_cell("B1", "=AVERAGE(A1:A3)")
    _expect(k, "B1", 15, "AVERAGE divides by numeric count, not range size")


# ---------- COUNT family ----------

def test_count_only_numerics():
    k = _fresh()
    k.write_user_cell("A1", 1)
    k.write_user_cell("A2", "two")
    k.write_user_cell("A3", 3)
    k.write_user_cell("B1", "=COUNT(A1:A3)")
    _expect(k, "B1", 2, "COUNT counts only numerics")


def test_counta_counts_non_empty():
    k = _fresh()
    k.write_user_cell("A1", 1)
    k.write_user_cell("A2", "text")
    # A3 blank
    k.write_user_cell("B1", "=COUNTA(A1:A3)")
    _expect(k, "B1", 2, "COUNTA counts any non-empty")


def test_countblank():
    k = _fresh()
    k.write_user_cell("A1", 1)
    # A2, A3 blank
    k.write_user_cell("B1", "=COUNTBLANK(A1:A3)")
    _expect(k, "B1", 2, "COUNTBLANK counts empty cells")


def test_countif_numeric_predicate():
    k = _fresh()
    for i, v in enumerate([5, 10, 15, 20, 25], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", '=COUNTIF(A1:A5, ">10")')
    _expect(k, "B1", 3, "COUNTIF >10 → 15,20,25")


def test_countif_text_match_case_insensitive():
    k = _fresh()
    k.write_user_cell("A1", "apple")
    k.write_user_cell("A2", "BANANA")
    k.write_user_cell("A3", "Apple")
    k.write_user_cell("B1", '=COUNTIF(A1:A3, "apple")')
    _expect(k, "B1", 2, "COUNTIF text match is case-insensitive")


def test_countif_wildcard():
    k = _fresh()
    k.write_user_cell("A1", "alpha")
    k.write_user_cell("A2", "beta")
    k.write_user_cell("A3", "alps")
    k.write_user_cell("B1", '=COUNTIF(A1:A3, "al*")')
    _expect(k, "B1", 2, "COUNTIF wildcard al*")


def test_countifs_two_conditions():
    k = _fresh()
    for i, (a, b) in enumerate([(10, "x"), (20, "y"), (10, "y"), (30, "y")], start=1):
        k.write_user_cell(f"A{i}", a)
        k.write_user_cell(f"B{i}", b)
    k.write_user_cell("C1", '=COUNTIFS(A1:A4, ">=20", B1:B4, "y")')
    _expect(k, "C1", 2, "COUNTIFS A>=20 AND B='y' → rows 2 and 4")


# ---------- SUMIF / SUMIFS / AVERAGEIF / MAXIFS / MINIFS ----------

def test_sumif_implicit_sum_range():
    k = _fresh()
    for i, v in enumerate([10, 20, 30, 40], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", '=SUMIF(A1:A4, ">15")')
    _expect(k, "B1", 90, "SUMIF >15 → 20+30+40")


def test_sumif_with_separate_sum_range():
    k = _fresh()
    for i, (cat, amt) in enumerate([("food", 10), ("rent", 800), ("food", 25), ("food", 5)], start=1):
        k.write_user_cell(f"A{i}", cat)
        k.write_user_cell(f"B{i}", amt)
    k.write_user_cell("C1", '=SUMIF(A1:A4, "food", B1:B4)')
    _expect(k, "C1", 40, "SUMIF over a separate sum_range")


def test_sumifs_three_criteria():
    k = _fresh()
    rows = [
        ("food", "lunch", 10),
        ("food", "dinner", 30),
        ("rent", "monthly", 800),
        ("food", "lunch", 12),
    ]
    for i, (cat, kind, amt) in enumerate(rows, start=1):
        k.write_user_cell(f"A{i}", cat)
        k.write_user_cell(f"B{i}", kind)
        k.write_user_cell(f"C{i}", amt)
    k.write_user_cell("D1", '=SUMIFS(C1:C4, A1:A4, "food", B1:B4, "lunch")')
    _expect(k, "D1", 22, "SUMIFS food+lunch → 10+12")


def test_averageif():
    k = _fresh()
    for i, v in enumerate([5, 10, 100, 1000], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", '=AVERAGEIF(A1:A4, "<=100")')
    _expect(k, "B1", (5 + 10 + 100) / 3, "AVERAGEIF below threshold")


def test_maxifs():
    k = _fresh()
    for i, (cat, v) in enumerate([("a", 10), ("b", 99), ("a", 50)], start=1):
        k.write_user_cell(f"A{i}", cat)
        k.write_user_cell(f"B{i}", v)
    k.write_user_cell("C1", '=MAXIFS(B1:B3, A1:A3, "a")')
    _expect(k, "C1", 50, "MAXIFS within category 'a'")


# ---------- Date arithmetic ----------

def test_date_constructor_serial():
    k = _fresh()
    k.write_user_cell("A1", "=DATE(2024, 1, 1)")
    # Excel serial for 2024-01-01 is 45292
    _expect(k, "A1", 45292, "DATE(2024,1,1) = 45292")


def test_year_month_day_round_trip():
    k = _fresh()
    k.write_user_cell("A1", "=DATE(2025, 7, 15)")
    k.write_user_cell("B1", "=YEAR(A1)")
    k.write_user_cell("C1", "=MONTH(A1)")
    k.write_user_cell("D1", "=DAY(A1)")
    _expect(k, "B1", 2025, "YEAR")
    _expect(k, "C1", 7, "MONTH")
    _expect(k, "D1", 15, "DAY")


def test_datedif_years():
    k = _fresh()
    k.write_user_cell("A1", "=DATE(2020, 6, 1)")
    k.write_user_cell("A2", "=DATE(2025, 4, 25)")
    k.write_user_cell("B1", '=DATEDIF(A1, A2, "Y")')
    _expect(k, "B1", 4, "DATEDIF years (2020-06 to 2025-04 = 4 full years)")


def test_eomonth():
    k = _fresh()
    k.write_user_cell("A1", "=DATE(2024, 2, 15)")
    k.write_user_cell("B1", "=EOMONTH(A1, 0)")
    # Feb 2024 last day = 29 (leap year). Serial for 2024-02-29.
    k.write_user_cell("C1", "=DAY(B1)")
    _expect(k, "C1", 29, "EOMONTH gives last day of Feb 2024 (leap)")


def test_weekday():
    k = _fresh()
    # 2024-01-01 was a Monday
    k.write_user_cell("A1", "=DATE(2024, 1, 1)")
    k.write_user_cell("B1", "=WEEKDAY(A1, 2)")  # type 2: Mon=1
    _expect(k, "B1", 1, "WEEKDAY type-2 for Monday")


# ---------- Strings ----------

def test_left_right_mid():
    k = _fresh()
    k.write_user_cell("A1", "Hello World")
    k.write_user_cell("B1", "=LEFT(A1, 5)")
    k.write_user_cell("C1", "=RIGHT(A1, 5)")
    k.write_user_cell("D1", "=MID(A1, 7, 5)")
    _expect(k, "B1", "Hello", "LEFT")
    _expect(k, "C1", "World", "RIGHT")
    _expect(k, "D1", "World", "MID")


def test_len_trim_upper_lower():
    k = _fresh()
    k.write_user_cell("A1", "  Hello  World  ")
    k.write_user_cell("B1", "=TRIM(A1)")
    k.write_user_cell("C1", "=LEN(B1)")
    k.write_user_cell("D1", "=UPPER(B1)")
    k.write_user_cell("E1", "=LOWER(B1)")
    _expect(k, "B1", "Hello World", "TRIM collapses whitespace")
    _expect(k, "C1", 11, "LEN of trimmed string")
    _expect(k, "D1", "HELLO WORLD", "UPPER")
    _expect(k, "E1", "hello world", "LOWER")


def test_find_search():
    k = _fresh()
    k.write_user_cell("A1", "AMAZON.COM*1T9SS2M")
    k.write_user_cell("B1", '=FIND("*", A1)')
    k.write_user_cell("C1", '=SEARCH("amazon", A1)')
    _expect(k, "B1", 11, "FIND locates *")
    _expect(k, "C1", 1, "SEARCH is case-insensitive")


def test_substitute():
    k = _fresh()
    k.write_user_cell("A1", "foo bar foo baz")
    k.write_user_cell("B1", '=SUBSTITUTE(A1, "foo", "XX")')
    k.write_user_cell("C1", '=SUBSTITUTE(A1, "foo", "XX", 2)')
    _expect(k, "B1", "XX bar XX baz", "SUBSTITUTE all")
    _expect(k, "C1", "foo bar XX baz", "SUBSTITUTE 2nd instance only")


def test_concat_textjoin():
    k = _fresh()
    k.write_user_cell("A1", "hello")
    k.write_user_cell("A2", "")
    k.write_user_cell("A3", "world")
    k.write_user_cell("B1", '=TEXTJOIN(", ", TRUE, A1:A3)')
    k.write_user_cell("C1", '=CONCAT(A1, A3)')
    _expect(k, "B1", "hello, world", "TEXTJOIN skips empty when ignore_empty=TRUE")
    _expect(k, "C1", "helloworld", "CONCAT")


# ---------- Lookups + misc ----------

def test_match_exact():
    k = _fresh()
    for i, v in enumerate(["a", "b", "c", "d"], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", '=MATCH("c", A1:A4, 0)')
    _expect(k, "B1", 3, "MATCH exact returns 1-based position")


def test_index_2d():
    k = _fresh()
    # 3x2 grid: rows are A1:B3
    k.write_user_cell("A1", 1); k.write_user_cell("B1", 10)
    k.write_user_cell("A2", 2); k.write_user_cell("B2", 20)
    k.write_user_cell("A3", 3); k.write_user_cell("B3", 30)
    k.write_user_cell("D1", "=INDEX(A1:B3, 2, 2)")
    _expect(k, "D1", 20, "INDEX row 2 col 2 → B2 = 20")


def test_vlookup_exact():
    k = _fresh()
    # Lookup table in A1:B3
    k.write_user_cell("A1", "alpha"); k.write_user_cell("B1", 100)
    k.write_user_cell("A2", "beta");  k.write_user_cell("B2", 200)
    k.write_user_cell("A3", "gamma"); k.write_user_cell("B3", 300)
    k.write_user_cell("D1", '=VLOOKUP("beta", A1:B3, 2, FALSE)')
    _expect(k, "D1", 200, "VLOOKUP exact match")


def test_iferror_with_div_zero():
    k = _fresh()
    k.write_user_cell("A1", 10)
    k.write_user_cell("B1", 0)
    k.write_user_cell("C1", '=IFERROR(A1/B1, "n/a")')
    _expect(k, "C1", "n/a", "IFERROR catches #DIV/0!")


def test_round_family():
    k = _fresh()
    k.write_user_cell("A1", 3.14159)
    k.write_user_cell("B1", "=ROUND(A1, 2)")
    k.write_user_cell("C1", "=ROUNDUP(A1, 1)")
    k.write_user_cell("D1", "=ROUNDDOWN(A1, 1)")
    _expect(k, "B1", 3.14, "ROUND")
    _expect(k, "C1", 3.2, "ROUNDUP")
    _expect(k, "D1", 3.1, "ROUNDDOWN")


def test_mod():
    k = _fresh()
    k.write_user_cell("A1", "=MOD(10, 3)")
    _expect(k, "A1", 1, "MOD basic")


def test_isnumber_istext_isblank():
    k = _fresh()
    k.write_user_cell("A1", 42)
    k.write_user_cell("A2", "hi")
    # A3 blank
    k.write_user_cell("B1", "=ISNUMBER(A1)")
    k.write_user_cell("B2", "=ISTEXT(A2)")
    k.write_user_cell("B3", "=ISBLANK(A3)")
    _expect(k, "B1", True, "ISNUMBER")
    _expect(k, "B2", True, "ISTEXT")
    _expect(k, "B3", True, "ISBLANK")


def test_nested_countif_inside_if():
    """Realistic benchmark-shape: nested expressions across the new function set."""
    k = _fresh()
    for i, v in enumerate([10, 20, 30, 40], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", '=IF(COUNTIF(A1:A4, ">25") >= 2, "OK", "FAIL")')
    _expect(k, "B1", "OK", "IF wrapping COUNTIF result")


def test_sumproduct_basic():
    k = _fresh()
    for i, (a, b) in enumerate([(2, 5), (3, 4), (4, 3)], start=1):
        k.write_user_cell(f"A{i}", a)
        k.write_user_cell(f"B{i}", b)
    # 2*5 + 3*4 + 4*3 = 10 + 12 + 12 = 34
    k.write_user_cell("D1", "=SUMPRODUCT(A1:A3, B1:B3)")
    _expect(k, "D1", 34, "SUMPRODUCT element-wise multiply + sum")


def test_medianifs():
    k = _fresh()
    for i, (cat, val) in enumerate([("a", 10), ("b", 50), ("a", 30), ("a", 20), ("b", 100)], start=1):
        k.write_user_cell(f"A{i}", cat)
        k.write_user_cell(f"B{i}", val)
    # MEDIANIFS values where cat=a → 10, 30, 20 → sorted 10,20,30 → median 20
    k.write_user_cell("D1", '=MEDIANIFS(B1:B5, A1:A5, "a")')
    _expect(k, "D1", 20, "MEDIANIFS within category")


def test_percentile():
    k = _fresh()
    for i, v in enumerate([10, 20, 30, 40, 50], start=1):
        k.write_user_cell(f"A{i}", v)
    # 50th percentile = median = 30
    k.write_user_cell("B1", "=PERCENTILE(A1:A5, 0.5)")
    _expect(k, "B1", 30, "PERCENTILE 50th")


def test_quartile():
    k = _fresh()
    for i, v in enumerate([1, 2, 3, 4, 5, 6, 7, 8], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", "=QUARTILE(A1:A8, 2)")  # Q2 = median
    _expect(k, "B1", 4.5, "QUARTILE 2 = median")


def test_large_small_rank():
    k = _fresh()
    for i, v in enumerate([10, 50, 30, 20, 40], start=1):
        k.write_user_cell(f"A{i}", v)
    k.write_user_cell("B1", "=LARGE(A1:A5, 2)")
    k.write_user_cell("C1", "=SMALL(A1:A5, 2)")
    k.write_user_cell("D1", "=RANK(30, A1:A5)")
    _expect(k, "B1", 40, "LARGE 2nd")
    _expect(k, "C1", 20, "SMALL 2nd")
    _expect(k, "D1", 3, "RANK descending")


# ---------- Run-all entry point (no pytest required) ----------

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
