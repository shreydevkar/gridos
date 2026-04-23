import json
import re
import threading
import uuid
from copy import deepcopy
from typing import Optional

from core.functions import FormulaEvaluator
from core.models import AgentIntent, CellState, ChartSpec
from core.utils import a1_to_coords, coords_to_a1


class _FormulaParseError(Exception):
    pass


class VersionConflict(Exception):
    """Raised by write_user_range when an optimistic-concurrency check fails.
    Carries the cell id, the expected version, and the actual version so the
    API layer can render an actionable 409 for the client."""
    def __init__(self, cell: str, expected: int, actual: int):
        self.cell = cell
        self.expected = expected
        self.actual = actual
        super().__init__(f"Cell {cell} version {actual} does not match expected {expected}")


_TOKEN_PATTERN = re.compile(
    # QCELL (sheet-qualified cell ref) must come BEFORE both STRING and
    # CELL. BEFORE STRING because the quoted-sheet-name form starts with
    # a single-quote — without priority, 'Monthly Budget' gets gobbled as
    # a STRING and the trailing !A1 dangles. BEFORE CELL so `Sheet2!A1`
    # doesn't tokenize as three tokens (NAME, <!>, CELL). The regex only
    # matches when `!` + cell-ref follows, so unrelated single-quoted
    # strings ('hello' inside =GREET('hello')) still fall through to STRING.
    r"(?P<QCELL>(?:[A-Za-z_][A-Za-z0-9_]*|'(?:[^'\\]|\\.)*')![A-Za-z]+\d+)"
    r'|(?P<STRING>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
    r"|(?P<NUMBER>\d+\.\d*|\.\d+|\d+)"
    r"|(?P<CELL>[A-Za-z]+\d+)"
    r"|(?P<NAME>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<POW>\*\*|\^)"
    # Comparison first — longest alternatives first so <> / <= / >= aren't
    # misread as < followed by stray token. '=' appears here as equality
    # because the leading '=' that opens every formula is stripped before
    # tokenizing (see _evaluate_formula_string).
    r"|(?P<CMP><>|<=|>=|<|>|=)"
    r"|(?P<CONCAT>&)"
    r"|(?P<OP>[+\-*/])"
    r"|(?P<LPAREN>\()"
    r"|(?P<RPAREN>\))"
    r"|(?P<COMMA>,)"
    r"|(?P<COLON>:)"
    r"|(?P<WS>\s+)"
)


_PERCENT_SUFFIX = re.compile(r"(\d+\.\d*|\.\d+|\d+)\s*%")
# Handles $A1, A$1, $A$1 — all three Excel absolute-ref shapes.
_ABSOLUTE_CELL_REF = re.compile(r"\$?([A-Za-z]+)\$?(\d+)")
_UNICODE_OP_MAP = {
    "\u2212": "-",  # U+2212 minus sign (LLMs sometimes emit this for negatives)
    "\u2013": "-",  # en-dash
    "\u2014": "-",  # em-dash
    "\u00d7": "*",  # multiplication sign
    "\u2217": "*",  # asterisk operator
    "\u00f7": "/",  # division sign
}


def _normalize_excel_formula(expr: str) -> str:
    """Defang common Excel-isms that our parser doesn't natively accept.

    LLM agents are trained on Excel examples and routinely emit formulas
    with dollar-sign absolute refs, percent literals, or unicode math
    operators. Rather than fail those with #PARSE_ERROR!, rewrite them
    to their grid-native equivalents before tokenizing.
    """
    for src_char, dst_char in _UNICODE_OP_MAP.items():
        if src_char in expr:
            expr = expr.replace(src_char, dst_char)
    # $C$5 / $C5 / C$5 → C5 (no fill-down semantics here, so $ is noise)
    expr = _ABSOLUTE_CELL_REF.sub(r"\1\2", expr)
    # 15% → (15*0.01), 0.5% → (0.5*0.01)
    expr = _PERCENT_SUFFIX.sub(r"(\1*0.01)", expr)
    return expr


def _tokenize_formula(src: str):
    """Tokenize a GridOS cell formula.

    NAME and CELL tokens are case-insensitive (normalized to upper), but STRING
    contents are preserved verbatim so plugins like =GREET("Shrey") see the
    caller's exact text.
    """
    tokens = []
    pos = 0
    while pos < len(src):
        match = _TOKEN_PATTERN.match(src, pos)
        if not match:
            raise _FormulaParseError(f"Unexpected character at position {pos}: {src[pos]!r}")
        kind = match.lastgroup
        if kind == "WS":
            pos = match.end()
            continue
        value = match.group()
        if kind in ("CELL", "NAME"):
            value = value.upper()
        elif kind == "QCELL":
            # Split "Sheet2!A1" or "'Monthly Budget'!A1" into a (sheet, cell)
            # tuple so the parser + resolver don't have to re-split the string.
            # The cell part is uppercased; the sheet name is kept case-preserved
            # but matched case-insensitively in the resolver so =sheet2!A1 and
            # =Sheet2!A1 reach the same sheet.
            raw_sheet, raw_cell = value.rsplit("!", 1)
            if raw_sheet.startswith("'") and raw_sheet.endswith("'"):
                sheet_name = _unquote_string(raw_sheet)
            else:
                sheet_name = raw_sheet
            value = (sheet_name, raw_cell.upper())
        tokens.append((kind, value))
        pos = match.end()
    tokens.append(("EOF", ""))
    return tokens


_STRING_ESCAPES = {"\\n": "\n", "\\t": "\t", "\\\"": "\"", "\\'": "'", "\\\\": "\\"}


