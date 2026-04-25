import math
import re
import datetime as _dt
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


# ---------- Internal helpers (range/arg unpacking, type coercion) ----------
#
# The engine's parser hands functions a flat *args list where ranges arrive as
# one list element (_RangeValues, a list subclass) and scalars as individual
# elements. These helpers normalize that shape so each formula can focus on
# its semantics, not on the boundary between scalar args and range args.

def _is_range(a) -> bool:
    # _RangeValues subclasses list, so checking `list` catches both. We never
    # pass plain lists in any other code path, so this is safe.
    return isinstance(a, list)


def _flatten_all(args):
    """Yield every element from a mixed scalar+range arg list, preserving raw types."""
    for a in args:
        if _is_range(a):
            for v in a:
                yield v
        else:
            yield a


def _to_num(v):
    """Coerce to float when possible, else None. Booleans → 1.0/0.0 (Excel)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (ValueError, AttributeError):
            return None
    return None


def _flatten_numerics(args):
    """Yield numeric values, skipping blanks and non-numeric text. Used by SUM/MAX/MIN/AVERAGE."""
    for v in _flatten_all(args):
        n = _to_num(v)
        if n is not None:
            yield n


def _first_error(args) -> Optional[str]:
    """Return the first '#FOO!'-style error sentinel found in args/ranges, or None.
    Excel propagates errors through aggregations rather than silently filtering them."""
    for v in _flatten_all(args):
        if isinstance(v, str) and v.startswith("#") and (v.endswith("!") or v == "#N/A"):
            return v
    return None


def _truthy(v):
    if v is None or v == "":
        return False
    return bool(v)


# ---------- Excel-style criteria parser (powers COUNTIF/SUMIF/AVERAGEIF) ----------

_CRITERIA_OPS = (">=", "<=", "<>", ">", "<", "=")


def _wildcard_to_regex(s: str) -> re.Pattern:
    # Excel wildcards: * matches any run, ? matches single char. Backslash
    # escapes the metas. Match must be case-insensitive and anchor to the
    # whole string (Excel-compatible).
    out = ["^"]
    i = 0
    while i < len(s):
        c = s[i]
        if c == "~" and i + 1 < len(s) and s[i + 1] in ("*", "?"):
            out.append(re.escape(s[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out), re.IGNORECASE)


def _make_criteria(crit) -> Callable:
    """Build a predicate from an Excel-style criteria value.

    Handles: numeric scalars, op-prefixed strings (">5", "<>foo"), bare literals,
    and * / ? wildcards. Case-insensitive on text matches.
    """
    # Numeric scalar — equality
    if isinstance(crit, (int, float)) and not isinstance(crit, bool):
        target = float(crit)
        return lambda v: (_to_num(v) is not None and _to_num(v) == target)

    s = str(crit).strip()

    # Op-prefix
    op = next((o for o in _CRITERIA_OPS if s.startswith(o)), None)
    if op:
        tail = s[len(op):].strip()
        target_num = _to_num(tail)
        if target_num is not None:
            if op == ">=": return lambda v: (_to_num(v) is not None and _to_num(v) >= target_num)
            if op == "<=": return lambda v: (_to_num(v) is not None and _to_num(v) <= target_num)
            if op == ">":  return lambda v: (_to_num(v) is not None and _to_num(v) > target_num)
            if op == "<":  return lambda v: (_to_num(v) is not None and _to_num(v) < target_num)
            if op == "<>": return lambda v: (_to_num(v) is None or _to_num(v) != target_num)
            if op == "=":  return lambda v: (_to_num(v) is not None and _to_num(v) == target_num)
        # Non-numeric tail → string equality semantics (Excel <>foo, =bar)
        if op == "<>": return lambda v: str(v if v is not None else "").strip().lower() != tail.lower()
        if op == "=":  return lambda v: str(v if v is not None else "").strip().lower() == tail.lower()
        # >, <, >=, <= against text — Excel does lexicographic comparison
        if op == ">":  return lambda v: (v is not None and str(v).lower() > tail.lower())
        if op == "<":  return lambda v: (v is not None and str(v).lower() < tail.lower())
        if op == ">=": return lambda v: (v is not None and str(v).lower() >= tail.lower())
        if op == "<=": return lambda v: (v is not None and str(v).lower() <= tail.lower())

    # Wildcards
    if "*" in s or "?" in s:
        pat = _wildcard_to_regex(s)
        return lambda v: (v is not None and bool(pat.match(str(v))))

    # Plain literal — Excel matches both numeric and string-equal
    target_num = _to_num(s)
    s_lower = s.lower()
    def _match(v):
        if target_num is not None and _to_num(v) is not None and _to_num(v) == target_num:
            return True
        if v is None:
            return s == ""
        return str(v).strip().lower() == s_lower
    return _match


# ---------- Excel date <-> Python helpers (1900 system, Lotus bug aware) ----------
#
# Excel's "1900 date system" treats 1900-02-29 as a real day (Lotus-1-2-3 bug
# Microsoft preserved). Day 1 = 1900-01-01, day 60 = 1900-02-29 (fictional),
# day 61 = 1900-03-01. We model that by anchoring to 1899-12-30: serial 1 →
# 1899-12-30 + 1 day = 1899-12-31 in pre-bug land, but for any post-1900-03-01
# date the math lines up with Excel.

_EXCEL_EPOCH = _dt.datetime(1899, 12, 30)

_ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
_US_DATE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$")
_EU_DATE = re.compile(r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*$")


def _to_serial(v):
    """Convert v to an Excel date serial. Accepts serial numbers, ISO/US/EU
    date strings, and datetime objects. Returns None if not interpretable."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, _dt.datetime):
        return (v - _EXCEL_EPOCH).total_seconds() / 86400.0
    if isinstance(v, _dt.date):
        return float((v - _EXCEL_EPOCH.date()).days)
    if isinstance(v, str):
        s = v.strip()
        n = _to_num(s)
        if n is not None:
            return n
        m = _ISO_DATE.match(s)
        if m:
            try:
                d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return float((d - _EXCEL_EPOCH.date()).days)
            except ValueError:
                return None
        m = _US_DATE.match(s)
        if m:
            try:
                yy = int(m.group(3))
                if yy < 100:
                    yy += 2000 if yy < 50 else 1900
                d = _dt.date(yy, int(m.group(1)), int(m.group(2)))
                return float((d - _EXCEL_EPOCH.date()).days)
            except ValueError:
                return None
        m = _EU_DATE.match(s)
        if m:
            try:
                d = _dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                return float((d - _EXCEL_EPOCH.date()).days)
            except ValueError:
                return None
    return None


