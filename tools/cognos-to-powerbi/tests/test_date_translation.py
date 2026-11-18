"""Tests for translating Cognos business date/time functions to scalar DAX."""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from cognos2powerbi.core.translate import translate_measure_expression
from cognos2powerbi.core.translate.expressions import _has_invalid_tokens, _has_text_operand

D = "[M].[S].[d]"


def _translate(expression: str, aggregate: str = "none", separator: str = ","):
    return translate_measure_expression(expression, "Q", aggregate, list_separator=separator)


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        (f"_add_days({D}, 3)", "IF(ISBLANK(Q[d]), BLANK(), Q[d] + 3)"),
        (f"_add_days({D}, -1)", "IF(ISBLANK(Q[d]), BLANK(), Q[d] - 1)"),
        (
            f"_add_days({D}, [M].[S].[n])",
            "IF(OR(ISBLANK(Q[d]), ISBLANK(Q[n])), BLANK(), Q[d] + Q[n])",
        ),
        (f"_add_hours({D}, 2)", "IF(ISBLANK(Q[d]), BLANK(), Q[d] + 2 / 24)"),
        (f"_add_minutes({D}, 90)", "IF(ISBLANK(Q[d]), BLANK(), Q[d] + 90 / 1440)"),
        (f"_add_seconds({D}, -5)", "IF(ISBLANK(Q[d]), BLANK(), Q[d] - 5 / 86400)"),
        # EDATE clamps Jan 31 + 1 month to Feb 28/29 but drops the time of day; adding the day
        # difference to the original value keeps it, as Cognos does for timestamps.
        (
            f"_add_months({D}, 1)",
            "IF(ISBLANK(Q[d]), BLANK(), Q[d] + DATEDIFF(Q[d], EDATE(Q[d], 1), DAY))",
        ),
        (
            f"_add_months({D}, -13)",
            "IF(ISBLANK(Q[d]), BLANK(), Q[d] + DATEDIFF(Q[d], EDATE(Q[d], -13), DAY))",
        ),
        (
            f"_add_years({D}, 1)",
            "IF(ISBLANK(Q[d]), BLANK(), Q[d] + DATEDIFF(Q[d], EDATE(Q[d], 12), DAY))",
        ),
        (
            f"_add_years({D}, [M].[S].[n])",
            "IF(OR(ISBLANK(Q[d]), ISBLANK(Q[n])), BLANK(), "
            "Q[d] + DATEDIFF(Q[d], EDATE(Q[d], 12 * Q[n]), DAY))",
        ),
        (f"_first_of_month({D})", "IF(ISBLANK(Q[d]), BLANK(), Q[d] - DAY(Q[d]) + 1)"),
        (
            f"_last_of_month({D})",
            "IF(ISBLANK(Q[d]), BLANK(), Q[d] - DAY(Q[d]) + DAY(EOMONTH(Q[d], 0)))",
        ),
        (
            f"_days_to_end_of_month({D})",
            "IF(ISBLANK(Q[d]), BLANK(), DAY(EOMONTH(Q[d], 0)) - DAY(Q[d]))",
        ),
        (f"_day_of_week({D}, 1)", "WEEKDAY(Q[d], 2)"),
        (f"_day_of_week({D}, 7)", "WEEKDAY(Q[d], 1)"),
        (f"_day_of_week({D}, 3)", "IF(ISBLANK(Q[d]), BLANK(), MOD(WEEKDAY(Q[d], 2) - 3, 7) + 1)"),
        (
            f"_day_of_year({D})",
            "IF(ISBLANK(Q[d]), BLANK(), DATEDIFF(DATE(YEAR(Q[d]), 1, 1), Q[d], DAY) + 1)",
        ),
        (f"_week_of_year({D})", "WEEKNUM(Q[d], 21)"),
        (f"_year({D})", "YEAR(Q[d])"),
        (f"_month({D})", "MONTH(Q[d])"),
        (f"_day({D})", "DAY(Q[d])"),
        (f"_hour({D})", "HOUR(Q[d])"),
        (f"_minute({D})", "MINUTE(Q[d])"),
    ],
)
def test_date_function_translates_to_exact_scalar_dax(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True
    assert result.note is None


def _evaluate_day_of_week(dax: str, day: date) -> int:
    """Evaluate the emitted day-of-week DAX for one date using the documented DAX semantics.

    WEEKDAY(x, 2) numbers Monday 1 .. Sunday 7, WEEKDAY(x, 1) Sunday 1 .. Saturday 7, and DAX
    MOD takes the sign of the divisor like Python's ``%``.
    """
    iso = day.isoweekday()
    text = re.sub(r"^IF\(ISBLANK\(Q\[d\]\), BLANK\(\), (.*)\)$", r"\1", dax)
    text = text.replace("WEEKDAY(Q[d], 2)", str(iso)).replace("WEEKDAY(Q[d], 1)", str(iso % 7 + 1))
    text = re.sub(r"MOD\((.+), 7\)", r"((\1) % 7)", text)
    assert re.fullmatch(r"[\d\s()+\-%]+", text), text
    return int(eval(text))


@pytest.mark.parametrize("start", range(1, 8))
def test_day_of_week_numbers_days_from_the_given_start_day(start: int) -> None:
    # IBM: 1 is the first day of the week given by the second argument (1 = Monday, 7 = Sunday).
    result = _translate(f"_day_of_week({D}, {start})")
    assert result.confident is True
    monday = date(2024, 1, 1)
    for offset in range(7):
        day = monday + timedelta(days=offset)
        assert _evaluate_day_of_week(result.dax, day) == (day.isoweekday() - start) % 7 + 1


def test_day_of_week_matches_ibm_example() -> None:
    # IBM: _day_of_week(2003-01-01, 1) returns 3 (a Wednesday, weeks starting on Monday).
    result = _translate(f"_day_of_week({D}, 1)")
    assert _evaluate_day_of_week(result.dax, date(2003, 1, 1)) == 3


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        ("current_date", "TODAY()"),
        ("_year(current_date)", "YEAR(TODAY())"),
        ("_first_of_month (current_date)", "TODAY() - DAY(TODAY()) + 1"),
        (
            "_add_days (current_date, - _day_of_week (current_date, 7) + 1)",
            "TODAY() + (- WEEKDAY(TODAY(), 1) + 1)",
        ),
        (
            "_add_days (current_date, - _day_of_year (current_date) + 1)",
            "TODAY() + (- (DATEDIFF(DATE(YEAR(TODAY()), 1, 1), TODAY(), DAY) + 1) + 1)",
        ),
        ("localtimestamp", "NOW()"),
    ],
)
def test_clock_expressions_are_confident_with_refresh_time_note(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True
    assert result.note is not None
    assert "refresh" in result.note and "UTC" in result.note


@pytest.mark.parametrize(
    "expression",
    [
        "current_time",
        "localtime",
        "null",
        "case when [M].[S].[a] is null then 0 else 1 end",
        "[M].[S].[a] between 1 and 2",
        # A word before a column reference is not a table name.
        f"_add_days({D}, [M].[S].[n]) between {D} and [M].[S].[e]",
        "[M].[S].[a] like [M].[S].[b]",
        "[M].[S].[a] contains [M].[S].[b]",
        "[M].[S].[a] in_range [M].[S].[b]",
        f"current_date between  {D} and [M].[S].[e]",
        f"current_date between\n{D}",
        "current_date[x]",
        f"count(distinct {D})",
        f"_hour({D}) = 10:00",
        "--3",
        f"{D} --3",
        "[M].[S].[a], [M].[S].[b]",
        "[M].[S].[a] * 1,5",
        "[M].[S].[a] + [M].[S].[b]; [M].[S].[c]",
    ],
)
def test_leftover_cognos_tokens_are_not_confident(expression: str) -> None:
    result = _translate(expression)
    assert result.confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        ("if ([M].[S].[a] > 1) then (TRUE) else (FALSE)", "IF(Q[a] > 1, TRUE, FALSE)"),
        ("NOT [M].[S].[f]", "NOT Q[f]"),
        # NOT before a value or a call (not a column reference) exercises the keyword list.
        (f"not _year({D}) = 2020", "not YEAR(Q[d]) = 2020"),
        ("[M].[S].[a] - -3", "Q[a] - -3"),
        # Words and references inside a string literal are text, not code.
        ("[M].[S].[a] = 'between [x]: a || b'", 'Q[a] = "between [x]: a || b"'),
        ("[M].[F].[escaped ]] x] * 2", "Q[escaped ]] x] * 2"),
        ("_add_days([M].[S].[a ]],b], 1)", "IF(ISBLANK(Q[a ]],b]), BLANK(), Q[a ]],b] + 1)"),
        ("abs([M].[S].[a]) + 1.5", "ABS(Q[a]) + 1.5"),
    ],
)
def test_valid_dax_words_and_escaped_names_stay_confident(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        # Cognos returns null when any operand is null; BLANK() - 3 is -3 in DAX, so each
        # column under the arithmetic is guarded rather than the arithmetic result.
        (
            "_add_hours([M].[S].[ts], [M].[S].[Key] - [M].[S].[Start])",
            "IF(OR(ISBLANK(Q[ts]), OR(ISBLANK(Q[Key]), ISBLANK(Q[Start]))), BLANK(), "
            "Q[ts] + (Q[Key] - Q[Start]) / 24)",
        ),
        (
            f"_add_days({D}, - _day_of_week([M].[S].[e], 7) + 1)",
            "IF(OR(ISBLANK(Q[d]), ISBLANK(Q[e])), BLANK(), Q[d] + (- WEEKDAY(Q[e], 1) + 1))",
        ),
        (
            f"_hour(_add_hours({D}, [M].[S].[a] - 3))",
            "HOUR(IF(OR(ISBLANK(Q[d]), ISBLANK(Q[a])), BLANK(), Q[d] + (Q[a] - 3) / 24))",
        ),
        (
            f"_add_months(_add_days({D}, 1), [M].[S].[n])",
            "(VAR __c2d1 = IF(ISBLANK(Q[d]), BLANK(), Q[d] + 1) RETURN "
            "IF(OR(ISBLANK(Q[d]), ISBLANK(Q[n])), BLANK(), "
            "__c2d1 + DATEDIFF(__c2d1, EDATE(__c2d1, Q[n]), DAY)))",
        ),
        (
            f"_add_days({D}, abs([M].[S].[n]))",
            "(VAR __c2d1 = ABS(Q[n]) RETURN "
            "IF(OR(ISBLANK(Q[d]), ISBLANK(__c2d1)), BLANK(), Q[d] + __c2d1))",
        ),
        # HOUR(BLANK()) is BLANK(), but HOUR(BLANK() + 1) is not: guard arithmetic arguments only.
        (
            "_hour([M].[S].[t] + [M].[S].[n])",
            "IF(OR(ISBLANK(Q[t]), ISBLANK(Q[n])), BLANK(), HOUR((Q[t] + Q[n])))",
        ),
        (
            f"_week_of_year(_add_days({D}, 1))",
            "WEEKNUM(IF(ISBLANK(Q[d]), BLANK(), Q[d] + 1), 21)",
        ),
    ],
)
def test_nested_and_computed_arguments_keep_cognos_null_semantics(
    expression: str, dax: str
) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


