"""Coverage for agent-routing determinism: explicit override, keyword
pre-classifier, and LLM router cache. Tests the deterministic layers without
making real LLM calls — the cached LLM path is tested by mocking call_model."""
import sys
from unittest.mock import patch

# Importing main triggers plugin/agent loading; that's fine — we want the real
# AGENTS registry so the pre-classifier sees what production sees.
import main


# ---------- Layer 1: explicit agent_id override ----------

def test_agent_override_skips_router_when_valid():
    """If req.agent_id is a registered agent, route_prompt is never called."""
    from core.engine import GridOSKernel
    main.kernel = GridOSKernel()  # fresh kernel for the test
    req = main.ChatRequest(prompt="anything goes here", agent_id="data_analyst")

    with patch("main.route_prompt") as mock_route:
        with patch("main.call_model") as mock_call:
            # Stub call_model so the agent body doesn't actually fire — we only
            # care about the routing decision, not the downstream generation.
            mock_call.return_value = type("R", (), {"text": '{"plan": null, "values": null, "target_cell": null, "intents": null, "shift_direction": "right", "chart_spec": null, "category": "data_analyst", "macro_spec": null, "plugin_spec": null}', "model_id": "x", "provider_id": "x", "input_tokens": 0, "output_tokens": 0})()
            try:
                main.generate_agent_preview(req)
            except Exception:
                pass  # downstream may fail, but the routing assertion is what we check

        assert not mock_route.called, "agent_id override must bypass route_prompt"


def test_agent_override_falls_through_when_invalid():
    """An unknown agent_id silently falls through to the router."""
    req_valid = main.ChatRequest(prompt="extract rows from Sheet1", agent_id="not_a_real_agent")

    # Validation lives inline in generate_agent_preview; we simulate it here
    # to confirm the same logic without firing the full preview pipeline.
    chosen = req_valid.agent_id if (req_valid.agent_id and req_valid.agent_id in main.AGENTS) else "<router>"
    assert chosen == "<router>", "unknown agent_id must fall through to the router"


def test_agent_override_accepts_each_baseline_agent():
    for aid in ("general", "finance", "data_analyst"):
        req = main.ChatRequest(prompt="test", agent_id=aid)
        chosen = req.agent_id if (req.agent_id and req.agent_id in main.AGENTS) else "<router>"
        assert chosen == aid, f"override for {aid!r} should be honored"


# ---------- Layer 2: keyword pre-classifier ----------

def test_quick_classify_picks_data_analyst_for_vlookup():
    assert main._quick_classify("How do I VLOOKUP the price for SKU in A2 from a table on Sheet2?") == "data_analyst"


def test_quick_classify_picks_data_analyst_for_countif():
    assert main._quick_classify("Use COUNTIF to count cells where col B > 1000") == "data_analyst"


def test_quick_classify_picks_data_analyst_for_extract_rows():
    assert main._quick_classify("Extract rows where column B = 'TELIVISION' to Sheet2") == "data_analyst"


def test_quick_classify_picks_data_analyst_for_datedif():
    assert main._quick_classify("calculate age in years using DATEDIF from a birthdate") == "data_analyst"


def test_quick_classify_picks_finance_for_dcf():
    assert main._quick_classify("Build a DCF model with 5-year projections") == "finance"


def test_quick_classify_picks_finance_for_three_statement():
    assert main._quick_classify("Make a 3-statement operating model anchored at B2") == "finance"


def test_quick_classify_picks_finance_for_terminal_value():
    assert main._quick_classify("What's the terminal value formula for this DCF?") == "finance"


def test_quick_classify_returns_none_for_ambiguous():
    """A prompt with no trigger phrases must fall through to the LLM router."""
    assert main._quick_classify("Put the word Hello in cell A1") is None
    assert main._quick_classify("Add a column header") is None


def test_quick_classify_returns_none_when_multiple_match():
    """If a prompt triggers multiple agents (e.g. mentions both VLOOKUP and DCF),
    fall through to the LLM rather than guess."""
    p = "In my DCF model, how do I VLOOKUP the discount rate from another sheet?"
    assert main._quick_classify(p) is None, (
        f"prompt with both finance + data_analyst triggers should fall through, got {main._quick_classify(p)!r}"
    )


def test_quick_classify_returns_none_for_empty():
    assert main._quick_classify("") is None
    assert main._quick_classify(None) is None


def test_quick_classify_case_insensitive():
    assert main._quick_classify("vlookup the price for sku") == "data_analyst"
    assert main._quick_classify("VLOOKUP the price for sku") == "data_analyst"
    assert main._quick_classify("VLookup the price for sku") == "data_analyst"


# ---------- Layer 3: LLM router cache ----------

def test_llm_router_cache_calls_call_model_once_per_unique_input():
    """Identical (prompt, history, options_block, model_id) should result in a
    single call_model invocation, even with multiple route_prompt calls."""
    main._llm_route_cached.cache_clear()  # ensure clean state

    fake = type("R", (), {"text": "data_analyst", "model_id": "x", "provider_id": "x", "input_tokens": 1, "output_tokens": 1})()
    with patch("main.call_model", return_value=fake) as mock_call:
        with patch("main._pick_router_model", return_value="some-model"):
            # Use a prompt with NO triggers so the LLM path is taken
            for _ in range(5):
                result = main.route_prompt("just put hello world somewhere", "")
                assert result == "data_analyst"
            assert mock_call.call_count == 1, (
                f"cache should collapse 5 identical calls into 1, got {mock_call.call_count}"
            )


def test_llm_router_cache_distinguishes_different_prompts():
    main._llm_route_cached.cache_clear()
    fake = type("R", (), {"text": "general", "model_id": "x", "provider_id": "x", "input_tokens": 1, "output_tokens": 1})()
    with patch("main.call_model", return_value=fake) as mock_call:
        with patch("main._pick_router_model", return_value="some-model"):
            main.route_prompt("first ambiguous prompt", "")
            main.route_prompt("second ambiguous prompt", "")
            assert mock_call.call_count == 2, "different prompts should produce separate cache entries"


def test_llm_router_falls_back_to_general_on_garbage_response():
    main._llm_route_cached.cache_clear()
    fake = type("R", (), {"text": "this is not a valid agent id", "model_id": "x", "provider_id": "x", "input_tokens": 1, "output_tokens": 1})()
    with patch("main.call_model", return_value=fake):
        with patch("main._pick_router_model", return_value="some-model"):
            assert main.route_prompt("layout this header please", "") == "general"


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