def _from_serial(serial: float) -> _dt.date:
    return (_EXCEL_EPOCH + _dt.timedelta(days=int(serial))).date()


# ---------- Arithmetic core ----------

@register_tool("SUM")
def _sum(*args):
    err = _first_error(args)
    if err:
        return err
    return sum(_flatten_numerics(args))


@register_tool("MAX")
def _max(*args):
    err = _first_error(args)
    if err:
        return err
    nums = list(_flatten_numerics(args))
    if not nums:
        return 0
    return max(nums)


@register_tool("MIN")
def _min(*args):
    err = _first_error(args)
    if err:
        return err
    nums = list(_flatten_numerics(args))
    if not nums:
        return 0
    return min(nums)


@register_tool("AVERAGE")
def _average(*args):
    err = _first_error(args)
    if err:
        return err
    nums = list(_flatten_numerics(args))
    if not nums:
        return 0
    return sum(nums) / len(nums)


@register_tool("PRODUCT")
def _product(*args):
    err = _first_error(args)
    if err:
        return err
    nums = list(_flatten_numerics(args))
    if not nums:
        return 0
    out = 1.0
    for n in nums:
        out *= n
    return out


@register_tool("CEIL")
def _ceil(value):
    return math.ceil(value)


@register_tool("CEILING")
def _ceiling(value, significance=1):
    sig = float(significance) if significance else 1.0
    if sig == 0:
        return 0
    return math.ceil(float(value) / sig) * sig


@register_tool("FLOOR")
def _floor(value, significance=1):
    sig = float(significance) if significance else 1.0
    if sig == 0:
        return 0
    return math.floor(float(value) / sig) * sig


@register_tool("INT")
def _int(value):
    n = _to_num(value)
    if n is None:
        return "#VALUE!"
    return math.floor(n)


@register_tool("MOD")
def _mod(a, b):
    na, nb = _to_num(a), _to_num(b)
    if na is None or nb is None or nb == 0:
        return "#DIV/0!" if nb == 0 else "#VALUE!"
    return na - nb * math.floor(na / nb)


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


@register_tool("ROUNDUP")
def _roundup(value, digits=0):
    n = _to_num(value)
    if n is None:
        return "#VALUE!"
    factor = 10 ** int(digits)
    return math.copysign(math.ceil(abs(n) * factor), n) / factor


@register_tool("ROUNDDOWN")
def _rounddown(value, digits=0):
    n = _to_num(value)
    if n is None:
        return "#VALUE!"
    factor = 10 ** int(digits)
    return math.copysign(math.floor(abs(n) * factor), n) / factor


@register_tool("TRUNC")
def _trunc(value, digits=0):
    return _rounddown(value, digits)