def test_row_level_date_calculation_is_aggregated_per_row() -> None:
    result = _translate(f"_add_days({D}, 1)", aggregate="total")
    assert result.dax == "SUMX(Q, IF(ISBLANK(Q[d]), BLANK(), Q[d] + 1))"
    assert result.confident is True


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        # Day boundaries from the first to the second date: DATEDIFF(start, end).
        ("_days_between([M].[S].[a], [M].[S].[b])", "DATEDIFF(Q[b], Q[a], DAY)"),
        (f"_second({D})", "SECOND(Q[d])"),
        ("_make_timestamp(2020, 1, 31)", "DATE(2020, 1, 31)"),
        ("current_timestamp", "UTCNOW()"),
        (
            f"_day_of_week({D}, 8)",
            "IF(ISBLANK(Q[d]), BLANK(), MOD(WEEKDAY(Q[d], 2) - 8, 7) + 1)",
        ),
        (
            f"_day_of_week({D}, [M].[S].[n])",
            "IF(OR(ISBLANK(Q[d]), ISBLANK(Q[n])), BLANK(), MOD(WEEKDAY(Q[d], 2) - Q[n], 7) + 1)",
        ),
    ],
)
def test_inexact_date_translations_are_hints_only(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is False
    assert result.note is not None


@pytest.mark.parametrize(
    "expression",
    [
        "_months_between([M].[S].[a], [M].[S].[b])",
        "_years_between([M].[S].[a], [M].[S].[b])",
        "_add_days([M].[S].[d])",
        "mod([M].[S].[a], 24)",
        "datediff([M].[S].[a], [M].[S].[b])",
    ],
)
def test_untranslated_or_user_written_calls_are_not_confident(expression: str) -> None:
    result = _translate(expression)
    assert result.confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        (
            "#timestampMask ($current_timestamp, 'yyyy') + '-01-01'#",
            '#timestampMask ($current_timestamp, "yyyy") + "-01-01"#',
        ),
        (
            "cast(dateadd({day}, -datepart({dw}, getdate()) + 1, getdate()) as date)",
            "cast(dateadd({day}, -datepart({dw}, getdate()) + 1, getdate()) as date)",
        ),
        ("getdate()", "getdate()"),
        ("year(getdate())", "year(getdate())"),
    ],
)
def test_macros_and_native_sql_stay_untouched_and_not_confident(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is False


@pytest.mark.parametrize("macro", ["#1#", "#prompt('n', 'integer')#", "?n?"])
def test_macro_or_prompt_inside_date_function_is_not_confident(macro: str) -> None:
    # The date pass binds the argument to a VAR, which would otherwise hide the macro text.
    assert _translate(f"_add_days({D}, {macro})").confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        ("_day_of_week([M].[S].[Datum]; 1)", "WEEKDAY(Q[Datum], 2)"),
        (f"_add_days({D}; 2,0)", "IF(ISBLANK(Q[d]), BLANK(), Q[d] + 2.0)"),
        (
            "CASE _day_of_week([M].[S].[Datum]; 1) WHEN 1 THEN 'Maandag' ELSE '' END",
            'SWITCH(WEEKDAY(Q[Datum], 2), 1, "Maandag", "")',
        ),
        (
            "_add_hours([M].[S].[ts]; [M].[S].[Key] - [M].[S].[Start])",
            "IF(OR(ISBLANK(Q[ts]), OR(ISBLANK(Q[Key]), ISBLANK(Q[Start]))), BLANK(), "
            "Q[ts] + (Q[Key] - Q[Start]) / 24)",
        ),
    ],
)
def test_semicolon_separator_with_decimal_comma(expression: str, dax: str) -> None:
    result = _translate(expression, separator=";")
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize("expression", [f"_add_days({D}; 1, 5)", f"_add_days({D}, 1)"])
def test_semicolon_separator_rejects_stray_commas(expression: str) -> None:
    assert _translate(expression, separator=";").confident is False


