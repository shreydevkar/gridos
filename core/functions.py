import math
from contextvars import ContextVar
from typing import Callable, Optional

_REGISTRY: dict[str, Callable] = {}

# Map of formula name → owning plugin slug. Built-ins (registered via
# @register_tool inside this file) don't get an entry — only plugin-registered
# formulas do, so FormulaEvaluator.evaluate knows which calls to gate. Kept
# here instead of in core/plugins.py so the evaluator has a single import.
_FORMULA_PLUGIN_SOURCE: dict[str, str] = {}

# Per-request set of plugin slugs the current user has installed, or None to
# mean "no gating" (OSS default, or any request that didn't set it). When set,
# FormulaEvaluator rejects calls to plugin-sourced formulas whose slug isn't
# in the set. The kernel + built-ins are always callable.
_installed_plugins: ContextVar[Optional[set]] = ContextVar(
    "gridos_installed_plugins", default=None
)


def register_tool(name: str | None = None):
    """Decorator that registers a callable into the shared GridOS formula/tool registry.

    Usage:
        @register_tool()
        def average(*args):
            return sum(args) / len(args)

        @register_tool("PRODUCT")
        def multiply(a, b):
            return a * b
    """

    def decorator(func: Callable) -> Callable:
        key = (name or func.__name__).upper()
        _REGISTRY[key] = func
        return func

    return decorator


@register_tool("SUM")
def _sum(*args):
    return sum(args)


@register_tool("MAX")
def _max(*args):
    return max(args)


@register_tool("MIN")
def _min(*args):
    return min(args)


@register_tool("CEIL")
def _ceil(value):
    return math.ceil(value)


@register_tool("MINUS")
def _minus(a, b):
    return a - b


@register_tool("SUBTRACT")
def _subtract(a, b):
    return a - b


@register_tool("MULTIPLY")
def _multiply(a, b):
    return a * b


@register_tool("DIVIDE")
def _divide(a, b):
    if b == 0:
        return "#DIV/0!"
    return a / b


@register_tool("AVERAGE")
def _average(*args):
    if not args:
        return 0
    return sum(args) / len(args)


@register_tool("POWER")
def _power(base, exponent):
    return base ** exponent


@register_tool("SQRT")
def _sqrt(value):
    return math.sqrt(value)


@register_tool("ABS")
def _abs(value):
    return abs(value)


@register_tool("ROUND")
def _round(value, digits=0):
    return round(value, int(digits))


def _truthy(v):
    # Empty string / None are falsy; everything else follows Python truthiness.
    if v is None or v == "":
        return False
    return bool(v)


@register_tool("IF")
def _if(condition, when_true, when_false):
    return when_true if _truthy(condition) else when_false


@register_tool("AND")
def _and(*args):
    return all(_truthy(a) for a in args)


@register_tool("OR")
def _or(*args):
    return any(_truthy(a) for a in args)


@register_tool("NOT")
def _not(value):
    return not _truthy(value)


@register_tool("GT")
def _gt(a, b):
    return a > b


@register_tool("LT")
def _lt(a, b):
    return a < b


@register_tool("EQ")
def _eq(a, b):
    return a == b


@register_tool("GTE")
def _gte(a, b):
    return a >= b


@register_tool("LTE")
def _lte(a, b):
    return a <= b


class FormulaEvaluator:
    def __init__(self):
        self.registry = _REGISTRY

    def register_custom(self, name: str, func: Callable):
        """Legacy API — prefer the @register_tool decorator."""
        self.registry[name.upper()] = func

    def evaluate(self, func_name: str, args: list):
        key = func_name.upper()
        fn = self.registry.get(key)
        if not fn:
            return f"#NAME? (Unknown function: {func_name})"
        # Per-user plugin gate. If the request set _installed_plugins and
        # this formula belongs to a plugin that isn't in the set, refuse.
        # Built-ins have no entry in _FORMULA_PLUGIN_SOURCE so they're
        # always callable. OSS requests never set the ContextVar so OSS
        # behavior is unchanged.
        plugin = _FORMULA_PLUGIN_SOURCE.get(key)
        if plugin is not None:
            installed = _installed_plugins.get()
            if installed is not None and plugin not in installed:
                return f"#NOT_INSTALLED: enable the '{plugin}' plugin in File > Marketplace"
        try:
            return fn(*args)
        except TypeError:
            return f"#VALUE! (Invalid number of arguments for {func_name})"
        except Exception as e:
            return f"#VALUE! ({str(e)})"