@register_tool("SIGN")
def _sign(value):
    n = _to_num(value)
    if n is None:
        return "#VALUE!"
    return 1 if n > 0 else (-1 if n < 0 else 0)


@register_tool("EXP")
def _exp(value):
    return math.exp(float(value))


@register_tool("LN")
def _ln(value):
    return math.log(float(value))


@register_tool("LOG10")
def _log10(value):
    return math.log10(float(value))


@register_tool("LOG")
def _log(value, base=10):
    return math.log(float(value), float(base))


# ---------- Logic ----------

@register_tool("IF")
def _if(condition, when_true, when_false=False):
    return when_true if _truthy(condition) else when_false


@register_tool("IFS")
def _ifs(*args):
    # IFS(test1, val1, test2, val2, ...) — first true branch wins.
    if len(args) % 2 != 0:
        return "#N/A"
    for i in range(0, len(args), 2):
        if _truthy(args[i]):
            return args[i + 1]
    return "#N/A"


@register_tool("AND")
def _and(*args):
    return all(_truthy(a) for a in _flatten_all(args))


@register_tool("OR")
def _or(*args):
    return any(_truthy(a) for a in _flatten_all(args))


@register_tool("NOT")
def _not(value):
    return not _truthy(value)


@register_tool("XOR")
def _xor(*args):
    n = sum(1 for a in _flatten_all(args) if _truthy(a))
    return n % 2 == 1


@register_tool("TRUE")
def _true():
    return True


@register_tool("FALSE")
def _false():
    return False


@register_tool("IFERROR")
def _iferror(value, fallback):
    if isinstance(value, str) and value.startswith("#"):
        return fallback
    return value


@register_tool("IFNA")
def _ifna(value, fallback):
    if value == "#N/A":
        return fallback
    return value


# ---------- Comparisons (callable form, used by older agent prompts) ----------

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


# ---------- Type predicates ----------

@register_tool("ISBLANK")
def _isblank(value):
    return value is None or value == ""


@register_tool("ISNUMBER")
def _isnumber(value):
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float))


@register_tool("ISTEXT")
def _istext(value):
    return isinstance(value, str) and not (value.startswith("#") and value.endswith("!"))


@register_tool("ISERROR")
def _iserror(value):
    return isinstance(value, str) and value.startswith("#")


@register_tool("ISNA")
def _isna(value):
    return value == "#N/A"


@register_tool("ISLOGICAL")
def _islogical(value):
    return isinstance(value, bool)


@register_tool("ISEVEN")
def _iseven(value):
    n = _to_num(value)
    if n is None:
        return "#VALUE!"
    return int(n) % 2 == 0


@register_tool("ISODD")
def _isodd(value):
    n = _to_num(value)
    if n is None:
        return "#VALUE!"
    return int(n) % 2 != 0


@register_tool("N")
def _n_coerce(value):
    if isinstance(value, bool):
        return 1 if value else 0
    n = _to_num(value)
    return n if n is not None else 0


# ---------- Counts ----------

@register_tool("COUNT")
def _count(*args):
    """Excel COUNT — counts numeric cells only (ignores text and blanks)."""
    return sum(1 for v in _flatten_all(args)
               if not isinstance(v, bool) and isinstance(v, (int, float)))


@register_tool("COUNTA")
def _counta(*args):
    return sum(1 for v in _flatten_all(args) if v is not None and v != "")


@register_tool("COUNTBLANK")
def _countblank(*args):
    return sum(1 for v in _flatten_all(args) if v is None or v == "")


@register_tool("COUNTIF")
def _countif(rng, criteria):
    if not _is_range(rng):
        rng = [rng]
    pred = _make_criteria(criteria)
    return sum(1 for v in rng if pred(v))


@register_tool("COUNTIFS")
def _countifs(*args):
    """COUNTIFS(range1, crit1, range2, crit2, ...) — AND across all pairs."""
    if len(args) < 2 or len(args) % 2 != 0:
        return "#VALUE!"
    pairs = list(zip(args[::2], args[1::2]))
    ranges = []
    preds = []
    for r, c in pairs:
        if not _is_range(r):
            return "#VALUE!"
        ranges.append(r)
        preds.append(_make_criteria(c))
    n = len(ranges[0])
    if any(len(r) != n for r in ranges):
        return "#VALUE!"
    count = 0
    for i in range(n):
        if all(preds[j](ranges[j][i]) for j in range(len(ranges))):
            count += 1
    return count


# ---------- Conditional aggregations ----------