def test_unsupported_separator_is_rejected() -> None:
    with pytest.raises(ValueError):
        _translate(f"_add_days({D}, 1)", separator="|")


@pytest.mark.parametrize(
    ("expression", "hint"),
    [
        ("_add_days(2024-01-31, 1)", "DATE(y, m, d) in DAX"),
        ("_year(2024-01-31)", "DATE(y, m, d) in DAX"),
        (f"{D} = 2024-01-31", "DATE(y, m, d) in DAX"),
        (f"{D} > 2024-01-31 10:00:00", "DATE(y, m, d) + TIME(h, m, s)"),
    ],
)
def test_bare_date_literal_is_not_confident(expression: str, hint: str) -> None:
    # DAX reads 2024-01-31 as the number 1992; Cognos means a date.
    result = _translate(expression)
    assert result.confident is False
    assert hint in result.note


@pytest.mark.parametrize(
    "expression",
    [
        "_add_days('2024-01-31', 1)",
        "_year('2024-01-31')",
        "_first_of_month('2024-01-31')",
        f"_days_between({D}, '2024-01-31')",
        f"_add_months({D}, '1')",
    ],
)
def test_string_literal_date_function_argument_is_not_confident(expression: str) -> None:
    result = _translate(expression)
    assert result.confident is False
    assert "string literal" in result.note


@pytest.mark.parametrize(
    ("expression", "separator"),
    [
        (f"_add_days({D}, 1.5)", ","),
        (f"_add_days({D}; 1,5)", ";"),
        (f"_add_hours({D}, 1.5)", ","),
        (f"_add_minutes({D}, -2.5)", ","),
        (f"_add_months({D}, 1.5)", ","),
        (f"_add_months({D}, -1.5)", ","),
        (f"_add_years({D}; 0,5)", ";"),
    ],
)
def test_fractional_literal_offset_is_not_confident(expression: str, separator: str) -> None:
    # Cognos documents these offsets as integers; EDATE silently rounds 1.5 to 2.
    result = _translate(expression, separator=separator)
    assert result.confident is False
    assert "integer" in result.note


def test_fractional_seconds_offset_stays_confident() -> None:
    # IBM documents fractional seconds for _add_seconds.
    result = _translate(f"_add_seconds({D}, 1.5)")
    assert result.dax == "IF(ISBLANK(Q[d]), BLANK(), Q[d] + 1.5 / 86400)"
    assert result.confident is True


def test_cognos_concatenation_is_not_confident() -> None:
    # Cognos || concatenates strings; DAX || is a logical OR that fails on text.
    result = _translate(f"_year({D}) || 'x'")
    assert result.confident is False
    assert "concatenation" in result.note


@pytest.mark.parametrize(
    "expression",
    [
        f"_day_of_week({D}, 0)",
        f"_day_of_week({D}, 8)",
        "_add_days(, 1)",
        f"_add_days({D},)",
        "Q",
        "1 Q [M].[S].[n]",
        f"_add_days({D}, 1)(2)",
    ],
)
def test_invalid_calls_and_operands_are_not_confident(expression: str) -> None:
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    "dax",
    [
        "between Q[x]",
        "between  Q[x]",
        "between\nQ[x]",
        "XQ[x]",
        "NOTQ[x]",
        "IF(Q, 1, 2)",
        "SUMX(Q)",
        "TODAY()[x]",
        "TRUE [x]",
        "Q",
        "1 Q Q[n]",
        "SUMX(Q Q[n])",
        "IF(TRUE, 1, 2)(2)",
        "Q[a] Q[b]",
        '"a" "b"',
        "() + 1",
        "Q[a] // 2",
    ],
)
def test_invalid_dax_leftovers_are_detected(dax: str) -> None:
    assert _has_invalid_tokens(dax, "Q") is True


@pytest.mark.parametrize(
    "dax",
    [
        "SUMX(Q, Q[a] * 2)",
        "NOT Q[f]",
        'Q[a] = "between [x]"',
        "(VAR __c2d1 = ABS(Q[n]) RETURN IF(ISBLANK(__c2d1), BLANK(), Q[d] + __c2d1))",
        "DATEDIFF(Q[a], Q[b], DAY) - -3",
        "TODAY() + 1",
    ],
)
def test_valid_dax_passes_leftover_check(dax: str) -> None:
    assert _has_invalid_tokens(dax, "Q") is False


@pytest.mark.parametrize("expression", ["_add_days(, 1)", f"_add_days({D},)", "_year()"])
def test_empty_date_function_argument_is_left_untranslated(expression: str) -> None:
    result = _translate(expression)
    assert result.dax == expression.replace(D, "Q[d]")
    assert result.confident is False


