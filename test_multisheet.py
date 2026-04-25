"""Coverage for multi-sheet context exposure and benchmark-style cross-sheet
formula patterns. The 155 cross-sheet questions in SpreadsheetBench Verified-400
all assume the agent can see data on sheets other than the active one — these
tests verify both halves of that contract: the engine surfaces other sheets'
data, and the formulas the agent would emit actually resolve correctly.
"""
import sys
import json
from core.engine import GridOSKernel
from core.utils import a1_to_coords


def _fresh_with_two_sheets():
    k = GridOSKernel()
    # Sheet1 already exists by default
    k.create_sheet("Source")
    return k


def _val(k, a1, sheet=None):
    state = k._sheet_state(sheet)
    r, c = a1_to_coords(a1)
    cell = state["cells"].get((r, c))
    return cell.value if cell else None


# ---------- Multi-sheet context exposure ----------

def test_context_includes_all_sheet_names():
    k = _fresh_with_two_sheets()
    k.create_sheet("Result")
    ctx = k.get_context_for_ai("Sheet1")
    assert ctx.get("all_sheets") == ["Sheet1", "Source", "Result"], (
        f"all_sheets should list every sheet, got {ctx.get('all_sheets')}"
    )
    assert ctx.get("active_sheet") == "Sheet1"


def test_other_sheets_data_renders_non_active_sheet_contents():
    k = _fresh_with_two_sheets()
    k.write_user_cell("A1", "Product", sheet_name="Source")
    k.write_user_cell("B1", "Price", sheet_name="Source")
    k.write_user_cell("A2", "Widget", sheet_name="Source")
    k.write_user_cell("B2", 9.99, sheet_name="Source")
    ctx = k.get_context_for_ai("Sheet1")
    others = ctx.get("other_sheets_data") or []
    assert len(others) == 1, f"expected 1 other sheet, got {len(others)}"
    src = others[0]
    assert src["name"] == "Source"
    assert "Product" in src["formatted_data"]
    assert "9.99" in src["formatted_data"]
    bounds = src["occupied_bounds"]
    assert bounds["rows"] == 2 and bounds["cols"] == 2


def test_other_sheets_data_truncates_huge_sheets():
    k = _fresh_with_two_sheets()
    # Write 600 cells to Source — should be capped at 400 + a "truncated" marker
    for i in range(1, 601):
        k.write_user_cell(f"A{i}", f"row_{i}", sheet_name="Source")
    ctx = k.get_context_for_ai("Sheet1")
    src = ctx["other_sheets_data"][0]
    assert src["truncated"] is True
    assert "truncated" in src["formatted_data"].lower()


def test_other_sheets_data_omits_active_sheet():
    k = _fresh_with_two_sheets()
    k.write_user_cell("A1", "active-only", sheet_name="Sheet1")
    ctx = k.get_context_for_ai("Sheet1")
    others = ctx["other_sheets_data"]
    assert all(s["name"] != "Sheet1" for s in others), (
        "active sheet should NOT appear in other_sheets_data"
    )


def test_empty_workbook_still_lists_default_sheet():
    k = GridOSKernel()
    ctx = k.get_context_for_ai()
    assert ctx["all_sheets"] == ["Sheet1"]
    assert ctx["active_sheet"] == "Sheet1"
    assert ctx["other_sheets_data"] == []


# ---------- Cross-sheet formula patterns the data_analyst agent emits ----------

def test_crosssheet_vlookup_resolves():
    k = _fresh_with_two_sheets()
    # Lookup table on Source
    k.write_user_cell("A1", "alpha", sheet_name="Source"); k.write_user_cell("B1", 100, sheet_name="Source")
    k.write_user_cell("A2", "beta",  sheet_name="Source"); k.write_user_cell("B2", 200, sheet_name="Source")
    k.write_user_cell("A3", "gamma", sheet_name="Source"); k.write_user_cell("B3", 300, sheet_name="Source")
    # Formula on Sheet1 looking up on Source
    k.write_user_cell("D1", '=VLOOKUP("beta", Source!A1:B3, 2, FALSE)', sheet_name="Sheet1")
    assert _val(k, "D1", "Sheet1") == 200, "cross-sheet VLOOKUP"


def test_crosssheet_countif_resolves():
    k = _fresh_with_two_sheets()
    for i, v in enumerate([100, 250, 500, 1100, 2000], start=1):
        k.write_user_cell(f"A{i}", v, sheet_name="Source")
    k.write_user_cell("B1", '=COUNTIF(Source!A1:A5, ">1000")', sheet_name="Sheet1")
    assert _val(k, "B1", "Sheet1") == 2, "cross-sheet COUNTIF"