@register_tool("SUMIF")
def _sumif(rng, criteria, sum_range=None):
    """SUMIF(range, criteria, [sum_range]). When sum_range omitted, sum the
    matching cells of `range` itself."""
    if not _is_range(rng):
        rng = [rng]
    if sum_range is None:
        sum_range = rng
    elif not _is_range(sum_range):
        sum_range = [sum_range]
    pred = _make_criteria(criteria)
    total = 0.0
    for i, v in enumerate(rng):
        if pred(v):
            if i < len(sum_range):
                n = _to_num(sum_range[i])
                if n is not None:
                    total += n
    return total


@register_tool("SUMIFS")
def _sumifs(*args):
    """SUMIFS(sum_range, range1, crit1, range2, crit2, ...). Note: sum_range FIRST."""
    if len(args) < 3 or len(args) % 2 != 1:
        return "#VALUE!"
    sum_range = args[0]
    if not _is_range(sum_range):
        sum_range = [sum_range]
    pairs = list(zip(args[1::2], args[2::2]))
    ranges, preds = [], []
    for r, c in pairs:
        if not _is_range(r):
            return "#VALUE!"
        ranges.append(r)
        preds.append(_make_criteria(c))
    n = len(sum_range)
    if any(len(r) != n for r in ranges):
        return "#VALUE!"
    total = 0.0
    for i in range(n):
        if all(preds[j](ranges[j][i]) for j in range(len(ranges))):
            num = _to_num(sum_range[i])
            if num is not None:
                total += num
    return total


@register_tool("AVERAGEIF")
def _averageif(rng, criteria, avg_range=None):
    if not _is_range(rng):
        rng = [rng]
    if avg_range is None:
        avg_range = rng
    elif not _is_range(avg_range):
        avg_range = [avg_range]
    pred = _make_criteria(criteria)
    nums = []
    for i, v in enumerate(rng):
        if pred(v) and i < len(avg_range):
            n = _to_num(avg_range[i])
            if n is not None:
                nums.append(n)
    if not nums:
        return "#DIV/0!"
    return sum(nums) / len(nums)


@register_tool("AVERAGEIFS")
def _averageifs(*args):
    if len(args) < 3 or len(args) % 2 != 1:
        return "#VALUE!"
    avg_range = args[0]
    if not _is_range(avg_range):
        avg_range = [avg_range]
    pairs = list(zip(args[1::2], args[2::2]))
    ranges, preds = [], []
    for r, c in pairs:
        if not _is_range(r):
            return "#VALUE!"
        ranges.append(r)
        preds.append(_make_criteria(c))
    n = len(avg_range)
    if any(len(r) != n for r in ranges):
        return "#VALUE!"
    nums = []
    for i in range(n):
        if all(preds[j](ranges[j][i]) for j in range(len(ranges))):
            num = _to_num(avg_range[i])
            if num is not None:
                nums.append(num)
    if not nums:
        return "#DIV/0!"
    return sum(nums) / len(nums)


@register_tool("MAXIFS")
def _maxifs(*args):
    if len(args) < 3 or len(args) % 2 != 1:
        return "#VALUE!"
    target = args[0]
    if not _is_range(target):
        target = [target]
    pairs = list(zip(args[1::2], args[2::2]))
    ranges, preds = [], []
    for r, c in pairs:
        if not _is_range(r):
            return "#VALUE!"
        ranges.append(r)
        preds.append(_make_criteria(c))
    n = len(target)
    if any(len(r) != n for r in ranges):
        return "#VALUE!"
    out = None
    for i in range(n):
        if all(preds[j](ranges[j][i]) for j in range(len(ranges))):
            num = _to_num(target[i])
            if num is not None and (out is None or num > out):
                out = num
    return out if out is not None else 0


@register_tool("MINIFS")
def _minifs(*args):
    if len(args) < 3 or len(args) % 2 != 1:
        return "#VALUE!"
    target = args[0]
    if not _is_range(target):
        target = [target]
    pairs = list(zip(args[1::2], args[2::2]))
    ranges, preds = [], []
    for r, c in pairs:
        if not _is_range(r):
            return "#VALUE!"
        ranges.append(r)
        preds.append(_make_criteria(c))
    n = len(target)
    if any(len(r) != n for r in ranges):
        return "#VALUE!"
    out = None
    for i in range(n):
        if all(preds[j](ranges[j][i]) for j in range(len(ranges))):
            num = _to_num(target[i])
            if num is not None and (out is None or num < out):
                out = num
    return out if out is not None else 0


# ---------- Statistical: rank / large / small ----------

@register_tool("LARGE")
def _large(rng, k):
    if not _is_range(rng):
        rng = [rng]
    nums = sorted(
        (n for n in (_to_num(v) for v in rng) if n is not None),
        reverse=True,
    )
    k = int(k)
    if k < 1 or k > len(nums):
        return "#NUM!"
    return nums[k - 1]