def _as_concat_str(v) -> str:
    """Excel-compatible string coercion for the & operator.
    Booleans render as TRUE/FALSE; integer-valued floats drop the .0."""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _formula_compare(left, right, op: str) -> bool:
    """Comparison for =, <>, <, >, <=, >=. Numbers compare numerically; strings
    case-insensitive (matches Excel). Cross-type (num vs str) is never equal
    rather than raising — users type heterogeneous cells all the time."""
    if isinstance(left, bool) or isinstance(right, bool):
        left = int(left) if isinstance(left, bool) else left
        right = int(right) if isinstance(right, bool) else right
    same_kind = (
        isinstance(left, (int, float)) and isinstance(right, (int, float))
    ) or (
        isinstance(left, str) and isinstance(right, str)
    )
    if not same_kind:
        return op == "<>"
    if isinstance(left, str):
        left = left.lower()
        right = right.lower()
    if op == "=":
        return left == right
    if op == "<>":
        return left != right
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == ">=":
        return left >= right
    raise _FormulaParseError(f"Unknown comparison operator: {op!r}")


def _unquote_string(raw: str) -> str:
    """Strip surrounding quotes and decode a small set of escapes."""
    body = raw[1:-1]
    if "\\" not in body:
        return body
    out = []
    i = 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            pair = body[i:i + 2]
            out.append(_STRING_ESCAPES.get(pair, pair[1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


class _ExpressionEvaluator:
    """Recursive-descent evaluator for GridOS cell formulas.

    Grammar (Excel-compatible precedence, lowest to highest):
        expression  -> comparison
        comparison  -> concat ((= | <> | < | > | <= | >=) concat)*
        concat      -> additive ('&' additive)*
        additive    -> term (('+' | '-') term)*
        term        -> unary (('*' | '/') unary)*
        unary       -> ('+' | '-') unary | power
        power       -> primary (('^' | '**') unary)?   # right-associative
        primary     -> NUMBER | CELL | NAME '(' args? ')' | '(' expression ')'
        args        -> arg (',' arg)*
        arg         -> CELL ':' CELL   // range, valid only as a direct function arg
                     | expression       // scalar
    """

    def __init__(self, func_registry: FormulaEvaluator, state: dict, target_coords: tuple[int, int], kernel=None, current_sheet: Optional[str] = None):
        self.func_registry = func_registry
        self.state = state
        self.target_coords = target_coords
        self.tokens: list = []
        self.pos = 0
        # Cross-sheet support: when `kernel` + `current_sheet` are provided,
        # QCELL tokens (Sheet2!A1) resolve through kernel.sheets. When either
        # is None (legacy callers), QCELL resolves to #REF! — preserves
        # backward compat for any code path that still instantiates the
        # evaluator without the kernel reference.
        self.kernel = kernel
        self.current_sheet = current_sheet

    def run(self, expression: str):
        self.tokens = _tokenize_formula(expression)
        self.pos = 0
        result = self._parse_expression()
        if self._peek()[0] != "EOF":
            raise _FormulaParseError(f"Unexpected token after expression: {self._peek()[1]!r}")
        return result

    def _peek(self):
        return self.tokens[self.pos]

    def _advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, kind: str):
        tok = self._peek()
        if tok[0] != kind:
            raise _FormulaParseError(f"Expected {kind}, got {tok[0]} ({tok[1]!r})")
        return self._advance()

    def _parse_expression(self):
        return self._parse_comparison()

    def _parse_comparison(self):
        result = self._parse_concat()
        while self._peek()[0] == "CMP":
            op = self._advance()[1]
            right = self._parse_concat()
            # Returning int 1/0 matches Excel's numeric coercion of booleans —
            # IF(A1=B1, x, y) works with no extra casting, and 1*TRUE stays sane.
            result = 1 if _formula_compare(result, right, op) else 0
        return result

    def _parse_concat(self):
        result = self._parse_additive()
        while self._peek()[0] == "CONCAT":
            self._advance()
            right = self._parse_additive()
            # Excel's '&' is string-first: both operands become their display
            # form before joining. Mirror that so =A1&" "&B1 with numbers works.
            result = _as_concat_str(result) + _as_concat_str(right)
        return result

    def _parse_additive(self):
        result = self._parse_term()
        while self._peek()[0] == "OP" and self._peek()[1] in ("+", "-"):
            op = self._advance()[1]
            right = self._parse_term()
            result = result + right if op == "+" else result - right
        return result

    def _parse_term(self):
        result = self._parse_unary()
        while self._peek()[0] == "OP" and self._peek()[1] in ("*", "/"):
            op = self._advance()[1]
            right = self._parse_unary()
            if op == "*":
                result = result * right
            else:
                result = result / right  # ZeroDivisionError propagates
        return result

    def _parse_unary(self):
        if self._peek()[0] == "OP" and self._peek()[1] in ("+", "-"):
            op = self._advance()[1]
            value = self._parse_unary()
            return -value if op == "-" else +value
        return self._parse_power()

    def _parse_power(self):
        base = self._parse_primary()
        if self._peek()[0] == "POW":
            self._advance()
            exponent = self._parse_unary()  # right-associative
            return base ** exponent
        return base

    def _parse_primary(self):
        tok = self._peek()
        kind, text = tok

        if kind == "NUMBER":
            self._advance()
            return float(text) if "." in text else int(text)

        if kind == "STRING":
            self._advance()
            return _unquote_string(text)

        if kind == "CELL":
            self._advance()
            return self._resolve_cell_ref(text)

        if kind == "QCELL":
            # Sheet-qualified cell reference: =Sheet2!A1 or ='Sheet Name'!A1.
            # The tokenizer already split the text into (sheet_name, cell_a1).
            self._advance()
            sheet_name, cell_a1 = text
            return self._resolve_cell_ref(cell_a1, sheet_name=sheet_name)

        if kind == "NAME":
            self._advance()
            self._expect("LPAREN")
            args: list = []
            if self._peek()[0] != "RPAREN":
                args.extend(self._parse_arg())
                while self._peek()[0] == "COMMA":
                    self._advance()
                    args.extend(self._parse_arg())
            self._expect("RPAREN")
            return self.func_registry.evaluate(text, args)

        if kind == "LPAREN":
            self._advance()
            value = self._parse_expression()
            self._expect("RPAREN")
            return value

        raise _FormulaParseError(f"Unexpected token {kind} ({text!r})")

    def _parse_arg(self) -> list:
        """Parse a single function argument. Returns a list — length 1 for a scalar,
        length N for a range literal like A1:A5 (expanded to N cell values).

        Ranges support sheet qualifiers:
          =SUM(A1:A5)              single-sheet range (default sheet)
          =SUM(Sheet2!A1:A5)       qualified start; end implicitly same sheet
          =SUM(Sheet2!A1:Sheet2!A5) qualified both; sheets must match
        """
        peek_kind = self._peek()[0]
        if peek_kind == "CELL":
            saved = self.pos
            start_tok = self._advance()
            if self._peek()[0] == "COLON":
                self._advance()
                if self._peek()[0] != "CELL":
                    raise _FormulaParseError("Expected cell reference after ':'")
                end_tok = self._advance()
                return self._resolve_range_values(start_tok[1], end_tok[1])
            self.pos = saved  # not a range — rewind and parse as a normal expression
        if peek_kind == "QCELL":
            saved = self.pos
            start_tok = self._advance()
            if self._peek()[0] == "COLON":
                self._advance()
                start_sheet, start_cell = start_tok[1]
                next_kind, next_val = self._peek()
                if next_kind == "CELL":
                    self._advance()
                    return self._resolve_range_values(start_cell, next_val, sheet_name=start_sheet)
                if next_kind == "QCELL":
                    self._advance()
                    end_sheet, end_cell = next_val
                    # Excel rejects cross-sheet ranges where the sheets differ;
                    # we do the same — it's almost always a user typo, and
                    # silently collapsing to one side's sheet is worse than a
                    # loud #REF!.
                    if end_sheet.lower() != start_sheet.lower():
                        return ["#REF!"]
                    return self._resolve_range_values(start_cell, end_cell, sheet_name=start_sheet)
                raise _FormulaParseError("Expected cell reference after ':'")
            self.pos = saved
        return [self._parse_expression()]

    def _state_for_sheet(self, sheet_name: Optional[str]):
        """Return the (sheet_state, canonical_sheet_name) for a ref.

        None → self.state (current sheet, legacy path).
        Otherwise → look up on the kernel case-insensitively. Returns
        (None, None) when the named sheet doesn't exist — callers turn
        that into a #REF! sentinel. Falls back to self.state when the
        kernel reference wasn't injected (old callers that constructed
        _ExpressionEvaluator without kernel+current_sheet) so nothing
        in the pre-cross-sheet codepath regresses.
        """
        if sheet_name is None:
            return self.state, self.current_sheet
        if self.kernel is None:
            return None, None
        # Case-insensitive sheet-name lookup (Excel-style).
        target_upper = sheet_name.upper()
        canonical = next(
            (name for name in self.kernel.sheets if name.upper() == target_upper),
            None,
        )
        if canonical is None:
            return None, None
        return self.kernel._sheet_state(canonical), canonical

    def _resolve_range_values(self, start_ref: str, end_ref: str, sheet_name: Optional[str] = None) -> list:
        state, _ = self._state_for_sheet(sheet_name)
        if state is None:
            # Missing sheet — surface as a single #REF! so SUM/AVERAGE/etc.
            # propagate the error rather than silently summing zeros.
            return ["#REF!"]
        r1, c1 = a1_to_coords(start_ref)
        r2, c2 = a1_to_coords(end_ref)
        top, bottom = min(r1, r2), max(r1, r2)
        left, right = min(c1, c2), max(c1, c2)
        values: list = []
        for r in range(top, bottom + 1):
            for c in range(left, right + 1):
                # Dependency tracking for cross-sheet refs is single-sheet-only
                # in v1 — we record the dep in the source sheet's map so
                # intra-sheet recalc still works, but cross-sheet recalc
                # propagation doesn't yet. Initial read is always correct;
                # upstream changes require re-evaluation of the dependent
                # cell (e.g. by editing it or triggering a full rebuild).
                state["dependencies"].setdefault((r, c), set()).add(self.target_coords)
                ref_cell = state["cells"].get((r, c))
                if ref_cell is None:
                    values.append(0.0)
                    continue
                v = ref_cell.value
                if isinstance(v, bool):
                    values.append(1.0 if v else 0.0)
                elif isinstance(v, (int, float)):
                    values.append(float(v))
                else:
                    values.append(0.0)
        return values

    def _resolve_cell_ref(self, ref: str, sheet_name: Optional[str] = None):
        state, _ = self._state_for_sheet(sheet_name)
        if state is None:
            return "#REF!"
        ref_r, ref_c = a1_to_coords(ref)
        state["dependencies"].setdefault((ref_r, ref_c), set()).add(self.target_coords)
        ref_cell = state["cells"].get((ref_r, ref_c))
        if ref_cell is None:
            return 0.0
        value = ref_cell.value
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                # Preserve the raw string. Numeric ops (+ - * /) will raise
                # TypeError → #VALUE!; string ops (&, comparisons) will work.
                # Treating text as 0.0 here silently corrupted =A1&B1 with
                # real strings — that regression is what this fix addresses.
                return value
        return 0.0


class GridOSKernel:
    def __init__(self):
        self.evaluator = FormulaEvaluator()
        self.sheets: dict[str, dict] = {}
        self.sheet_order: list[str] = []
        self.active_sheet = "Sheet1"
        self.workbook_name: str = "Untitled workbook"
        self.chat_log: list[dict] = []
        # RLock so a write inside another write (e.g. a formula recalc that
        # observes a dependent cell's state) doesn't deadlock against itself.
        # The lock is per-kernel, not per-sheet, because cross-sheet formula
        # refs make a single sheet's lock insufficient. When shared workbooks
        # land and one kernel is serving N users, this lock is what prevents
        # interleaved commits from corrupting the cell graph.
        self._write_lock = threading.RLock()
        # Post-commit hooks: each is called with a dict {sheet, changes,
        # agent_id} *after* the write lock is released. main.py uses this to
        # broadcast cell changes over Supabase Realtime so other clients see
        # the edit without refreshing. Engine stays domain-pure — it doesn't
        # know about Supabase, only fires whatever callbacks were registered.
        self._post_commit_hooks: list = []
        self._ensure_sheet(self.active_sheet)

    def add_post_commit_hook(self, hook):
        """Register a callback fired after every successful _commit_write.
        Hook receives {sheet, changes, agent_id} where `changes` is a list
        of {cell, value, formula, version}. Exceptions from hooks are logged
        but never interrupt the commit path."""
        self._post_commit_hooks.append(hook)

    def set_chat_log(self, entries: list[dict]) -> list[dict]:
        if not isinstance(entries, list):
            raise ValueError("chat_log must be a list of entries.")
        self.chat_log = [e for e in entries if isinstance(e, dict)]
        return self.chat_log

    def clear_chat_log(self) -> None:
        self.chat_log = []

    def rename_workbook(self, new_name: str) -> str:
        cleaned = (new_name or "").strip()
        if not cleaned:
            raise ValueError("Workbook name cannot be empty.")
        if len(cleaned) > 120:
            raise ValueError("Workbook name must be 120 characters or fewer.")
        self.workbook_name = cleaned
        return cleaned

    @property
    def cells(self):
        return self.sheets[self.active_sheet]["cells"]

    @property
    def dependencies(self):
        return self.sheets[self.active_sheet]["dependencies"]

    def _ensure_sheet(self, sheet_name: str):
        if sheet_name not in self.sheets:
            self.sheets[sheet_name] = {"cells": {}, "dependencies": {}, "charts": []}
            self.sheet_order.append(sheet_name)
        elif "charts" not in self.sheets[sheet_name]:
            self.sheets[sheet_name]["charts"] = []

    def _sheet_state(self, sheet_name: str | None = None):
        target = sheet_name or self.active_sheet
        self._ensure_sheet(target)
        return self.sheets[target]

    def list_sheets(self) -> list[dict]:
        return [
            {"name": name, "active": name == self.active_sheet}
            for name in self.sheet_order
        ]

    def create_sheet(self, name: str | None = None) -> str:
        base = (name or "Sheet").strip() or "Sheet"
        candidate = base
        counter = 2
        while candidate in self.sheets:
            candidate = f"{base} {counter}"
            counter += 1

        self._ensure_sheet(candidate)
        self.active_sheet = candidate
        return candidate

    def rename_sheet(self, old_name: str, new_name: str) -> str:
        cleaned = new_name.strip()
        if not cleaned:
            raise ValueError("Sheet name cannot be empty.")
        if old_name not in self.sheets:
            raise ValueError(f"Sheet '{old_name}' does not exist.")
        if cleaned != old_name and cleaned in self.sheets:
            raise ValueError(f"Sheet '{cleaned}' already exists.")

        self.sheets[cleaned] = self.sheets.pop(old_name)
        self.sheet_order = [cleaned if name == old_name else name for name in self.sheet_order]
        if self.active_sheet == old_name:
            self.active_sheet = cleaned
        return cleaned

    def activate_sheet(self, name: str) -> str:
        if name not in self.sheets:
            raise ValueError(f"Sheet '{name}' does not exist.")
        self.active_sheet = name
        return name

    def delete_sheet(self, name: str) -> str:
        """Remove a sheet from the workbook. Refuses to delete the last
        remaining sheet (a workbook must always have at least one), and
        re-activates a neighbor if the deleted sheet was the active one.
        Returns the name of the now-active sheet so the caller can update
        its UI without re-fetching."""
        if name not in self.sheets:
            raise ValueError(f"Sheet '{name}' does not exist.")
        if len(self.sheet_order) <= 1:
            raise ValueError("Can't delete the last remaining sheet — a workbook needs at least one.")
        # Pick the neighbor to activate next BEFORE we pop, so index math is
        # straightforward. Prefer the sheet to the right; fall back to the
        # sheet on the left when the deleted sheet was the last one.
        was_active = self.active_sheet == name
        idx = self.sheet_order.index(name)
        neighbor = self.sheet_order[idx + 1] if idx + 1 < len(self.sheet_order) else self.sheet_order[idx - 1]

        self.sheets.pop(name, None)
        self.sheet_order.remove(name)
        if was_active:
            self.active_sheet = neighbor
        return self.active_sheet

    def lock_range(self, start_a1: str, end_a1: str, owner: str = "User", sheet_name: str | None = None):
        state = self._sheet_state(sheet_name)
        r1, c1 = a1_to_coords(start_a1)
        r2, c2 = a1_to_coords(end_a1)

        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                state["cells"][(r, c)] = CellState(locked=True, agent_owner=owner)

    def clear_unlocked(self, sheet_name: str | None = None):
        # Same hook pattern as clear_cells — capture what got wiped so realtime
        # peers see the change. Without this, "Clear unlocked cells" from the
        # File menu silently wiped the other collaborator's grid.
        hook_data = None
        with self._write_lock:
            state = self._sheet_state(sheet_name)
            cleared_a1 = [
                coords_to_a1(r, c)
                for (r, c), cell in state["cells"].items()
                if not cell.locked
            ]
            state["cells"] = {coords: cell for coords, cell in state["cells"].items() if cell.locked}
            state["dependencies"] = {}
            self._rebuild_dependencies(sheet_name)
            if cleared_a1 and self._post_commit_hooks:
                hook_data = {
                    "sheet": sheet_name or self.active_sheet,
                    "changes": [
                        {
                            "cell": a1,
                            "value": "",
                            "formula": None,
                            "datatype": "string",
                            "version": 0,
                            "cleared": True,
                        }
                        for a1 in cleared_a1
                    ],
                    "agent_id": "User",
                }
        if hook_data:
            for hook in self._post_commit_hooks:
                try:
                    hook(hook_data)
                except Exception as e:
                    print(f"[post_commit_hook] {type(e).__name__}: {e}")

    def _is_space_free(self, state: dict, start_r: int, start_c: int, rows: int, cols: int) -> bool:
        for r in range(start_r, start_r + rows):
            for c in range(start_c, start_c + cols):
                cell = state["cells"].get((r, c))
                if cell and cell.locked:
                    return False
        return True

    def _resolve_target(self, state: dict, start_a1: str, payload: list[list], shift_direction: str) -> tuple[int, int, str]:
        start_r, start_c = a1_to_coords(start_a1)
        rows = len(payload)
        cols = len(payload[0]) if rows > 0 else 0
        current_r, current_c = start_r, start_c
        attempts = 0

        while not self._is_space_free(state, current_r, current_c, rows, cols):
            attempts += 1
            if attempts >= 500:
                return start_r, start_c, "ERROR_NO_SPACE"
            if shift_direction == "right":
                current_c += 1
            else:
                current_r += 1

        return current_r, current_c, coords_to_a1(current_r, current_c)

    def process_agent_intent(self, intent: AgentIntent, sheet_name: str | None = None) -> tuple[str, str]:
        state = self._sheet_state(sheet_name)
        target_r, target_c, actual_a1 = self._resolve_target(
            state,
            intent.target_start_a1,
            intent.data_payload,
            intent.shift_direction,
        )
        if actual_a1 == "ERROR_NO_SPACE":
            return intent.target_start_a1, actual_a1

        self._commit_write(target_r, target_c, intent.data_payload, intent.agent_id, sheet_name)
        return intent.target_start_a1, actual_a1

    def preview_agent_intent(self, intent: AgentIntent, sheet_name: str | None = None) -> dict:
        state = self._sheet_state(sheet_name)
        target_r, target_c, actual_a1 = self._resolve_target(
            state,
            intent.target_start_a1,
            intent.data_payload,
            intent.shift_direction,
        )
        preview_cells = []
        if actual_a1 != "ERROR_NO_SPACE":
            for r_offset, row in enumerate(intent.data_payload):
                for c_offset, value in enumerate(row):
                    a1 = coords_to_a1(target_r + r_offset, target_c + c_offset)
                    preview_cells.append({"cell": a1, "value": value})

        return {
            "original_target": intent.target_start_a1,
            "actual_target": actual_a1,
            "preview_cells": preview_cells,
        }

    def write_user_cell(self, target_a1: str, raw_value, user_id: str = "User", sheet_name: str | None = None) -> str:
        return self.write_user_range(target_a1, [[raw_value]], user_id=user_id, sheet_name=sheet_name)

    def clear_cells(self, cell_a1_list: list[str], sheet_name: str | None = None) -> dict:
        """Bulk-clear a list of cells in a single pass. Locked cells are
        skipped (not an error — UX would feel broken if a single locked cell
        in a Del-key selection aborted the entire clear).

        One `_rebuild_dependencies` at the end instead of N — what made the
        per-cell HTTP loop slow on the frontend was also slow on the backend.

        Fires post-commit hooks with the cleared cells so realtime peers
        see the delete without a refresh. Before this was added, Delete-key
        clears weren't broadcasting (only writes through `_commit_write`
        did) and the other user's tab stayed on the stale value until they
        reloaded.
        """
        hook_data = None
        with self._write_lock:
            state = self._sheet_state(sheet_name)
            cells = state["cells"]
            cleared, skipped_locked = 0, 0
            cleared_a1: list[str] = []
            for a1 in cell_a1_list:
                coords = a1_to_coords(a1.upper())
                existing = cells.get(coords)
                if existing is None:
                    continue
                if existing.locked:
                    skipped_locked += 1
                    continue
                del cells[coords]
                cleared += 1
                cleared_a1.append(a1.upper())
            if cleared:
                self._rebuild_dependencies(sheet_name)
                if self._post_commit_hooks:
                    hook_data = {
                        "sheet": sheet_name or self.active_sheet,
                        "changes": [
                            {
                                "cell": a1,
                                "value": "",
                                "formula": None,
                                "datatype": "string",
                                "version": 0,
                                "cleared": True,
                            }
                            for a1 in cleared_a1
                        ],
                        "agent_id": "User",
                    }
        if hook_data:
            for hook in self._post_commit_hooks:
                try:
                    hook(hook_data)
                except Exception as e:
                    print(f"[post_commit_hook] {type(e).__name__}: {e}")
        return {"cleared": cleared, "skipped_locked": skipped_locked}

    def set_cell_format(self, target_a1: str, decimals: Optional[int], sheet_name: str | None = None) -> dict:
        """Set the per-cell display decimals. None clears the override.

        Mutates ONLY the format — value/formula/datatype/locked are untouched,
        so downstream formula references see the same precise number. If the
        cell does not yet exist, an empty cell is materialized so the format
        sticks if the user later types a number into it.
        """
        state = self._sheet_state(sheet_name)
        cells = state["cells"]
        coords = a1_to_coords(target_a1)
        existing = cells.get(coords)
        if existing is None:
            cells[coords] = CellState(value="", decimals=decimals)
        else:
            existing.decimals = decimals
        return {"cell": target_a1, "decimals": decimals}

    def write_user_range(self, target_a1: str, payload: list[list], user_id: str = "User", sheet_name: str | None = None, expected_versions: dict | None = None) -> str:
        """Commit a rectangle of user-typed values.

        `expected_versions` is an optional {a1: int} map used for optimistic
        concurrency on shared workbooks. If any addressed cell's stored
        version differs from the caller's expectation, the whole write is
        rejected with VersionConflict — the caller should refetch and retry.
        """
        with self._write_lock:
            state = self._sheet_state(sheet_name)
            start_r, start_c = a1_to_coords(target_a1)

            for r_offset, row in enumerate(payload):
                for c_offset, _ in enumerate(row):
                    coords = (start_r + r_offset, start_c + c_offset)
                    existing = state["cells"].get(coords)
                    if existing and existing.locked:
                        raise ValueError(f"Cell {coords_to_a1(*coords)} is locked.")
                    if expected_versions:
                        a1 = coords_to_a1(*coords)
                        if a1 in expected_versions:
                            actual = existing.version if existing else 0
                            if actual != expected_versions[a1]:
                                raise VersionConflict(a1, expected_versions[a1], actual)

            self._commit_write(start_r, start_c, payload, user_id, sheet_name)
            return target_a1

    def _commit_write(self, start_r: int, start_c: int, payload: list[list], agent_id: str, sheet_name: str | None = None):
        # Serialize every commit so two concurrent writers (multi-user
        # scenario, or an agent-apply racing with a user edit) can't interleave
        # partial updates. The RLock means a recalc triggered from inside the
        # commit — which itself can call back into the kernel — still works.
        hook_data = None
        with self._write_lock:
            state = self._sheet_state(sheet_name)
            cells = state["cells"]

            for r_offset, row_data in enumerate(payload):
                for c_offset, raw_val in enumerate(row_data):
                    r = start_r + r_offset
                    c = start_c + c_offset

                    val = self._normalize_user_value(raw_val)
                    computed_val = val
                    formula_str = None

                    if isinstance(val, str) and val.startswith("="):
                        formula_str = val
                        computed_val = self._evaluate_formula_string(val, r, c, sheet_name)

                    existing_cell = cells.get((r, c))
                    existing_locked = existing_cell.locked if existing_cell and existing_cell.locked else False
                    existing_decimals = existing_cell.decimals if existing_cell else None
                    # Optimistic-concurrency version. Bump on every commit so
                    # clients that cached version N can detect their write is
                    # stale. Starts at 1 for a new cell (0 == sentinel "never
                    # written").
                    prev_version = existing_cell.version if existing_cell else 0
                    cells[(r, c)] = CellState(
                        value=computed_val,
                        formula=formula_str,
                        datatype=type(computed_val).__name__,
                        locked=existing_locked,
                        agent_owner=agent_id,
                        decimals=existing_decimals,
                        version=prev_version + 1,
                    )

            self._rebuild_dependencies(sheet_name)
            affected = [(start_r + r_offset, start_c + c_offset) for r_offset, row in enumerate(payload) for c_offset, _ in enumerate(row)]
            for r, c in affected:
                self._recalculate(r, c, sheet_name=sheet_name)

            # Snapshot the direct-write cells for realtime broadcast. Built
            # inside the lock so the snapshot is coherent with the commit;
            # fired outside the lock below so a slow Supabase HTTP call
            # doesn't block other writers.
            if self._post_commit_hooks:
                changes = []
                for r, c in affected:
                    cell = cells.get((r, c))
                    if cell is None:
                        continue
                    changes.append({
                        "cell": coords_to_a1(r, c),
                        "value": cell.value,
                        "formula": cell.formula,
                        "datatype": cell.datatype,
                        "version": cell.version,
                    })
                hook_data = {
                    "sheet": sheet_name or self.active_sheet,
                    "changes": changes,
                    "agent_id": agent_id,
                }

        if hook_data:
            for hook in self._post_commit_hooks:
                try:
                    hook(hook_data)
                except Exception as e:
                    print(f"[post_commit_hook] {type(e).__name__}: {e}")

    def _evaluate_formula_string(self, formula: str, target_r: int, target_c: int, sheet_name: str | None = None):
        expr = formula.strip()
        if not expr.startswith("="):
            return "#PARSE_ERROR!"

        state = self._sheet_state(sheet_name)
        current_sheet = sheet_name or self.active_sheet
        # Pass kernel + current_sheet so QCELL tokens can resolve across
        # sheets via kernel.sheets. Legacy callers that constructed the
        # evaluator with only (registry, state, coords) still work — those
        # kwargs are optional with None defaults.
        parser = _ExpressionEvaluator(
            self.evaluator, state, (target_r, target_c),
            kernel=self, current_sheet=current_sheet,
        )
        try:
            return parser.run(_normalize_excel_formula(expr[1:]))
        except _FormulaParseError:
            return "#PARSE_ERROR!"
        except TypeError:
            return "#VALUE!"
        except ZeroDivisionError:
            return "#DIV/0!"
        except ValueError:
            return "#VALUE! (Invalid Arguments)"

    def _rebuild_dependencies(self, sheet_name: str | None = None):
        state = self._sheet_state(sheet_name)
        state["dependencies"] = {}
        for (r, c), cell in list(state["cells"].items()):
            if cell.formula:
                self._evaluate_formula_string(cell.formula, r, c, sheet_name)

    def _normalize_user_value(self, value):
        if value is None:
            return ""
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned == "":
                return ""
            if cleaned.startswith("="):
                return cleaned
            if cleaned.lower() == "true":
                return True
            if cleaned.lower() == "false":
                return False
            try:
                if "." in cleaned:
                    return float(cleaned)
                return int(cleaned)
            except ValueError:
                return value
        return value

    def _recalculate(self, target_r: int, target_c: int, visited: set | None = None, sheet_name: str | None = None):
        state = self._sheet_state(sheet_name)
        if visited is None:
            visited = set()
        if (target_r, target_c) in visited:
            return
        visited.add((target_r, target_c))

        for dep_r, dep_c in state["dependencies"].get((target_r, target_c), set()):
            dep_cell = state["cells"].get((dep_r, dep_c))
            if dep_cell and dep_cell.formula:
                new_val = self._evaluate_formula_string(dep_cell.formula, dep_r, dep_c, sheet_name)
                dep_cell.value = new_val
                dep_cell.datatype = type(new_val).__name__
                self._recalculate(dep_r, dep_c, visited, sheet_name)

    def get_context_for_ai(self, sheet_name: str | None = None, selected_cells: list[str] | None = None, scope: str = "sheet") -> dict:
        state = self._sheet_state(sheet_name)
        cells = state["cells"]
        if not cells:
            return {
                "occupied_info": "The grid is currently empty.",
                "formatted_data": "No data present.",
                "cell_metadata": {},
                "cell_metadata_json": "{}",
                "occupied_bounds": None,
                "scope": scope,
            }

        if scope == "selection" and selected_cells:
            selected_set = {cell.upper() for cell in selected_cells}
            entries = [
                (a1_to_coords(a1), cells[a1_to_coords(a1)])
                for a1 in selected_set
                if a1_to_coords(a1) in cells
            ]
            occupied_info = ", ".join(sorted(selected_set)) or "No selected cells."
        else:
            entries = sorted(cells.items())
            occupied_info = ", ".join(coords_to_a1(r, c) for (r, c), _ in entries)

        grid_lines = []
        cell_metadata: dict[str, dict] = {}
        rows_coords = []
        cols_coords = []
        for (r, c), cell in entries:
            a1 = coords_to_a1(r, c)
            formula = f" (Formula: {cell.formula})" if cell.formula else ""
            lock = " [LOCKED]" if cell.locked else ""
            grid_lines.append(f"{a1}: {cell.value}{formula}{lock}")
            cell_metadata[a1] = {
                "val": cell.value,
                "locked": bool(cell.locked),
                "type": "formula" if cell.formula else "static",
            }
            rows_coords.append(r)
            cols_coords.append(c)

        occupied_bounds = None
        if rows_coords:
            top = min(rows_coords)
            bottom = max(rows_coords)
            left = min(cols_coords)
            right = max(cols_coords)
            occupied_bounds = {
                "top_left": coords_to_a1(top, left),
                "bottom_right": coords_to_a1(bottom, right),
                "rows": bottom - top + 1,
                "cols": right - left + 1,
            }

        return {
            "occupied_info": occupied_info or "No occupied cells in scope.",
            "formatted_data": "\n".join(grid_lines) if grid_lines else "No data present.",
            "cell_metadata": cell_metadata,
            "cell_metadata_json": json.dumps(cell_metadata, default=str),
            "occupied_bounds": occupied_bounds,
            "scope": scope,
        }

    def export_sheet(self, sheet_name: str | None = None) -> dict:
        target = sheet_name or self.active_sheet
        state = self._sheet_state(target)
        return {
            coords_to_a1(r, c): cell.model_dump()
            for (r, c), cell in state["cells"].items()
        }

    def export_state_dict(self) -> dict:
        return {
            "workbook_name": self.workbook_name,
            "active_sheet": self.active_sheet,
            "sheet_order": self.sheet_order,
            "sheets": {
                name: {
                    "cells": {
                        coords_to_a1(r, c): cell.model_dump()
                        for (r, c), cell in self.sheets[name]["cells"].items()
                    },
                    "charts": [chart.model_dump() for chart in self.sheets[name].get("charts", [])],
                }
                for name in self.sheet_order
            },
            "chat_log": list(self.chat_log),
        }

    def save_state(self, filepath: str = "system_state.gridos"):
        with open(filepath, "w") as f:
            json.dump(self.export_state_dict(), f, indent=2)

    def load_state(self, filepath: str = "system_state.gridos"):
        try:
            with open(filepath, "r") as f:
                import_data = json.load(f)
        except FileNotFoundError:
            return False
        self.apply_state_dict(import_data)
        return True

    def apply_state_dict(self, import_data: dict):
        imported_name = import_data.get("workbook_name")
        if isinstance(imported_name, str) and imported_name.strip():
            self.workbook_name = imported_name.strip()[:120]
        elif "workbook_name" in import_data:
            self.workbook_name = "Untitled workbook"
        if "sheets" in import_data:
            self.sheets = {}
            import_order = import_data.get("sheet_order", list(import_data["sheets"].keys()))
            self.sheet_order = []
            for name in import_order:
                self._ensure_sheet(name)
                sheet_payload = import_data["sheets"].get(name, {})
                # Backward compat: older files stored cells directly at the sheet level
                # (no "cells" key), so treat the whole dict as cells in that case.
                if "cells" in sheet_payload or "charts" in sheet_payload:
                    cell_payload = sheet_payload.get("cells", {})
                    chart_payload = sheet_payload.get("charts", [])
                else:
                    cell_payload = sheet_payload
                    chart_payload = []
                self.sheets[name]["cells"] = {}
                for a1_key, state_dict in cell_payload.items():
                    r, c = a1_to_coords(a1_key)
                    self.sheets[name]["cells"][(r, c)] = CellState(**state_dict)
                self.sheets[name]["charts"] = [ChartSpec(**c) for c in chart_payload]
                self._rebuild_dependencies(name)
            self.active_sheet = import_data.get("active_sheet", self.sheet_order[0] if self.sheet_order else "Sheet1")
        else:
            self.sheets = {}
            self.sheet_order = []
            self.active_sheet = "Sheet1"
            self._ensure_sheet(self.active_sheet)
            for a1_key, state_dict in import_data.items():
                r, c = a1_to_coords(a1_key)
                self.cells[(r, c)] = CellState(**state_dict)
            self._rebuild_dependencies(self.active_sheet)

        imported_log = import_data.get("chat_log")
        self.chat_log = [e for e in imported_log if isinstance(e, dict)] if isinstance(imported_log, list) else []

    # ---------- Charts ----------

    def list_charts(self, sheet_name: str | None = None) -> list[dict]:
        state = self._sheet_state(sheet_name)
        return [chart.model_dump() for chart in state.get("charts", [])]

    def add_chart(self, spec: dict, sheet_name: str | None = None) -> dict:
        state = self._sheet_state(sheet_name)
        payload = dict(spec)
        title = (payload.get("title") or "").strip()

        if not payload.get("id") and title:
            for idx, existing in enumerate(state["charts"]):
                if (existing.title or "").strip().lower() == title.lower():
                    merged = existing.model_dump()
                    merged.update({k: v for k, v in payload.items() if v is not None})
                    updated = ChartSpec(**merged)
                    state["charts"][idx] = updated
                    return updated.model_dump()

        if not payload.get("id"):
            payload["id"] = f"chart_{uuid.uuid4().hex[:8]}"
        chart = ChartSpec(**payload)
        state["charts"].append(chart)
        return chart.model_dump()

    def update_chart(self, chart_id: str, updates: dict, sheet_name: str | None = None) -> dict:
        state = self._sheet_state(sheet_name)
        for idx, chart in enumerate(state["charts"]):
            if chart.id == chart_id:
                merged = chart.model_dump()
                merged.update({k: v for k, v in updates.items() if v is not None})
                updated = ChartSpec(**merged)
                state["charts"][idx] = updated
                return updated.model_dump()
        raise ValueError(f"Chart '{chart_id}' not found on sheet '{sheet_name or self.active_sheet}'.")

    def apply_template_respecting_locks(self, template: dict) -> dict:
        """Apply a template snapshot in place, preserving locked cells.

        For each sheet in the template:
          * keep every currently-locked cell in its current state
          * clear all other (unlocked) cells
          * write template cells into non-locked targets (skip locked collisions)
          * replace charts wholesale (charts have no lock concept)
        """
        sheets_payload = template.get("sheets") or {}
        applied = 0
        skipped_locked = 0
        sheet_order = template.get("sheet_order") or list(sheets_payload.keys())

        for sheet_name in sheet_order:
            if not sheet_name:
                continue
            self._ensure_sheet(sheet_name)
            state = self._sheet_state(sheet_name)
            sheet_payload = sheets_payload.get(sheet_name, {}) or {}
            cell_payload = sheet_payload.get("cells", {}) if isinstance(sheet_payload, dict) else {}
            chart_payload = sheet_payload.get("charts", []) if isinstance(sheet_payload, dict) else []

            locked_cells = {
                coords: cell for coords, cell in state["cells"].items() if cell.locked
            }

            state["cells"] = dict(locked_cells)
            state["dependencies"] = {}

            for a1_key, cell_dict in cell_payload.items():
                try:
                    r, c = a1_to_coords(a1_key)
                except ValueError:
                    continue
                if (r, c) in locked_cells:
                    skipped_locked += 1
                    continue
                state["cells"][(r, c)] = CellState(**cell_dict)
                applied += 1

            state["charts"] = [ChartSpec(**c) for c in chart_payload]
            self._rebuild_dependencies(sheet_name)

        active = template.get("active_sheet")
        if active and active in self.sheets:
            self.active_sheet = active

        return {"applied": applied, "skipped_locked": skipped_locked}

    def delete_chart(self, chart_id: str, sheet_name: str | None = None) -> bool:
        state = self._sheet_state(sheet_name)
        before = len(state["charts"])
        state["charts"] = [c for c in state["charts"] if c.id != chart_id]
        return len(state["charts"]) < before
