import math
from typing import Callable

_REGISTRY: dict[str, Callable] = {}


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
        fn = self.registry.get(func_name.upper())
        if not fn:
            return f"#NAME? (Unknown function: {func_name})"
        try:
            return fn(*args)
        except TypeError:
            return f"#VALUE! (Invalid number of arguments for {func_name})"
        except Exception as e:
            return f"#VALUE! ({str(e)})"