def test_crosssheet_sumifs_two_conditions():
    k = _fresh_with_two_sheets()
    rows = [("east", "Q1", 100), ("east", "Q2", 200), ("west", "Q1", 50), ("east", "Q1", 150)]
    for i, (region, qtr, val) in enumerate(rows, start=1):
        k.write_user_cell(f"A{i}", region, sheet_name="Source")
        k.write_user_cell(f"B{i}", qtr,    sheet_name="Source")
        k.write_user_cell(f"C{i}", val,    sheet_name="Source")
    k.write_user_cell(
        "E1",
        '=SUMIFS(Source!C1:C4, Source!A1:A4, "east", Source!B1:B4, "Q1")',
        sheet_name="Sheet1",
    )
    assert _val(k, "E1", "Sheet1") == 250, "east+Q1 → 100+150 = 250"


def test_consolidate_across_three_sheets():
    """Real benchmark shape: monthly sheets summed into a master cell."""
    k = GridOSKernel()
    for month, val in [("Jan", 100), ("Feb", 150), ("Mar", 200)]:
        k.create_sheet(month)
        k.write_user_cell("B10", val, sheet_name=month)
    k.activate_sheet("Sheet1")
    k.write_user_cell("A1", "=Jan!B10 + Feb!B10 + Mar!B10", sheet_name="Sheet1")
    assert _val(k, "A1", "Sheet1") == 450


def test_quoted_sheet_name_with_spaces_in_formula():
    k = GridOSKernel()
    k.create_sheet("Monthly Budget")
    k.write_user_cell("B5", 1234, sheet_name="Monthly Budget")
    k.write_user_cell("A1", "=SUM('Monthly Budget'!B5:B5)", sheet_name="Sheet1")
    assert _val(k, "A1", "Sheet1") == 1234


# ---------- Sheet-qualified target_cell (rectangle routing) ----------

def test_split_sheet_qualified_target_routes_to_named_sheet():
    """Agent rectangles emitted with target_cell='Sheet2!A1' route to that
    sheet, not the active one. Required for benchmark questions that ask the
    answer to land on a named sheet."""
    from core.models import AgentIntent
    k = _fresh_with_two_sheets()  # has Sheet1 (active) + Source
    intent = AgentIntent(
        agent_id="data_analyst",
        target_start_a1="Source!B5",
        data_payload=[["written-from-intent"]],
        shift_direction="right",
    )
    k.process_agent_intent(intent, sheet_name="Sheet1")  # caller says active = Sheet1
    # The rectangle should land on Source, not Sheet1
    assert _val(k, "B5", "Source") == "written-from-intent"
    assert _val(k, "B5", "Sheet1") is None


def test_split_sheet_qualified_target_creates_missing_sheet():
    """If the agent targets a sheet that doesn't exist yet, create it on the
    fly. Otherwise the agent would have to emit a sheet-create step first,
    which isn't representable in a single rectangle response."""
    from core.models import AgentIntent
    k = GridOSKernel()  # only Sheet1 initially
    intent = AgentIntent(
        agent_id="data_analyst",
        target_start_a1="Result!A1",
        data_payload=[["hello"]],
        shift_direction="right",
    )
    k.process_agent_intent(intent, sheet_name="Sheet1")
    assert "Result" in k.sheets, "missing destination sheet should be auto-created"
    assert _val(k, "A1", "Result") == "hello"


def test_split_sheet_qualified_target_handles_quoted_name():
    """Agent target like \"'Monthly Budget'!A1\" should round-trip when the
    destination has spaces (Excel-style single-quoted sheet ref)."""
    from core.models import AgentIntent
    k = GridOSKernel()
    k.create_sheet("Monthly Budget")
    intent = AgentIntent(
        agent_id="data_analyst",
        target_start_a1="'Monthly Budget'!C3",
        data_payload=[[42]],
        shift_direction="right",
    )
    k.process_agent_intent(intent, sheet_name="Sheet1")
    assert _val(k, "C3", "Monthly Budget") == 42


# ---------- Three-agent registry sanity ----------

def test_three_agents_load_with_data_analyst():
    from agents import load_agents
    agents = load_agents()
    assert set(agents.keys()) == {"general", "finance", "data_analyst"}, (
        f"expected the three baseline agents, got {set(agents.keys())}"
    )
    da = agents["data_analyst"]
    assert "router_description" in da and len(da["router_description"]) > 50
    assert "system_prompt" in da and "AVAILABLE PRIMITIVES" in da["system_prompt"]


def test_data_analyst_router_description_mentions_benchmark_shapes():
    from agents import load_agents
    da = load_agents()["data_analyst"]
    desc = da["router_description"].lower()
    # Sanity: the router description should mention enough keywords that the
    # router actually picks this agent for filter / lookup / aggregation tasks.
    for keyword in ("filter", "look up", "count", "sum", "extract"):
        assert keyword in desc, f"router_description missing keyword {keyword!r}"


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
