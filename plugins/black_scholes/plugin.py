"""Black-Scholes European-option pricer.

=BLACK_SCHOLES(S, K, T, r, sigma, type)
  S     spot price
  K     strike price
  T     time to expiry, in years
  r     risk-free rate (decimal, e.g. 0.05)
  sigma volatility (decimal, e.g. 0.20)
  type  "call" or "put"  (default "call")
"""
import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _as_float(x):
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def register(kernel):
    @kernel.formula("BLACK_SCHOLES")
    def black_scholes(S, K, T, r, sigma, option_type="call"):
        S_f, K_f, T_f, r_f, sig = _as_float(S), _as_float(K), _as_float(T), _as_float(r), _as_float(sigma)
        # Any missing or non-positive input → show as blank rather than an
        # error sentinel, so half-filled demo sheets don't look broken.
        if None in (S_f, K_f, T_f, r_f, sig) or T_f <= 0 or sig <= 0 or K_f <= 0 or S_f <= 0:
            return ""
        d1 = (math.log(S_f / K_f) + (r_f + 0.5 * sig * sig) * T_f) / (sig * math.sqrt(T_f))
        d2 = d1 - sig * math.sqrt(T_f)
        kind = str(option_type).strip().lower()
        if kind == "call":
            return S_f * _norm_cdf(d1) - K_f * math.exp(-r_f * T_f) * _norm_cdf(d2)
        if kind == "put":
            return K_f * math.exp(-r_f * T_f) * _norm_cdf(-d2) - S_f * _norm_cdf(-d1)
        return ""
