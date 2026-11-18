"""Deterministic Cognos-to-DAX expression translation.

This module converts a curated subset of Cognos report expressions into DAX without calling an AI
provider. Translations that are fully understood are returned with ``confident=True`` so the
pipeline can skip the AI step. Expressions that contain constructs outside the supported set are
still translated on a best-effort basis and returned with ``confident=False`` so a human or AI
provider can review and refine them.

Cognos business date/time functions (``_add_days``, ``_day_of_week``, ``current_date`` ...) are
translated to scalar DAX date arithmetic, never to time-intelligence table functions. Functions
whose Cognos semantics are undocumented or differ from the closest DAX function are emitted as a
best-effort hint with ``confident=False``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# Cognos summary/aggregate keyword -> DAX aggregation function.
_AGGREGATE_TO_DAX: dict[str, str] = {
    "total": "SUM",
    "sum": "SUM",
    "average": "AVERAGE",
    "avg": "AVERAGE",
    "minimum": "MIN",
    "min": "MIN",
    "maximum": "MAX",
    "max": "MAX",
    "count": "COUNT",
    "countdistinct": "DISTINCTCOUNT",
}

# Row-level (detail) expressions under an aggregate need the iterator form of the aggregate.
_ITERATOR_FOR_AGGREGATE: dict[str, str] = {
    "SUM": "SUMX",
    "AVERAGE": "AVERAGEX",
    "MIN": "MINX",
    "MAX": "MAXX",
    "COUNT": "COUNTX",
}

# Cognos scalar function -> DAX function (same argument order unless noted in _translate_functions).
# Cognos nullif is not mapped: DAX has no NULLIF function.
_FUNCTION_MAP: dict[str, str] = {
    "substring": "MID",
    "character_length": "LEN",
    "char_length": "LEN",
    "length": "LEN",
    "upper": "UPPER",
    "lower": "LOWER",
    "trim": "TRIM",
    "abs": "ABS",
    "round": "ROUND",
    "ceiling": "CEILING",
    "floor": "FLOOR",
    "sqrt": "SQRT",
    "coalesce": "COALESCE",
}

# Names that are valid DAX functions after translation; used to detect untranslated calls.
_KNOWN_DAX_FUNCTIONS = (
    set(_FUNCTION_MAP.values())
    | set(_AGGREGATE_TO_DAX.values())
    | set(_ITERATOR_FOR_AGGREGATE.values())
    | {
        "BLANK",
        "IF",
        "SWITCH",
        "YEAR",
        "MONTH",
        "DAY",
        "TODAY",
        "NOW",
        "AND",
        "OR",
        "NOT",
        "TRUE",
        "FALSE",
    }
)

_REFERENCE = re.compile(r"\[[^\[\]]+\](?:\.\[[^\[\]]+\])*")
_SIMPLE_REFERENCE = re.compile(r"^\[[^\[\]]+\](?:\.\[[^\[\]]+\])*$")
# Cognos function names may contain hyphens or leading underscores (running-count, _round,
# _add_years). Capture the whole token so unknown Cognos functions are detected, not just the DAX
# fragment after a hyphen.
_FUNCTION_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_\-]*)\s*\(")
# A Cognos string literal ('' escapes a quote) or a bracketed name that may contain quotes.
_LITERAL_OR_BRACKET = re.compile(r"'(?:[^']|'')*'|\[[^\[\]]*\]")
# Macros (#...#) and prompt parameters (?name?) have no deterministic DAX equivalent.
_MACRO_OR_PARAMETER = re.compile(r"#|\?[^?\s][^?]*\?")
# An innermost case block: it contains no nested ``case`` keyword.
_CASE_BLOCK = re.compile(r"\bcase\b(?P<body>(?:(?!\bcase\b).)*?)\bend\b", re.IGNORECASE | re.DOTALL)
_CASE_WHEN = re.compile(r"\bwhen\b", re.IGNORECASE)
_CASE_CLAUSE = re.compile(
    r"\bwhen\s+(.+?)\s+then\s+(.+?)(?=\s+when\s+|\s+else\s+|\s*$)", re.IGNORECASE | re.DOTALL
)
_CASE_ELSE = re.compile(r"\belse\s+(.+?)\s*$", re.IGNORECASE | re.DOTALL)
_COLUMN_REFERENCE = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*\[[^\[\]]+\]")
# A bracketed DAX name or a double-quoted DAX string literal.
_DAX_NAME_OR_STRING = re.compile(r'\[[^\[\]]*\]|"(?:[^"]|"")*"')
_AGGREGATE_CALL = re.compile(
    r"\b(?:SUMX?|AVERAGEX?|MINX?|MAXX?|COUNTX?|COUNTA|COUNTROWS|DISTINCTCOUNT)\s*\(",
    re.IGNORECASE,
)

# Cognos business date/time functions handled by _translate_date_functions (name -> arity).
_DATE_FUNCTION_ARITY: dict[str, int] = {
    "_add_days": 2,
    "_add_hours": 2,
    "_add_minutes": 2,
    "_add_seconds": 2,
    "_add_months": 2,
    "_add_years": 2,
    "_first_of_month": 1,
    "_last_of_month": 1,
    "_days_to_end_of_month": 1,
    "_day_of_week": 2,
    "_day_of_year": 1,
    "_week_of_year": 1,
    "_days_between": 2,
    "_year": 1,
    "_month": 1,
    "_day": 1,
    "_hour": 1,
    "_minute": 1,
    "_second": 1,
    "_make_timestamp": 3,
}
_COGNOS_DATE_FUNCTION_NAMES = frozenset(name.upper() for name in _DATE_FUNCTION_ARITY)
# Cognos date part functions that map one-to-one onto a DAX function returning BLANK() for BLANK().
_NATIVE_DATE_PARTS: dict[str, str] = {
    "_year": "YEAR",
    "_month": "MONTH",
    "_day": "DAY",
    "_hour": "HOUR",
    "_minute": "MINUTE",
    "_second": "SECOND",
}
# Cognos _add_* function -> divisor that converts its amount into days.
_OFFSET_DIVISORS: dict[str, str | None] = {
    "_add_days": None,
    "_add_hours": "24",
    "_add_minutes": "1440",
    "_add_seconds": "86400",
}
# Cognos clock constants (written without parentheses) -> DAX function.
_DATE_CONSTANTS: dict[str, str] = {
    "current_date": "TODAY()",
    "localtimestamp": "NOW()",
    "current_timestamp": "UTCNOW()",
}
# DAX functions that only the date translation emits. They are accepted after the date pass but
# not in the source expression, so a user-written Cognos mod(), datediff() etc. (whose semantics
# differ from DAX) still counts as an untranslated call.
_DATE_DAX_FUNCTIONS = frozenset(
    {
        "WEEKDAY",
        "WEEKNUM",
        "EDATE",
        "EOMONTH",
        "DATEDIFF",
        "DATE",
        "MOD",
        "HOUR",
        "MINUTE",
        "SECOND",
        "ISBLANK",
        "UTCNOW",
        "VAR",
        "RETURN",
    }
)
_CLOCK_NOTE = (
    "current_date/localtimestamp became TODAY()/NOW(): a calculated column evaluates them at "
    "data refresh (a measure at query time) and the Power BI service clock is UTC, while Cognos "
    "reads the database server clock at query time"
)
_IDENTIFIER_START = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INTEGER_LITERAL = re.compile(r"[+-]?\s*\d+")
# A bracketed name; ``]]`` is an escaped ``]`` in both Cognos and DAX.
_BRACKET_NAME = r"\[(?:[^\]]|\]\])*\]"
_LEAF_REFERENCE = re.compile(rf"(?:[A-Za-z_][A-Za-z0-9_]*|'(?:[^']|'')*')?{_BRACKET_NAME}")
_ATOMIC_OPERAND = re.compile(
    r"\d+(?:\.\d+)?|(?:TODAY|NOW|UTCNOW)\(\)|__c2d\d+"
    rf"|(?:[A-Za-z_][A-Za-z0-9_ ]*|'(?:[^']|'')*'){_BRACKET_NAME}"
)
_LITERAL_OR_BRACKET_NAME = re.compile(rf"'(?:[^']|'')*'|{_BRACKET_NAME}")
# Bare words that are valid in the emitted DAX.
_ALLOWED_BARE_WORDS = frozenset({"TRUE", "FALSE", "NOT", "VAR", "RETURN"})
_DATEDIFF_INTERVAL = re.compile(
    r",\s*(?:SECOND|MINUTE|HOUR|DAY|WEEK|MONTH|QUARTER|YEAR)\s*\)", re.IGNORECASE
)
_DAX_NAME_STRING_OR_QUOTED = re.compile(rf"{_BRACKET_NAME}|\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*'")
_BARE_WORD = re.compile(r"(?<![A-Za-z0-9_.$])[A-Za-z_][A-Za-z0-9_]*")
# The only place the emitted DAX names the table without a column: an iterator's first argument.
_ITERATOR_TABLE_ARGUMENT = re.compile(r"\b(?:SUMX|AVERAGEX|MINX|MAXX|COUNTX)\s*\(\s*$", re.I)
# Tokens of DAX text whose names and strings were reduced to ``[]``, ``""`` and ``''``.
_REDUCED_TOKEN = re.compile(
    r"''\[\]|\[\]|\"\"|''|\d+\.?\d*|\.\d+|[A-Za-z_][A-Za-z0-9_]*|<=|>=|<>|&&|\|\||\S"
)
_EMPTY_PARENTHESES = re.compile(r"(?:^|[^A-Za-z0-9_\s])\s*\(\s*\)")
# A Cognos date or timestamp literal (2024-01-31, 2024-01-31 10:00:00) outside strings.
_DATE_LITERAL = re.compile(r"(?<![\w.])\d{4}-\d{1,2}-\d{1,2}(?![\w.])")
_TIMESTAMP_LITERAL = re.compile(r"(?<![\w.])\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}")
_DAX_STRING_ARGUMENT = re.compile(r'\(*\s*"(?:[^"]|"")*"\s*\)*')
# A number literal written with a decimal point (1.5, .5, 2.).
_DECIMAL_LITERAL = re.compile(r"(?<![\w.])(?:\d+\.\d*|\.\d+)")
# DAX strings and bracketed names, hidden from the rewrite passes, and their placeholders.
_PROTECTED_TEXT = re.compile(rf'"(?:[^"]|"")*"|{_BRACKET_NAME}')
_PLACEHOLDER = re.compile(r'"(\d+)"|\[(\d+)\]')
_DAX_NUMBER = re.compile(r"\d+\.?\d*|\.\d+")
_BINARY_OPERATORS = frozenset(
    {"+", "-", "*", "/", "^", "&", "=", "<", ">", "<=", ">=", "<>", "&&", "||"}
)
_OPERAND_TOKENS = frozenset({"[]", "''[]", '""', "''"})
_ARITHMETIC_OPERATORS = frozenset({"+", "-", "*", "/", "^"})
_GENERATED_VARIABLE = re.compile(r"__c2d\d+")
# (minimum, maximum) argument count of each DAX function the translation emits, as enforced by the
# Power BI engine ("Too few/many arguments were passed to the <F> function"); None: no maximum.
# MINX and MAXX accept an optional third flag that the translation never emits.
_DAX_ARITY: dict[str, tuple[int, int | None]] = {
    **dict.fromkeys(("BLANK", "TODAY", "NOW", "UTCNOW", "TRUE", "FALSE"), (0, 0)),
    **dict.fromkeys(
        (
            "LEN",
            "UPPER",
            "LOWER",
            "TRIM",
            "ABS",
            "SQRT",
            "SUM",
            "AVERAGE",
            "COUNT",
            "DISTINCTCOUNT",
            "YEAR",
            "MONTH",
            "DAY",
            "HOUR",
            "MINUTE",
            "SECOND",
            "ISBLANK",
        ),
        (1, 1),
    ),
    **dict.fromkeys(("MIN", "MAX", "WEEKDAY", "WEEKNUM"), (1, 2)),
    **dict.fromkeys(
        (
            "ROUND",
            "CEILING",
            "FLOOR",
            "SUMX",
            "AVERAGEX",
            "MINX",
            "MAXX",
            "COUNTX",
            "AND",
            "OR",
            "EDATE",
            "EOMONTH",
            "MOD",
        ),
        (2, 2),
    ),
    **dict.fromkeys(("MID", "DATEDIFF", "DATE"), (3, 3)),
    "IF": (2, 3),
    "COALESCE": (2, None),
    "SWITCH": (3, None),
}
# Calls whose value is an integer whatever their arguments (Cognos and DAX date parts, LEN,
# CEILING(x, 1), FLOOR(x, 1)); Cognos _second can be fractional. ABS keeps an integer an integer,
# so its argument is checked instead.
_INTEGER_VALUED_CALLS = frozenset(
    {
        "_YEAR",
        "_MONTH",
        "_DAY",
        "_HOUR",
        "_MINUTE",
        "_DAY_OF_WEEK",
        "_DAY_OF_YEAR",
        "_WEEK_OF_YEAR",
        "_DAYS_TO_END_OF_MONTH",
        "YEAR",
        "MONTH",
        "DAY",
        "LEN",
        "CEILING",
        "FLOOR",
    }
)
_INTEGER_PRESERVING_CALLS = frozenset({"ABS"})
# DAX calls that return text; in arithmetic: "Cannot convert value ... of type Text to type Number".
_TEXT_VALUED_CALLS = frozenset({"UPPER", "LOWER", "TRIM", "MID"})
# Emitted DAX calls whose value is a number or date, or True/False, whatever their arguments.
# MIN/MAX(X) and COALESCE take the type of their arguments, so they are not listed.
_NUMBER_VALUED_CALLS = frozenset(
    {
        *("LEN", "ABS", "ROUND", "CEILING", "FLOOR", "SQRT", "MOD"),
        *("SUM", "AVERAGE", "COUNT", "DISTINCTCOUNT", "SUMX", "AVERAGEX", "COUNTX"),
        *("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "WEEKDAY", "WEEKNUM", "DATEDIFF"),
        *("DATE", "EDATE", "EOMONTH", "TODAY", "NOW", "UTCNOW"),
    }
)
_BOOLEAN_VALUED_CALLS = frozenset({"AND", "OR", "ISBLANK", "TRUE", "FALSE"})
# Argument positions (0-based; None: every argument) where DAX needs a number ("Cannot convert
# value ... of type Text to type Number"; SUMX/AVERAGEX: "cannot work with values of type
# String") or a True/False value ("... to type True/False"). Only functions the translation
# emits are listed; an unmapped Cognos power, exp, ln or log is already an untranslated call.
_NUMBER_ARGUMENTS: dict[str, tuple[int, ...] | None] = {
    **dict.fromkeys(("ABS", "SQRT", "ROUND", "CEILING", "FLOOR", "MOD"), None),
    "MID": (1, 2),
    "SUMX": (1,),
    "AVERAGEX": (1,),
}
_LOGICAL_ARGUMENTS: dict[str, tuple[int, ...] | None] = {
    "AND": None,
    "OR": None,
    "IF": (0,),
}
# Date (and DATE's year/month/day) argument positions of the emitted DAX. Text that comes only
# from literals is rejected there, as Cognos rejects a string literal as a date ("Cannot convert
# value 'x' of type Text to type Date"); text from a column may hold a date and converts.
_DATE_ARGUMENTS: dict[str, tuple[int, ...] | None] = {
    **dict.fromkeys(("YEAR", "MONTH", "DAY", "HOUR", "MINUTE", "SECOND", "DATE"), None),
    **dict.fromkeys(("WEEKDAY", "WEEKNUM", "EDATE", "EOMONTH"), (0,)),
    "DATEDIFF": (0, 1),
}
_COMPARISON_OPERATORS = frozenset({"=", "<", ">", "<=", ">=", "<>"})
# Value kinds inferred by _TextOperandFinder: unknown type, BLANK(), a number computed only from
# number literals, another number or date, True/False, text, text computed only from literals,
# a conditional that may yield text (text and unknown results), and a conditional whose results
# have different known types (a variant), with or without text.
_UNKNOWN, _BLANK, _NUMBER_LITERAL, _NUMBER = "unknown", "blank", "number literal", "number"
_BOOLEAN, _TEXT, _LITERAL_TEXT = "boolean", "text", "literal"
_MAYBE_TEXT, _MIXED_TEXT, _MIXED = "maybe text", "mixed text", "mixed"
# Kinds that may be text: rejected where DAX needs a number or True/False.
_TEXT_KINDS = frozenset({_TEXT, _LITERAL_TEXT, _MAYBE_TEXT, _MIXED_TEXT})
_LITERAL_KINDS = frozenset({_NUMBER_LITERAL, _LITERAL_TEXT})
_VARIANT_KINDS = frozenset({_MIXED_TEXT, _MIXED})
# Kinds of a known DAX type; DAX compares numbers with dates, but no other two of these types.
_TYPE_OF_KIND = {
    _TEXT: "text",
    _LITERAL_TEXT: "text",
    _NUMBER_LITERAL: "number",
    _NUMBER: "number",
    _BOOLEAN: "boolean",
}
# DAX binding power of each binary operator (higher binds tighter), and of unary NOT and sign.
_BINARY_POWER = {
    "||": 1,
    "&&": 2,
    **dict.fromkeys(("=", "<", ">", "<=", ">=", "<>"), 4),
    "&": 5,
    "+": 6,
    "-": 6,
    "*": 7,
    "/": 7,
    "^": 9,
}
_NOT_POWER = 3
_SIGN_POWER = 8
_ROUNDING_CALL = re.compile(r"\b(CEILING|FLOOR)\(")
# Cognos documents the offset of these functions as an integer; EDATE rounds a fraction.
_INTEGER_OFFSET_FUNCTIONS = frozenset(
    {"_add_days", "_add_hours", "_add_minutes", "_add_months", "_add_years"}
)


@dataclass
class TranslationResult:
    """Outcome of translating a single Cognos expression to DAX."""

    dax: str | None
    confident: bool
    note: str | None = None


def _last_segment(reference: str) -> str:
    """Return the final ``[segment]`` of a qualified Cognos reference, without brackets."""
    parts = re.findall(r"\[([^\[\]]+)\]", reference)
    return parts[-1] if parts else reference.strip("[]")


def _convert_references(expression: str, table_name: str) -> str:
    """Rewrite qualified Cognos references to ``Table[Column]`` form, outside DAX strings."""

    def repl(match: re.Match[str]) -> str:
        if match.group(0).startswith('"'):
            return match.group(0)
        column = _last_segment(match.group(0))
        return f"{table_name}[{column}]"

    return re.sub(rf'"(?:[^"]|"")*"|{_REFERENCE.pattern}', repl, expression)


def _translate_extract(expression: str) -> str:
    """Translate ``extract(part, value)`` into the matching DAX date function."""
    pattern = re.compile(r"\bextract\s*\(\s*(year|month|day)\s*,\s*(.+?)\)", re.IGNORECASE)

    def repl(match: re.Match[str]) -> str:
        part = match.group(1).upper()
        value = match.group(2).strip()
        return f"{part}({value})"

    return pattern.sub(repl, expression)


def _translate_if_then_else(expression: str) -> str:
    """Translate ``if (cond) then (a) else (b)`` into ``IF(cond, a, b)``."""
    pattern = re.compile(
        r"\bif\s*\((?P<cond>.+?)\)\s*then\s*\((?P<then>.+?)\)\s*else\s*\((?P<else>.+?)\)",
        re.IGNORECASE | re.DOTALL,
    )

    def repl(match: re.Match[str]) -> str:
        cond = match.group("cond").strip()
        then_value = match.group("then").strip()
        else_value = match.group("else").strip()
        return f"IF({cond}, {then_value}, {else_value})"

    return pattern.sub(repl, expression)


def _case_to_dax(match: re.Match[str]) -> str:
    """Translate one innermost case block; a simple case keeps its subject via SWITCH."""
    body = match.group("body")
    first_when = _CASE_WHEN.search(body)
    if first_when is None:
        return match.group(0)
    subject = body[: first_when.start()].strip()
    branches = body[first_when.start() :]
    clauses = _CASE_CLAUSE.findall(branches)
    else_match = _CASE_ELSE.search(branches)
    if subject:
        arguments = [subject]
        for value, result in clauses:
            arguments.extend((value.strip(), result.strip()))
        if else_match:
            arguments.append(else_match.group(1).strip())
        return f"SWITCH({', '.join(arguments)})"
    output = else_match.group(1).strip() if else_match else "BLANK()"
    for cond, value in reversed(clauses):
        output = f"IF({cond.strip()}, {value.strip()}, {output})"
    return output


def _translate_case(expression: str) -> str:
    """Translate ``case`` blocks into nested IF (searched) or SWITCH (simple), innermost first."""
    previous = None
    while previous != expression:
        previous = expression
        expression = _CASE_BLOCK.sub(_case_to_dax, expression)
    return expression


def _convert_string_literals(expression: str) -> str:
    """Rewrite Cognos single-quoted string literals as DAX double-quoted strings."""

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith("["):
            return token
        text = token[1:-1].replace("''", "'")
        return '"' + text.replace('"', '""') + '"'

    return _LITERAL_OR_BRACKET.sub(repl, expression)


def _mask_protected_text(dax: str) -> tuple[str, list[str]]:
    """Replace DAX strings and bracketed names with numbered placeholders (``"0"``, ``[1]``).

    The rewrite passes (operators, function names, case, if, whitespace) then cannot change
    words, spaces or parentheses inside them; :func:`_unmask_protected_text` restores them.
    """
    saved: list[str] = []

    def repl(match: re.Match[str]) -> str:
        # Equal text shares a placeholder, so repeated references still compare equal.
        if match.group(0) not in saved:
            saved.append(match.group(0))
        index = saved.index(match.group(0))
        return f'"{index}"' if match.group(0).startswith('"') else f"[{index}]"

    return _PROTECTED_TEXT.sub(repl, dax), saved


def _unmask_protected_text(dax: str, saved: list[str]) -> str:
    """Restore the text hidden by :func:`_mask_protected_text`."""
    return _PLACEHOLDER.sub(lambda m: saved[int(m.group(1) or m.group(2))], dax)


def _may_be_fractional(argument: str) -> bool:
    """Return True when an argument may not be an integer.

    That is a decimal literal with a fraction, a division, a power or a call that is in neither
    ``_INTEGER_VALUED_CALLS`` nor ``_INTEGER_PRESERVING_CALLS`` (e.g. ``SQRT``, ``ROUND``, ``IF``).
    The argument of an integer-valued call is not inspected: ``_day(_add_hours(d, 1))`` is an
    integer.
    """
    text = _DAX_NAME_STRING_OR_QUOTED.sub(" ", argument)
    call = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    position = 0
    while match := call.search(text, position):
        name = match.group(1).upper()
        if name in _INTEGER_VALUED_CALLS:
            close = _matching_paren(text, match.end() - 1)
            if close is None:
                return True
            text = text[: match.start()] + "0" + text[close + 1 :]
            position = match.start() + 1
        elif name in _INTEGER_PRESERVING_CALLS:
            position = match.end()
        else:
            return True
    if "/" in text or "^" in text:
        return True
    return any(not float(number).is_integer() for number in _DECIMAL_LITERAL.findall(text))


def _has_macro_or_parameter(expression: str) -> bool:
    """Return True when a macro or prompt parameter appears outside literals and names."""
    return bool(_MACRO_OR_PARAMETER.search(_LITERAL_OR_BRACKET.sub(" ", expression)))


def _dax_table_reference(table_name: str) -> str:
    """Quote a table name for use as a DAX table expression when required."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        return table_name
    return "'" + table_name.replace("'", "''") + "'"


