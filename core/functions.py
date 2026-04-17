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