@pytest.mark.parametrize("expression", ["current_date[x]", "current_date[]"])
def test_clock_constant_followed_by_bracket_is_not_translated(expression: str) -> None:
    # current_date[...] is not the clock constant; it must not become TODAY()[...].
    result = _translate(expression)
    assert "TODAY" not in result.dax
    assert result.confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        (
            f"_year({D}) = 2024 and [M].[S].[a] = 'black or white'",
            'YEAR(Q[d]) = 2024 && Q[a] = "black or white"',
        ),
        (
            f"_add_days({D},1) > [M].[S].[e] and [M].[S].[a] = 'p and q'",
            'IF(ISBLANK(Q[d]), BLANK(), Q[d] + 1) > Q[e] && Q[a] = "p and q"',
        ),
        ("[M].[S].[a] = 'upper(x)'", 'Q[a] = "upper(x)"'),
        ("[M].[S].[a] = 'case when x then y end'", 'Q[a] = "case when x then y end"'),
        ("[M].[S].[a] = 'a''nd b'", 'Q[a] = "a\'nd b"'),
        ("[M].[S].[a] = 'it''s'", 'Q[a] = "it\'s"'),
        ("[M].[S].[a] = 'say \"hi\" or not'", 'Q[a] = "say ""hi"" or not"'),
        ("'x and y' = [M].[S].[a]", '"x and y" = Q[a]'),
        ("[M].[S].[a] = 'or' or [M].[S].[b] = 'and'", 'Q[a] = "or" || Q[b] = "and"'),
        ("[M].[S].[a] = 'a  b'", 'Q[a] = "a  b"'),
        ("[M].[S].[sales and returns] + 1", "Q[sales and returns] + 1"),
        ("upper([M].[S].[x or y])", "UPPER(Q[x or y])"),
        (
            "case when [M].[S].[a] = 'end' then 'when' else 'then' end",
            'IF(Q[a] = "end", "when", "then")',
        ),
        ("if ([M].[S].[a] = ')') then ('x') else ('y')", 'IF(Q[a] = ")", "x", "y")'),
        # A date inside a string is text, not a bare date literal.
        ("[M].[S].[a] = '2024-01-31'", 'Q[a] = "2024-01-31"'),
    ],
)
def test_strings_and_names_are_not_rewritten(expression: str, dax: str) -> None:
    # Operators, function names, case keywords and spacing inside a Cognos string or a column
    # name are data, not syntax.
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


def test_repeated_reference_in_date_argument_is_guarded_once() -> None:
    result = _translate(f"_add_days({D}, _day(_add_hours({D}, 1)))")
    assert result.dax == (
        "IF(ISBLANK(Q[d]), BLANK(), Q[d] + DAY(IF(ISBLANK(Q[d]), BLANK(), Q[d] + 1 / 24)))"
    )
    assert result.confident is True


def test_logical_word_touching_a_string_is_not_confident() -> None:
    # 'x'and is not rewritten to &&; the leftover word must not be reported as confident DAX.
    assert _translate("[M].[S].[a]='x'and [M].[S].[b]='y'").confident is False


@pytest.mark.parametrize(
    "offset", ["(1.5)", "-(1.5)", "3/2", "1.5*1", "0.5 + 0", ".5", "[M].[S].[n] / 2"]
)
@pytest.mark.parametrize("function", ["_add_days", "_add_months", "_add_years"])
def test_wrapped_fractional_offset_is_not_confident(function: str, offset: str) -> None:
    result = _translate(f"{function}({D}, {offset})")
    assert result.confident is False
    assert "integer" in result.note


@pytest.mark.parametrize(
    ("offset", "months"),
    [("2.", "(2.)"), ("2.0", "2.0"), ("[M].[S].[n]", "Q[n]"), ("_hour([M].[S].[t]) - 2", None)],
)
def test_integral_or_column_offset_stays_confident(offset: str, months: str | None) -> None:
    # The engine evaluates EDATE(DATE(2024,1,31), (2.)) as 2024-03-31.
    result = _translate(f"_add_months({D}, {offset})")
    assert result.confident is True
    if months:
        assert f"EDATE(Q[d], {months})" in result.dax


@pytest.mark.parametrize(
    "expression",
    [
        "([M].[S].[a]",
        "[M].[S].[a])",
        "[M].[S].[a] +",
        "[M].[S].[a] ** 2",
        "[M].[S].[a] % 2",
        "[M].[S].[a] != 2",
        "[M].[S].[a] =< 2",
        "[M].[S].[a] == 2",
        "[M].[S].[a] .5",
        "[M].[S].[a] 2",
        "[M].[S].[a] not [M].[S].[b]",
        "[M].[S].[a] /* c */",
        # DAX reads -- as a line comment, not as minus minus.
        "[M].[S].[a] -- 2",
        "([M].[S].[a], 2)",
    ],
)
def test_invalid_operator_syntax_is_not_confident(expression: str) -> None:
    # The DAX engine rejects each of these with a syntax error.
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        ("[M].[S].[a] <> 2", "Q[a] <> 2"),
        ("[M].[S].[a] <= 2", "Q[a] <= 2"),
        ("[M].[S].[a] >= 2", "Q[a] >= 2"),
        ("[M].[S].[a] - -3", "Q[a] - -3"),
        ("[M].[S].[a] * +3", "Q[a] * +3"),
        ("[M].[S].[a] ^ 2", "Q[a] ^ 2"),
        ("[M].[S].[a] & 'x'", 'Q[a] & "x"'),
        ("[M].[S].[a] + .5", "Q[a] + .5"),
        ("not [M].[S].[a] > 2", "not Q[a] > 2"),
    ],
)
def test_valid_operator_syntax_stays_confident(expression: str, dax: str) -> None:
    # Each form was evaluated by the Power BI Desktop DAX engine.
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


def test_parenthesized_string_date_argument_is_not_confident() -> None:
    result = _translate("_add_days(('1'), 1)")
    assert result.confident is False
    assert "string literal" in result.note


# Each DAX call below was run in the Power BI Desktop engine: the "invalid" ones fail with
# "Too few/many arguments were passed to the <F> function", the "valid" ones evaluate.
@pytest.mark.parametrize(
    "dax",
    [
        'MID("abc", 2)',
        'MID("abc", 2, 1, 1)',
        "LEN()",
        'LEN("a", 1)',
        "UPPER()",
        'UPPER("a", 1)',
        "LOWER()",
        'LOWER("a", 1)',
        "TRIM()",
        'TRIM("a", 1)',
        "ABS()",
        "ABS(1, 2)",
        "SQRT()",
        "SQRT(4, 1)",
        "ROUND(2.5)",
        "ROUND(2.5, 0, 1)",
        "CEILING(2.5)",
        "CEILING(2.5, 1, 1)",
        "FLOOR(2.5)",
        "FLOOR(2.5, 1, 1)",
        "COALESCE(1)",
        "SUM()",
        "SUM(Q[a], Q[b])",
        "AVERAGE()",
        "AVERAGE(Q[a], Q[b])",
        "COUNT()",
        "COUNT(Q[a], Q[b])",
        "DISTINCTCOUNT()",
        "DISTINCTCOUNT(Q[a], Q[b])",
        "MIN()",
        "MIN(1, 2, 3)",
        "MAX()",
        "MAX(1, 2, 3)",
        "SUMX(Q)",
        "SUMX(Q, 1, 2)",
        "AVERAGEX(Q)",
        "AVERAGEX(Q, 1, 2)",
        "MINX(Q)",
        "MAXX(Q)",
        "COUNTX(Q)",
        "COUNTX(Q, Q[a], 1)",
        "BLANK(1)",
        "TODAY(1)",
        "NOW(1)",
        "UTCNOW(1)",
        "TRUE(1)",
        "FALSE(1)",
        "IF(TRUE)",
        "IF(TRUE, 1, 2, 3)",
        "SWITCH(1, 2)",
        "YEAR()",
        "YEAR(Q[d], 1)",
        "MONTH()",
        "MONTH(Q[d], 1)",
        "DAY()",
        "DAY(Q[d], 1)",
        "HOUR()",
        "HOUR(Q[d], 1)",
        "MINUTE()",
        "MINUTE(Q[d], 1)",
        "SECOND()",
        "SECOND(Q[d], 1)",
        "ISBLANK()",
        "ISBLANK(1, 2)",
        "AND(TRUE)",
        "AND(TRUE, TRUE, TRUE)",
        "OR(TRUE)",
        "OR(TRUE, TRUE, TRUE)",
        "WEEKDAY()",
        "WEEKDAY(Q[d], 2, 1)",
        "WEEKNUM()",
        "WEEKNUM(Q[d], 21, 1)",
        "EDATE(Q[d])",
        "EDATE(Q[d], 1, 1)",
        "EOMONTH(Q[d])",
        "EOMONTH(Q[d], 0, 1)",
        "MOD(5)",
        "MOD(5, 2, 1)",
        "DATEDIFF(Q[d], Q[e])",
        "DATEDIFF(Q[d], Q[e], DAY, 1)",
        "DATE(2024, 1)",
        "DATE(2024, 1, 1, 1)",
        "1 + ABS()",
        "IF(TRUE, ABS(), 1)",
    ],
)
def test_wrong_argument_count_is_invalid_dax(dax: str) -> None:
    assert _has_invalid_tokens(dax, "Q") is True