def _translate_operators(expression: str) -> str:
    """Translate Cognos logical/string operators into their DAX equivalents."""
    expression = re.sub(r"\s+and\s+", " && ", expression, flags=re.IGNORECASE)
    expression = re.sub(r"\s+or\s+", " || ", expression, flags=re.IGNORECASE)
    # Cognos string concatenation (||) is left as is; translate_measure_expression reports it as
    # not confident because DAX reads || as a logical OR.
    return expression


def _translate_functions(expression: str, issues: list[str]) -> str:
    """Replace mapped Cognos scalar function names with DAX function names.

    DAX CEILING and FLOOR need a significance: Cognos ``ceiling(x)`` becomes ``CEILING(x, 1)``,
    which the engine rounds towards +infinity as Cognos does (``CEILING(-2.5, 1)`` is -2) and
    which keeps BLANK(). Any other argument count is not Cognos and is reported in ``issues``.
    """
    result = expression
    for cognos_name, dax_name in _FUNCTION_MAP.items():
        result = re.sub(rf"\b{re.escape(cognos_name)}\s*\(", f"{dax_name}(", result, flags=re.I)
    return _complete_rounding_calls(result, issues)


def _complete_rounding_calls(text: str, issues: list[str]) -> str:
    """Add the significance ``1`` to each one-argument CEILING/FLOOR call in ``text``."""
    output: list[str] = []
    position = 0
    while match := _ROUNDING_CALL.search(text, position):
        open_index = match.end() - 1
        close = _matching_paren(text, open_index)
        if close is None:
            break
        inner = _complete_rounding_calls(text[open_index + 1 : close], issues)
        if len(_split_arguments(inner)) == 1 and inner.strip():
            inner = f"{inner}, 1"
        else:
            issues.append(f"{match.group(1).lower()}: Cognos takes exactly one argument")
        output.append(f"{text[position : open_index + 1]}{inner})")
        position = close + 1
    output.append(text[position:])
    return "".join(output)


