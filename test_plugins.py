"""Offline tests for the V0 plugin loader. No server, no LLM calls.

Run with: python test_plugins.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.functions import FormulaEvaluator, _REGISTRY
from core.plugins import PluginKernel, discover_and_load, load_manifests


PLUGINS_DIR = Path(__file__).parent / "plugins"


def test_plugins_dir_exists():
    assert PLUGINS_DIR.is_dir(), "plugins/ directory missing"


def test_hello_world_formula_registered():
    kernel = discover_and_load(PLUGINS_DIR)
    assert "GREET" in _REGISTRY, f"GREET not registered; got {sorted(_REGISTRY)[:20]}..."
    assert FormulaEvaluator().evaluate("GREET", ["plugins"]) == "Hello, plugins!"


def test_hello_world_agent_registered():
    kernel = discover_and_load(PLUGINS_DIR)
    assert "greeter" in kernel.agents
    assert "system_prompt" in kernel.agents["greeter"]


def test_black_scholes_formula():
    discover_and_load(PLUGINS_DIR)
    # Known sanity values: S=100, K=100, T=1, r=0.05, sigma=0.2, call ≈ 10.45
    call = FormulaEvaluator().evaluate("BLACK_SCHOLES", [100, 100, 1, 0.05, 0.2, "call"])
    assert 10.4 < call < 10.5, f"expected ~10.45, got {call}"
    put = FormulaEvaluator().evaluate("BLACK_SCHOLES", [100, 100, 1, 0.05, 0.2, "put"])
    assert 5.5 < put < 5.6, f"expected ~5.57, got {put}"


def test_black_scholes_bad_input_returns_blank():
    """Bad inputs (missing, non-positive) must return blank, not a '#VALUE!'
    string — half-filled demo sheets shouldn't look broken."""
    discover_and_load(PLUGINS_DIR)
    ev = FormulaEvaluator()
    assert ev.evaluate("BLACK_SCHOLES", [100, 100, 0, 0.05, 0.2, "call"]) == ""
    assert ev.evaluate("BLACK_SCHOLES", ["", 100, 1, 0.05, 0.2, "call"]) == ""
    assert ev.evaluate("BLACK_SCHOLES", [100, 100, 1, 0.05, 0.2, "banana"]) == ""


def test_real_estate_cap_rate():
    discover_and_load(PLUGINS_DIR)
    ev = FormulaEvaluator()
    assert ev.evaluate("CAP_RATE", [80_000, 1_000_000]) == 0.08
    # Blank divisor → blank cell, NOT '#DIV/0!' — plugins should degrade gracefully.
    assert ev.evaluate("CAP_RATE", [1, 0]) == ""
    assert ev.evaluate("CAP_RATE", ["", 1_000_000]) == ""
    assert ev.evaluate("DSCR", [100, 0]) == ""


def test_plugin_records_populated():
    kernel = discover_and_load(PLUGINS_DIR)
    slugs = {r.slug for r in kernel.records}
    assert {"hello_world", "black_scholes", "real_estate"}.issubset(slugs), slugs
    hw = next(r for r in kernel.records if r.slug == "hello_world")
    assert "GREET" in hw.formulas
    assert "greeter" in hw.agents


def test_only_filter_limits_loaded_plugins():
    kernel = discover_and_load(PLUGINS_DIR, only={"hello_world"})
    slugs = {r.slug for r in kernel.records}
    assert slugs == {"hello_world"}, slugs


def test_load_manifests_no_side_effects():
    manifests = load_manifests(PLUGINS_DIR)
    slugs = {m["slug"] for m in manifests}
    assert {"hello_world", "black_scholes", "real_estate"}.issubset(slugs)
    for m in manifests:
        for key in ("name", "description", "category", "author", "version"):
            assert key in m, f"manifest missing {key}: {m}"


def test_missing_plugins_dir_returns_empty_kernel():
    kernel = discover_and_load(Path("/nonexistent/gridos/plugins"))
    assert kernel.records == []
    assert kernel.errors == []


def test_bad_plugin_is_isolated(tmp_path_builder=None):
    """A plugin that raises at register() should be caught, not crash the loader."""
    import tempfile
    import shutil

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp) / "plugins"
        tmp_dir.mkdir()
        bad = tmp_dir / "broken"
        bad.mkdir()
        (bad / "manifest.json").write_text('{"name": "Broken", "description": "raises", "category": "test"}')
        (bad / "plugin.py").write_text("def register(kernel):\n    raise RuntimeError('boom')\n")
        good = tmp_dir / "good"
        good.mkdir()
        (good / "plugin.py").write_text(
            "def register(kernel):\n"
            "    @kernel.formula('ISOLATED_OK')\n"
            "    def ok():\n        return 42\n"
        )

        kernel = discover_and_load(tmp_dir)
        assert any(r.slug == "good" for r in kernel.records), kernel.records
        assert any(e["plugin"] == "broken" for e in kernel.errors), kernel.errors
        assert "ISOLATED_OK" in _REGISTRY


def test_string_literal_in_formula():
    """End-to-end: =GREET("Shrey") should parse strings and return the plugin's text.

    Skipped when pydantic is not available (the kernel depends on it); only
    fires in environments with the full runtime installed."""
    try:
        from core.engine import GridOSKernel
    except ImportError:
        print("  SKIP  test_string_literal_in_formula (pydantic not installed)")
        return
    discover_and_load(PLUGINS_DIR)
    k = GridOSKernel()
    k.write_user_cell("A1", '=GREET("Shrey")')
    cell = k.state["cells"][(0, 0)]
    assert cell.value == "Hello, Shrey!", f"expected greeting, got {cell.value!r}"

    k.write_user_cell("B1", '=BLACK_SCHOLES(100, 100, 1, 0.05, 0.2, "call")')
    bs_cell = k.state["cells"][(0, 1)]
    assert isinstance(bs_cell.value, (int, float)) and 10.4 < bs_cell.value < 10.5, bs_cell.value

    k.write_user_cell("C1", "=GREET()")
    default_cell = k.state["cells"][(0, 2)]
    assert default_cell.value == "Hello, world!", default_cell.value


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