@pytest.mark.parametrize(
    "dax",
    [
        'MID("abc", 2, 1)',
        "ROUND(2.5, 0)",
        "COALESCE(1, 2)",
        "COALESCE(1, 2, 3)",
        "MIN(1, 2)",
        "MAX(1, 2)",
        "TRUE()",
        "FALSE()",
        "IF(TRUE, 1)",
        "SWITCH(1, 1, 2)",
        "SWITCH(1, 1, 2, 3)",
        "WEEKDAY(Q[d])",
        "WEEKNUM(Q[d])",
        "CEILING(Q[a], 1)",
    ],
)
def test_valid_argument_count_passes_leftover_check(dax: str) -> None:
    assert _has_invalid_tokens(dax, "Q") is False


@pytest.mark.parametrize(
    ("expression", "separator"),
    [
        # In a decimal-comma locale Cognos reads 2,5 as one number: DAX COALESCE needs two.
        ("coalesce(2,5)", ";"),
        ("coalesce(2.5)", ","),
        ("coalesce([M].[S].[a])", ","),
        ("abs()", ","),
        ("sqrt(4, 1)", ","),
        ("round([M].[S].[a])", ","),
        ("substring([M].[S].[a], 2)", ","),
        # DAX has no NULLIF function ("Failed to resolve name 'NULLIF'").
        ("nullif([M].[S].[a], 0)", ","),
        # Cognos ceiling and floor take one argument.
        ("ceiling([M].[S].[a], 2)", ","),
        ("floor([M].[S].[a], 2)", ","),
        ("ceiling()", ","),
    ],
)
def test_wrong_function_arity_is_not_confident(expression: str, separator: str) -> None:
    assert _translate(expression, separator=separator).confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        # Engine: CEILING(-2.5, 1) = -2, FLOOR(-2.5, 1) = -3, CEILING(BLANK(), 1) is BLANK().
        ("ceiling([M].[S].[a])", "CEILING(Q[a], 1)"),
        ("floor([M].[S].[a])", "FLOOR(Q[a], 1)"),
        ("ceiling(floor([M].[S].[a]) / 2)", "CEILING(FLOOR(Q[a], 1) / 2, 1)"),
        ("CEILING ([M].[S].[a])", "CEILING(Q[a], 1)"),
    ],
)
def test_one_argument_ceiling_and_floor_get_a_significance_of_one(
    expression: str, dax: str
) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


def test_one_argument_rounding_note_names_cognos_arity() -> None:
    result = _translate("ceiling()")
    assert result.note is not None and "ceiling: Cognos takes exactly one argument" in result.note


@pytest.mark.parametrize(
    "expression",
    [
        "[M].[S].[a] var [M].[S].[b]",
        "var [M].[S].[a] = 1 return [M].[S].[a]",
        # Valid DAX, but not Cognos: only the translator's own VAR blocks are accepted.
        "(var __c2d1 = 1 return __c2d1)",
        "[M].[S].[a] + __c2d1",
        # Valid DAX, but the generated VAR __c2d1 would capture the user's __c2d1.
        f"_add_months(_add_days({D}, [M].[S].[n]), __c2d1)",
    ],
)
def test_user_written_var_or_return_is_not_confident(expression: str) -> None:
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    "dax",
    [
        "Q[a] VAR Q[b]",
        "VAR Q[a] = 1 RETURN Q[a]",
        "(VAR x = 1 RETURN x)",
        "(VAR __c2d1 = 1 RETURN __c2d2)",
        "(VAR __c2d1 = __c2d1 RETURN 1)",
        "(VAR __c2d1 = 1)",
        "(VAR __c2d1 1 RETURN __c2d1)",
        "(VAR __c2d1 = 1 + RETURN __c2d1)",
        "(VAR __c2d1 = 1 RETURN __c2d1 VAR __c2d2 = 2)",
        "(VAR __c2d1 = 1 RETURN __c2d1) + __c2d1",
        "(1 VAR __c2d1 = 1 RETURN __c2d1)",
        "(+ VAR __c2d1 = 1 RETURN __c2d1)",
        "ABS(VAR __c2d1 = 1 RETURN __c2d1)",
        "(VAR __c2d1 = 1 RETURN 1) RETURN 1",
        "__c2d1",
        "(VAR TRUE = 1 RETURN 1)",
        "(VAR __c2d1 + 1 RETURN __c2d1)",
        "(VAR __c2d1 = 1 + VAR __c2d2 = 2 RETURN __c2d2)",
        "(1 RETURN 1)",
        "(VAR __c2d1 = 1 RETURN 1 RETURN 1)",
    ],
)
def test_misplaced_var_or_return_is_invalid_dax(dax: str) -> None:
    # The engine rejects each (syntax error or "Failed to resolve name '__c2d1'").
    assert _has_invalid_tokens(dax, "Q") is True


@pytest.mark.parametrize(
    "dax",
    [
        "(VAR __c2d1 = 1 VAR __c2d2 = __c2d1 + 1 RETURN __c2d2)",
        "((VAR __c2d1 = 1 RETURN __c2d1))",
        "(VAR __c2d1 = 1 RETURN (VAR __c2d2 = __c2d1 RETURN __c2d2))",
    ],
)
def test_generated_var_blocks_pass_leftover_check(dax: str) -> None:
    # Each evaluates in the engine.
    assert _has_invalid_tokens(dax, "Q") is False


