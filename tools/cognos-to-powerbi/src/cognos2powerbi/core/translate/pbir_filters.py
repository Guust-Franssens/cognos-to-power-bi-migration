"""Render translated Cognos detail filters as PBIR visual-level filters.

A translated :class:`FilterClause` condition becomes a semantic-query condition in the shape Power
BI Desktop writes to ``visual.json`` (``filterConfig.filters``, PBIR filterConfiguration 1.3.0 /
semanticQuery 1.4.0): ``In`` for value lists, ``Comparison`` (``ComparisonKind`` 0 equal,
1 greater, 2 greater-or-equal, 3 less, 4 less-or-equal), ``StartsWith``/``Contains``, and
``Not``/``And``/``Or``. Literals are typed for the filtered column: ``'text'`` (quotes doubled),
``1000L`` (int64), ``2.5D`` (double), ``2.5M`` (decimal), ``datetime'2024-01-31T00:00:00'``.
A literal that does not fit the column's type is refused rather than coerced.

Cognos drops NULL rows for every negative or ordering predicate (``<>``, ``not in``, ``<``,
``between`` ...), measured on Cognos Analytics 12. How Power BI treats blanks in those filters is
not relied on: :func:`apply_null_semantics` states Cognos's outcome explicitly on every clause.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import reduce
from typing import Literal

from cognos2powerbi.core.ir.models import DataType, FilterCondition, FilterValue
from cognos2powerbi.core.translate.filters import UntranslatableFilter, literal_values

_COMPARISON_KINDS = {"eq": 0, "gt": 1, "ge": 2, "lt": 3, "le": 4}
_EQUAL = 0
_NULL = {"Literal": {"Value": "null"}}
_NUMERIC_SUFFIXES = {DataType.DOUBLE: "D", DataType.DECIMAL: "M"}
_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")


@dataclass(frozen=True)
class FilterField:
    """The model field a visual-level filter applies to.

    ``entity`` is the table the field is read from (for a measure, the table that holds it in the
    model), ``data_type`` the column type (``None`` for a measure).
    """

    entity: str
    name: str
    kind: Literal["column", "measure"] = "column"
    data_type: DataType | None = None

    def expression(self, source: dict[str, str]) -> dict:
        key = "Measure" if self.kind == "measure" else "Column"
        return {key: {"Expression": {"SourceRef": source}, "Property": self.name}}

    @property
    def label(self) -> str:
        return f"{self.entity}[{self.name}]"


def check_literals(condition: FilterCondition, field: FilterField) -> None:
    """Raise ``UntranslatableFilter('literal-type')`` if a literal does not fit the field's type."""
    for value in literal_values(condition):
        encode_literal(value, field)


def encode_literal(value: FilterValue, field: FilterField) -> str:
    """Encode a literal as a PBIR ``Literal`` value typed for the filtered field."""
    if field.kind == "measure":
        number = _number(value)
        if number is not None:
            return f"{_plain(number)}D"
    elif field.data_type == DataType.STRING:
        if value.kind == "string":
            return "'" + value.value.replace("'", "''") + "'"
    elif field.data_type == DataType.INT64:
        number = _number(value)
        if number is not None and number == number.to_integral_value():
            return f"{int(number)}L"
    elif field.data_type in _NUMERIC_SUFFIXES:
        number = _number(value)
        if number is not None:
            return f"{_plain(number)}{_NUMERIC_SUFFIXES[field.data_type]}"
    elif field.data_type == DataType.DATE_TIME:
        stamp = _datetime_text(value)
        if stamp is not None:
            return f"datetime'{stamp}'"
    raise UntranslatableFilter("literal-type", _mismatch(value, field))


def render_condition(
    condition: FilterCondition, keeps_null: bool, field: FilterField, alias: str
) -> dict:
    """Render one clause as a semantic-query condition, with Cognos NULL semantics applied."""
    target = field.expression({"Source": alias})
    rendered = _render(condition, target, field, top=True)
    return apply_null_semantics(rendered, condition, keeps_null, target)


def apply_null_semantics(
    rendered: dict, condition: FilterCondition, keeps_null: bool, target: dict
) -> dict:
    """State Cognos's outcome for NULL rows explicitly, whatever Power BI does with blanks.

    This is the one place that encodes it. Cognos keeps a NULL row only when the filter is true
    for NULL (``is null`` ...): such a clause gets ``or is blank`` unless it already tests for
    blank at its top level. Every other clause drops NULL rows in Cognos, so it gets ``and is
    not blank`` unless it is a positive match on non-empty values, which cannot hold for a
    blank. "is blank" is written the way Power BI Desktop writes its own "is blank" filter: an
    equality comparison with the ``null`` literal.
    """
    is_blank = _comparison(_EQUAL, target, _NULL)
    if keeps_null:
        if _tests_null(condition):
            return rendered
        return {"Or": {"Left": rendered, "Right": is_blank}}
    if _excludes_blank(condition):
        return rendered
    return {"And": {"Left": rendered, "Right": {"Not": {"Expression": is_blank}}}}


def filter_container(
    name: str, field: FilterField, clauses: list[tuple[FilterCondition, bool]]
) -> dict:
    """A visual-level PBIR filter on ``field`` holding every clause (ANDed), locked in view mode.

    The filter is ``Categorical`` when it is a plain value list (the shape of Desktop's basic
    filter), otherwise ``Advanced``.
    """
    alias = _alias(field.entity)
    conditions = [
        render_condition(condition, keeps_null, field, alias) for condition, keeps_null in clauses
    ]
    where = reduce(lambda left, right: {"And": {"Left": left, "Right": right}}, conditions)
    return {
        "name": name,
        "field": field.expression({"Entity": field.entity}),
        "type": "Categorical" if set(where) == {"In"} else "Advanced",
        "filter": {
            "Version": 2,
            "From": [{"Name": alias, "Entity": field.entity, "Type": 0}],
            "Where": [{"Condition": where}],
        },
        "howCreated": "User",
        "isLockedInViewMode": True,
    }