@register_tool("SMALL")
def _small(rng, k):
    if not _is_range(rng):
        rng = [rng]
    nums = sorted(n for n in (_to_num(v) for v in rng) if n is not None)
    k = int(k)
    if k < 1 or k > len(nums):
        return "#NUM!"
    return nums[k - 1]


@register_tool("RANK")
def _rank(value, rng, order=0):
    if not _is_range(rng):
        rng = [rng]
    target = _to_num(value)
    if target is None:
        return "#VALUE!"
    nums = [n for n in (_to_num(v) for v in rng) if n is not None]
    if int(order) == 0:
        # Descending — largest is rank 1
        return sum(1 for n in nums if n > target) + 1
    return sum(1 for n in nums if n < target) + 1


@register_tool("MEDIAN")
def _median(*args):
    nums = sorted(_flatten_numerics(args))
    if not nums:
        return 0
    n = len(nums)
    if n % 2 == 1:
        return nums[n // 2]
    return (nums[n // 2 - 1] + nums[n // 2]) / 2


@register_tool("MODE")
def _mode(*args):
    nums = list(_flatten_numerics(args))
    if not nums:
        return "#N/A"
    counts: dict[float, int] = {}
    for n in nums:
        counts[n] = counts.get(n, 0) + 1
    best = max(counts.values())
    if best < 2:
        return "#N/A"
    for n in nums:
        if counts[n] == best:
            return n
    return "#N/A"


@register_tool("STDEV")
def _stdev(*args):
    nums = list(_flatten_numerics(args))
    if len(nums) < 2:
        return "#DIV/0!"
    mean = sum(nums) / len(nums)
    return math.sqrt(sum((n - mean) ** 2 for n in nums) / (len(nums) - 1))


@register_tool("VAR")
def _var(*args):
    nums = list(_flatten_numerics(args))
    if len(nums) < 2:
        return "#DIV/0!"
    mean = sum(nums) / len(nums)
    return sum((n - mean) ** 2 for n in nums) / (len(nums) - 1)


# ---------- Lookups (1D MATCH; INDEX with shape from _RangeValues) ----------

@register_tool("MATCH")
def _match(lookup_value, rng, match_type=1):
    """MATCH(lookup_value, lookup_array, [match_type]).
    match_type 0 = exact (any order); 1 = largest <= (asc sorted); -1 = smallest >= (desc sorted)."""
    if not _is_range(rng):
        rng = [rng]
    mt = int(match_type) if match_type is not None else 1
    if mt == 0:
        # Exact match — case-insensitive strings, numeric equality
        target = _to_num(lookup_value)
        if target is not None:
            for i, v in enumerate(rng):
                vn = _to_num(v)
                if vn is not None and vn == target:
                    return i + 1
        s = str(lookup_value).lower() if lookup_value is not None else ""
        for i, v in enumerate(rng):
            if v is not None and str(v).lower() == s:
                return i + 1
        return "#N/A"
    target = _to_num(lookup_value)
    if target is None:
        return "#N/A"
    if mt == 1:
        # Range must be ascending — return position of largest value ≤ target
        last = None
        for i, v in enumerate(rng):
            n = _to_num(v)
            if n is None:
                continue
            if n <= target:
                last = i + 1
            else:
                break
        return last if last is not None else "#N/A"
    if mt == -1:
        last = None
        for i, v in enumerate(rng):
            n = _to_num(v)
            if n is None:
                continue
            if n >= target:
                last = i + 1
            else:
                break
        return last if last is not None else "#N/A"
    return "#N/A"


@register_tool("INDEX")
def _index(rng, row_num, col_num=None):
    """INDEX(range, row_num, [col_num]). Uses shape stashed by _RangeValues
    (rows/cols attrs) when available. Falls back to 1D treatment."""
    if not _is_range(rng):
        rng = [rng]
    rows = getattr(rng, "rows", None)
    cols = getattr(rng, "cols", None)
    r = int(row_num) if row_num is not None else 0
    c = int(col_num) if col_num is not None else 0
    if rows and cols:
        # Excel allows INDEX(range, 0, c) → whole column, but we return a
        # single cell because GridOS doesn't spill. Treat 0 as 1 for now.
        if r == 0:
            r = 1
        if c == 0:
            c = 1
        if r < 1 or r > rows or c < 1 or c > cols:
            return "#REF!"
        idx = (r - 1) * cols + (c - 1)
        return rng[idx] if idx < len(rng) else "#REF!"
    # 1D fallback — treat row_num as the index, ignore col_num
    if r < 1 or r > len(rng):
        return "#REF!"
    return rng[r - 1]


@register_tool("CHOOSE")
def _choose(index, *options):
    i = int(index)
    if i < 1 or i > len(options):
        return "#VALUE!"
    return options[i - 1]


@register_tool("VLOOKUP")
def _vlookup(lookup_value, rng, col_index, exact=False):
    """VLOOKUP(lookup_value, table, col_index, [range_lookup]).
    range_lookup TRUE (default in Excel) = approximate, FALSE = exact.
    GridOS default to exact (False) since approximate matches silently fail
    on unsorted data and that surprises non-spreadsheet-native users."""
    if not _is_range(rng):
        return "#VALUE!"
    rows = getattr(rng, "rows", None)
    cols = getattr(rng, "cols", None)
    if not rows or not cols:
        return "#REF!"
    col_idx = int(col_index)
    if col_idx < 1 or col_idx > cols:
        return "#REF!"
    is_exact = not _truthy(exact) if isinstance(exact, bool) else (str(exact).upper() == "FALSE" or _to_num(exact) == 0)
    target_num = _to_num(lookup_value)
    target_str = str(lookup_value).lower() if lookup_value is not None else ""
    last_match_row = None
    for r in range(rows):
        first_col = rng[r * cols]
        if is_exact:
            if target_num is not None and _to_num(first_col) is not None and _to_num(first_col) == target_num:
                return rng[r * cols + (col_idx - 1)]
            if first_col is not None and str(first_col).lower() == target_str:
                return rng[r * cols + (col_idx - 1)]
        else:
            n = _to_num(first_col)
            if target_num is not None and n is not None and n <= target_num:
                last_match_row = r
            elif target_num is not None and n is not None and n > target_num:
                break
    if last_match_row is not None:
        return rng[last_match_row * cols + (col_idx - 1)]
    return "#N/A"


@register_tool("HLOOKUP")
def _hlookup(lookup_value, rng, row_index, exact=False):
    if not _is_range(rng):
        return "#VALUE!"
    rows = getattr(rng, "rows", None)
    cols = getattr(rng, "cols", None)
    if not rows or not cols:
        return "#REF!"
    row_idx = int(row_index)
    if row_idx < 1 or row_idx > rows:
        return "#REF!"
    target_num = _to_num(lookup_value)
    target_str = str(lookup_value).lower() if lookup_value is not None else ""
    for c in range(cols):
        head = rng[c]
        if target_num is not None and _to_num(head) is not None and _to_num(head) == target_num:
            return rng[(row_idx - 1) * cols + c]
        if head is not None and str(head).lower() == target_str:
            return rng[(row_idx - 1) * cols + c]
    return "#N/A"


# ---------- Date/time ----------

@register_tool("DATE")
def _date(year, month, day):
    try:
        d = _dt.date(int(year), int(month), int(day))
        return float((d - _EXCEL_EPOCH.date()).days)
    except (ValueError, TypeError):
        return "#NUM!"


@register_tool("YEAR")
def _year(value):
    s = _to_serial(value)
    if s is None:
        return "#VALUE!"
    return _from_serial(s).year


@register_tool("MONTH")
def _month(value):
    s = _to_serial(value)
    if s is None:
        return "#VALUE!"
    return _from_serial(s).month


@register_tool("DAY")
def _day(value):
    s = _to_serial(value)
    if s is None:
        return "#VALUE!"
    return _from_serial(s).day


@register_tool("WEEKDAY")
def _weekday(value, return_type=1):
    s = _to_serial(value)
    if s is None:
        return "#VALUE!"
    # Python: Mon=0..Sun=6. Excel return_type=1 (default): Sun=1..Sat=7.
    py_dow = _from_serial(s).weekday()
    rt = int(return_type)
    if rt == 1:
        return ((py_dow + 1) % 7) + 1
    if rt == 2:
        return py_dow + 1  # Mon=1..Sun=7
    if rt == 3:
        return py_dow      # Mon=0..Sun=6
    return "#NUM!"


@register_tool("TODAY")
def _today():
    return float((_dt.date.today() - _EXCEL_EPOCH.date()).days)


@register_tool("NOW")
def _now():
    delta = _dt.datetime.now() - _EXCEL_EPOCH
    return delta.total_seconds() / 86400.0


@register_tool("DATEDIF")
def _datedif(start, end, unit):
    s1 = _to_serial(start)
    s2 = _to_serial(end)
    if s1 is None or s2 is None:
        return "#VALUE!"
    d1 = _from_serial(s1)
    d2 = _from_serial(s2)
    u = str(unit).upper().strip('"\'')
    if u == "D":
        return (d2 - d1).days
    if u == "M":
        return (d2.year - d1.year) * 12 + (d2.month - d1.month) - (1 if d2.day < d1.day else 0)
    if u == "Y":
        years = d2.year - d1.year
        if (d2.month, d2.day) < (d1.month, d1.day):
            years -= 1
        return years
    if u == "MD":
        # Days difference, ignoring months/years
        return (d2.day - d1.day) % 31
    if u == "YM":
        return (d2.month - d1.month) % 12
    if u == "YD":
        try:
            anniv = d1.replace(year=d2.year)
        except ValueError:
            anniv = d1.replace(year=d2.year, day=28)
        if anniv > d2:
            anniv = anniv.replace(year=d2.year - 1)
        return (d2 - anniv).days
    return "#VALUE!"


@register_tool("DAYS")
def _days(end, start):
    s1 = _to_serial(start)
    s2 = _to_serial(end)
    if s1 is None or s2 is None:
        return "#VALUE!"
    return int(s2 - s1)


@register_tool("EDATE")
def _edate(start, months):
    s = _to_serial(start)
    if s is None:
        return "#VALUE!"
    d = _from_serial(s)
    total_months = d.year * 12 + d.month - 1 + int(months)
    new_year, new_month = divmod(total_months, 12)
    new_month += 1
    # Clamp to last day of month if d.day too large
    import calendar
    last = calendar.monthrange(new_year, new_month)[1]
    new_day = min(d.day, last)
    out = _dt.date(new_year, new_month, new_day)
    return float((out - _EXCEL_EPOCH.date()).days)


@register_tool("EOMONTH")
def _eomonth(start, months):
    s = _to_serial(start)
    if s is None:
        return "#VALUE!"
    d = _from_serial(s)
    total_months = d.year * 12 + d.month - 1 + int(months)
    new_year, new_month = divmod(total_months, 12)
    new_month += 1
    import calendar
    last = calendar.monthrange(new_year, new_month)[1]
    out = _dt.date(new_year, new_month, last)
    return float((out - _EXCEL_EPOCH.date()).days)


@register_tool("DATEVALUE")
def _datevalue(text):
    s = _to_serial(text)
    if s is None:
        return "#VALUE!"
    return s


@register_tool("HOUR")
def _hour(value):
    s = _to_serial(value)
    if s is None:
        return "#VALUE!"
    frac = s - math.floor(s)
    return int(frac * 24)


@register_tool("MINUTE")
def _minute(value):
    s = _to_serial(value)
    if s is None:
        return "#VALUE!"
    frac = s - math.floor(s)
    return int((frac * 24 * 60) % 60)


@register_tool("SECOND")
def _second(value):
    s = _to_serial(value)
    if s is None:
        return "#VALUE!"
    frac = s - math.floor(s)
    return int(round(frac * 86400)) % 60


# ---------- String/text ----------

def _str(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


@register_tool("LEN")
def _len(text):
    return len(_str(text))


@register_tool("LEFT")
def _left(text, num_chars=1):
    n = int(num_chars)
    if n < 0:
        return "#VALUE!"
    return _str(text)[:n]


@register_tool("RIGHT")
def _right(text, num_chars=1):
    n = int(num_chars)
    if n < 0:
        return "#VALUE!"
    return _str(text)[-n:] if n > 0 else ""


@register_tool("MID")
def _mid(text, start, num_chars):
    s = _str(text)
    start_i = int(start) - 1
    n = int(num_chars)
    if start_i < 0 or n < 0:
        return "#VALUE!"
    return s[start_i:start_i + n]


@register_tool("TRIM")
def _trim(text):
    # Excel TRIM collapses internal whitespace too, not just edge trim
    return re.sub(r"\s+", " ", _str(text)).strip()


@register_tool("UPPER")
def _upper(text):
    return _str(text).upper()


@register_tool("LOWER")
def _lower(text):
    return _str(text).lower()


@register_tool("PROPER")
def _proper(text):
    return _str(text).title()


@register_tool("FIND")
def _find(needle, haystack, start=1):
    s = _str(haystack)
    n = _str(needle)
    start_i = max(0, int(start) - 1)
    idx = s.find(n, start_i)
    if idx < 0:
        return "#VALUE!"
    return idx + 1


@register_tool("SEARCH")
def _search(needle, haystack, start=1):
    # Like FIND but case-insensitive and supports * / ? wildcards
    s = _str(haystack)
    n = _str(needle)
    start_i = max(0, int(start) - 1)
    if "*" in n or "?" in n:
        pat = _wildcard_to_regex(n)
        m = pat.search(s[start_i:])
        if not m:
            return "#VALUE!"
        return start_i + m.start() + 1
    idx = s.lower().find(n.lower(), start_i)
    if idx < 0:
        return "#VALUE!"
    return idx + 1


@register_tool("SUBSTITUTE")
def _substitute(text, old, new, instance=None):
    s = _str(text)
    o = _str(old)
    nw = _str(new)
    if not o:
        return s
    if instance is None:
        return s.replace(o, nw)
    inst = int(instance)
    count = 0
    pos = 0
    out = []
    while True:
        i = s.find(o, pos)
        if i < 0:
            out.append(s[pos:])
            break
        count += 1
        if count == inst:
            out.append(s[pos:i])
            out.append(nw)
            out.append(s[i + len(o):])
            return "".join(out)
        out.append(s[pos:i + len(o)])
        pos = i + len(o)
    return "".join(out)


@register_tool("REPLACE")
def _replace(text, start, num_chars, new_text):
    s = _str(text)
    start_i = int(start) - 1
    n = int(num_chars)
    if start_i < 0 or n < 0:
        return "#VALUE!"
    return s[:start_i] + _str(new_text) + s[start_i + n:]


@register_tool("CONCAT")
def _concat(*args):
    return "".join(_str(v) for v in _flatten_all(args))


@register_tool("CONCATENATE")
def _concatenate(*args):
    return _concat(*args)


@register_tool("TEXTJOIN")
def _textjoin(separator, ignore_empty, *args):
    sep = _str(separator)
    skip_empty = _truthy(ignore_empty)
    parts = []
    for v in _flatten_all(args):
        if skip_empty and (v is None or v == ""):
            continue
        parts.append(_str(v))
    return sep.join(parts)


@register_tool("REPT")
def _rept(text, n):
    return _str(text) * int(n)


@register_tool("EXACT")
def _exact(a, b):
    return _str(a) == _str(b)


@register_tool("VALUE")
def _value(text):
    n = _to_num(text)
    if n is None:
        return "#VALUE!"
    return n


@register_tool("TEXT")
def _text(value, fmt):
    """Best-effort TEXT — supports plain numeric formats and a couple of
    date formats. Full Excel format-string compat is out of scope for v1."""
    f = str(fmt).strip('"\'')
    n = _to_num(value)
    # Date formats
    if any(k in f.lower() for k in ("yyyy", "yy", "mm", "dd", "mmm")):
        s = _to_serial(value)
        if s is None:
            return _str(value)
        d = _from_serial(s)
        out = f
        out = out.replace("yyyy", f"{d.year:04d}").replace("YYYY", f"{d.year:04d}")
        out = out.replace("yy", f"{d.year % 100:02d}").replace("YY", f"{d.year % 100:02d}")
        # Order matters — mmm/MMM before mm
        out = out.replace("mmmm", d.strftime("%B")).replace("MMMM", d.strftime("%B"))
        out = out.replace("mmm", d.strftime("%b")).replace("MMM", d.strftime("%b"))
        out = out.replace("mm", f"{d.month:02d}").replace("MM", f"{d.month:02d}")
        out = out.replace("dd", f"{d.day:02d}").replace("DD", f"{d.day:02d}")
        return out
    # Numeric formats
    if n is not None:
        if "%" in f:
            decimals = 0
            m = re.search(r"\.0+", f)
            if m:
                decimals = len(m.group(0)) - 1
            return f"{n * 100:.{decimals}f}%"
        m = re.match(r"^0+(\.0+)?$", f)
        if m:
            decimals = len(f.split(".")[1]) if "." in f else 0
            return f"{n:.{decimals}f}"
        # Comma-grouped, e.g. "#,##0" or "#,##0.00"
        if "#,##" in f or "0,000" in f:
            decimals = len(f.split(".")[1].replace("0", "")) if "." in f else 0
            decimals = len(f.split(".")[1]) if "." in f else 0
            return f"{n:,.{decimals}f}"
        return _str(value)
    return _str(value)


@register_tool("CHAR")
def _char(n):
    try:
        return chr(int(n))
    except (ValueError, TypeError):
        return "#VALUE!"


@register_tool("CODE")
def _code(text):
    s = _str(text)
    if not s:
        return "#VALUE!"
    return ord(s[0])


# ---------- Misc placeholders the agent prompts may reference ----------

@register_tool("ROW")
def _row():
    # Without target_coords plumbed into functions, we fall back to 1.
    # Most benchmark questions use ROW() inside formulas only when a cell
    # already has a known position; this is a best-effort placeholder.
    return 1


@register_tool("COLUMN")
def _column():
    return 1


@register_tool("PI")
def _pi():
    return math.pi


@register_tool("E")
def _e_const():
    return math.e


@register_tool("RAND")
def _rand():
    import random
    return random.random()


@register_tool("RANDBETWEEN")
def _randbetween(low, high):
    import random
    return random.randint(int(low), int(high))


# ---------- Evaluator (dispatch + plugin gating) ----------

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
