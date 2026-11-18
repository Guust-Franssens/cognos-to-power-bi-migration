"""Tests for the deterministic Cognos-to-DAX expression translator."""

from __future__ import annotations

from cognos2powerbi.core.translate import translate_measure_expression


def test_simple_reference_with_aggregate() -> None:
    result = translate_measure_expression("[Sales].[Measures].[Revenue]", "Sales", "total")
    assert result.dax == "SUM(Sales[Revenue])"
    assert result.confident is True


def test_simple_reference_without_aggregate() -> None:
    result = translate_measure_expression("[Sales].[Products].[Line]", "Sales", "none")
    assert result.dax == "Sales[Line]"
    assert result.confident is True


def test_arithmetic_of_references_is_aggregated() -> None:
    result = translate_measure_expression(
        "[Sales].[Measures].[Revenue] - [Sales].[Measures].[Cost]", "Sales", "total"
    )
    assert result.dax == "SUM(Sales[Revenue]) - SUM(Sales[Cost])"
    assert result.confident is True


def test_if_then_else_becomes_dax_if() -> None:
    result = translate_measure_expression(
        "if ([Sales].[Measures].[Revenue] > 0) then (1) else (0)", "Sales", "none"
    )
    assert result.dax == "IF(Sales[Revenue] > 0, 1, 0)"
    assert result.confident is True


def test_substring_function_maps_to_mid() -> None:
    result = translate_measure_expression(
        "substring([Sales].[Products].[Line], 1, 3)", "Sales", "none"
    )
    assert result.dax == "MID(Sales[Line], 1, 3)"
    assert result.confident is True


def test_extract_year_maps_to_year() -> None:
    result = translate_measure_expression(
        "extract(year, [Sales].[Time].[Order date])", "Sales", "none"
    )
    assert result.dax == "YEAR(Sales[Order date])"
    assert result.confident is True


def test_unknown_function_is_not_confident() -> None:
    result = translate_measure_expression(
        "rank([Sales].[Measures].[Revenue])", "Sales", "calculated"
    )
    assert result.confident is False
    assert result.dax is not None


def test_empty_expression_returns_none() -> None:
    result = translate_measure_expression("", "Sales", "total")
    assert result.dax is None
    assert result.confident is False


def test_row_level_calculation_under_aggregate_uses_iterator() -> None:
    result = translate_measure_expression(
        "case when [M].[S].[Work] > 900 then 900 else [M].[S].[Work] end", "Q", "total"
    )
    assert result.dax == "SUMX(Q, IF(Q[Work] > 900, 900, Q[Work]))"
    assert result.confident is True


def test_row_level_distinct_count_is_not_confident() -> None:
    result = translate_measure_expression("upper([M].[S].[X])", "Q", "countdistinct")
    assert result.confident is False


def test_simple_case_keeps_subject_in_switch() -> None:
    result = translate_measure_expression(
        "case [M].[S].[Code] when 1 then 'A' when 2 then 'B' else 'C' end", "Q", "none"
    )
    assert result.dax == 'SWITCH(Q[Code], 1, "A", 2, "B", "C")'
    assert result.confident is True


def test_simple_case_with_unknown_subject_function_is_not_confident() -> None:
    result = translate_measure_expression(
        "CASE _day_of_week([M].[S].[Datum]; 1) WHEN 1 THEN 'Maandag' END", "Q", "none"
    )
    assert result.dax is not None and result.dax.startswith("SWITCH(_day_of_week(")
    assert result.confident is False


def test_nested_case_translates_innermost_first() -> None:
    result = translate_measure_expression(
        "case when [M].[S].[A] > 1 then case when [M].[S].[B] > 2 then 'x' else 'y' end "
        "else 'z' end",
        "Q",
        "none",
    )
    assert result.dax == 'IF(Q[A] > 1, IF(Q[B] > 2, "x", "y"), "z")'
    assert result.confident is True


def test_searched_case_without_else_returns_blank() -> None:
    result = translate_measure_expression("case when [M].[S].[A] > 1 then 1 end", "Q", "none")
    assert result.dax == "IF(Q[A] > 1, 1, BLANK())"


def test_string_literals_become_dax_strings() -> None:
    result = translate_measure_expression(
        "case when [M].[S].[O'Brien] = 'O''Brien' then 'say \"hi\"' else '' end", "Q", "none"
    )
    assert result.dax == 'IF(Q[O\'Brien] = "O\'Brien", "say ""hi""", "")'
    assert result.confident is True


def test_macros_and_prompt_parameters_are_not_confident() -> None:
    assert (
        translate_measure_expression("#prompt('p')# + [M].[S].[X]", "Q", "none").confident is False
    )
    assert translate_measure_expression("?p? + [M].[S].[X]", "Q", "none").confident is False
    # A literal that merely contains '#' or '?' is not a macro.
    literal = translate_measure_expression("upper(' #1 ?a?')", "Q", "none")
    assert literal.confident is True


def test_parentheses_inside_column_names_are_not_calls() -> None:
    result = translate_measure_expression("upper([M].[S].[Duur (sec)])", "Q", "none")
    assert result.dax == "UPPER(Q[Duur (sec)])"
    assert result.confident is True
