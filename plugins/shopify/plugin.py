"""Shopify connector — first SaaS bridge.

Exposes four scalar formulas backed by the Shopify Admin REST API:
  =SHOPIFY_REVENUE(days_back)           → gross revenue over the window
  =SHOPIFY_ORDER_COUNT(days_back)       → number of orders over the window
  =SHOPIFY_AVG_ORDER_VALUE(days_back)   → revenue / orders, 0 if no orders
  =SHOPIFY_PRODUCT_COUNT()              → total published products

Authentication comes from two env vars so the token never lives in a workbook:
  SHOPIFY_STORE_DOMAIN   e.g. "myshop.myshopify.com" (no scheme)
  SHOPIFY_ADMIN_TOKEN    Admin API access token (shpat_...)

We use stdlib urllib (no new deps) and a 60s in-process cache keyed by
(endpoint, params) so spreadsheet-wide recalcs don't hammer the API. Errors
surface as sentinel strings matching the '#…!' convention so the failure is
visible in-cell rather than crashing the whole recalc.
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


def _get(endpoint: str, params: dict | None = None):
    domain = os.environ.get("SHOPIFY_STORE_DOMAIN", "").strip()
    token = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()
    if not domain or not token:
        return {"__error__": "#SHOPIFY_AUTH!"}

    key = (endpoint, tuple(sorted((params or {}).items())))
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"https://{domain}/admin/api/2024-01/{endpoint}{qs}"
    req = urllib.request.Request(url, headers={
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"__error__": "#SHOPIFY_AUTH!"}
        return {"__error__": f"#SHOPIFY_{e.code}!"}
    except urllib.error.URLError:
        return {"__error__": "#SHOPIFY_OFFLINE!"}
    except Exception:
        return {"__error__": "#SHOPIFY_ERROR!"}

    _CACHE[key] = (time.time(), body)
    return body


def _window_iso(days_back) -> str:
    n = int(float(days_back)) if days_back else 30
    if n < 1:
        n = 1
    if n > 365:
        n = 365
    cutoff = datetime.now(timezone.utc) - timedelta(days=n)
    return cutoff.isoformat()


def _coerce_days(v):
    if v is None or v == "":
        return 30
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 30


def register(kernel):
    @kernel.formula("SHOPIFY_REVENUE")
    def shopify_revenue(days_back=30):
        data = _get("orders.json", {
            "status": "any",
            "created_at_min": _window_iso(_coerce_days(days_back)),
            "fields": "total_price",
            "limit": 250,
        })
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        total = 0.0
        for order in data.get("orders", []):
            try:
                total += float(order.get("total_price") or 0)
            except (TypeError, ValueError):
                continue
        return round(total, 2)

    @kernel.formula("SHOPIFY_ORDER_COUNT")
    def shopify_order_count(days_back=30):
        data = _get("orders/count.json", {
            "status": "any",
            "created_at_min": _window_iso(_coerce_days(days_back)),
        })
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        return int(data.get("count", 0))

    @kernel.formula("SHOPIFY_AVG_ORDER_VALUE")
    def shopify_aov(days_back=30):
        rev = shopify_revenue(days_back)
        if isinstance(rev, str) and rev.startswith("#"):
            return rev
        count = shopify_order_count(days_back)
        if isinstance(count, str) and count.startswith("#"):
            return count
        if not count:
            return 0.0
        return round(rev / count, 2)

    @kernel.formula("SHOPIFY_PRODUCT_COUNT")
    def shopify_product_count():
        data = _get("products/count.json", {})
        if isinstance(data, dict) and data.get("__error__"):
            return data["__error__"]
        return int(data.get("count", 0))

    kernel.agent({
        "id": "shopify",
        "display_name": "Shopify Analyst",
        "router_description": "Shopify store metrics, revenue, orders, product catalog",
        "system_prompt": (
            "You are a Shopify store analyst. When the user asks about store "
            "performance, emit formulas that use the built-in Shopify primitives: "
            "SHOPIFY_REVENUE(days_back), SHOPIFY_ORDER_COUNT(days_back), "
            "SHOPIFY_AVG_ORDER_VALUE(days_back), SHOPIFY_PRODUCT_COUNT(). "
            "Prefer formulas over hardcoded numbers so the grid stays live. "
            "Lay out labels in column A and values in column B. "
            "Return a 2D 'values' array and a top-left 'target_cell'. "
            "Do NOT invent numeric values — if data is unavailable the formula "
            "will surface a #SHOPIFY_* sentinel the user can act on."
        ),
    })