@pytest.mark.parametrize("function", ["_add_days", "_add_months", "_add_years"])
@pytest.mark.parametrize(
    "offset",
    [
        "sqrt(2)",
        "3 ^ -1",
        "round([M].[S].[n], 1)",
        "_second([M].[S].[t])",
        "abs(1.5)",
        "coalesce([M].[S].[n], 1)",
    ],
)
def test_offset_that_may_be_fractional_is_not_confident(function: str, offset: str) -> None:
    # The engine evaluates EDATE(DATE(2024,1,31), SQRT(2)) as 2024-02-29: EDATE drops the fraction.
    result = _translate(f"{function}({D}, {offset})")
    assert result.confident is False
    assert "integer" in result.note


@pytest.mark.parametrize(
    "offset",
    [
        "_year([M].[S].[t])",
        "_month([M].[S].[t])",
        "_day([M].[S].[t])",
        "_hour([M].[S].[t])",
        "_minute([M].[S].[t])",
        "_day_of_week([M].[S].[t], 1)",
        "_day_of_year([M].[S].[t])",
        "_week_of_year([M].[S].[t])",
        "_days_to_end_of_month([M].[S].[t])",
        "extract(year, [M].[S].[t])",
        "extract(month, [M].[S].[t])",
        "extract(day, [M].[S].[t])",
        "char_length([M].[S].[s])",
        "abs([M].[S].[n])",
        "ceiling([M].[S].[a])",
        "floor([M].[S].[a])",
    ],
)
def test_integer_valued_function_offset_stays_confident(offset: str) -> None:
    assert _translate(f"_add_months({D}, {offset})").confident is True


@pytest.mark.parametrize(
    "expression",
    [
        "[M].[S].[a] + ''",
        f"_add_months({D}, 1 + 'x')",
        f"_add_days({D}, 1 - 'x')",
        "- 'x'",
        "('x') * 2",
        "'x' ^ 2",
        "[M].[S].[a] / ('')",
        "upper('x') + 1",
        "lower([M].[S].[a]) * 2",
        "trim([M].[S].[a]) - 1",
        "substring([M].[S].[a], 1, 2) / 2",
    ],
)
def test_text_literal_in_arithmetic_is_not_confident(expression: str) -> None:
    # The engine fails with "Cannot convert value 'x' of type Text to type Number".
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    "expression",
    [
        # A text branch of if/case/coalesce in arithmetic.
        "if ([M].[S].[a] > 0) then ('x') else ([M].[S].[b]) + 1",
        "if ([M].[S].[a] > 0) then ([M].[S].[b]) else ('x') + 1",
        "case [M].[S].[a] when 1 then 'x' else 'y' end * 2",
        "case [M].[S].[a] when 1 then 2 else 'y' end * 2",
        "case [M].[S].[a] when 1 then 2 when 2 then 'y' end * 2",
        "case when [M].[S].[a] > 0 then 'x' else [M].[S].[b] end - 1",
        "coalesce([M].[S].[a], 'x') + 1",
        "2 ^ coalesce([M].[S].[a], upper([M].[S].[b]))",
        "- if ([M].[S].[a] > 0) then ('x') else ('y')",
        # Text passed where a number is needed.
        "ceiling('x')",
        "floor(lower([M].[S].[a]))",
        "abs('x')",
        "sqrt('x')",
        "round('x', 0)",
        "round([M].[S].[a], 'x')",
        "abs(coalesce([M].[S].[a], 'x'))",
        "substring([M].[S].[a], 'x', 2)",
        # Text where DAX needs True/False.
        "not 'x'",
        "not ('x')",
        "not trim([M].[S].[a])",
        "[M].[S].[a] > 0 and 'x'",
        "'x' or [M].[S].[a] > 0",
        "if ('x') then (1) else (2)",
        # Text from a column is still text: "Cannot convert value '2024-01-05' ... to True/False".
        "if (upper([M].[S].[s])) then (1) else (2)",
        "upper([M].[S].[s]) and [M].[S].[a] > 0",
        # Arithmetic binds tighter than a comparison, so "y" is an operand of +.
        "'x' = 'y' + 1",
        "([M].[S].[a] & 'x') + 1",
    ],
)
def test_text_reaching_number_or_logic_is_not_confident(expression: str) -> None:
    # Engine: "Cannot convert value 'x' of type Text to type Number" (or "True/False").
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    "dax",
    [
        'AND(Q[a] > 0, "x")',
        'OR("x", Q[a] > 0)',
        "OR(UPPER(Q[s]), Q[a] > 0)",
        "IF(UPPER(Q[s]), 1, 2)",
        'MOD("x", 7)',
        '(VAR __c2d1 = "x" RETURN __c2d1 + 1)',
        "(VAR __c2d1 = 1 VAR __c2d2 = UPPER(Q[a]) RETURN __c2d1 * __c2d2)",
    ],
)
def test_text_in_logical_call_or_variable_arithmetic_is_invalid_dax(dax: str) -> None:
    assert _has_invalid_tokens(dax, "Q") is True


def test_number_variable_in_arithmetic_is_valid_dax() -> None:
    assert _has_invalid_tokens('(VAR __c2d1 = LEN("x") RETURN __c2d1 + 1)', "Q") is False


@pytest.mark.parametrize("tokens", [["(", "1"], ["1", "1"], ["ABS", "(", "1", "1"]])
def test_unreadable_tokens_count_as_text_operand(tokens: list[str]) -> None:
    # Unreachable after the operator-sequence check; kept so a parser gap stays not confident.
    assert _has_text_operand(tokens) is True


@pytest.mark.parametrize(
    "expression",
    [
        "upper([M].[S].[a])",
        "if ([M].[S].[a] > 0) then ('x') else ('y')",
        "coalesce([M].[S].[a], 'x')",
    ],
)
@pytest.mark.parametrize("aggregate", ["total", "average"])
def test_summed_or_averaged_text_is_not_confident(expression: str, aggregate: str) -> None:
    # Engine: "The function SUMX cannot work with values of type String" (AVERAGEX alike).
    result = _translate(expression, aggregate)
    assert result.dax.startswith(("SUMX(", "AVERAGEX("))
    assert result.confident is False


