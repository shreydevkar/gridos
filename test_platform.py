"""Offline tests for the GridOS platform additions.

No server, no Google API calls. Run with: python test_platform.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agents import load_agents
from core.engine import GridOSKernel
from core.functions import FormulaEvaluator, _REGISTRY, register_tool


# --- @register_tool ---


def test_register_tool_with_explicit_name():
    @register_tool("DOUBLE")
    def double(x):
        return x * 2

    assert "DOUBLE" in _REGISTRY
    assert FormulaEvaluator().evaluate("DOUBLE", [5]) == 10


def test_register_tool_defaults_to_func_name():
    @register_tool()
    def triple(x):
        return x * 3

    assert "TRIPLE" in _REGISTRY
    assert FormulaEvaluator().evaluate("triple", [4]) == 12


def test_register_tool_builtins_still_work():
    evaluator = FormulaEvaluator()
    assert evaluator.evaluate("SUM", [1, 2, 3]) == 6
    assert evaluator.evaluate("MAX", [5, 9, 2]) == 9
    assert evaluator.evaluate("MINUS", [10, 4]) == 6


def test_register_custom_legacy_alias():
    evaluator = FormulaEvaluator()
    evaluator.register_custom("SQUARE", lambda x: x * x)
    assert evaluator.evaluate("SQUARE", [7]) == 49


# --- Agent loader ---


def test_load_agents_returns_finance_and_general():
    agents = load_agents()
    assert "finance" in agents
    assert "general" in agents
    for agent in agents.values():
        assert "id" in agent
        assert "system_prompt" in agent


def test_agent_prompts_are_nonempty():
    agents = load_agents()
    assert len(agents["finance"]["system_prompt"]) > 50
    assert len(agents["general"]["system_prompt"]) > 50


# --- get_context_for_ai metadata shape ---


def test_context_empty_grid():
    kernel = GridOSKernel()
    ctx = kernel.get_context_for_ai()
    assert ctx["cell_metadata"] == {}
    assert ctx["cell_metadata_json"] == "{}"
    assert ctx["occupied_bounds"] is None


def test_context_static_cell_metadata():
    kernel = GridOSKernel()
    kernel.write_user_cell("A1", "Hello")
    kernel.write_user_cell("B2", "100")

    ctx = kernel.get_context_for_ai()
    assert "A1" in ctx["cell_metadata"]
    assert ctx["cell_metadata"]["A1"]["val"] == "Hello"
    assert ctx["cell_metadata"]["A1"]["locked"] is False
    assert ctx["cell_metadata"]["A1"]["type"] == "static"
    assert ctx["cell_metadata"]["B2"]["val"] == 100


def test_context_formula_cell_is_tagged():
    kernel = GridOSKernel()
    kernel.write_user_cell("A1", "=SUM(1, 2)")
    ctx = kernel.get_context_for_ai()
    assert ctx["cell_metadata"]["A1"]["type"] == "formula"


def test_context_locked_flag():
    kernel = GridOSKernel()
    kernel.lock_range("B2", "B3")
    ctx = kernel.get_context_for_ai()
    assert ctx["cell_metadata"]["B2"]["locked"] is True
    assert ctx["cell_metadata"]["B3"]["locked"] is True


def test_context_metadata_json_is_valid():
    kernel = GridOSKernel()
    kernel.write_user_cell("A1", "x")
    kernel.write_user_cell("B2", 42)
    ctx = kernel.get_context_for_ai()
    parsed = json.loads(ctx["cell_metadata_json"])
    assert parsed["A1"]["val"] == "x"
    assert parsed["B2"]["val"] == 42


def test_context_occupied_bounds():
    kernel = GridOSKernel()
    kernel.write_user_cell("B2", "x")
    kernel.write_user_cell("D5", "y")
    ctx = kernel.get_context_for_ai()
    bounds = ctx["occupied_bounds"]
    assert bounds["top_left"] == "B2"
    assert bounds["bottom_right"] == "D5"
    assert bounds["rows"] == 4
    assert bounds["cols"] == 3


# --- Kernel sanity (regression) ---


def test_kernel_preview_returns_all_cells():
    from core.models import AgentIntent
    kernel = GridOSKernel()
    intent = AgentIntent(
        agent_id="test",
        target_start_a1="C3",
        data_payload=[[1, 2, 3], [4, 5, 6]],
        shift_direction="right",
    )
    preview = kernel.preview_agent_intent(intent)
    assert len(preview["preview_cells"]) == 6
    assert preview["preview_cells"][0]["cell"] == "C3"
    assert preview["preview_cells"][-1]["cell"] == "E4"


def test_kernel_clear_unlocked_preserves_locks():
    kernel = GridOSKernel()
    kernel.lock_range("A1", "A1")
    kernel.write_user_cell("B1", "delete_me")
    kernel.clear_unlocked()
    ctx = kernel.get_context_for_ai()
    assert "A1" in ctx["cell_metadata"]
    assert "B1" not in ctx["cell_metadata"]


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