def filter_name(*parts: str) -> str:
    """A stable 20-character hexadecimal filter name, the form Power BI Desktop uses."""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]


def compares_text(condition: FilterCondition) -> bool:
    """Whether a condition compares text values (Power BI compares them case-insensitively)."""
    return any(value.kind == "string" for value in literal_values(condition))


# --------------------------------------------------------------------------------- render


def _render(condition: FilterCondition, target: dict, field: FilterField, top: bool) -> dict:
    op = condition.op
    values = _in_values(condition)
    if values is not None and (top or op != "eq"):
        return {
            "In": {
                "Expressions": [target],
                "Values": [[_literal(value, field)] for value in values],
            }
        }
    if op in _COMPARISON_KINDS:
        return _comparison(_COMPARISON_KINDS[op], target, _literal(condition.values[0], field))
    if op == "between":
        low, high = condition.values
        return {
            "And": {
                "Left": _comparison(_COMPARISON_KINDS["ge"], target, _literal(low, field)),
                "Right": _comparison(_COMPARISON_KINDS["le"], target, _literal(high, field)),
            }
        }
    if op == "is_null":
        return _comparison(_EQUAL, target, _NULL)
    if op in {"starts_with", "contains"}:
        key = "StartsWith" if op == "starts_with" else "Contains"
        return {key: {"Left": target, "Right": _literal(condition.values[0], field)}}
    operands = [_render(operand, target, field, top=False) for operand in condition.operands]
    if op == "not":
        return {"Not": {"Expression": operands[0]}}
    key = "And" if op == "and" else "Or"
    return reduce(lambda left, right: {key: {"Left": left, "Right": right}}, operands)


def _in_values(condition: FilterCondition) -> list[FilterValue] | None:
    """The values of an equality, IN list or OR of those, or ``None`` for anything else."""
    if condition.op in {"eq", "in"}:
        return list(condition.values)
    if condition.op == "or":
        values: list[FilterValue] = []
        for operand in condition.operands:
            inner = _in_values(operand)
            if inner is None:
                return None
            values.extend(inner)
        return values
    return None


def _comparison(kind: int, left: dict, right: dict) -> dict:
    return {"Comparison": {"ComparisonKind": kind, "Left": left, "Right": right}}


def _literal(value: FilterValue, field: FilterField) -> dict:
    return {"Literal": {"Value": encode_literal(value, field)}}


def _tests_null(condition: FilterCondition) -> bool:
    """Whether a clause keeps blank rows itself: it is, or ORs in, an ``is null`` test."""
    if condition.op == "is_null":
        return True
    return condition.op == "or" and any(_tests_null(operand) for operand in condition.operands)


def _excludes_blank(condition: FilterCondition) -> bool:
    """Whether a clause can only match non-blank values, so excluding blank is redundant.

    A positive match (=, in, starts with, contains) on values that are neither empty text,
    whitespace nor zero cannot match a blank under any reading of blank; ``is not null`` is the
    exclusion itself. Negative and ordering predicates are never assumed to exclude blank.
    """
    op = condition.op
    if op in {"eq", "in", "starts_with", "contains"}:
        return all(_never_blank(value) for value in condition.values)
    if op == "not":
        return condition.operands[0].op == "is_null"
    if op == "or":
        return all(_excludes_blank(operand) for operand in condition.operands)
    if op == "and":
        return any(_excludes_blank(operand) for operand in condition.operands)
    return False


def _never_blank(value: FilterValue) -> bool:
    if value.kind == "string":
        return bool(value.value.strip())
    if value.kind == "number":
        number = _number(value)
        return number is not None and number != 0
    return True


def _alias(entity: str) -> str:
    first = entity[:1].lower()
    return first if first.isascii() and first.isalpha() else "t"


# -------------------------------------------------------------------------------- literals


def _number(value: FilterValue) -> Decimal | None:
    if value.kind != "number":
        return None
    try:
        number = Decimal(value.value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _plain(number: Decimal) -> str:
    """A number without exponent or trailing fractional zeros (``1E+3`` -> ``1000``)."""
    text = format(number.normalize(), "f")
    return "0" if text in {"-0", "0"} else text


def _datetime_text(value: FilterValue) -> str | None:
    if value.kind not in {"date", "string"}:
        return None
    text = value.value.strip().replace(" ", "T")
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        stamp = parsed.strftime("%Y-%m-%dT%H:%M:%S")
        return f"{stamp}.{parsed.microsecond:06d}" if parsed.microsecond else stamp
    return None


def _mismatch(value: FilterValue, field: FilterField) -> str:
    shown = f"'{value.value}'" if value.kind == "string" else value.value
    if field.kind == "measure":
        return f"measure {field.label} is compared with the {value.kind} {shown}"
    data_type = field.data_type.value if field.data_type else "untyped"
    detail = f"{field.label} is {data_type} but is compared with the {value.kind} {shown}"
    if field.data_type == DataType.STRING and value.kind == "number":
        detail += (
            "; a column the Cognos report gives no data type defaults to text, so set its "
            "type in the model if it is numeric"
        )
    return detail