def _is_arithmetic_of_columns(expression: str) -> bool:
    """Return True when the expression is only column references, numbers and arithmetic."""
    stripped = re.sub(r"[A-Za-z_][A-Za-z0-9_ ]*\[[^\[\]]+\]", "", expression)
    stripped = re.sub(r"\d+(\.\d+)?", "", stripped)
    return bool(re.fullmatch(r"[\s+\-*/().]*", stripped)) and "[" in expression


def _wrap_columns_with_aggregate(expression: str, dax_func: str) -> str:
    """Wrap each ``Table[Column]`` reference with the given aggregate function."""
    return _COLUMN_REFERENCE.sub(lambda m: f"{dax_func}({m.group(0)})", expression)


def _token_end(text: str, index: int) -> int | None:
    """Return the end of a string, quoted name or bracketed name starting at ``index``."""
    opener = text[index]
    if opener == "[":
        close = text.find("]", index + 1)
        while close >= 0 and text[close + 1 : close + 2] == "]":
            close = text.find("]", close + 2)
        return len(text) if close < 0 else close + 1
    if opener in "\"'":
        position = index + 1
        while position < len(text):
            if text[position] == opener:
                if text[position + 1 : position + 2] == opener:
                    position += 2
                    continue
                return position + 1
            position += 1
        return len(text)
    return None


