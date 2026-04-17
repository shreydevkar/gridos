"""User-defined macros.

Macros are parsed into an AST and compiled into a pure Python closure that
invokes only whitelisted primitives from core.functions._REGISTRY. No eval or
exec is ever used on user-supplied text. Grid cells reference the macro as a
flat call (e.g. ``=MARGIN(C2, D2)``) that the existing engine evaluates; the
nested composition lives inside the compiled closure.
"""

import re
from dataclasses import dataclass
from typing import Callable, List, Union


class MacroError(ValueError):
    pass


@dataclass
class Call:
    name: str
    args: List["Node"]


@dataclass
class Param:
    name: str


@dataclass
class Literal:
    value: float


Node = Union[Call, Param, Literal]


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<punct>[(),]))"
)
RESERVED_KEYWORDS = {"TRUE", "FALSE", "NULL", "NONE"}


def _tokenize(body: str) -> list:
    stripped = (body or "").strip()
    if stripped.startswith("="):
        stripped = stripped[1:]
    tokens: list = []
    pos = 0
    while pos < len(stripped):
        m = _TOKEN_RE.match(stripped, pos)
        if not m or m.end() == pos:
            raise MacroError(f"Unexpected character at position {pos}: {stripped[pos]!r}")
        if m.group("num") is not None:
            tokens.append(("NUM", float(m.group("num"))))
        elif m.group("name") is not None:
            tokens.append(("NAME", m.group("name")))
        else:
            tokens.append(("PUNCT", m.group("punct")))
        pos = m.end()
    return tokens


def _parse_expr(tokens: list, i: int, params_upper: set) -> tuple:
    if i >= len(tokens):
        raise MacroError("Unexpected end of expression.")
    kind, val = tokens[i]
    if kind == "NUM":
        return Literal(float(val)), i + 1
    if kind == "NAME":
        if i + 1 < len(tokens) and tokens[i + 1] == ("PUNCT", "("):
            return _parse_call(tokens, i, params_upper)
        upper = val.upper()
        if upper in params_upper:
            return Param(upper), i + 1
        raise MacroError(
            f"Unknown identifier '{val}'. Must be a declared parameter or a function call."
        )
    raise MacroError(f"Unexpected token {val!r}.")


def _parse_call(tokens: list, i: int, params_upper: set) -> tuple:
    name = tokens[i][1].upper()
    # tokens[i+1] asserted to be '('
    i += 2
    args: list = []
    if i < len(tokens) and tokens[i] == ("PUNCT", ")"):
        return Call(name, args), i + 1
    while True:
        node, i = _parse_expr(tokens, i, params_upper)
        args.append(node)
        if i >= len(tokens):
            raise MacroError("Unterminated function call.")
        if tokens[i] == ("PUNCT", ","):
            i += 1
            continue
        if tokens[i] == ("PUNCT", ")"):
            return Call(name, args), i + 1
        raise MacroError(f"Expected ',' or ')' but got {tokens[i][1]!r}.")


def parse_macro_body(body: str, params: List[str]) -> Node:
    tokens = _tokenize(body)
    if not tokens:
        raise MacroError("Macro body is empty.")
    params_upper = {p.upper() for p in params}
    node, end = _parse_expr(tokens, 0, params_upper)
    if end != len(tokens):
        raise MacroError("Unexpected trailing tokens in macro body.")
    return node


def _validate(node: Node, primitives_upper: set) -> None:
    if isinstance(node, Call):
        if node.name not in primitives_upper:
            raise MacroError(
                f"Unknown function '{node.name}'. Macros may only call registered primitives."
            )
        for a in node.args:
            _validate(a, primitives_upper)


def compile_macro(
    name: str,
    params: List[str],
    body: str,
    registry: dict,
) -> Callable:
    """Compile a macro spec into a callable usable by the formula evaluator.

    Raises MacroError for any invalid input. Never touches eval/exec.
    """
    if not name or not _IDENT_RE.match(name):
        raise MacroError("Macro name must be a valid identifier (letters, digits, underscore).")
    upper_name = name.upper()
    if upper_name in RESERVED_KEYWORDS:
        raise MacroError(f"Macro name '{name}' is reserved.")

    primitives_upper = {k.upper() for k in registry.keys()}
    if upper_name in primitives_upper:
        raise MacroError(
            f"Macro name '{name}' collides with a built-in primitive. Choose a different name."
        )

    seen = set()
    normalized_params: List[str] = []
    for p in params:
        if not p or not _IDENT_RE.match(p):
            raise MacroError(f"Parameter '{p}' is not a valid identifier.")
        upper = p.upper()
        if upper in seen:
            raise MacroError(f"Duplicate parameter '{p}'.")
        seen.add(upper)
        normalized_params.append(upper)

    tree = parse_macro_body(body, normalized_params)
    _validate(tree, primitives_upper)

    index_by_param = {p: idx for idx, p in enumerate(normalized_params)}
    arity = len(normalized_params)

    def _walk(node: Node, values: list):
        if isinstance(node, Literal):
            return node.value
        if isinstance(node, Param):
            return values[index_by_param[node.name]]
        if isinstance(node, Call):
            resolved = [_walk(a, values) for a in node.args]
            fn = registry[node.name]
            return fn(*resolved)
        raise MacroError("Malformed macro AST.")

    def macro_fn(*args):
        if len(args) != arity:
            raise ValueError(
                f"{upper_name} expected {arity} argument(s), got {len(args)}"
            )
        return _walk(tree, list(args))

    macro_fn.__name__ = upper_name
    return macro_fn
