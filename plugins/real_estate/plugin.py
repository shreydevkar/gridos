"""Real-estate underwriting plugin.

Demonstrates the agent seam: registers a domain-specific system prompt so the
router can send real-estate prompts to a specialist. Also exposes a handy
=CAP_RATE(noi, price) primitive.
"""


def _as_float(x):
    """Coerce a cell value to float, returning None for blanks or non-numeric
    input so callers can short-circuit to an empty result instead of raising."""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def register(kernel):
    @kernel.formula("CAP_RATE")
    def cap_rate(noi, price):
        noi_f = _as_float(noi)
        price_f = _as_float(price)
        if noi_f is None or price_f is None or price_f == 0:
            return ""
        return noi_f / price_f

    @kernel.formula("DSCR")
    def dscr(noi, debt_service):
        noi_f = _as_float(noi)
        ds_f = _as_float(debt_service)
        if noi_f is None or ds_f is None or ds_f == 0:
            return ""
        return noi_f / ds_f

    kernel.agent({
        "id": "real_estate",
        "display_name": "Real Estate Copilot",
        "router_description": "Real-estate underwriting: cap rate, NOI, cash-on-cash, DSCR, rent rolls, property pro-formas",
        "system_prompt": (
            "You are a Real Estate Underwriting Copilot.\n\n"
            "RULES:\n"
            "1. Prefer real-estate-native metrics: Cap Rate = NOI / Price, Cash-on-Cash = Annual Cash Flow / Cash Invested, "
            "DSCR = NOI / Debt Service, GRM = Price / Gross Annual Rent.\n"
            "2. Use the GridOS formulas =CAP_RATE, =DSCR, =SUM, =DIVIDE, =MULTIPLY, =MINUS — no infix operators or nesting.\n"
            "3. Return a 2D 'values' array and a single top-left target_cell, one contiguous rectangle per response.\n"
            "4. When the user asks for a property pro-forma, lay it out row-wise: Revenue → Operating Expenses → NOI → "
            "Debt Service → Cash Flow → Cap Rate / DSCR / Cash-on-Cash at the bottom.\n"
            "5. Keep responses preview-safe. Do not claim anything was written."
        ),
    })