@pytest.mark.parametrize(
    ("expression", "aggregate", "dax"),
    [
        ("if ([M].[S].[a] > 0) then ('x') else ('y')", "none", 'IF(Q[a] > 0, "x", "y")'),
        ("upper([M].[S].[a]) = 'X'", "none", 'UPPER(Q[a]) = "X"'),
        ("coalesce([M].[S].[a], 'x')", "none", 'COALESCE(Q[a], "x")'),
        ("case [M].[S].[a] when 'x' then 1 else 2 end * 2", "none", 'SWITCH(Q[a], "x", 1, 2) * 2'),
        (
            "case [M].[S].[a] when 'y' then 2 when 'z' then 3 end + 1",
            "none",
            'SWITCH(Q[a], "y", 2, "z", 3) + 1',
        ),
        ("if ([M].[S].[a] = 'x') then (1) else (2) + 1", "none", 'IF(Q[a] = "x", 1, 2) + 1'),
        ("not ([M].[S].[a] = 'x')", "none", 'not (Q[a] = "x")'),
        # NOT binds looser than a comparison: NOT (UPPER(Q[a]) = "X").
        ("not upper([M].[S].[a]) = 'X'", "none", 'not UPPER(Q[a]) = "X"'),
        # & binds looser than +: "a" & (1 + 2) is "a3".
        ("'a' & 1 + 2", "none", '"a" & 1 + 2'),
        ("abs(char_length([M].[S].[a])) + 1", "none", "ABS(LEN(Q[a])) + 1"),
        ("substring(upper([M].[S].[a]), 1, 2)", "none", "MID(UPPER(Q[a]), 1, 2)"),
        ("upper([M].[S].[a])", "minimum", "MINX(Q, UPPER(Q[a]))"),
    ],
)
def test_text_valued_look_alikes_stay_confident(expression: str, aggregate: str, dax: str) -> None:
    # Each evaluates in the engine: text results that are compared, chosen or not used as numbers.
    result = _translate(expression, aggregate)
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        ("char_length('abc') + 1", 'LEN("abc") + 1'),
        ("[M].[S].[a] & 'x' & 'y'", 'Q[a] & "x" & "y"'),
        ("[M].[S].[a] = '' or [M].[S].[b] < 2", 'Q[a] = "" || Q[b] < 2'),
        ("[M].[S].[a] > 2", "Q[a] > 2"),
        ("[M].[S].[a] < 2", "Q[a] < 2"),
    ],
)
def test_text_and_comparison_operands_stay_confident(expression: str, dax: str) -> None:
    # Each evaluates in the engine (LEN("abc") + 1 = 4).
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize(
    "expression",
    [
        # DAX reads NOT (x) & "y" as NOT((x) & "y"): "Cannot convert value '1x' ... to True/False".
        "not (1) & 'x'",
        "not ([M].[S].[a]) & 'x'",
        # NOT binds tighter than &&, so NOT gets the text.
        "not ('x') and [M].[S].[a] > 0",
    ],
)
def test_not_before_parenthesis_applying_to_text_is_not_confident(expression: str) -> None:
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        # NOT applies to the comparison: NOT(("x") = "y") is TRUE in the engine.
        ("not ('x') = 'y'", 'not ("x") = "y"'),
        ("not (upper([M].[S].[a])) = 'X'", 'not (UPPER(Q[a])) = "X"'),
    ],
)
def test_not_before_parenthesized_text_compared_stays_confident(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize(
    "expression",
    [
        f"_year(if ({D} > 0) then ('x') else ('y'))",
        f"_month(case when {D} > 0 then 'x' else 'y' end)",
        "_day(case [M].[S].[a] when 1 then 'x' else 'y' end)",
        "_hour(coalesce('x', 'y'))",
        "_minute(coalesce('x', 'y'))",
        "_week_of_year(lower('x') & 'y')",
        f"_day_of_week(if ({D} > 0) then ('x') else ('y'), 1)",
        # The argument is bound to a VAR used twice.
        f"_day_of_year(if ({D} > 0) then ('x') else ('y'))",
        f"_days_to_end_of_month(if ({D} > 0) then ('x') else ('y'))",
        # BLANK() fits any type, so the missing else keeps the result literal text.
        f"_year(case when {D} > 0 then 'x' end)",
        # A substring of literal text is literal text, whatever its positions ('a' for a = 1).
        "_hour(substring('abc', 1, [M].[S].[a]))",
        # A conditional of number literals is a number literal: "Cannot convert value '1' ...".
        "_year(coalesce(1, 2) & '')",
        "_year(if ([M].[S].[a] > 0) then (1) else (2) & '')",
    ],
)
def test_literal_text_computed_into_date_argument_is_not_confident(expression: str) -> None:
    # Engine: "Cannot convert value 'x' of type Text to type Date" (as for _year('x')).
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    "dax",
    [
        'EDATE(IF(Q[a] > 0, "x", "y"), 1)',
        'EOMONTH(COALESCE("x", "y"), 0)',
        'SECOND(UPPER("x"))',
        'DATEDIFF(IF(Q[a] > 0, "x", "y"), Q[d], DAY)',
        'DATEDIFF(Q[d], COALESCE("x", "y"), DAY)',
        'DATE(1, IF(Q[a] > 0, "x", "y"), 1)',
    ],
)
def test_literal_text_in_date_argument_is_invalid_dax(dax: str) -> None:
    # _second, _days_between and _make_timestamp are never confident (precision notes), and
    # EDATE/EOMONTH values are also used in arithmetic; so these are checked on their own.
    assert _has_invalid_tokens(dax, "Q") is True


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        # Text from a column may hold a date: YEAR(UPPER(Q[s])) evaluates in the engine.
        ("_year(upper([M].[S].[a]))", "YEAR(UPPER(Q[a]))"),
        ("_hour(substring([M].[S].[s], 1, 10))", "HOUR(MID(Q[s], 1, 10))"),
        ("_month(upper([M].[S].[s]) & '')", 'MONTH((UPPER(Q[s]) & ""))'),
        # Documented limit: "x" & Q[a] is never a date, but & with a column is not literal text,
        # as text built from a column may be one; the engine rejects this one.
        ("_year('x' & [M].[S].[a])", 'YEAR(("x" & Q[a]))'),
        (
            f"_month(if ({D} > 0) then ({D}) else ([M].[S].[e]))",
            "MONTH(IF(Q[d] > 0, Q[d], Q[e]))",
        ),
        # Documented limit: the column branch decides the type, which is not known here.
        (f"_year(if ({D} > 0) then ('x') else ({D}))", 'YEAR(IF(Q[d] > 0, "x", Q[d]))'),
    ],
)
def test_column_derived_date_argument_stays_confident(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize(
    "expression",
    [
        "'x' = 1",
        "1 < 'x'",
        "'x' <> -1",
        "'x' = 1 + 1",
        "upper('x') >= 1",
        "case [M].[S].[a] when 1 then 'a' when 'b' then 'c' end",
        "case 'x' when 1 then 2 else 3 end",
    ],
)
def test_literal_text_compared_with_number_literal_is_not_confident(expression: str) -> None:
    # Engine: "DAX comparison operations do not support comparing values of type Text with
    # values of type Integer".
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        ("'1' = '1'", '"1" = "1"'),
        ("case [M].[S].[a] when 1 then 'a' when 2 then 'b' end", 'SWITCH(Q[a], 1, "a", 2, "b")'),
        ("case [M].[S].[a] when 1 then 2 else 3 end", "SWITCH(Q[a], 1, 2, 3)"),
        # The trailing else is a result, not a compared value.
        ("case upper([M].[S].[s]) when 'X' then 1 else 2 end", 'SWITCH(UPPER(Q[s]), "X", 1, 2)'),
    ],
)
def test_same_type_literal_comparison_stays_confident(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize(
    "expression",
    [
        # Text from a column or a text call against a number.
        "upper([M].[S].[s]) = 1",
        "substring([M].[S].[s], 1, 2) = 1",
        "[M].[S].[s] & 'x' = 1",
        "case upper([M].[S].[s]) when 1 then 2 else 3 end",
        "case char_length([M].[S].[s]) when 'x' then 1 else 2 end",
        # Text against a number from a call, a conditional or arithmetic.
        "'x' = abs(1)",
        "'x' = round(1.4, 0)",
        "'x' = coalesce(1, 2)",
        "'x' = if ([M].[S].[a] > 0) then (1) else (2)",
        "'x' = [M].[S].[a] + 1",
        "'x' = -[M].[S].[a]",
        # BLANK() of a missing else does not hide the text type.
        "case when [M].[S].[a] > 0 then 'x' end = 1",
        # Integer or date against text.
        f"_year({D}) = '2024'",
        f"_day_of_year({D}) = 'x'",
        f"_add_days({D}, 1) = 'x'",
        # True/False against text or a number.
        "('x' = 'y') = 'z'",
        "([M].[S].[a] > 0) = 1",
        "true = 'x'",
        "false = 'x'",
        "(case when [M].[S].[a] > 0 then true else false end) = 'x'",
        "(case when [M].[S].[a] > 0 then [M].[S].[a] > 0 else [M].[S].[b] > 0 end) = 'x'",
        # Branches that are all text, not only literal text, make text.
        "(case when [M].[S].[a] > 0 then upper([M].[S].[s]) else 'x' end) = 1",
    ],
)
def test_comparison_of_different_known_types_is_not_confident(expression: str) -> None:
    # Engine: "DAX comparison operations do not support comparing values of type Text with
    # values of type Integer" (Number, Date, True/False alike; SWITCH likewise).
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        # A column may hold text, and a variant may be compared with anything.
        ("'x' = [M].[S].[s]", '"x" = Q[s]'),
        (
            "case when [M].[S].[a] > 0 then 1 else 'x' end = 1",
            'IF(Q[a] > 0, 1, "x") = 1',
        ),
        # A date and a number compare.
        (f"_year({D}) = 2024", "YEAR(Q[d]) = 2024"),
        (f"_add_days({D}, 1) > {D}", "IF(ISBLANK(Q[d]), BLANK(), Q[d] + 1) > Q[d]"),
        ("([M].[S].[a] > 0) = ([M].[S].[b] > 0)", "(Q[a] > 0) = (Q[b] > 0)"),
        ("substring([M].[S].[s], 1, 2) = '20'", 'MID(Q[s], 1, 2) = "20"'),
        ("true = ([M].[S].[a] > 0)", "true = (Q[a] > 0)"),
        (
            "(case when [M].[S].[a] > 0 then upper([M].[S].[s]) else lower([M].[S].[s]) end) = 'x'",
            '(IF(Q[a] > 0, UPPER(Q[s]), LOWER(Q[s]))) = "x"',
        ),
        # DAX keeps +Q[s] as text, so a unary plus on a column is not a number.
        ("+[M].[S].[s] = 'x'", '+Q[s] = "x"'),
        # Text next to a column may be a variant: (IF(Q[a] > 0, "x", Q[b])) = 1 evaluates.
        (
            "(if ([M].[S].[a] > 0) then ('x') else ([M].[S].[b])) = 1",
            '(IF(Q[a] > 0, "x", Q[b])) = 1',
        ),
    ],
)
def test_comparison_of_same_or_unknown_types_stays_confident(expression: str, dax: str) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


