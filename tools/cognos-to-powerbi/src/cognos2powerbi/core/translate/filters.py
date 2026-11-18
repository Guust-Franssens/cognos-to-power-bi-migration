"""Translate Cognos detail-filter expressions into a target-neutral condition IR.

A Cognos detail filter is a boolean expression over a query's data items. This module parses the
statically translatable subset - one item compared with literals (``=``, ``<>``, ``<``, ``<=``,
``>``, ``>=``), ``in`` lists, ``is null``/``is missing``, ``between``, ``like`` patterns that map
to equality/starts-with/contains, ``contains`` and ``starts with``, combined with ``not``, ``and``
and an ``or`` over a single item - into one :class:`FilterCondition` tree per filtered item.
Anything else raises :class:`UntranslatableFilter` with a reason code, so the caller keeps an
honest review flag instead of guessing.

NULL semantics follow Cognos, measured on Cognos Analytics 12: a predicate on a NULL value is
unknown and the row is dropped, so negative and ordering predicates (``<>``, ``not in``,
``not like``, ``<``, ``between`` ...) drop NULL rows, ``= null`` means ``is null`` and
``<> null`` means ``is not null``. Each clause records whether Cognos keeps NULL rows
(``keeps_null``); target renderers encode that explicitly rather than rely on how the target
treats blanks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from cognos2powerbi.core.ir.models import FilterCondition, FilterValue

# Human-readable reasons, keyed by the untranslated code recorded on the filter.
REASONS: dict[str, str] = {
    "prompt-or-macro": "it depends on a prompt parameter or macro",
    "relative-date": "it uses a relative date (for example current_date or _add_days)",
    "function": "it calls a Cognos function",
    "expression": "it filters a computed expression rather than a data item",
    "item-vs-item": "it compares two data items, which a visual-level filter cannot express",
    "or-across-items": "it combines different data items with OR/NOT, which a visual-level "
    "filter cannot express",
    "ends-with": "Power BI filters have no 'ends with' condition",
    "like-pattern": "its LIKE pattern cannot be expressed as equality, starts-with or contains",
    "bare-item": "it is a data item without a comparison",
    "empty-text": "it compares with an empty string in a way that depends on whether the target "
    "treats '' as blank",
    "blank-text": "it compares with a whitespace-only string, whose trailing-space handling "
    "depends on the database collation",
    "in-list-null": "its IN list contains null",
    "unsupported-syntax": "its syntax is not supported by the filter translator",
    # Resolution of the filtered item against the query (parser) and the visuals (generator).
    "unresolved-item": "its data item could not be resolved to a single model column of the query",
    "calculated-item": "it filters a calculation that has no confident DAX translation",
    "aggregate-base": "it filters an aggregated data item before aggregation, and the item's "
    "detail value is not a single model column",
    "summary-item": "it filters a summary expression (for example total(...)) before "
    "aggregation, whose Cognos scope cannot be reproduced as a row filter",
    "post-aggregation": "it filters aggregated values (postAutoAggregation), which a visual "
    "filter reproduces only on a measure bound in a visual that groups by every grouping item "
    "of the query",
    "literal-type": "its literal does not match the data type of the model column",
    "no-visual": "no visual binds to the query's table",
    "query-operand": "the query only feeds other queries (join, union or reference), whose "
    "visuals a filter on this query does not reach",
}

# Cognos words that make a filter depend on the run date.
_RELATIVE_DATE_WORDS = {
    "current_date",
    "current_time",
    "current_timestamp",
    "localtime",
    "localtimestamp",
    "sysdate",
    "today",
    "now",
}
# Operator words that may precede a parenthesis without being a function call.
_KEYWORDS = {
    "and",
    "or",
    "not",
    "in",
    "is",
    "between",
    "like",
    "contains",
    "starts",
    "ends",
    "with",
    "null",
    "missing",
}
_COMPARISON_OPS = {"=": "eq", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
_NEGATION_OPS = {"<>", "!="}
# Flipping the operands of a comparison (literal op item -> item op' literal).
_FLIPPED = {"=": "=", "<>": "<>", "!=": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
_ARITHMETIC_OPS = {"+", "-", "*", "/", "||"}

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+|/\*[\s\S]*?\*/|//[^\r\n]*|--[^\r\n]*)
  | (?P<macro>\#[^#]*\#)
  | (?P<param>\?[^?]*\?)
  | (?P<ref>\[[^\[\]]*\](?:\s*\.\s*\[[^\[\]]*\])*)
  | (?P<string>'(?:[^']|'')*')
  | (?P<date>\d{4}-\d{2}-\d{2}(?:[T\x20]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?)(?![\w.])
  | (?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<op><>|<=|>=|!=|\|\||[=<>(),;+\-*/{}:])
  | (?P<word>[A-Za-z_][A-Za-z0-9_]*(?:-[A-Za-z_][A-Za-z0-9_]*)*)
    """,
    re.VERBOSE,
)
_COMMENT_ONLY_MACRO_RE = re.compile(r"#(?:\s|/\*[\s\S]*?\*/)*#")
_REF_SEPARATOR_RE = re.compile(r"\]\s*\.\s*\[")