def _matching_paren(text: str, open_index: int) -> int | None:
    """Return the index of the parenthesis closing the one at ``open_index``."""
    depth = 0
    position = open_index
    while position < len(text):
        end = _token_end(text, position)
        if end is not None:
            position = end
            continue
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return None


def _split_arguments(text: str) -> list[str]:
    """Split call arguments at top-level commas, ignoring strings, names and nested calls."""
    arguments: list[str] = []
    depth = start = position = 0
    while position < len(text):
        end = _token_end(text, position)
        if end is not None:
            position = end
            continue
        char = text[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            arguments.append(text[start:position].strip())
            start = position + 1
        position += 1
    arguments.append(text[start:].strip())
    return arguments


def _is_enclosed(dax: str) -> bool:
    """Return True for an atom, a single function call or a fully parenthesized expression."""
    if _ATOMIC_OPERAND.fullmatch(dax):
        return True
    name = _IDENTIFIER_START.match(dax)
    open_index = name.end() if name else 0
    return (
        dax[open_index : open_index + 1] == "(" and _matching_paren(dax, open_index) == len(dax) - 1
    )


def _is_delimited(text: str, start: int, end: int) -> bool:
    """Return True when ``text[start:end]`` is a whole argument (bounded by ``(``/``,``/``)``)."""
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    return before[-1:] in ("", "(", ",") and after[:1] in ("", ")", ",")


def _integer_literal(dax: str) -> int | None:
    """Return the value of a (signed) integer literal, else None."""
    return int(dax.replace(" ", "")) if _INTEGER_LITERAL.fullmatch(dax) else None


@dataclass
class _Operand:
    """A translated date-function argument.

    ``leaves`` are the references whose null makes the Cognos value null (empty: never null), or
    None when the argument holds other constructs. ``propagates`` is True when the DAX is
    BLANK() exactly when a leaf is: a single reference or a single translated call. Arithmetic
    does not propagate, because DAX treats BLANK() as zero in ``BLANK() - 3``.
    """

    dax: str
    leaves: tuple[str, ...] | None
    propagates: bool


class _DateTranslator:
    """Translate Cognos business date/time functions and clock constants into scalar DAX.

    Cognos returns null when an argument is null; DAX arithmetic treats BLANK() as zero, so
    translations guard the null sources of each argument with ``IF(ISBLANK(...))``. A complex
    argument used more than once is bound to a ``VAR``.
    """

    def __init__(self) -> None:
        self.issues: list[str] = []
        self.uses_clock = False
        self._variables = 0

    def rewrite(self, text: str) -> _Operand:
        """Rewrite date calls and clock constants in ``text`` and derive its null sources."""
        output: list[str] = []
        kinds: list[str] = []
        leaves: dict[str, None] | None = {}
        position = 0
        while position < len(text):
            plain = position > 0 and (text[position - 1].isalnum() or text[position - 1] in "_$.")
            leaf = None if plain else _LEAF_REFERENCE.match(text, position)
            if leaf:
                output.append(leaf.group(0))
                kinds.append("leaf")
                if leaves is not None:
                    leaves[leaf.group(0)] = None
                position = leaf.end()
                continue
            end = _token_end(text, position)
            if end is not None:
                output.append(text[position:end])
                kinds.append("other")
                leaves = None
                position = end
                continue
            match = _IDENTIFIER_START.match(text, position)
            if match is None:
                char = text[position]
                output.append(char)
                if not char.isspace():
                    kinds.append("other")
                    if char not in "0123456789.+-*/()":
                        leaves = None
                position += 1
                continue
            name = match.group(0)
            lowered = name.lower()
            following = len(text) - len(text[match.end() :].lstrip())
            next_char = text[following : following + 1]
            if not plain and lowered in _DATE_FUNCTION_ARITY and next_char == "(":
                close = _matching_paren(text, following)
                if close is not None:
                    dax, call_leaves = self._call(name, text[following + 1 : close])
                    if not _is_enclosed(dax) and not _is_delimited(text, position, close + 1):
                        dax = f"({dax})"
                    output.append(dax)
                    kinds.append("call")
                    if leaves is not None and call_leaves is not None:
                        leaves.update(dict.fromkeys(call_leaves))
                    else:
                        leaves = None
                    position = close + 1
                    continue
            if not plain and lowered in _DATE_CONSTANTS and next_char not in ("(", "["):
                output.append(_DATE_CONSTANTS[lowered])
                kinds.append("other")
                if lowered == "current_timestamp":
                    self.issues.append(
                        "current_timestamp: IBM documents both the database server clock and "
                        "GMT, so the DAX clock function is a hint only"
                    )
                else:
                    self.uses_clock = True
                position = match.end()
                continue
            output.append(name)
            kinds.append("other")
            leaves = None
            position = match.end()
        return _Operand(
            "".join(output),
            None if leaves is None else tuple(leaves),
            kinds in (["leaf"], ["call"]),
        )

    def _call(self, name: str, inner: str) -> tuple[str, tuple[str, ...] | None]:
        """Translate one call; an arity mismatch leaves it as an untranslated call."""
        lowered = name.lower()
        raw_arguments = _split_arguments(inner)
        if len(raw_arguments) != _DATE_FUNCTION_ARITY[lowered] or not all(raw_arguments):
            return f"{name}({self.rewrite(inner).dax})", None
        operands = []
        for raw in raw_arguments:
            operand = self.rewrite(raw)
            operand.dax = operand.dax.strip()
            operands.append(operand)
        if any(_DAX_STRING_ARGUMENT.fullmatch(operand.dax) for operand in operands):
            # DAX would compute with the text or convert it to a number, not to a date.
            self.issues.append(f"{lowered}: a string literal is passed as a date or number")
        if lowered in _INTEGER_OFFSET_FUNCTIONS and _may_be_fractional(raw_arguments[1]):
            self.issues.append(f"{lowered}: Cognos documents the offset as an integer")
        leaves: dict[str, None] | None = {}
        for operand in operands:
            if leaves is None or operand.leaves is None:
                leaves = None
            else:
                leaves.update(dict.fromkeys(operand.leaves))
        return self._build(lowered, operands), None if leaves is None else tuple(leaves)

    def _bind(
        self,
        operands: list[_Operand],
        uses: tuple[int, ...],
        build: Callable[[list[str]], str],
        guard: bool = True,
    ) -> str:
        """Bind operands to inline text or VARs, build the body and add Cognos null handling.

        ``guard=False`` marks a DAX function that already returns BLANK() for a BLANK() argument;
        its arguments are then only guarded when they do not propagate BLANK() themselves.
        """
        bindings: list[str] = []
        references: list[str] = []
        blank_checks: list[str] = []
        for operand, count in zip(operands, uses, strict=True):
            whole = guard and operand.leaves is None
            if operand.leaves and (guard or not operand.propagates):
                for leaf in operand.leaves:
                    if f"ISBLANK({leaf})" not in blank_checks:
                        blank_checks.append(f"ISBLANK({leaf})")
            if _ATOMIC_OPERAND.fullmatch(operand.dax):
                reference = operand.dax
            elif count + whole > 1:
                self._variables += 1
                reference = f"__c2d{self._variables}"
                bindings.append(f"VAR {reference} = {operand.dax}")
            elif _is_enclosed(operand.dax):
                reference = operand.dax
            else:
                reference = f"({operand.dax})"
            references.append(reference)
            if whole:
                blank_checks.append(f"ISBLANK({reference})")
        body = build(references)
        if blank_checks:
            condition = blank_checks[-1]
            for check in reversed(blank_checks[:-1]):
                condition = f"OR({check}, {condition})"
            body = f"IF({condition}, BLANK(), {body})"
        if bindings:
            body = f"({' '.join(bindings)} RETURN {body})"
        return body

    def _offset(self, operands: list[_Operand], divisor: str | None) -> str:
        """Add a (possibly fractional) number of days, hours, minutes or seconds."""
        literal = re.fullmatch(r"([+-]?)\s*(\d+(?:\.\d+)?)", operands[1].dax)
        if literal:
            sign = "-" if literal.group(1) == "-" else "+"
            amount = literal.group(2) + (f" / {divisor}" if divisor else "")
            return self._bind(operands[:1], (1,), lambda r: f"{r[0]} {sign} {amount}")
        scale = f" / {divisor}" if divisor else ""
        return self._bind(operands, (1, 1), lambda r: f"{r[0]} + {r[1]}{scale}")

    def _shift_months(self, operands: list[_Operand], factor: int) -> str:
        """Add months (EDATE clamps to month end) while keeping the time of day."""
        months = _integer_literal(operands[1].dax)
        if months is not None:
            count = months * factor
            return self._bind(
                operands[:1],
                (3,),
                lambda r: f"{r[0]} + DATEDIFF({r[0]}, EDATE({r[0]}, {count}), DAY)",
            )
        prefix = f"{factor} * " if factor != 1 else ""
        return self._bind(
            operands,
            (3, 1),
            lambda r: f"{r[0]} + DATEDIFF({r[0]}, EDATE({r[0]}, {prefix}{r[1]}), DAY)",
        )

    def _day_of_week(self, operands: list[_Operand]) -> str:
        """Day of week numbered from the given start day (1 = Monday ... 7 = Sunday)."""
        start = _integer_literal(operands[1].dax)
        if start == 1:
            return self._bind(operands[:1], (1,), lambda r: f"WEEKDAY({r[0]}, 2)", guard=False)
        if start == 7:
            return self._bind(operands[:1], (1,), lambda r: f"WEEKDAY({r[0]}, 1)", guard=False)
        if start is not None and 2 <= start <= 6:
            return self._bind(
                operands[:1], (1,), lambda r: f"MOD(WEEKDAY({r[0]}, 2) - {start}, 7) + 1"
            )
        self.issues.append("_day_of_week: the start day is not an integer literal from 1 to 7")
        return self._bind(operands, (1, 1), lambda r: f"MOD(WEEKDAY({r[0]}, 2) - {r[1]}, 7) + 1")

    def _build(self, name: str, operands: list[_Operand]) -> str:
        """Return the DAX for one Cognos date function with a matching argument count."""
        if name in _NATIVE_DATE_PARTS:
            if name == "_second":
                self.issues.append("_second: Cognos can return fractional seconds, DAX SECOND not")
            function = _NATIVE_DATE_PARTS[name]
            return self._bind(operands, (1,), lambda r: f"{function}({r[0]})", guard=False)
        if name in _OFFSET_DIVISORS:
            return self._offset(operands, _OFFSET_DIVISORS[name])
        if name in ("_add_months", "_add_years"):
            return self._shift_months(operands, 1 if name == "_add_months" else 12)
        if name == "_day_of_week":
            return self._day_of_week(operands)
        if name == "_first_of_month":
            return self._bind(operands, (2,), lambda r: f"{r[0]} - DAY({r[0]}) + 1")
        if name == "_last_of_month":
            return self._bind(
                operands, (3,), lambda r: f"{r[0]} - DAY({r[0]}) + DAY(EOMONTH({r[0]}, 0))"
            )
        if name == "_days_to_end_of_month":
            return self._bind(operands, (2,), lambda r: f"DAY(EOMONTH({r[0]}, 0)) - DAY({r[0]})")
        if name == "_day_of_year":
            return self._bind(
                operands, (2,), lambda r: f"DATEDIFF(DATE(YEAR({r[0]}), 1, 1), {r[0]}, DAY) + 1"
            )
        if name == "_week_of_year":
            return self._bind(operands, (1,), lambda r: f"WEEKNUM({r[0]}, 21)", guard=False)
        if name == "_days_between":
            self.issues.append(
                "_days_between: DATEDIFF counts calendar-day boundaries; Cognos does not "
                "document the result for timestamps"
            )
            return self._bind(
                operands, (1, 1), lambda r: f"DATEDIFF({r[1]}, {r[0]}, DAY)", guard=False
            )
        self.issues.append("_make_timestamp: DAX DATE rolls invalid dates over and maps years 0-99")
        return self._bind(operands, (1, 1, 1), lambda r: f"DATE({r[0]}, {r[1]}, {r[2]})")


def _normalize_list_separator(expression: str) -> tuple[str, bool]:
    """Turn a ``;``-separated expression (decimal comma locales) into ``,``-separated text.

    Returns the converted text and whether a comma that is not a decimal separator remained,
    which makes argument boundaries ambiguous.
    """
    stray = False

    def convert(segment: str) -> str:
        nonlocal stray
        segment = re.sub(r"(?<=\d),(?=\d)", ".", segment)
        stray = stray or "," in segment
        return segment.replace(";", ",")

    parts: list[str] = []
    position = 0
    for match in _LITERAL_OR_BRACKET_NAME.finditer(expression):
        parts.append(convert(expression[position : match.start()]))
        parts.append(match.group(0))
        position = match.end()
    parts.append(convert(expression[position:]))
    return "".join(parts), stray


def _has_invalid_tokens(dax: str, table_name: str, calculated_column: bool = False) -> bool:
    """Return True for leftovers that are not valid DAX.

    Rejected are ``;``, ``:``, a top-level comma, a DAX comment opener, empty parentheses that
    are not a call, an invalid operand/operator sequence (see
    :func:`_is_valid_operator_sequence`), text where DAX needs a number or True/False, a
    comparison of different types and, with ``calculated_column``, a variant value (see
    :func:`_has_text_operand`) and bare words. A bare word is
    an identifier that is not a call, a ``Table[Column]`` reference of ``table_name``, a DAX
    keyword, a generated variable, a DATEDIFF interval or an iterator's table argument, e.g.
    Cognos ``null``, ``in``, ``between`` or an untranslated constant such as ``current_time``.
    """
    replacements = {"[": "[]", '"': '""', "'": "''"}
    text = _DAX_NAME_STRING_OR_QUOTED.sub(lambda m: replacements[m.group(0)[0]], dax)
    # Only the table itself may qualify a column; any other word before ``[`` is left over.
    text = re.sub(rf"(?<![\w$.]){re.escape(table_name)}\[\]", "[]", text)
    if ";" in text or ":" in text or re.search(r"--|//|/\*", text):
        return True
    if _EMPTY_PARENTHESES.search(text):
        return True
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth <= 0:
            return True
    text = _DATEDIFF_INTERVAL.sub(", 0)", text)
    for match in _BARE_WORD.finditer(text):
        word = match.group(0)
        following = text[match.end() :].lstrip()[:1]
        if following == "(":
            continue
        if word.upper() in _ALLOWED_BARE_WORDS or re.fullmatch(r"__c2d\d+", word):
            continue
        if (
            word.casefold() == table_name.casefold()
            and following == ","
            and _ITERATOR_TABLE_ARGUMENT.search(text[: match.start()])
        ):
            continue
        return True
    tokens = _REDUCED_TOKEN.findall(text)
    return not _is_valid_operator_sequence(tokens) or _has_text_operand(tokens, calculated_column)


@dataclass
class _Group:
    """An open parenthesis seen by :func:`_is_valid_operator_sequence`.

    ``kind`` is ``call`` (an argument list), ``group`` (a parenthesized expression), ``var``
    (a translator VAR block before its RETURN) or ``return`` (after it).
    """

    kind: str
    function: str = ""
    arguments: int = 0
    variables: frozenset[str] = frozenset()
    pending: str = ""


def _arity_allows(function: str, count: int) -> bool:
    """Return True when DAX accepts ``count`` arguments for ``function`` (unknown: True)."""
    low, high = _DAX_ARITY.get(function, (0, None))
    return low <= count and (high is None or count <= high)


def _is_valid_operator_sequence(tokens: list[str]) -> bool:
    """Return True when operands, operators, calls and parentheses alternate as in DAX.

    Rejected are adjacent operands (``1 Q``, ``[a] NOT [b]``), a missing or doubled binary
    operator (``**``, ``=<``, a trailing ``+``), operators that DAX lacks (``!=``, ``%``),
    unbalanced parentheses and a call with an argument count outside ``_DAX_ARITY``. Unary
    ``+``, ``-`` and ``NOT`` may precede an operand. ``VAR`` and ``RETURN`` are only accepted in
    the translator's own block shape ``(VAR __c2d1 = ... [VAR __c2d2 = ...] RETURN ...)``, and a
    ``__c2d`` variable only inside the block that defines it.
    """
    expect_operand = True
    groups: list[_Group] = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        word = token.upper() if _IDENTIFIER_START.fullmatch(token) else ""
        group = groups[-1] if groups else None
        starts_group = position > 0 and tokens[position - 1] == "("
        position += 1
        if word == "VAR":
            if group is None:
                return False
            opens_block = group.kind == "group" and starts_group
            if not (opens_block or (group.kind == "var" and not expect_operand)):
                return False
            name, equals = (tokens[position : position + 2] + ["", ""])[:2]
            if not _GENERATED_VARIABLE.fullmatch(name) or equals != "=":
                return False
            if group.kind == "var":
                group.variables |= {group.pending}
            group.kind, group.pending = "var", name
            position += 2
            expect_operand = True
        elif word == "RETURN":
            if group is None or group.kind != "var" or expect_operand:
                return False
            group.variables |= {group.pending}
            group.kind = "return"
            expect_operand = True
        elif expect_operand:
            if token in ("+", "-") or word == "NOT":
                continue
            if token == "(":
                groups.append(_Group("group"))
            elif word and tokens[position : position + 1] == ["("]:
                position += 1
                if tokens[position : position + 1] == [")"]:
                    position += 1
                    if not _arity_allows(word, 0):
                        return False
                    expect_operand = False
                else:
                    groups.append(_Group("call", word, 1))
            elif _GENERATED_VARIABLE.fullmatch(token):
                if not any(token in open_group.variables for open_group in groups):
                    return False
                expect_operand = False
            elif word or token in _OPERAND_TOKENS or _DAX_NUMBER.fullmatch(token):
                expect_operand = False
            else:
                return False
        elif token == ")" and group is not None:
            groups.pop()
            if group.kind == "var":
                return False
            if group.kind == "call" and not _arity_allows(group.function, group.arguments):
                return False
        elif token == "," and group is not None and group.kind == "call":
            group.arguments += 1
            expect_operand = True
        elif token in _BINARY_OPERATORS:
            expect_operand = True
        else:
            return False
    return not expect_operand and not groups


class _TextOperandFinder:
    """Recursive-descent reader of reduced DAX tokens that infers the kind of each value.

    Text is a string literal, a ``_TEXT_VALUED_CALLS`` call or an ``&`` concatenation; it is
    literal text when its text operands are literals (a MID position may come from a column).
    Arithmetic and a sign yield a number (or date), and a ``_NUMBER_VALUED_CALLS`` call a number;
    a comparison, ``&&``, ``||`` and a ``_BOOLEAN_VALUED_CALLS`` call yield True/False. An
    IF/SWITCH/COALESCE takes the kind its results share, ignoring BLANK(); results of different
    known types make a variant, and text next to an unknown result makes possible text. Column
    references, MIN/MAX and ``NOT`` have an unknown kind, as the engine types ``NOT`` of an
    integer as an integer. ``NOT`` is the DAX prefix operator, also before ``(``: the engine
    reads ``NOT (x) & "y"`` as ``NOT((x) & "y")``.

    ``found`` is set when possible text is the operand of ``+ - * / ^``, a sign, ``NOT``, ``&&``
    or ``||``, or a ``_NUMBER_ARGUMENTS`` or ``_LOGICAL_ARGUMENTS`` argument ("Cannot convert
    value ... of type Text to type Number/True/False"), when literal text is a
    ``_DATE_ARGUMENTS`` argument, and when a comparison operator or SWITCH compares two different
    known types other than a number with a date ("DAX comparison operations do not support
    comparing values of type Text with values of type Integer"). A variant can be compared.
    """

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.position = 0
        self.variables: dict[str, str] = {}
        self.found = False

    def _peek(self) -> str:
        return self.tokens[self.position] if self.position < len(self.tokens) else ""

    def _take(self, expected: str | None = None) -> str:
        token = self._peek()
        if not token or (expected is not None and token.upper() != expected):
            raise ValueError(f"expected {expected or 'a token'}, got {token!r}")
        self.position += 1
        return token

    def expression(self, min_power: int = 0) -> str:
        """Read an expression whose operators bind tighter than ``min_power``; return its kind."""
        kind = self._prefix()
        while (power := _BINARY_POWER.get(self._peek(), 0)) > min_power:
            operator = self._take()
            right = self.expression(power)
            if operator in _ARITHMETIC_OPERATORS or operator in ("&&", "||"):
                self.found = self.found or kind in _TEXT_KINDS or right in _TEXT_KINDS
            elif operator in _COMPARISON_OPERATORS:
                self._check_comparable([kind, right])
            if operator == "&":
                kind = _LITERAL_TEXT if {kind, right} <= _LITERAL_KINDS else _TEXT
            elif operator in _ARITHMETIC_OPERATORS:
                kind = _NUMBER_LITERAL if kind == right == _NUMBER_LITERAL else _NUMBER
            else:
                kind = _BOOLEAN
        return kind

    def _check_comparable(self, kinds: list[str]) -> None:
        """Flag values of two different known types that are compared with each other."""
        types = {_TYPE_OF_KIND[kind] for kind in kinds if kind in _TYPE_OF_KIND}
        self.found = self.found or len(types) > 1

    def _prefix(self) -> str:
        token = self._take()
        if token in ("+", "-") or token.upper() == "NOT":
            # DAX keeps +"x" as "x" (only -"x" fails); a signed text is still not confident.
            is_not = token.upper() == "NOT"
            operand = self.expression(_NOT_POWER if is_not else _SIGN_POWER)
            self.found = self.found or operand in _TEXT_KINDS
            if is_not:
                return _UNKNOWN
            if token == "+" and operand not in (_NUMBER_LITERAL, _NUMBER):
                return _UNKNOWN
            return _NUMBER_LITERAL if operand == _NUMBER_LITERAL else _NUMBER
        if token == "(":
            kind = self._variable_block() if self._peek().upper() == "VAR" else self.expression()
            self._take(")")
            return kind
        if _IDENTIFIER_START.fullmatch(token) and self._peek() == "(":
            return self._call(token.upper())
        if token == '""':
            return _LITERAL_TEXT
        if _DAX_NUMBER.fullmatch(token):
            return _NUMBER_LITERAL
        if token.upper() in ("TRUE", "FALSE"):
            return _BOOLEAN
        return self.variables.get(token, _UNKNOWN)

    def _variable_block(self) -> str:
        while self._peek().upper() == "VAR":
            self._take()
            name = self._take()
            self._take("=")
            self.variables[name] = self.expression()
        self._take("RETURN")
        return self.expression()

    def _call(self, function: str) -> str:
        self._take("(")
        arguments: list[str] = []
        if self._peek() == ")":
            self._take()
        else:
            arguments.append(self.expression())
            while self._take() == ",":
                arguments.append(self.expression())
            if self.tokens[self.position - 1] != ")":
                raise ValueError(f"unexpected token in the arguments of {function}")
        for required, rejected in (
            (_NUMBER_ARGUMENTS, _TEXT_KINDS),
            (_LOGICAL_ARGUMENTS, _TEXT_KINDS),
            (_DATE_ARGUMENTS, {_LITERAL_TEXT}),
        ):
            if function in required:
                positions = required[function]
                checked = (
                    arguments
                    if positions is None
                    else [arguments[i] for i in positions if i < len(arguments)]
                )
                self.found = self.found or any(kind in rejected for kind in checked)
        if function in _TEXT_VALUED_CALLS:
            return _LITERAL_TEXT if arguments[:1] and arguments[0] in _LITERAL_KINDS else _TEXT
        if function in _NUMBER_VALUED_CALLS:
            return _NUMBER
        if function in _BOOLEAN_VALUED_CALLS:
            return _BOOLEAN
        if function == "BLANK":
            return _BLANK
        if function == "IF":
            return self._result_kind(arguments[1:3])
        if function == "SWITCH":
            # SWITCH(subject, value1, result1, ..., [else]): the values are the odd positions
            # before a trailing else, which is present when the argument count is even.
            self._check_comparable(arguments[:1] + arguments[1 : len(arguments) - 1 : 2])
            has_else = len(arguments) % 2 == 0
            return self._result_kind(arguments[2::2] + (arguments[-1:] if has_else else []))
        if function == "COALESCE":
            return self._result_kind(arguments)
        return _UNKNOWN

    @staticmethod
    def _result_kind(results: list[str]) -> str:
        """Kind of a conditional value from the kinds of its results (BLANK() fits any type)."""
        known = [kind for kind in results if kind != _BLANK]
        if not known:
            return _BLANK
        for shared in (_LITERAL_TEXT, _NUMBER_LITERAL):
            if all(kind == shared for kind in known):
                return shared
        types = {_TYPE_OF_KIND.get(kind) for kind in known}
        if types == {"text"}:
            return _TEXT
        if types == {"number"}:
            return _NUMBER
        if types == {"boolean"}:
            return _BOOLEAN
        has_text = any(kind in _TEXT_KINDS for kind in known)
        if len(types - {None}) > 1 or any(kind in _VARIANT_KINDS for kind in known):
            return _MIXED_TEXT if has_text else _MIXED
        return _MAYBE_TEXT if has_text else _UNKNOWN


def _has_text_operand(tokens: list[str], calculated_column: bool = False) -> bool:
    """Return True when text reaches arithmetic, a sign, a logical operator or a numeric or
    logical function argument, or values of different types are compared (see
    :class:`_TextOperandFinder`).

    With ``calculated_column`` a value whose results have different known types, such as
    ``IF(Q[a] > 0, "x", 1)``, is rejected too: "Expressions that yield variant data-type cannot be
    used to define calculated columns" (a measure may be a variant). ``LEN("abc") + 1`` and
    ``UPPER(Q[a]) = "X"`` are accepted. Tokens that cannot be read count as text in use, so the
    translation is not confident.
    """
    finder = _TextOperandFinder(tokens)
    try:
        kind = finder.expression()
    except (ValueError, IndexError):
        return True
    if calculated_column and kind in _VARIANT_KINDS:
        return True
    return finder.found or finder.position != len(tokens)


def _has_untranslated_calls(expression: str, extra_known: frozenset[str] = frozenset()) -> bool:
    """Return True when the expression still contains unknown function-style calls.

    ``extra_known`` holds additional upper-case names accepted at this stage of translation.
    """
    # Column names and string literals may contain parentheses; they are not calls.
    searchable = _DAX_NAME_OR_STRING.sub(" ", expression)
    for match in _FUNCTION_CALL.finditer(searchable):
        name = match.group(1)
        if name.upper() not in {fn.upper() for fn in _KNOWN_DAX_FUNCTIONS} | extra_known:
            return True
    return False


def translate_measure_expression(
    expression: str | None,
    table_name: str,
    aggregate: str = "none",
    *,
    list_separator: str = ",",
) -> TranslationResult:
    """Translate a Cognos measure/data-item expression into DAX.

    Args:
        expression: The raw Cognos expression text (may be ``None``).
        table_name: The owning table name, used to qualify column references.
        aggregate: The Cognos ``aggregate`` attribute (e.g. ``total``, ``average``).
        list_separator: The argument separator of the report's expression locale: ``","``
            (e.g. en-us) or ``";"`` (decimal-comma locales such as nl-be, where ``1,5`` is a
            number).

    Returns:
        A :class:`TranslationResult`. ``confident`` is ``True`` only when every construct in the
        expression was recognized. A confident result may still carry a ``note`` with a semantic
        caveat (for example that ``TODAY()`` is evaluated at refresh time in a calculated column).
    """
    if list_separator not in (",", ";"):
        raise ValueError(f"unsupported list separator: {list_separator!r}")
    if not expression or not expression.strip():
        return TranslationResult(dax=None, confident=False, note="empty expression")

    expr = expression.strip()
    issues: list[str] = []
    if list_separator == ";":
        expr, stray_comma = _normalize_list_separator(expr)
        if stray_comma:
            issues.append("a ',' that is not a decimal separator appears in a ';' expression")
    dax_func = _AGGREGATE_TO_DAX.get(aggregate.strip().lower())
    source_text = _LITERAL_OR_BRACKET_NAME.sub(" ", expr)
    if "||" in source_text:
        issues.append(
            "Cognos || is string concatenation (null if an operand is null), while DAX || is a "
            "logical OR and & treats BLANK() as an empty string"
        )
    if _TIMESTAMP_LITERAL.search(source_text):
        issues.append("a bare timestamp literal needs DATE(y, m, d) + TIME(h, m, s) in DAX")
    elif _DATE_LITERAL.search(source_text):
        issues.append("a bare date literal needs DATE(y, m, d) in DAX")
    if re.search(r"\b(?:var|return)\b|__c2d", source_text, re.IGNORECASE):
        # Cognos has no variables; only the VAR blocks the date pass generates are accepted.
        issues.append("var, return or a __c2d name is not Cognos")

    # Case 1: a plain qualified reference such as [Sales].[Measures].[Revenue].
    if _SIMPLE_REFERENCE.match(expr):
        column = _last_segment(expr)
        if dax_func:
            return TranslationResult(dax=f"{dax_func}({table_name}[{column}])", confident=True)
        return TranslationResult(dax=f"{table_name}[{column}]", confident=True)

    # Case 2: structured translation of functions, conditionals and operators.
    has_macro = _has_macro_or_parameter(expr)
    converted = _convert_string_literals(expr)
    converted = _convert_references(converted, table_name)
    converted, protected = _mask_protected_text(converted)
    converted = _translate_extract(converted)
    converted = _translate_if_then_else(converted)
    converted = _translate_case(converted)
    converted = _translate_functions(converted, issues)

    iterator_supported = True
    # Pure arithmetic of references with an aggregate gets each operand aggregated.
    if dax_func and _is_arithmetic_of_columns(converted):
        converted = _wrap_columns_with_aggregate(converted, dax_func)
    elif dax_func and _COLUMN_REFERENCE.search(converted) and not _AGGREGATE_CALL.search(converted):
        # A detail-level calculation is evaluated per row and then aggregated, as in Cognos.
        iterator = _ITERATOR_FOR_AGGREGATE.get(dax_func)
        if iterator:
            converted = f"{iterator}({_dax_table_reference(table_name)}, {converted})"
        else:
            iterator_supported = False

    # Checked before the date pass so a user-written mod(), datediff() etc. is not mistaken for
    # the DAX the date translation emits.
    calls_known = not _has_untranslated_calls(converted, _COGNOS_DATE_FUNCTION_NAMES)
    # After case/if translation, so the emitted IF(ISBLANK(...)) guards are not re-parsed as
    # Cognos if-then-else, and after aggregation, so row-level dates sit inside the iterator.
    dates = _DateTranslator()
    converted = dates.rewrite(converted).dax
    issues.extend(dates.issues)
    converted = _translate_operators(converted)
    converted = re.sub(r"\s+", " ", converted).strip()
    converted = _unmask_protected_text(converted, protected)

    confident = (
        not has_macro
        and iterator_supported
        and calls_known
        and not issues
        and not _has_untranslated_calls(converted, _DATE_DAX_FUNCTIONS)
        # Under ``none`` the caller emits a calculated column, which cannot hold a variant.
        and not _has_invalid_tokens(
            converted, table_name, calculated_column=aggregate.strip().lower() == "none"
        )
    )
    notes = [] if confident else ["expression contains constructs that need review", *issues]
    if dates.uses_clock:
        notes.append(_CLOCK_NOTE)
    return TranslationResult(dax=converted, confident=confident, note="; ".join(notes) or None)
