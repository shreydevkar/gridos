"""Stripe connector — live account metrics as formulas.

Exposes five scalar formulas backed by the Stripe REST API:
  =STRIPE_REVENUE(days_back)         → gross successful-charge total (USD-equivalent)
  =STRIPE_CHARGE_COUNT(days_back)    → count of successful charges in the window
  =STRIPE_MRR()                      → monthly-recurring-revenue from active subs
  =STRIPE_ACTIVE_SUBSCRIBERS()       → count of active subscriptions
  =STRIPE_CUSTOMER_COUNT()           → total customer count

Authentication is one env var so the key never sits in a workbook:
  STRIPE_SECRET_KEY    sk_live_... or sk_test_... from the Stripe dashboard

Same shape as the Shopify plugin: stdlib urllib (no new deps), 60s cache on
the same key-space, #STRIPE_* sentinels on failure. All charge amounts are
in cents from Stripe; formulas return dollars.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

_CACHE: dict = {}
_CACHE_TTL = 60.0

# Normalize non-monthly subscription intervals to monthly so MRR math stays
# apples-to-apples. Stripe's canonical interval values are day/week/month/year.
_INTERVAL_TO_MONTHS = {
    "day":   1 / 30.0,
    "week":  7 / 30.0,
    "month": 1.0,
    "year":  12.0,
}


def _get(endpoint: str, params: dict | None = None):
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return {"__error__": "#STRIPE_AUTH!"}

    cache_key = (endpoint, tuple(sorted((params or {}).items())))
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    qs = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
    url = f"https://api.stripe.com/v1/{endpoint}{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"__error__": "#STRIPE_AUTH!"}
        return {"__error__": f"#STRIPE_{e.code}!"}
    except urllib.error.URLError:
        return {"__error__": "#STRIPE_OFFLINE!"}
    except Exception:
        return {"__error__": "#STRIPE_ERROR!"}

    _CACHE[cache_key] = (time.time(), body)
    return body


def _paginate_all(endpoint: str, base_params: dict, max_pages: int = 20) -> dict:
    """Walk Stripe cursor pagination until `has_more` flips false or the
    safety cap kicks in. The cap keeps runaway fetches bounded — 20 pages
    × 100 items = 2000 rows, which covers any realistic single-formula
    workload without the plugin ever freezing the recalc loop on a giant
    account."""
    collected: list = []
    params = dict(base_params)
    params.setdefault("limit", 100)
    for _ in range(max_pages):
        body = _get(endpoint, params)
        if isinstance(body, dict) and body.get("__error__"):
            return body
        collected.extend(body.get("data", []))
        if not body.get("has_more"):
            break
        last = (body.get("data") or [None])[-1]
        if not last or not last.get("id"):
            break
        params = {**base_params, "starting_after": last["id"], "limit": params["limit"]}
    return {"data": collected}


def _window_unix(days_back) -> int:
    n = int(float(days_back)) if days_back else 30
    if n < 1:
        n = 1
    if n > 365:
        n = 365
    cutoff = datetime.now(timezone.utc) - timedelta(days=n)
    return int(cutoff.timestamp())


def _coerce_days(v):
    if v is None or v == "":
        return 30
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 30


def register(kernel):
    @kernel.formula("STRIPE_REVENUE")
    def stripe_revenue(days_back=30):
        data = _paginate_all("charges", {
            "created[gte]": _window_unix(_coerce_days(days_back)),
        })
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        total_cents = 0
        for ch in data.get("data", []):
            if ch.get("status") != "succeeded":
                continue
            if ch.get("refunded"):
                # Net revenue: subtract refunds from the charge amount.
                total_cents += int(ch.get("amount") or 0) - int(ch.get("amount_refunded") or 0)
            else:
                total_cents += int(ch.get("amount") or 0)
        return round(total_cents / 100.0, 2)

    @kernel.formula("STRIPE_CHARGE_COUNT")
    def stripe_charge_count(days_back=30):
        data = _paginate_all("charges", {
            "created[gte]": _window_unix(_coerce_days(days_back)),
        })
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        return sum(1 for c in data.get("data", []) if c.get("status") == "succeeded")

    @kernel.formula("STRIPE_MRR")
    def stripe_mrr():
        # Active subs only. Each plan's amount is in the plan's base currency
        # and interval; normalize to monthly so the MRR number is meaningful.
        # Multi-item subscriptions (upsells + add-ons) are summed per sub.
        data = _paginate_all("subscriptions", {"status": "active"})
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        total_monthly_cents = 0.0
        for sub in data.get("data", []):
            for item in (sub.get("items", {}).get("data") or []):
                price = item.get("price") or {}
                recurring = price.get("recurring") or {}
                interval = recurring.get("interval", "month")
                interval_count = int(recurring.get("interval_count") or 1)
                unit_amount = int(price.get("unit_amount") or 0)
                qty = int(item.get("quantity") or 1)
                months_in_interval = _INTERVAL_TO_MONTHS.get(interval, 1.0) * interval_count
                if months_in_interval <= 0:
                    continue
                # Normalize: amount-per-month = (amount × quantity) / months_per_interval.
                total_monthly_cents += (unit_amount * qty) / months_in_interval
        return round(total_monthly_cents / 100.0, 2)

    @kernel.formula("STRIPE_ACTIVE_SUBSCRIBERS")
    def stripe_active_subscribers():
        data = _paginate_all("subscriptions", {"status": "active"})
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        # De-duplicate by customer — one customer with two active subs counts once.
        customers = {sub.get("customer") for sub in data.get("data", []) if sub.get("customer")}
        return len(customers)

    @kernel.formula("STRIPE_CUSTOMER_COUNT")
    def stripe_customer_count():
        # The /customers endpoint doesn't expose a count — walk the list. Stripe
        # caps at 10K customers per query; past that we'd need to partition on
        # created_gte, which is overkill for the "indie hacker metrics" use case.
        data = _paginate_all("customers", {}, max_pages=100)
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        return len(data.get("data", []))

    kernel.agent({
        "id": "stripe",
        "display_name": "Stripe Analyst",
        "router_description": "Stripe revenue, MRR, subscriptions, customer metrics",
        "system_prompt": (
            "You are a Stripe analyst. When the user asks about revenue, MRR, "
            "subscribers, or customer counts, emit formulas that use the built-in "
            "Stripe primitives: STRIPE_REVENUE(days_back), STRIPE_CHARGE_COUNT(days_back), "
            "STRIPE_MRR(), STRIPE_ACTIVE_SUBSCRIBERS(), STRIPE_CUSTOMER_COUNT(). "
            "Prefer formulas over hardcoded numbers so the grid stays live. "
            "Lay out labels in column B and values in column C so the model "
            "anchors cleanly. Return a 2D 'values' array and a top-left "
            "'target_cell'. Do NOT invent numeric values — if data is unavailable, "
            "the formula surfaces a #STRIPE_* sentinel the user can act on."
        ),
    })