class UntranslatableFilter(Exception):  # noqa: N818 - reads as a result, not a crash
    """Raised when a filter expression is outside the statically translatable subset."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        reason = REASONS.get(code, code)
        super().__init__(f"{reason} ({detail})" if detail else reason)

    @property
    def reason(self) -> str:
        return str(self)


@dataclass(frozen=True)
class ParsedClause:
    """The condition on one filtered item, as parsed from a detail filter."""

    item: str
    condition: FilterCondition
    keeps_null: bool


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str

    def is_word(self, *words: str) -> bool:
        return self.kind == "word" and self.text.lower() in words

    def is_op(self, *ops: str) -> bool:
        return self.kind == "op" and self.text in ops


_END = _Token("end", "")


@dataclass(frozen=True)
class _Ref:
    text: str


@dataclass(frozen=True)
class _Null:
    pass


_Operand = _Ref | FilterValue | _Null


def item_key(reference: str) -> str:
    """Normalize a Cognos reference for comparison: no blanks between segments, casefolded."""
    return _REF_SEPARATOR_RE.sub("].[", reference.strip()).casefold()


def ref_segments(reference: str) -> list[str]:
    """Return the bracketed segments of a Cognos reference, without brackets."""
    return re.findall(r"\[([^\[\]]*)\]", reference)


def parse_filter(expression: str) -> list[ParsedClause]:
    """Parse a mandatory detail filter into one clause per filtered item (ANDed together).

    Raises :class:`UntranslatableFilter` when any part of the expression falls outside the
    supported subset; a filter is translated completely or not at all.
    """
    tokens = _tokenize(expression)
    _prescan(tokens)
    parser = _Parser(tokens)
    condition = parser.parse()
    groups: dict[str, list[FilterCondition]] = {}
    names: dict[str, str] = {}
    for conjunct in _conjuncts(condition):
        items = {item_key(item): item for item in _items(conjunct)}
        if len(items) != 1:
            raise UntranslatableFilter("or-across-items")
        key, item = next(iter(items.items()))
        groups.setdefault(key, []).append(conjunct)
        names.setdefault(key, item)
    clauses: list[ParsedClause] = []
    for key, conditions in groups.items():
        merged = (
            conditions[0]
            if len(conditions) == 1
            else FilterCondition(op="and", operands=conditions)
        )
        keeps_null = _evaluate_null(merged) is True
        _check_text_literals(merged, keeps_null)
        clauses.append(ParsedClause(item=names[key], condition=merged, keeps_null=keeps_null))
    return clauses


def literal_values(condition: FilterCondition) -> list[FilterValue]:
    """Return every literal in a condition tree."""
    values = list(condition.values)
    for operand in condition.operands:
        values.extend(literal_values(operand))
    return values


def not_applied_message(query: str, reason: str | None) -> str:
    """Review message for a mandatory detail filter that the output does not apply."""
    because = (
        f"It cannot be converted to a Power BI filter automatically because {reason}. "
        if reason
        else ""
    )
    return (
        f"Query '{query}' has a mandatory Cognos detail filter that was not applied. "
        f"{because}Recreate it as a Power Query step, a report/page filter, or a measure "
        "filter as appropriate."
    )


# ------------------------------------------------------------------------------- tokenizer


def _tokenize(expression: str) -> list[_Token]:
    text = _COMMENT_ONLY_MACRO_RE.sub(" ", expression)
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if match is None:
            raise UntranslatableFilter("unsupported-syntax", f"unexpected {text[position]!r}")
        position = match.end()
        kind = match.lastgroup or ""
        if kind == "ws":
            continue
        tokens.append(_Token(kind, match.group()))
    return tokens


def _prescan(tokens: list[_Token]) -> None:
    """Reject whole-expression constructs first, so the reason names the real blocker."""
    if any(token.kind in {"macro", "param"} for token in tokens):
        raise UntranslatableFilter("prompt-or-macro")
    words = [token.text.lower() for token in tokens if token.kind == "word"]
    relative = [word for word in words if word in _RELATIVE_DATE_WORDS]
    if relative:
        raise UntranslatableFilter("relative-date", relative[0])
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else _END
        if token.kind == "word" and token.text.lower() not in _KEYWORDS and following.is_op("("):
            raise UntranslatableFilter("function", f"{token.text}()")
    if any(token.is_op("{", "}", ":") for token in tokens):
        raise UntranslatableFilter("unsupported-syntax")


# ---------------------------------------------------------------------------------- parser


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.position = 0

    def peek(self, offset: int = 0) -> _Token:
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else _END

    def advance(self) -> _Token:
        token = self.peek()
        self.position += 1
        return token

    def parse(self) -> FilterCondition:
        if not self.tokens:
            raise UntranslatableFilter("unsupported-syntax", "empty expression")
        condition = self.parse_or()
        if self.peek().kind != "end":
            raise UntranslatableFilter("unsupported-syntax", f"unexpected {self.peek().text!r}")
        return condition

    def parse_or(self) -> FilterCondition:
        operands = [self.parse_and()]
        while self.peek().is_word("or"):
            self.advance()
            operands.append(self.parse_and())
        return operands[0] if len(operands) == 1 else _combine("or", operands)

    def parse_and(self) -> FilterCondition:
        operands = [self.parse_not()]
        while self.peek().is_word("and"):
            self.advance()
            operands.append(self.parse_not())
        return operands[0] if len(operands) == 1 else _combine("and", operands)

    def parse_not(self) -> FilterCondition:
        if self.peek().is_word("not"):
            self.advance()
            return _negate(self.parse_not())
        return self.parse_primary()

    def parse_primary(self) -> FilterCondition:
        if self.peek().is_op("("):
            self.advance()
            condition = self.parse_or()
            self.expect_op(")")
            return condition
        return self.parse_predicate()

    def expect_op(self, op: str) -> None:
        token = self.advance()
        if not token.is_op(op):
            raise UntranslatableFilter("unsupported-syntax", f"expected {op!r}")

    def parse_operand(self) -> _Operand:
        token = self.advance()
        operand: _Operand
        if token.kind == "ref":
            operand = _Ref(token.text)
        elif token.kind == "string":
            operand = FilterValue(kind="string", value=token.text[1:-1].replace("''", "'"))
        elif token.kind == "number":
            operand = FilterValue(kind="number", value=_number_text(token.text))
        elif token.kind == "date":
            operand = FilterValue(kind="date", value=_date_text(token.text))
        elif token.is_op("-") and self.peek().kind == "number":
            operand = FilterValue(kind="number", value=_number_text("-" + self.advance().text))
        elif token.is_word("null"):
            operand = _Null()
        elif token.is_op("("):
            operand = self.parse_operand()
            self.expect_op(")")
        else:
            raise UntranslatableFilter("unsupported-syntax", f"unexpected {token.text!r}")
        if self.peek().is_op(*_ARITHMETIC_OPS):
            raise UntranslatableFilter("expression")
        return operand

    def parse_predicate(self) -> FilterCondition:
        left = self.parse_operand()
        token = self.peek()
        if not isinstance(left, _Ref):
            # literal <op> [item] reads as [item] <flipped op> literal.
            if token.kind == "op" and token.text in _FLIPPED:
                self.advance()
                right = self.parse_operand()
                if isinstance(right, _Ref):
                    if isinstance(left, _Null):
                        return self.null_comparison(right.text, _FLIPPED[token.text])
                    return _comparison(right.text, _FLIPPED[token.text], left)
            raise UntranslatableFilter("unsupported-syntax", "no data item on the left")
        item = left.text
        if token.kind == "op" and token.text in _FLIPPED:
            self.advance()
            right = self.parse_operand()
            if isinstance(right, _Ref):
                raise UntranslatableFilter("item-vs-item")
            if isinstance(right, _Null):
                return self.null_comparison(item, token.text)
            return _comparison(item, token.text, right)
        negated = False
        if token.is_word("not"):
            self.advance()
            negated = True
            token = self.peek()
        condition = self.parse_keyword_predicate(item, token)
        return _negate(condition) if negated else condition

    def null_comparison(self, item: str, op: str) -> FilterCondition:
        if op == "=":
            return FilterCondition(op="is_null", item=item)
        if op in _NEGATION_OPS:
            return _negate(FilterCondition(op="is_null", item=item))
        raise UntranslatableFilter("unsupported-syntax", f"{op} null")

    def parse_keyword_predicate(self, item: str, token: _Token) -> FilterCondition:
        if token.is_word("in"):
            self.advance()
            return FilterCondition(op="in", item=item, values=self.parse_in_list())
        if token.is_word("is"):
            self.advance()
            negated = False
            if self.peek().is_word("not"):
                self.advance()
                negated = True
            if not self.advance().is_word("null", "missing"):
                raise UntranslatableFilter("unsupported-syntax", "is without null/missing")
            condition = FilterCondition(op="is_null", item=item)
            return _negate(condition) if negated else condition
        if token.is_word("between"):
            self.advance()
            low = self.literal_operand()
            if not self.advance().is_word("and"):
                raise UntranslatableFilter("unsupported-syntax", "between without and")
            high = self.literal_operand()
            return FilterCondition(op="between", item=item, values=[low, high])
        if token.is_word("like"):
            self.advance()
            pattern = self.literal_operand()
            if self.peek().is_word("escape"):
                raise UntranslatableFilter("like-pattern", "escape clause")
            return _like(item, pattern)
        if token.is_word("contains"):
            self.advance()
            return FilterCondition(op="contains", item=item, values=[self.string_operand()])
        if token.is_word("starts", "ends"):
            self.advance()
            if self.peek().is_word("with"):
                self.advance()
            value = self.string_operand()
            if token.is_word("ends"):
                raise UntranslatableFilter("ends-with")
            return FilterCondition(op="starts_with", item=item, values=[value])
        if token.kind == "end" or token.is_op(")") or token.is_word("and", "or"):
            raise UntranslatableFilter("bare-item", item)
        raise UntranslatableFilter("unsupported-syntax", f"unexpected {token.text!r}")

    def parse_in_list(self) -> list[FilterValue]:
        self.expect_op("(")
        values: list[FilterValue] = []
        while True:
            operand = self.parse_operand()
            if isinstance(operand, _Ref):
                raise UntranslatableFilter("item-vs-item")
            if isinstance(operand, _Null):
                raise UntranslatableFilter("in-list-null")
            values.append(operand)
            token = self.advance()
            if token.is_op(")"):
                return values
            if not token.is_op(",", ";"):
                raise UntranslatableFilter("unsupported-syntax", "malformed IN list")

    def literal_operand(self) -> FilterValue:
        operand = self.parse_operand()
        if isinstance(operand, _Ref):
            raise UntranslatableFilter("item-vs-item")
        if isinstance(operand, _Null):
            raise UntranslatableFilter("unsupported-syntax", "null operand")
        return operand

    def string_operand(self) -> FilterValue:
        value = self.literal_operand()
        if value.kind != "string":
            raise UntranslatableFilter("unsupported-syntax", "text operator on a non-text value")
        return value


# --------------------------------------------------------------------------------- helpers


def _comparison(item: str, op: str, value: FilterValue) -> FilterCondition:
    if op in _NEGATION_OPS:
        return _negate(FilterCondition(op="eq", item=item, values=[value]))
    kind = _COMPARISON_OPS.get(op)
    if kind is None:
        raise UntranslatableFilter("unsupported-syntax", op)
    return FilterCondition(op=kind, item=item, values=[value])  # type: ignore[arg-type]


def _negate(condition: FilterCondition) -> FilterCondition:
    return FilterCondition(op="not", operands=[condition])


def _combine(op: str, operands: list[FilterCondition]) -> FilterCondition:
    flat: list[FilterCondition] = []
    for operand in operands:
        flat.extend(operand.operands if operand.op == op else [operand])
    return FilterCondition(op=op, operands=flat)  # type: ignore[arg-type]


def _like(item: str, pattern: FilterValue) -> FilterCondition:
    """Map a LIKE pattern to equality, starts-with or contains, measured as Cognos does."""
    if pattern.kind != "string":
        raise UntranslatableFilter("like-pattern", "non-text pattern")
    text = pattern.value
    if "_" in text or "[" in text:
        raise UntranslatableFilter("like-pattern", f"'{text}'")
    core = text.strip("%")
    if "%" in core or not core:
        raise UntranslatableFilter("like-pattern", f"'{text}'")
    leading = text.startswith("%")
    trailing = text.endswith("%")
    if leading and trailing:
        return FilterCondition(
            op="contains", item=item, values=[FilterValue(kind="string", value=core)]
        )
    if trailing:
        return FilterCondition(
            op="starts_with", item=item, values=[FilterValue(kind="string", value=core)]
        )
    if leading:
        raise UntranslatableFilter("ends-with")
    return FilterCondition(op="eq", item=item, values=[pattern])


def _number_text(text: str) -> str:
    try:
        Decimal(text)
    except InvalidOperation as error:
        raise UntranslatableFilter("unsupported-syntax", f"number {text}") from error
    return text


def _date_text(text: str) -> str:
    normalized = text.replace(" ", "T")
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            datetime.strptime(normalized, fmt)
        except ValueError:
            continue
        return normalized
    raise UntranslatableFilter("unsupported-syntax", f"invalid date {text}")


def _conjuncts(condition: FilterCondition) -> list[FilterCondition]:
    if condition.op == "and":
        return [part for operand in condition.operands for part in _conjuncts(operand)]
    return [condition]


def _items(condition: FilterCondition) -> list[str]:
    items = [condition.item] if condition.item else []
    for operand in condition.operands:
        items.extend(_items(operand))
    return items


def _evaluate_null(condition: FilterCondition) -> bool | None:
    """Cognos three-valued result for a NULL item value: True, False or None (unknown)."""
    if condition.op == "is_null":
        return True
    if condition.op == "not":
        inner = _evaluate_null(condition.operands[0])
        return None if inner is None else not inner
    if condition.op in {"and", "or"}:
        results = [_evaluate_null(operand) for operand in condition.operands]
        decisive = condition.op == "or"
        if decisive in results:
            return decisive
        return None if None in results else not decisive
    return None


def _evaluate_empty(condition: FilterCondition) -> bool:
    """The result for an empty-string item value ('' is a value in Cognos, not NULL)."""
    op = condition.op
    if op == "is_null":
        return False
    if op == "not":
        return not _evaluate_empty(condition.operands[0])
    if op == "and":
        return all(_evaluate_empty(operand) for operand in condition.operands)
    if op == "or":
        return any(_evaluate_empty(operand) for operand in condition.operands)
    texts = [value.value for value in condition.values]
    if op in {"eq", "in", "starts_with", "contains"}:
        return "" in texts
    if op == "lt":
        return texts[0] != ""
    if op == "le":
        return True
    if op == "gt":
        return False
    if op == "ge":
        return texts[0] == ""
    # between: '' <= high always holds, so it is '' >= low.
    return texts[0] == ""


def _check_text_literals(condition: FilterCondition, keeps_null: bool) -> None:
    """Refuse text literals whose outcome depends on how a target treats '' and blanks.

    A whitespace-only literal depends on trailing-space collation rules. An empty literal is
    safe only where Cognos treats '' and NULL rows alike, so a target that equates '' with
    blank (as Power BI may) still keeps exactly the rows Cognos keeps.
    """
    values = literal_values(condition)
    texts = [value.value for value in values if value.kind == "string"]
    if any(text and not text.strip() for text in texts):
        raise UntranslatableFilter("blank-text")
    if "" not in texts:
        return
    if any(value.kind != "string" for value in values):
        raise UntranslatableFilter("empty-text")
    if _evaluate_empty(condition) != keeps_null:
        raise UntranslatableFilter("empty-text")