@pytest.mark.parametrize(
    "expression",
    [
        "if ([M].[S].[a] > 0) then ('x') else (1)",
        "case [M].[S].[a] when 1 then 2 else 'x' end",
        "coalesce('x', 1)",
        "if ([M].[S].[a] > 0) then ([M].[S].[a] > 0) else (1)",
        f"if ([M].[S].[a] > 0) then (_add_days({D}, 1)) else ('x')",
        # A variant branch makes the whole result a variant.
        "case when [M].[S].[a] > 0 then (case when [M].[S].[b] > 0 then 'x' else 1 end)"
        " else 'y' end",
        "case when [M].[S].[a] > 0 then true else 1 end",
    ],
)
def test_variant_calculated_column_is_not_confident(expression: str) -> None:
    # Engine: "Expressions that yield variant data-type cannot be used to define calculated
    # columns". Under ``none`` the translation becomes a calculated column.
    assert _translate(expression).confident is False


@pytest.mark.parametrize(
    ("expression", "dax"),
    [
        # A column branch may have the type of the other branch.
        ("if ([M].[S].[a] > 0) then ('x') else ([M].[S].[s])", 'IF(Q[a] > 0, "x", Q[s])'),
        ("case [M].[S].[a] when 1 then 'a' else [M].[S].[s] end", 'SWITCH(Q[a], 1, "a", Q[s])'),
        ("case when [M].[S].[a] > 0 then 'x' end", 'IF(Q[a] > 0, "x", BLANK())'),
        ("case when [M].[S].[a] > 0 then true else false end", "IF(Q[a] > 0, true, false)"),
        # A date and a number make a date column; a variant inside LEN is not the column value.
        (f"if ([M].[S].[a] > 0) then ({D}) else (1)", "IF(Q[a] > 0, Q[d], 1)"),
        ("char_length(if ([M].[S].[a] > 0) then ('x') else (1))", 'LEN(IF(Q[a] > 0, "x", 1))'),
    ],
)
def test_calculated_column_of_one_or_unknown_type_stays_confident(
    expression: str, dax: str
) -> None:
    result = _translate(expression)
    assert result.dax == dax
    assert result.confident is True


def test_variant_measure_stays_confident() -> None:
    # A measure may yield a variant: the engine evaluates IF(1 = 1, "x", 1) as "x".
    expression = "if (1 = 1) then ('x') else (1)"
    assert _translate(expression, "none").confident is False
    result = _translate(expression, "total")
    assert result.dax == 'IF(1 = 1, "x", 1)'
    assert result.confident is True


@pytest.mark.parametrize(
    "dax", ["Q[a] ! 2", "Q[a] * } 2", "} 1", "Q[a] + ) 1", "Q[a] * }", "Q[a] + !"]
)
def test_bad_operator_or_operand_token_is_invalid_dax(dax: str) -> None:
    assert _has_invalid_tokens(dax, "Q") is True


def test_exponent_notation_stays_not_confident() -> None:
    # DAX evaluates 1e3 as 1000, but Cognos number syntax is not verified: keep it a hint.
    assert _translate("[M].[S].[a] + 1e3").confident is False
