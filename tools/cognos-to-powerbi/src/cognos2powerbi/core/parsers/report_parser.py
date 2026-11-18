"""Parser for Cognos report specification XML.

Cognos report specifications are namespaced XML produced by Report Studio / Cognos Analytics.
This parser is intentionally namespace-agnostic: it strips namespaces before traversal so a
single implementation handles the range of Cognos schema versions seen in the field.

The parser extracts:

- Queries and their data items -> semantic-model tables, columns, and measures.
- Layout pages and layout objects (list, crosstab, charts) -> report pages and visuals. Prompt
  pages are not report pages: their prompts are extracted as prompt parameters instead.

Anything that cannot be mapped deterministically is recorded as a review flag on the project
so it can be addressed manually or by the AI refinement stage. That includes layout constructs
the converter drops (prompt-page content, conditional rendering and styles, report variables,
HTML items, custom controls, repeaters, references to other reports' layout components).
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from lxml import etree

from cognos2powerbi.core.ir.models import (
    Cardinality,
    Column,
    DataType,
    FilterClause,
    FilterTarget,
    FilterUse,
    Measure,
    MigrationProject,
    Prompt,
    PromptControlType,
    QueryEdge,
    QueryEdgeKind,
    QueryFilter,
    QueryGraph,
    QueryNode,
    QueryRole,
    Relationship,
    ReportPage,
    Severity,
    Style,
    Table,
    TextBlock,
    Visual,
    VisualField,
    VisualType,
)
from cognos2powerbi.core.translate import translate_measure_expression
from cognos2powerbi.core.translate.filters import (
    UntranslatableFilter,
    item_key,
    literal_values,
    not_applied_message,
    parse_filter,
    ref_segments,
)

_LAYOUT_TO_VISUAL = {
    "list": VisualType.TABLE,
    "crosstab": VisualType.MATRIX,
    "vizColumn": VisualType.COLUMN_CHART,
    "vizBar": VisualType.BAR_CHART,
    "vizLine": VisualType.LINE_CHART,
    "vizPie": VisualType.PIE_CHART,
    "barChart": VisualType.BAR_CHART,
    "columnChart": VisualType.COLUMN_CHART,
    "lineChart": VisualType.LINE_CHART,
    "pieChart": VisualType.PIE_CHART,
    # Only a fallback: _combination_chart_type picks the type from the chart's series groups.
    "v2_combinationChart": VisualType.LINE_CHART,
}

# A Cognos combination chart overlays series groups of these types ("area, bar, and line").
_COMBINATION_CHART_TAGS = {"v2_area": "area", "v2_bar": "bar", "v2_line": "line"}
_COMBINATION_PRIMARY_AXIS = "v2_topLeftAxis"
# Cognos seriesType -> configuration; an absent seriesType is the standard (clustered) one.
_COMBINATION_SERIES_TYPES = {
    None: "standard",
    "absolute": "standard",
    "stacked": "stacked",
    "stacked100Percent": "100% stacked",
}
# (chart type, horizontal, configuration) -> the Power BI visual drawing exactly that shape.
# Bars are vertical (columns) unless the chart's orientation is horizontal.
_COMBINATION_VISUALS: dict[tuple[str, bool, str], VisualType] = {
    ("v2_bar", False, "standard"): VisualType.CLUSTERED_COLUMN_CHART,
    ("v2_bar", False, "stacked"): VisualType.COLUMN_CHART,
    ("v2_bar", False, "100% stacked"): VisualType.HUNDRED_PERCENT_STACKED_COLUMN_CHART,
    ("v2_bar", True, "standard"): VisualType.CLUSTERED_BAR_CHART,
    ("v2_bar", True, "stacked"): VisualType.BAR_CHART,
    ("v2_bar", True, "100% stacked"): VisualType.HUNDRED_PERCENT_STACKED_BAR_CHART,
    ("v2_line", False, "standard"): VisualType.LINE_CHART,
    ("v2_area", False, "standard"): VisualType.AREA_CHART,
    ("v2_area", False, "stacked"): VisualType.STACKED_AREA_CHART,
    ("v2_area", False, "100% stacked"): VisualType.HUNDRED_PERCENT_STACKED_AREA_CHART,
}
# Vertical shapes with no exact Power BI visual: the nearest visual and what it changes.
_COMBINATION_APPROXIMATIONS: dict[tuple[str, str], tuple[VisualType, str]] = {
    ("v2_line", "stacked"): (
        VisualType.LINE_CHART,
        "Power BI has no stacked line chart, so its stacked lines are drawn unstacked",
    ),
    ("v2_line", "100% stacked"): (
        VisualType.LINE_CHART,
        "Power BI has no 100% stacked line chart, so its lines are drawn unstacked with "
        "absolute values",
    ),
}
# Placeholder for "the chart's default measure" while pairing series with the measure they plot.
_COMBINATION_DEFAULT = "\0default"

# Cognos 11+ visualizations (vizControl) name their type ``com.ibm.vis.<type>``. Each type that
# Power BI draws maps to (visual, slot family, what the drawing changes or None when exact).
# Cognos area charts stack their series, so they map to the stacked area chart.
_VIZ_CONTROL_PREFIX = "com.ibm.vis."
_VIZ_CONTROL_VISUALS: dict[str, tuple[VisualType, str, str | None]] = {
    "clusteredColumn": (VisualType.CLUSTERED_COLUMN_CHART, "axis", None),
    "clusteredBar": (VisualType.CLUSTERED_BAR_CHART, "axis", None),
    "stackedColumn": (VisualType.COLUMN_CHART, "axis", None),
    "stackedBar": (VisualType.BAR_CHART, "axis", None),
    "line": (VisualType.LINE_CHART, "axis", None),
    "smoothLine": (
        VisualType.LINE_CHART,
        "axis",
        "Power BI draws straight line segments, not the smoothed curve",
    ),
    "stepLine": (
        VisualType.LINE_CHART,
        "axis",
        "Power BI draws straight line segments, not steps (set the line to stepped in Power BI)",
    ),
    "area": (VisualType.STACKED_AREA_CHART, "axis", None),
    "smoothArea": (
        VisualType.STACKED_AREA_CHART,
        "axis",
        "Power BI draws straight area edges, not the smoothed curve",
    ),
    "stepArea": (
        VisualType.STACKED_AREA_CHART,
        "axis",
        "Power BI draws straight area edges, not steps",
    ),
    "pie": (VisualType.PIE_CHART, "pie", None),
    "donut": (VisualType.DONUT_CHART, "pie", None),
    "treemap": (VisualType.TREEMAP, "treemap", None),
    "scatter": (VisualType.SCATTER_CHART, "scatter", None),
    "bubble": (VisualType.SCATTER_CHART, "scatter", None),
    "clusteredCombination": (VisualType.LINE_CLUSTERED_COLUMN_COMBO_CHART, "combination", None),
    "stackedCombination": (VisualType.LINE_STACKED_COLUMN_COMBO_CHART, "combination", None),
    "waterfall": (VisualType.WATERFALL_CHART, "waterfall", None),
}
# Slot id -> generic field role, per slot family. Roles ending in "series" or naming a
# grouping (category, details, breakdown) take columns; the others plot values.
_VIZ_SLOT_ROLES: dict[str, dict[str, str]] = {
    "axis": {"categories": "category", "values": "values", "color": "series", "series": "series"},
    "pie": {"categories": "category", "values": "values"},
    "treemap": {"categories": "category", "size": "values", "color": "color"},
    "scatter": {
        "categories": "category",
        "x": "x",
        "y": "y",
        "size": "size",
        "color": "series",
        "series": "series",
    },
    "combination": {
        "categories": "category",
        "columnValue": "values",
        "lineValue": "line_values",
        "columnColor": "series",
        "lineColor": "line_series",
    },
    "waterfall": {"categories": "category", "values": "values", "subcategories": "breakdown"},
}
_VIZ_VALUE_ROLES = {"values", "line_values", "x", "y", "size"}
# Cognos rollup and aggregate spellings -> one name, to compare a slot's rollup with its item.
_ROLLUP_NAMES = {
    "sum": "total",
    "avg": "average",
    "min": "minimum",
    "max": "maximum",
    "countdistinct": "countdistinct",
}
# Containers that repeat their content per row or cell, so a nested object depends on them.
_REPEATING_CONTAINERS = ("list", "crosstab", "repeater", "repeaterTable", "singleton")
# Card tiles for migrated singletons: at most this many per row, at most this tall, and never
# shorter than the minimum; cards that do not fit at the minimum height are not migrated.
_CARDS_PER_ROW = 6
_CARD_HEIGHT = 96.0
_MIN_CARD_HEIGHT = 48.0
# What a singleton may hold besides its one value and still become a card: layout wrappers, and
# formatting subtrees. Conditional styles are covered by the report-wide conditional-style flag.
# Static styles are not carried over, as for lists and crosstabs. The singleton's data format is
# carried to the model column or measure it shows (see ``_apply_data_formats``), which flags any
# format, or part of one, that Power BI cannot express. Card visuals may still add display units.
_SINGLETON_FORMATTING = ("style", "dataFormat", "conditionalStyleRefs", "conditionalStyles")
# Containers of conditional formats; the conditional-style flag reports them, not the base format.
_CONDITIONAL_FORMAT_PARENTS = {
    "styleCase",
    "styleDefault",
    "conditionalStyleCases",
    "conditionalStyle",
    "conditionalStyles",
}
_SINGLETON_CARD_PARTS = {
    "contents",
    "textItem",
    "dataSource",
    "dataItemValue",
    "staticValue",
    "table",
    "tableRows",
    "tableRow",
    "tableCells",
    "tableCell",
}

# Cognos Report Studio RS_dataType numeric codes -> TMDL data type. Codes seen in the field:
# 3 = character/string, 4 = dateTime, 5 = time, 7 = date, 8 = interval/timestamp, others numeric.
_RS_DATATYPE_TO_TMDL = {
    "1": DataType.INT64,
    "2": DataType.INT64,
    "3": DataType.STRING,
    "4": DataType.DATE_TIME,
    "5": DataType.DATE_TIME,
    "7": DataType.DATE_TIME,
    "8": DataType.DATE_TIME,
    "9": DataType.DECIMAL,
    "10": DataType.DOUBLE,
}

# A plain qualified Cognos reference such as [Namespace].[Query Subject].[Item].
_SIMPLE_REF_RE = re.compile(r"^\[[^\[\]]+\](?:\.\[[^\[\]]+\])*$")
# cast([reference]; targetType) - a type coercion of a single reference.
_CAST_FULL_RE = re.compile(
    r"^cast\s*\(\s*(?P<inner>.+?)\s*;\s*(?P<type>[A-Za-z0-9_]+)\s*\)$",
    re.IGNORECASE | re.DOTALL,
)
# Cognos functions that imply an integer or floating result when no type hint is present.
_COUNT_FUNCS_RE = re.compile(
    r"\b(running[-_]count|running[-_]total|count|_?rowcount)\s*\(", re.IGNORECASE
)
_FLOAT_FUNCS_RE = re.compile(
    r"\b(average|avg|median|stddev|std[-_]?dev|variance|percentile|ratio)\s*\(", re.IGNORECASE
)
_SUMMARY_FUNCTIONS = {
    "count",
    "countdistinct",
    "total",
    "sum",
    "average",
    "avg",
    "minimum",
    "min",
    "maximum",
    "max",
}
# Match whole Cognos function names, including hyphens (e.g. running-count).
_EXPRESSION_FUNCTION_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_\-]*)\s*\(")
_EXPRESSION_LITERALS_AND_COMMENTS = re.compile(
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|\[(?:[^\]]|\]\])*\]"""
    r"|/\*[\s\S]*?\*/|--[^\r\n]*|//[^\r\n]*"
)
# A Cognos macro block; macros are delimited by pairs of '#'.
_MACRO_BLOCK_RE = re.compile(r"#([^#]*)#")
_COMMENTS_ONLY_RE = re.compile(r"(?:\s|/\*(?:[^*]|\*(?!/))*\*/)*")
# Literals and comments are skipped; ``ref`` captures a (possibly qualified) reference chain.
_LITERAL_COMMENT_OR_REFERENCE_RE = re.compile(
    r"'(?:[^']|'')*'|/\*[\s\S]*?\*/|(?P<ref>\[[^\[\]]+\](?:\.\[[^\[\]]+\])*)"
)
_NUMERIC_TYPES = {DataType.INT64, DataType.DOUBLE, DataType.DECIMAL}
_QUALIFIED_REF_RE = re.compile(r"\[[^\[\]]+\](?:\s*\.\s*\[[^\[\]]+\])*")
# A Cognos date function call. Its first argument (both for _days_between) is a date, and the
# second argument of an _add_* function is a number.
_DATE_FUNCTION_CALL_RE = re.compile(
    r"(?<![\w$.])(?P<name>_add_(?:days|hours|minutes|seconds|months|years)|_first_of_month"
    r"|_last_of_month|_days_to_end_of_month|_day_of_week|_day_of_year|_week_of_year"
    r"|_days_between|_year|_month|_day|_hour|_minute|_second)\s*\(",
    re.IGNORECASE,
)
_ARITHMETIC_RE = re.compile(r"[\d\s.+\-*/()]*")
# Result type of the DAX that the translator emits for a whole Cognos date call: the _add_* and
# month-boundary functions return a datetime, the date-part functions an integer (YEAR, WEEKDAY,
# WEEKNUM, DATEDIFF, MOD and DAY arithmetic all return Int64).
_DATE_RESULT_TYPES: dict[str, DataType] = {
    **dict.fromkeys(
        ("_add_days", "_add_hours", "_add_minutes", "_add_seconds", "_add_months", "_add_years")
        + ("_first_of_month", "_last_of_month"),
        DataType.DATE_TIME,
    ),
    **dict.fromkeys(
        ("_year", "_month", "_day", "_hour", "_minute", "_day_of_week", "_day_of_year")
        + ("_week_of_year", "_days_between", "_days_to_end_of_month"),
        DataType.INT64,
    ),
}
_DATE_CONSTANT_RE = re.compile(r"current_date|localtimestamp", re.IGNORECASE)
_LEADING_CALL_RE = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IF_OPEN_RE = re.compile(r"if\s*\(", re.IGNORECASE)
_THEN_OPEN_RE = re.compile(r"\s*then\s*\(", re.IGNORECASE)
_ELSE_OPEN_RE = re.compile(r"\s*else\s*\(", re.IGNORECASE)
_CASE_TOKEN_RE = re.compile(r"[()]|\b(?:case|when|then|else|end)\b", re.IGNORECASE)
# Cognos separates function arguments with the expression locale's list separator: ";" where
# the decimal separator is a comma, "," otherwise (IBM, "Locale-sensitive expressions"). The
# tables below are the locales whose CLDR decimal symbol (Latin digits, default script) is a
# comma. Generated with babel 2.18.0 (CLDR 47), a dev-time tool only: for every
# ``babel.localedata.locale_identifiers()`` entry, key ``language[-region]`` and test
# ``babel.numbers.get_decimal_symbol(Locale.parse(key), numbering_system="latn") == ","``;
# a language is listed by its own default, and regions that differ from it are exceptions.
# fmt: off
_SEMICOLON_LANGUAGES = frozenset({
    "af", "agq", "ast", "az", "bas", "be", "bg", "blo", "br", "bs", "ca", "cs", "cv", "da",
    "de", "dsb", "dua", "dyo", "el", "eo", "es", "et", "eu", "ewo", "ff", "fi", "fo", "fr",
    "fy", "gl", "hr", "hsb", "ht", "hu", "hy", "ia", "id", "ie", "is", "it", "jgo", "jv", "ka",
    "kab", "kea", "kgp", "kk", "kkj", "kl", "ksf", "ku", "ky", "lb", "lij", "lld", "ln", "lo",
    "lt", "lu", "lv", "mgh", "mk", "mua", "nb", "nl", "nmg", "nn", "nnh", "no", "nr", "pl",
    "ps", "pt", "rn", "ro", "ru", "rw", "sah", "sc", "se", "seh", "sg", "shi", "sk", "sl",
    "smn", "sq", "sr", "ss", "su", "sv", "tg", "tk", "tok", "tr", "ts", "tt", "tzm", "uk", "uz",
    "ve", "vec", "vi", "wo", "yav", "yrl", "zgh",
})
# Regions of those languages that write a decimal point.
_COMMA_REGIONS = frozenset({
    "az-iq", "az-ir", "az-tr", "de-ch", "de-li", "es-419", "es-br", "es-bz", "es-cu", "es-do",
    "es-gt", "es-hn", "es-mx", "es-ni", "es-pa", "es-pe", "es-pr", "es-sv", "es-us", "it-ch",
    "kk-cn",
})
# Regions of other languages that write a decimal comma (IBM names English (South Africa)).
_SEMICOLON_REGIONS = frozenset({
    "ar-dz", "ar-lb", "ar-ly", "ar-ma", "ar-mr", "ar-tn", "en-at", "en-be", "en-cz", "en-de",
    "en-dk", "en-es", "en-fi", "en-fr", "en-hu", "en-id", "en-it", "en-nl", "en-no", "en-pl",
    "en-pt", "en-ro", "en-se", "en-si", "en-sk", "en-za", "ms-bn", "ms-id", "qu-bo", "sw-cd",
})
# Languages whose default writes a decimal point. Any other language (and "bgn", whose CLDR
# decimal symbol is U+066B) is unrecognized: its separator is assumed, never trusted.
_POINT_LANGUAGES = frozenset({
    "aa", "ab", "ak", "am", "an", "ann", "apc", "ar", "arn", "as", "asa", "ba", "bal", "bem",
    "bew", "bez", "bgc", "bho", "blt", "bm", "bn", "bo", "brx", "bss", "byn", "cad", "cch", "ccp",
    "ce", "ceb", "cgg", "cho", "chr", "cic", "ckb", "co", "cop", "csw", "cu", "cy", "dav", "dje",
    "doi", "dv", "dz", "ebu", "ee", "en", "fa", "fil", "frr", "fur", "ga", "gaa", "gd", "gez",
    "gn", "gsw", "gu", "guz", "gv", "ha", "haw", "he", "hi", "hnj", "ig", "ii", "io", "iu", "ja",
    "jbo", "jmc", "kaa", "kaj", "kam", "kcg", "kde", "ken", "khq", "ki", "kln", "km", "kn", "ko",
    "kok", "kpe", "ks", "ksb", "ksh", "kw", "kxv", "la", "lag", "lg", "lkt", "lmo", "lrc", "ltg",
    "luo", "luy", "mai", "mas", "mdf", "mer", "mfe", "mg", "mgo", "mhn", "mi", "mic", "ml", "mn",
    "mni", "moh", "mr", "ms", "mt", "mus", "my", "myv", "mzn", "naq", "nd", "nds", "ne", "nqo",
    "nso", "nus", "nv", "ny", "nyn", "oc", "om", "or", "os", "osa", "pa", "pap", "pcm", "pis",
    "prg", "qu", "quc", "raj", "rhg", "rif", "rm", "rof", "rwk", "sa", "saq", "sat", "sbp", "scn",
    "sd", "sdh", "ses", "shn", "si", "sid", "skr", "sma", "smj", "sms", "sn", "so", "ssy", "st",
    "sw", "syr", "szl", "ta", "te", "teo", "th", "ti", "tig", "tn", "to", "tpi", "trv", "trw",
    "twq", "tyv", "ug", "ur", "vai", "vmw", "vo", "vun", "wa", "wae", "wal", "wbp", "xh", "xnr",
    "xog", "yi", "yo", "yue", "za", "zh", "zu",
})
# fmt: on
# Legacy codes that Java (and so Cognos) may still write, mapped to their CLDR language.
_LANGUAGE_ALIASES = {"in": "id", "iw": "he", "ji": "yi", "mo": "ro", "tl": "fil"}
# A comma between two digits: a list separator under a decimal-point locale, a decimal comma
# under a decimal-comma one. Only meaningful on an expression with opaque parts masked.
_POSSIBLE_DECIMAL_COMMA_RE = re.compile(r"\d,\d")
# One unscoped summary over one reference, e.g. ``count(distinct [Q].[Id])`` or ``total([Amount])``.
_SIMPLE_SUMMARY_RE = re.compile(
    r"^\s*(?P<func>[A-Za-z]+)\s*\(\s*(?P<distinct>distinct\s+)?"
    r"(?P<ref>\[[^\[\]]+\](?:\s*\.\s*\[[^\[\]]+\])*)\s*\)\s*$",
    re.IGNORECASE,
)

# Content area of the 1280x720 report page, and the gap between stacked visuals.
_CONTENT_X, _CONTENT_Y, _CONTENT_WIDTH, _CONTENT_HEIGHT = 24.0, 24.0, 1232.0, 672.0
_VISUAL_GAP = 16.0
# A join filter of the form [A].[col] = [B].[col].
_JOIN_EQUALITY_RE = re.compile(
    r"^\s*(?P<left>\[[^\[\]]+\](?:\.\[[^\[\]]+\])*)\s*=\s*(?P<right>\[[^\[\]]+\](?:\.\[[^\[\]]+\])*)\s*$"
)

# A Cognos prompt parameter reference inside an expression, for example ``?p_From_Date?``.
_PARAM_REF_RE = re.compile(r"\?([^?]+)\?")

# Cognos set-operation source elements that combine queries into a union query.
_UNION_SOURCE_TAGS = ("queryOperation", "union", "intersect", "except", "setOperation")

# Cognos prompt-control element tags -> (control type, implied data type). Any element carrying a
# ``parameter`` attribute inside a prompt page is treated as a prompt; unknown tags fall back to a
# text/value control so the parameter is still surfaced.
_PROMPT_CONTROLS: dict[str, tuple[PromptControlType, DataType]] = {
    "selectDate": (PromptControlType.DATE, DataType.DATE_TIME),
    "selectDateTime": (PromptControlType.DATE_TIME, DataType.DATE_TIME),
    "selectTime": (PromptControlType.TIME, DataType.DATE_TIME),
    "selectInterval": (PromptControlType.INTERVAL, DataType.STRING),
    "selectValue": (PromptControlType.SELECT_VALUE, DataType.STRING),
    "selectWithList": (PromptControlType.SELECT_VALUE, DataType.STRING),
    "selectWithSearch": (PromptControlType.SELECT_VALUE, DataType.STRING),
    "selectWithTree": (PromptControlType.SELECT_VALUE, DataType.STRING),
    "selectWithRadio": (PromptControlType.SELECT_VALUE, DataType.STRING),
    "selectWithCheckbox": (PromptControlType.SELECT_VALUE, DataType.STRING),
    "textBox": (PromptControlType.TEXT, DataType.STRING),
    "numberBox": (PromptControlType.VALUE, DataType.DOUBLE),
    "generatedPrompt": (PromptControlType.GENERATED, DataType.STRING),
}

# Prompt controls that always collect more than one value.
_MULTI_SELECT_CONTROLS = {"selectWithCheckbox", "selectWithList"}

# Report-page constructs this converter does not map, which would otherwise vanish silently.
# Each tag is flagged once per report: tag -> (flag code, description, remedy).
_UNMIGRATED_LAYOUT: dict[str, tuple[str, str, str]] = {
    "repeater": (
        "layout-object-not-migrated",
        "repeater(s)",
        "Rebuild the per-row repetition (for example small multiples or a grouped matrix); "
        "objects nested in it were migrated without the repetition.",
    ),
    "repeaterTable": (
        "layout-object-not-migrated",
        "repeater table(s)",
        "Rebuild the per-row repetition (for example a concatenated measure or a matrix).",
    ),
    "singleton": (
        "layout-object-not-migrated",
        "singleton(s)",
        "A singleton showing exactly one data value becomes a card; these show no data value, "
        "several values, other data or static text beside the value, an HTML value, an item the "
        "query does not define, sit inside a repeating container, or did not fit on the page "
        "beside its other cards. Rebuild each, for example as a card or multi-row card bound to "
        "the same query items.",
    ),
    "viz": (
        "layout-object-not-migrated",
        "visualization(s)",
        "Rebuild each with the matching Power BI visual.",
    ),
    "HTMLItem": (
        "html-item",
        "HTML item(s)",
        "They can carry JavaScript or raw HTML that Power BI does not run; re-implement the "
        "behaviour natively or drop it deliberately.",
    ),
    "customControl": (
        "custom-control",
        "custom control(s)",
        "They run JavaScript that Power BI does not run; re-implement the behaviour natively or "
        "drop it deliberately.",
    ),
    "layoutComponentRef": (
        "layout-component-reference",
        "layout component reference(s)",
        "Each reference repeats a layout object (typically a shared header or footer) that is "
        "defined elsewhere; rebuild it on the Power BI pages.",
    ),
}

# Variable-driven alternatives, flagged by the outermost element of each family: a conditional
# block (conditionalBlocks and its cases) and a conditional text or image source
# (conditionalDataSources/conditionalDataSource). Prefixes, so every case element is covered.
_CONDITIONAL_FAMILIES = {
    "conditionalBlock": "conditional block(s)",
    "conditionalDataSource": "conditional text or image source(s)",
}

# Prompt-page content that has no target once prompts become slicers or parameters:
# tag -> (description, carries behaviour). Text, images and styling alone are informational;
# the rest needs review. Unmapped charts and conditional families are added by
# _prompt_page_content.
_PROMPT_PAGE_CONTENT: dict[str, tuple[str, bool]] = {
    "textItem": ("text item(s)", False),
    "image": ("image(s)", False),
    "conditionalStyleRef": ("conditional style(s)", False),
    "conditionalStyles": ("conditional style(s)", False),
    "HTMLItem": ("HTML item(s)", True),
    "customControl": ("custom control(s)", True),
    "pageModule": ("page module(s)", True),
    "layoutComponentRef": ("layout component reference(s)", True),
    "conditionalRender": ("conditionally rendered object(s)", True),
    **{
        tag: ("data object(s)", True)
        for tag in (
            *_LAYOUT_TO_VISUAL,
            "repeater",
            "repeaterTable",
            "singleton",
            "viz",
            "vizControl",
        )
    },
}


def _strip_namespaces(tree: etree._Element) -> etree._Element:
    """Remove XML namespaces in place so element lookups are version-agnostic."""
    for element in tree.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]
    etree.cleanup_namespaces(tree)
    return tree


def _sanitize_identifier(raw: str) -> str:
    """Produce a safe Power BI object name from a Cognos label."""
    name = raw.strip()
    name = re.sub(r"\s+", " ", name)
    return name or "Unnamed"


def _last_segment(reference: str) -> str:
    """Return the final ``[segment]`` of a qualified Cognos reference, without brackets."""
    parts = re.findall(r"\[([^\[\]]+)\]", reference)
    return _sanitize_identifier(parts[-1]) if parts else reference.strip("[]")


def _has_ancestor(element: etree._Element, tag: str) -> bool:
    """Return True if ``element`` has an ancestor with the given tag."""
    parent = element.getparent()
    while parent is not None:
        if parent.tag == tag:
            return True
        parent = parent.getparent()
    return False


def _outside_prompt_pages(root: etree._Element, tag: str) -> list[etree._Element]:
    """Return every ``tag`` element of the report that is not on a prompt page."""
    return [element for element in root.iter(tag) if not _has_ancestor(element, "promptPages")]


def _element_label(element: etree._Element) -> str | None:
    """Return the most useful identifying attribute of a layout element, if any."""
    return element.get("name") or element.get("path") or element.get("refLayoutElement")


def _quoted_names(names: Sequence[str | None], limit: int = 8) -> str:
    """Return the distinct non-empty names as a quoted list, truncated after ``limit``."""
    distinct = list(dict.fromkeys(name for name in names if name))
    shown = ", ".join(f"'{name}'" for name in distinct[:limit])
    hidden = len(distinct) - limit
    return f"{shown} and {hidden} more" if hidden > 0 else shown


def _tag_counts(elements: list[etree._Element]) -> str:
    """Summarize elements by tag, most frequent first, e.g. ``48 textItem, 12 block``."""
    return ", ".join(
        f"{count} {tag}" for tag, count in Counter(e.tag for e in elements).most_common()
    )


def _is_prompt_control_tag(element: etree._Element) -> bool:
    """Return True for an element of a prompt-control type, with or without a parameter."""
    tag = element.tag
    return isinstance(tag, str) and (tag in _PROMPT_CONTROLS or tag.startswith("select"))


def _is_prompt_control(element: etree._Element) -> bool:
    """Return True for a prompt control, the same controls _parse_prompts extracts."""
    return _is_prompt_control_tag(element) and bool((element.get("parameter") or "").strip())


def _is_layout_object(element: etree._Element) -> bool:
    """Return True for a layout object that is migrated or flagged in its own right."""
    tag = element.tag
    return isinstance(tag, str) and (
        tag in _LAYOUT_TO_VISUAL
        or tag in _UNMIGRATED_LAYOUT
        or tag == "vizControl"
        or _is_unmapped_chart(element)
        or _is_prompt_control(element)
    )


def _query_owner(element: etree._Element) -> etree._Element | None:
    """Return the layout object whose query ``element`` carries: itself or its nearest owner.

    A query sits on the object itself (a list, a repeater, a chart, a prompt control even
    without a parameter) or on one of its data-set parts (the ``vizDataSet`` of a visualization,
    the ``dataSet`` of a custom control).
    """
    for node in (element, *element.iterancestors()):
        if _is_layout_object(node) or _is_prompt_control_tag(node):
            return node
    return None


def _reported_as_crosstab_nested(element: etree._Element, table_names: set[str]) -> bool:
    """Return True when crosstab-nested-content already reports ``element``.

    A report-page crosstab whose query resolved to a table reports every query-bearing
    descendant, so it reports ``element`` when the query sits on ``element`` itself or on one of
    its data-set parts. A query-less object, such as a repeater or an HTML item around another
    query-bound object, is not reported.
    """
    in_bound_crosstab = any(
        crosstab.get("refQuery")
        and _sanitize_identifier(crosstab.get("refQuery")) in table_names
        and _has_ancestor(crosstab, "page")
        for crosstab in element.iterancestors("crosstab")
    )
    return in_bound_crosstab and any(
        isinstance(node.tag, str) and node.get("refQuery") and _query_owner(node) is element
        for node in element.iter()
    )


def _is_unmapped_chart(element: etree._Element) -> bool:
    """Return True for a Cognos chart element that does not become a Power BI visual.

    Every Cognos chart element name ends in ``Chart`` (``v2_pieChart``, ``gaugeChart``,
    ``mapChart``, ...); their axes, palettes, labels and nodes do not.
    """
    tag = element.tag
    return isinstance(tag, str) and tag.endswith("Chart") and tag not in _LAYOUT_TO_VISUAL


def _conditional_family(element: etree._Element) -> str | None:
    """Return the conditional family prefix when ``element`` is the outermost element of one."""
    parent = element.getparent()
    for prefix in _CONDITIONAL_FAMILIES:
        if element.tag.startswith(prefix) and not (
            parent is not None and isinstance(parent.tag, str) and parent.tag.startswith(prefix)
        ):
            return prefix
    return None


def _conditional_family_root(element: etree._Element) -> etree._Element | None:
    """Return the outermost element of the conditional family ``element`` belongs to, if any.

    A family is a chain of directly nested elements with the same prefix, for example
    conditionalBlocks/conditionalBlockCases/conditionalBlock; content inside a block's
    ``contents`` is not part of it.
    """
    if not isinstance(element.tag, str):
        return None
    for prefix in _CONDITIONAL_FAMILIES:
        if element.tag.startswith(prefix):
            root = element
            parent = root.getparent()
            while (
                parent is not None and isinstance(parent.tag, str) and parent.tag.startswith(prefix)
            ):
                root = parent
                parent = root.getparent()
            return root
    return None


def _prompt_page_content(element: etree._Element) -> tuple[str, bool] | None:
    """Return (description, carries behaviour) for prompt-page content with no Power BI target."""
    if not isinstance(element.tag, str):
        return None
    content = _PROMPT_PAGE_CONTENT.get(element.tag)
    if content is not None:
        return content
    if _is_unmapped_chart(element):
        return ("data object(s)", True)
    family = _conditional_family(element)
    return (_CONDITIONAL_FAMILIES[family], True) if family else None


def _format_place(owner: etree._Element) -> str:
    """Describe where a data format sits: its layout object and the nearest named container."""
    named = next((a for a in (owner, *owner.iterancestors()) if a.get("name")), None)
    return f"{owner.tag} in '{named.get('name')}'" if named is not None else owner.tag


def _is_conditional_style(element: etree._Element) -> bool:
    """Return True for a conditional style reference, style variable or conditional palette."""
    tag = element.tag
    return isinstance(tag, str) and (
        tag in {"conditionalStyleRef", "conditionalStyles"}
        # Conditional chart palettes, for example ``v2_conditionalLinePalette``.
        or (tag.startswith("v2_conditional") and tag.endswith("Palette"))
    )


def _has_external_report_path(element: etree._Element) -> bool:
    """Return True when a layout component reference points into another report."""
    return element.find("reportPath") is not None


def _placeholder_measure_note(draft_dax: str | None) -> str:
    """Describe how the generator emits a measure that still needs review."""
    comments = (
        "the Cognos expression and the unverified draft DAX as comments"
        if draft_dax and draft_dax.strip()
        else "the Cognos expression as a comment"
    )
    return (
        f" Unless AI refinement translates it, it is emitted as a BLANK() placeholder measure with "
        f"{comments} so the model loads; translate it to DAX."
    )


def _variable_use_owner(element: etree._Element) -> etree._Element:
    """Return the construct a ``refVariable`` belongs to.

    That is the element itself, its conditional style (which may carry its variable on its case
    list rather than on itself), or the outermost element of its conditional block or text
    source (a block may carry its variable on ``conditionalBlockCases``).
    """
    parent = element.getparent()
    if parent is not None and _is_conditional_style(parent) and not _is_conditional_style(element):
        return parent
    root = _conditional_family_root(element)
    return element if root is None else root


def _flagged_variable_use(element: etree._Element) -> bool:
    """Return True when a flag already reports this use of a report variable.

    Only the construct that carries the variable counts, never one it is nested in: conditional
    rendering, blocks, text sources and styles are reported on report pages by
    conditional-render/conditional-style, and on prompt pages by prompt-page-content (a
    conditional chart palette as part of its chart), which skips prompt-button captions.
    """
    owner = _variable_use_owner(element)
    if not (
        owner.tag == "conditionalRender"
        or _is_conditional_style(owner)
        or _conditional_family(owner)
    ):
        return False
    if not _has_ancestor(owner, "promptPages"):
        return True
    return _has_ancestor(owner, "page") and not _has_ancestor(owner, "promptButton")


# Default presentation for well-known Cognos style classes (refStyle). Their full definitions live
# in the Cognos global theme, not the report XML, so we encode the visually significant defaults for
# the classes seen in the field. Unknown classes are ignored (they fall back to generator defaults).
_REFSTYLE_DEFAULTS: dict[str, dict[str, object]] = {
    # List column title: bold, horizontally centered (the default Cognos list header treatment).
    "lt": {"bold": True, "text_align": "Center"},
}

# CSS length in points. Cognos emits pt directly; px is converted with the common 0.75pt/px ratio.
_CSS_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(pt|px)?\s*$", re.IGNORECASE)
_CSS_ALIGN = {"left": "Left", "center": "Center", "right": "Right", "justify": "Justify"}
_CSS_VALIGN = {"top": "Top", "middle": "Middle", "bottom": "Bottom"}


def _css_declarations(css_text: str) -> dict[str, str]:
    """Split a CSS ``value`` string into a lowercase-keyed declaration map."""
    out: dict[str, str] = {}
    for declaration in css_text.split(";"):
        if ":" not in declaration:
            continue
        prop, _, value = declaration.partition(":")
        prop = prop.strip().lower()
        value = value.strip()
        if prop and value:
            out[prop] = value
    return out


def _css_size_pt(value: str) -> float | None:
    match = _CSS_SIZE_RE.match(value)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "pt").lower()
    return round(number * 0.75, 1) if unit == "px" else number


def _apply_css(style: Style, css_text: str) -> None:
    """Merge CSS declarations from a Cognos ``<CSS value=.../>`` string onto a Style in place."""
    decl = _css_declarations(css_text)
    if "font-family" in decl:
        # Take the first family and strip quotes; drop any generic fallback list.
        family = decl["font-family"].split(",")[0].strip().strip("'\"")
        if family:
            style.font_family = family
    if "font-size" in decl:
        size = _css_size_pt(decl["font-size"])
        if size:
            style.font_size_pt = size
    if "font-weight" in decl:
        weight = decl["font-weight"].strip().lower()
        if weight in {"bold", "bolder"} or (weight.isdigit() and int(weight) >= 600):
            style.bold = True
    if decl.get("font-style", "").lower() == "italic":
        style.italic = True
    if "underline" in decl.get("text-decoration", "").lower():
        style.underline = True
    if "color" in decl:
        style.color = decl["color"].strip()
    if "background-color" in decl:
        style.background_color = decl["background-color"].strip()
    background = decl.get("background")
    if background and style.background_color is None:
        # A shorthand background may carry only a color token; keep it if it looks like one.
        token = background.split()[0].strip() if background.split() else ""
        if token.startswith("#") or token.isalpha():
            style.background_color = token
    if "text-align" in decl:
        style.text_align = _CSS_ALIGN.get(decl["text-align"].strip().lower())
    if "vertical-align" in decl:
        style.vertical_align = _CSS_VALIGN.get(decl["vertical-align"].strip().lower())


def _extract_style(style_element: etree._Element | None) -> Style | None:
    """Build a Style from a Cognos ``<style>`` element (named ``refStyle`` classes plus inline CSS).

    Named classes are applied first (as defaults) and inline CSS overrides them, matching how Cognos
    layers a class reference under an explicit CSS override.
    """
    if style_element is None:
        return None
    style = Style()
    for ref in style_element.iter("defaultStyle"):
        defaults = _REFSTYLE_DEFAULTS.get((ref.get("refStyle") or "").strip())
        if defaults:
            for key, value in defaults.items():
                setattr(style, key, value)
    for css in style_element.iter("CSS"):
        value = css.get("value")
        if value:
            _apply_css(style, value)
    return None if style.is_empty() else style


def _child_style(element: etree._Element) -> Style | None:
    """Return the extracted Style from a direct ``<style>`` child of an element, if any."""
    return _extract_style(element.find("style"))


# Cognos data formats (``<dataFormat>`` with one format element as its direct child) sit on
# layout objects, never on query data items (a data item has no Data Format property). They are
# translated into the format-string dialect Power BI reads at model level: VBA-style tokens,
# where "%" multiplies by 100 and each comma just left of the decimal point divides by 1,000.
_LOCALE_FORMAT_ATTRS = ("decimalDelimiter", "groupDelimiter", "dateSeparator", "timeSeparator")
_NUMBER_FORMAT_ATTRS = {"decimalSize", "useGrouping", "scale", "pattern", *_LOCALE_FORMAT_ATTRS}
_FORMAT_ATTRS = {
    "numberFormat": _NUMBER_FORMAT_ATTRS,
    "currencyFormat": _NUMBER_FORMAT_ATTRS
    | {"currencySymbol", "currencyCode", "useIntlSymbol", "useTrailingSymbol"},
    "percentFormat": _NUMBER_FORMAT_ATTRS | {"percentSymbol", "percentScale"},
}
# Local symbols of common currency codes (Cognos shows these unless useIntlSymbol is true).
_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "JPY": "\u00a5"}
# Characters a format string displays literally without quotes.
_RAW_NUMBER_LITERALS = frozenset("$-+() ")
_RAW_DATE_LITERALS = frozenset(" -,.")
# Cognos date/time styles -> Power BI named formats. Cognos medium has no Power BI equivalent.
_DATE_STYLES = {
    "short": "Short Date",
    "medium": "Long Date",
    "long": "Long Date",
    "full": "Long Date",
}
_TIME_STYLES = {
    "short": "Short Time",
    "medium": "Long Time",
    "long": "Long Time",
    "full": "Long Time",
}
_DATE_FORMATS = {"dateFormat", "timeFormat", "dateTimeFormat"}


@dataclass
class _TranslatedFormat:
    """A Cognos format element as a Power BI format string, plus what could not be kept.

    ``format_string`` is None when nothing was specified or when the format cannot be expressed
    without changing the magnitude or meaning of the value (then ``dropped`` says why).
    """

    format_string: str | None
    family: Literal["number", "date", "other"]
    dropped: list[str] = field(default_factory=list)
    locale: list[str] = field(default_factory=list)


def _format_literal(text: str, raw: frozenset[str] = _RAW_NUMBER_LITERALS) -> str:
    """Return ``text`` as format-string literal text: quoted, except characters shown as-is."""
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append('"' + "".join(run) + '"')
            run.clear()

    for char in text:
        if char in raw:
            flush()
            out.append(char)
        elif char == '"':
            # A double quote cannot sit inside a quoted literal; escape it on its own.
            flush()
            out.append('\\"')
        else:
            run.append(char)
    flush()
    return "".join(out)


def _int_attr(element: etree._Element, name: str) -> int | None:
    """Return an integer attribute, or None when it is absent or not an integer."""
    raw = (element.get(name) or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _scale_commas(element: etree._Element) -> str | None:
    """Return the scaling commas for a Cognos ``scale``; None when Power BI cannot express it.

    A negative scale divides by a power of ten (-3 shows thousands); Power BI divides by 1,000
    per comma, so only negative multiples of three are expressible.
    """
    raw = (element.get("scale") or "").strip()
    if not raw:
        return ""
    scale = _int_attr(element, "scale")
    if scale == 0:
        return ""
    if scale is not None and scale < 0 and scale % 3 == 0:
        return "," * (-scale // 3)
    return None


def _number_body(element: etree._Element, default_decimals: str, commas: str) -> str:
    """Return the digits of a format without a pattern: grouping, scaling and decimals."""
    grouping = (element.get("useGrouping") or "true").strip().lower() != "false"
    size = _int_attr(element, "decimalSize")
    if size is None or size < 0:
        decimals = default_decimals
    else:
        decimals = "." + "0" * size if size > 0 else ""
    return ("#,##0" if grouping else "0") + commas + decimals


def _invalid_decimal_size(element: etree._Element) -> list[str]:
    """Return a note when ``decimalSize`` is set but is not a usable number of decimals."""
    raw = element.get("decimalSize")
    if raw is None or not raw.strip() or element.get("pattern"):
        return []
    size = _int_attr(element, "decimalSize")
    if size is not None and size >= 0:
        return []
    return [f"decimalSize='{raw}' (not a number of decimals; the default decimals are shown)"]


def _split_pattern_sections(pattern: str) -> list[str]:
    """Split an ICU pattern into its positive and negative sections, ignoring quoted ';'."""
    sections = [""]
    in_quote = False
    for char in pattern:
        if char == "'":
            in_quote = not in_quote
        if char == ";" and not in_quote:
            sections.append("")
        else:
            sections[-1] += char
    return sections


def _number_pattern_section(
    section: str, symbol: str, commas: str, code: str
) -> tuple[str | None, str, list[str]]:
    """Translate one section of an ICU number pattern.

    Returns the format, why it cannot be translated (when the format is None) and what was
    approximated.
    """
    parts: list[str] = []
    literal: list[str] = []
    notes: list[str] = []
    integer_end: int | None = None
    seen_decimal = False
    integer_digits = ""

    def flush() -> None:
        if literal:
            parts.append(_format_literal("".join(literal)))
            literal.clear()

    index = 0
    in_quote = False
    while index < len(section):
        char = section[index]
        following = section[index + 1] if index + 1 < len(section) else ""
        if char == "'":
            if following == "'":
                literal.append("'")
                index += 2
                continue
            in_quote = not in_quote
        elif in_quote:
            literal.append(char)
        elif char in "0#":
            flush()
            parts.append(char)
            if not seen_decimal:
                integer_end = len(parts)
                integer_digits += char
        elif char == ".":
            flush()
            seen_decimal = True
            parts.append(char)
        elif char == ",":
            # A comma not followed by a digit would scale in Power BI but only groups in Cognos.
            if following not in ("0", "#"):
                return None, "a grouping comma that is not followed by a digit", []
            flush()
            parts.append(char)
            if not seen_decimal:
                integer_digits += char
        elif char == "%":
            flush()
            parts.append(char)
        elif char == "\u00a4":
            start = index
            while following == "\u00a4":
                index += 1
                following = section[index + 1] if index + 1 < len(section) else ""
            run = index - start + 1
            text = symbol
            if run >= 2:
                # ICU shows the ISO code for a doubled sign (the plural name for a tripled one),
                # spaced from an adjacent digit.
                if not code:
                    notes.append(f"the ISO currency code ('{char * run}'; no currencyCode is set)")
                else:
                    if run > 2:
                        notes.append(f"the currency name ('{char * run}'; the ISO code is shown)")
                    previous = section[start - 1] if start else ""
                    text = (" " if previous in ("0", "#") else "") + code
                    text += " " if following in ("0", "#") else ""
            literal.append(text)
        elif char in "\u2030E@*" or char in "123456789":
            return None, f"the pattern character '{char}'", []
        else:
            literal.append(char)
        index += 1
    flush()
    if integer_end is None:
        return None, "no integer digit", []
    groups = integer_digits.split(",")
    if len(groups) > 1 and (len(groups[-1]) != 3 or any(len(g) != 3 for g in groups[1:-1])):
        notes.append(f"the digit grouping of '{section}' (Power BI groups digits by three)")
    parts.insert(integer_end, commas)
    return "".join(parts), "", notes


def _translate_number_pattern(
    pattern: str, symbol: str, commas: str, code: str = ""
) -> tuple[str | None, str, list[str]]:
    """Translate an ICU number pattern (Cognos Group A letters).

    Returns the format (None when it cannot be translated), why not, and what was approximated.
    """
    out: list[str] = []
    notes: list[str] = []
    for section in _split_pattern_sections(pattern):
        translated, problem, section_notes = _number_pattern_section(section, symbol, commas, code)
        if translated is None:
            return None, problem, []
        out.append(translated)
        notes += section_notes
    return ";".join(out), "", list(dict.fromkeys(notes))


def _currency_symbol(element: etree._Element) -> tuple[str, list[str]]:
    """Return the symbol a Cognos currency format shows, and what about it could not be kept."""
    symbol = element.get("currencySymbol")
    if symbol is not None:
        return symbol, []
    code = (element.get("currencyCode") or "").strip()
    if not code:
        return "", ["the currency symbol (no currency code; Cognos shows the model's currency)"]
    if (element.get("useIntlSymbol") or "").strip().lower() == "true":
        return code + " ", []
    local = _CURRENCY_SYMBOLS.get(code.upper())
    if local is None:
        return code + " ", [f"the local symbol of currency '{code}' (the code is shown instead)"]
    return local, []


def _translate_number_format(element: etree._Element, tag: str) -> _TranslatedFormat:
    """Translate a Cognos number, currency or percent format."""
    attrs = dict(element.attrib)
    locale = [f"{name}='{attrs[name]}'" for name in _LOCALE_FORMAT_ATTRS if name in attrs]
    if tag != "percentFormat" and not set(attrs) - set(_LOCALE_FORMAT_ATTRS):
        return _TranslatedFormat(None, "number", [], locale)
    dropped = [
        f"{name}='{value}'"
        + (" (negative sign symbol; Power BI shows '-')" if name == "sign" else "")
        for name, value in attrs.items()
        if name not in _FORMAT_ATTRS[tag]
    ]

    def refused(reason: str) -> _TranslatedFormat:
        return _TranslatedFormat(None, "number", [f"the whole format: {reason}"])

    commas = _scale_commas(element)
    if commas is None:
        return refused(
            f"scale='{element.get('scale')}' is not a division by thousands, which is the only "
            "scaling a Power BI format string can express"
        )
    symbol = ""
    if tag == "currencyFormat":
        symbol, symbol_dropped = _currency_symbol(element)
        dropped += symbol_dropped
    suffix = ""
    if tag == "percentFormat":
        if (element.get("percentScale") or "0").strip() not in ("", "0"):
            return refused(
                f"percentScale='{element.get('percentScale')}' rescales the value in a way that "
                "could not be verified, so the shown magnitude would be uncertain"
            )
        percent = element.get("percentSymbol") or "%"
        if percent.strip() != "%":
            return refused(f"percentSymbol='{percent}' (only per hundred, '%', is expressible)")
        suffix = " " * (len(percent) - len(percent.rstrip(" "))) + "%"
    pattern = element.get("pattern")
    if pattern:
        code = (element.get("currencyCode") or "").strip() if tag == "currencyFormat" else ""
        format_string, problem, notes = _translate_number_pattern(pattern, symbol, commas, code)
        if format_string is None:
            return refused(f"pattern '{pattern}' has {problem}")
        return _TranslatedFormat(format_string, "number", dropped + notes, locale)
    dropped += _invalid_decimal_size(element)
    if tag == "currencyFormat":
        body = _number_body(element, ".00", commas)
        if (element.get("useTrailingSymbol") or "").strip().lower() == "true":
            # Spaces typed around a trailing symbol separate it from the number.
            spaces = " " * (len(symbol) - len(symbol.strip(" ")))
            format_string = body + spaces + _format_literal(symbol.strip(" "))
        else:
            format_string = _format_literal(symbol) + body
    elif tag == "percentFormat":
        format_string = _number_body(element, "", commas) + suffix
    else:
        format_string = _number_body(element, ".###", commas)
    return _TranslatedFormat(format_string, "number", dropped, locale)


def _translate_date_pattern(pattern: str) -> tuple[str | None, str, list[str], bool]:
    """Translate an ICU date/time pattern (Cognos Group A letters).

    Returns the format (None when it cannot be translated), why not, what was approximated and
    whether it shows the locale's date or time separator.
    """
    out: list[str] = []
    literal: list[str] = []
    notes: list[str] = []
    last_letter = ""
    fields = 0

    def flush() -> None:
        if literal:
            out.append(_format_literal("".join(literal), _RAW_DATE_LITERALS))
            literal.clear()

    index = 0
    in_quote = False
    while index < len(pattern):
        char = pattern[index]
        if char == "'":
            if pattern[index + 1 : index + 2] == "'":
                literal.append("'")
                index += 2
                continue
            in_quote = not in_quote
            index += 1
            continue
        if in_quote or not ("a" <= char.lower() <= "z"):
            if not in_quote and char in "/:":
                # Power BI shows its locale's separator here; the locale flag reports that.
                flush()
                out.append(char)
            else:
                literal.append(char)
            index += 1
            continue
        run = len(pattern) - index - len(pattern[index:].lstrip(char))
        token: str | None = None
        if char == "y":
            token = "yy" if run == 2 else "yyyy"
        elif char in "ML":
            token = "M" * min(run, 4)
        elif char == "E":
            token = "dddd" if run >= 4 else "ddd"
        elif char in "dHs" and run <= 2:
            token = char * run
        elif char == "m" and run <= 2 and last_letter == "H":
            token = "m" * run
        if token is None:
            return None, f"the pattern letters '{char * run}'", [], False
        if char in "MLE" and run >= 5:
            notes.append(f"the narrow form '{char * run}' (shown as '{token}')")
        flush()
        out.append(token)
        fields += 1
        if char != "s":
            last_letter = "H" if char in "Hm" else char
        index += run
    flush()
    if not fields:
        return None, "no date or time field", [], False
    return "".join(out), "", notes, "/" in out or ":" in out


def _date_from_parts(element: etree._Element) -> tuple[str | None, list[str]]:
    """Build a date format from Cognos display order and day/month/year display properties.

    Returns the format and what was assumed, or None and why it cannot be built. Without a
    ``dateSeparator`` the format uses "/", which Power BI shows as the locale's separator, as
    Cognos does.
    """
    order = (element.get("displayOrder") or "").strip().upper()
    if sorted(order) != ["D", "M", "Y"]:
        return None, [f"displayOrder='{element.get('displayOrder')}' is not an order of D, M and Y"]
    defaults = {"D": "d", "M": "M", "Y": "yyyy"}
    shows = {"D": "showDays", "M": "showMonths", "Y": "showYears"}
    notes: list[str] = []
    tokens: list[str] = []
    for part in order:
        raw = element.get(shows[part])
        value = (raw or "").strip().lower()
        if raw is None:
            token: str | None = defaults[part]
            notes.append(f"{shows[part]} is not set (shown as '{token}')")
        elif value.startswith("hide") and value != "hidecentury":
            continue
        elif value == "showcentury":
            token = "yyyy" if part == "Y" else None
        elif value == "hidecentury":
            token = "yy" if part == "Y" else None
        elif value.startswith("1"):
            token = {"D": "d", "M": "M"}.get(part)
        elif value.startswith("2"):
            token = {"D": "dd", "M": "MM"}.get(part)
        elif value.startswith("abbrev"):
            token = "MMM" if part == "M" else None
        elif value == "full":
            token = "MMMM" if part == "M" else None
        else:
            token = None
        if token is None:
            return None, [f"{shows[part]}='{raw}' has no Power BI format string"]
        tokens.append(token)
    if not tokens:
        return None, ["every date part is hidden, so nothing would be shown"]
    separator = element.get("dateSeparator")
    joiner = "/" if separator in (None, "/") else _format_literal(separator, _RAW_DATE_LITERALS)
    return joiner.join(tokens), notes


def _translate_date_format(element: etree._Element, tag: str) -> _TranslatedFormat:
    """Translate a Cognos date, time or date-time format."""
    attrs = dict(element.attrib)
    locale = [f"{name}='{attrs[name]}'" for name in _LOCALE_FORMAT_ATTRS if name in attrs]
    others = {name: value for name, value in attrs.items() if name not in _LOCALE_FORMAT_ATTRS}
    if not others:
        return _TranslatedFormat(None, "date", [], locale)
    pattern = element.get("pattern")
    if pattern:
        # A pattern overrides every other date property, so nothing else is lost.
        translated, problem, notes, separators = _translate_date_pattern(pattern)
        if translated is None:
            return _TranslatedFormat(
                None, "date", [f"the whole format: pattern '{pattern}' has {problem}"]
            )
        if separators:
            locale.append(f"pattern='{pattern}' separators")
        return _TranslatedFormat(translated, "date", notes, locale)
    if tag == "dateFormat" and element.get("displayOrder"):
        # Explicit day, month and year display overrides the date style it refines.
        built, notes = _date_from_parts(element)
        parts = {"displayOrder", "showDays", "showMonths", "showYears", "dateStyle"}
        if built is None:
            return _TranslatedFormat(None, "date", [f"the whole format: {notes[0]}"])
        notes += [f"{name}='{value}'" for name, value in others.items() if name not in parts]
        if element.get("dateSeparator") not in (None, "/"):
            # Any other separator is a literal in the format string, so it is kept as is.
            locale = [entry for entry in locale if not entry.startswith("dateSeparator=")]
        return _TranslatedFormat(built, "date", notes, locale)
    date_style = (element.get("dateStyle") or "").strip().lower()
    time_style = (element.get("timeStyle") or "").strip().lower()
    used: set[str] = set()
    dropped: list[str] = []
    format_string: str | None = None
    if tag == "dateTimeFormat" and (date_style or time_style):
        format_string = "General Date"
        used = {"dateStyle", "timeStyle"}
        dropped.append("the date/time styles (shown as the locale's General Date)")
    elif tag == "dateFormat" and date_style in _DATE_STYLES:
        format_string = _DATE_STYLES[date_style]
        used = {"dateStyle"}
        if date_style == "medium":
            dropped.append("dateStyle='medium' (shown as Long Date; Power BI has no medium style)")
        elif date_style in ("long", "full"):
            dropped.append(
                f"dateStyle='{date_style}' (shown as Long Date, which follows the viewer's "
                "locale and may differ, for example in showing the weekday)"
            )
    elif tag == "timeFormat" and time_style in _TIME_STYLES:
        format_string = _TIME_STYLES[time_style]
        used = {"timeStyle"}
        if time_style in ("long", "full"):
            dropped.append(f"timeStyle='{time_style}' (shown as Long Time, without the time zone)")
    dropped += [f"{name}='{value}'" for name, value in others.items() if name not in used]
    if format_string is None:
        dropped = [f"the whole format: {', '.join(dropped)} has no Power BI format string"]
        locale = []
    return _TranslatedFormat(format_string, "date", dropped, locale)


def _translate_data_format(element: etree._Element) -> _TranslatedFormat:
    """Translate the format element of a Cognos ``<dataFormat>`` into a Power BI format string."""
    tag = element.tag if isinstance(element.tag, str) else ""
    if tag in _FORMAT_ATTRS:
        translated = _translate_number_format(element, tag)
    elif tag in _DATE_FORMATS:
        translated = _translate_date_format(element, tag)
    else:
        return _TranslatedFormat(
            None, "other", [f"the whole format: {tag} has no Power BI equivalent"]
        )
    if translated.format_string is not None and not translated.format_string.strip():
        # An empty format string would show the value in Power BI's default format, silently.
        return _TranslatedFormat(
            None, translated.family, ["the whole format: it would show nothing of the value"]
        )
    return translated


_CROSSTAB_CELLS = frozenset({"crosstabIntersection", "crosstabFactCell", "factCell"})
_COUNT_AGGREGATES = {"count", "countdistinct"}
_NUMBER_AGGREGATES = {"total", "sum", "average", "avg"}


@dataclass
class _FormatTargets:
    """What the layout object of a data format shows, or why the format cannot be applied.

    ``note`` describes values the object also shows but the format is not applied to.
    ``shadowed_by`` lists the nearer formats that replace this one for its own values.
    """

    table: Table | None
    names: list[str]
    problem: str = ""
    note: str = ""
    shadowed_by: list[etree._Element] = field(default_factory=list)


def _crosstab_edge_paths(edge: etree._Element | None) -> list[list[etree._Element]]:
    """Return the member paths, outermost first, that end in a leaf member of a crosstab edge."""
    paths: list[list[etree._Element]] = []

    def walk(node: etree._Element, prefix: list[etree._Element]) -> None:
        nested = [
            child
            for holder in node.findall("crosstabNestedNodes")
            for child in holder.findall("crosstabNode")
            if next(child.iter("crosstabNodeMember"), None) is not None
        ]
        for member in node.findall("crosstabNodeMembers/crosstabNodeMember"):
            if not nested:
                paths.append([*prefix, member])
            for child in nested:
                walk(child, [*prefix, member])

    for node in edge.findall("crosstabNode") if edge is not None else []:
        walk(node, [])
    return paths or [[]]


def _crosstab_cells_shown(
    crosstab: etree._Element, measures: set[str]
) -> list[tuple[str, list[etree._Element], etree._Element]]:
    """Return the measure of each leaf cell of a crosstab that shows a cell value.

    Each entry also lists the elements whose data format applies to that cell, nearest first,
    and the cell that governs it (the place to name):
    the text item showing the value, the intersection, the member fact cells, the crosstab fact
    cell, then the crosstab and its containers. A cell takes its contents from the nearest of
    those cells that has any; an intersection usually has only a style.
    """
    default = crosstab.find("defaultMeasure")
    default_ref = default.get("refDataItem") if default is not None else None
    intersections = crosstab.findall("crosstabIntersections/crosstabIntersection")
    generic = crosstab.find("crosstabFactCell")
    outer = [crosstab, *crosstab.iterancestors()]
    shown: list[tuple[str, list[etree._Element], etree._Element]] = []
    for row in _crosstab_edge_paths(crosstab.find("crosstabRows")):
        for column in _crosstab_edge_paths(crosstab.find("crosstabColumns")):
            members = [*reversed(row), *reversed(column)]
            ref = next(
                (
                    member.get("refDataItem")
                    for member in members
                    if _sanitize_identifier(member.get("refDataItem") or "") in measures
                ),
                default_ref,
            )
            if not ref:
                continue
            rows = {m.get("edgeLocation") for m in row} - {None}
            columns = {m.get("edgeLocation") for m in column} - {None}
            crossing = next(
                (
                    cell
                    for cell in intersections
                    if (not rows or cell.get("row") in rows)
                    and (not columns or cell.get("column") in columns)
                ),
                None,
            )
            fact_cells = [next((c for c in m if c.tag == "factCell"), None) for m in members]
            cells = [c for c in (crossing, *fact_cells, generic) if c is not None]
            contents = next(
                (c.find("contents") for c in cells if c.find("contents") is not None), None
            )
            value = next(contents.iter("cellValue"), None) if contents is not None else None
            if value is None:
                continue
            inner = list(value.iterancestors())
            inner = inner[: inner.index(contents)] if contents in inner else []
            shown.append((ref, [*inner, *cells, *outer], cells[0]))
    return shown


def _measure_families(root: etree._Element) -> dict[tuple[str, str], str]:
    """Return the kind of values ("number", "date" or a type name) of data items, when known.

    Keys are (table, item) model names. Items whose type Cognos does not state and whose
    aggregate does not imply a number are left out.
    """
    families: dict[tuple[str, str], str] = {}
    for query in root.iter("query"):
        table = _sanitize_identifier(query.get("name") or "Query")
        for item in query.iter("dataItem"):
            expression_el = item.find("expression")
            text = expression_el.text if expression_el is not None else None
            expression = _strip_comment_macros(text.strip() if text else None)
            aggregate = (item.get("aggregate") or "").strip().lower()
            explicit = _explicit_data_type(item, expression)
            if aggregate in _COUNT_AGGREGATES:
                family = "number"
            elif explicit is not None:
                family = (
                    "date"
                    if explicit == DataType.DATE_TIME
                    else "number"
                    if explicit in _NUMERIC_TYPES
                    else explicit.value
                )
            elif (
                aggregate in _NUMBER_AGGREGATES
                or _infer_data_type(item, expression) in _NUMERIC_TYPES
            ):
                family = "number"
            else:
                continue
            families[(table, _sanitize_identifier(item.get("name") or "Item"))] = family
    return families


def _rs_data_type(data_item: etree._Element) -> DataType | None:
    """Return the TMDL type implied by an ``RS_dataType`` XML attribute, if present."""
    for attr in data_item.iter("XMLAttribute"):
        if attr.get("name") == "RS_dataType":
            code = (attr.get("value") or "").strip()
            return _RS_DATATYPE_TO_TMDL.get(code)
    return None


def _explicit_data_type(data_item: etree._Element, expression: str | None) -> DataType | None:
    """Return the type Cognos metadata states: a datatype attribute, RS_dataType or a cast."""
    for attr in ("datatype", "dataType", "type"):
        value = data_item.get(attr)
        if value:
            return DataType.from_cognos(value)
    rs_type = _rs_data_type(data_item)
    if rs_type is not None:
        return rs_type
    cast = _CAST_FULL_RE.match(expression.strip()) if expression else None
    return DataType.from_cognos(cast.group("type")) if cast else None


def _infer_data_type(data_item: etree._Element, expression: str | None) -> DataType:
    """Infer a TMDL data type from data-item attributes, RS_dataType, or the expression."""
    explicit = _explicit_data_type(data_item, expression)
    if explicit is not None:
        return explicit
    if expression:
        if _COUNT_FUNCS_RE.search(expression):
            return DataType.INT64
        if _FLOAT_FUNCS_RE.search(expression):
            return DataType.DOUBLE
    return DataType.STRING


def _reference_source(expression: str | None, fallback: str) -> str:
    """Return the physical source column for a reference or cast-of-reference expression."""
    if not expression:
        return fallback
    expr = expression.strip()
    if _SIMPLE_REF_RE.match(expr):
        return _last_segment(expr)
    cast = _CAST_FULL_RE.match(expr)
    if cast and _SIMPLE_REF_RE.match(cast.group("inner").strip()):
        return _last_segment(cast.group("inner").strip())
    return fallback


def _contains_summary_expression(expression: str) -> bool:
    """Detect aggregate calls anywhere in an expression without translating it."""
    # Comments are whitespace; literals and identifiers must remain opaque operand tokens.
    searchable = _EXPRESSION_LITERALS_AND_COMMENTS.sub(
        lambda token: " " if token.group().startswith(("/*", "--", "//")) else "?",
        expression,
    )
    return any(
        call.group(1).lower() in _SUMMARY_FUNCTIONS
        for call in _EXPRESSION_FUNCTION_CALL.finditer(searchable)
    )


def _is_reference_like(expression: str | None) -> bool:
    """Return True when the expression is a plain reference or a cast of a plain reference."""
    if not expression or not expression.strip():
        return True
    expr = expression.strip()
    if _SIMPLE_REF_RE.match(expr):
        return True
    cast = _CAST_FULL_RE.match(expr)
    return bool(cast and _SIMPLE_REF_RE.match(cast.group("inner").strip()))


def _strip_comment_macros(expression: str | None) -> str | None:
    """Remove macros that contain only comments; they expand to nothing in Cognos."""
    if not expression:
        return expression
    stripped = _MACRO_BLOCK_RE.sub(
        lambda macro: "" if _COMMENTS_ONLY_RE.fullmatch(macro.group(1)) else macro.group(0),
        expression,
    ).strip()
    return stripped or None


def _expand_measure_references(
    expression: str, measure_items: dict[str, str], visiting: frozenset[str]
) -> tuple[str, bool]:
    """Inline same-query aggregate data items so a detail-level calculation sees row values.

    Cognos evaluates an aggregated calculation per detail row, substituting the expressions of
    the data items it references. Returns the expanded text and ``False`` on a reference cycle.
    """
    complete = True

    def repl(match: re.Match[str]) -> str:
        nonlocal complete
        ref = match.group("ref")
        if ref is None or "].[" in ref:
            return match.group(0)
        name = _sanitize_identifier(ref[1:-1])
        if name not in measure_items:
            return ref
        if name in visiting:
            complete = False
            return ref
        inner, inner_complete = _expand_measure_references(
            measure_items[name], measure_items, visiting | {name}
        )
        complete = complete and inner_complete
        return inner if _SIMPLE_REF_RE.match(inner) else f"({inner})"

    return _LITERAL_COMMENT_OR_REFERENCE_RE.sub(repl, expression), complete


def _references_items(expression: str, names: dict[str, str]) -> bool:
    """Return True when the expression has a single-segment reference to one of ``names``."""
    return any(
        match.group("ref")
        and "].[" not in match.group("ref")
        and _sanitize_identifier(match.group("ref")[1:-1]) in names
        for match in _LITERAL_COMMENT_OR_REFERENCE_RE.finditer(expression)
    )


def _report_page_location(elements: list[etree._Element]) -> str:
    """Say where report objects sit: on report pages, outside them (layout components), or both."""
    on_pages = sum(1 for element in elements if _has_ancestor(element, "page"))
    if on_pages == len(elements):
        return "on report pages"
    if not on_pages:
        return "outside the report pages (for example in layout components)"
    return "on report pages and outside them (for example in layout components)"


def _stack_visuals(visuals: list[Visual]) -> list[Visual]:
    """Stack a page's visuals top to bottom in the content area so none overlap.

    Each list or crosstab is sized to fill the page on its own, so a page with several of them
    would otherwise draw them on top of each other. Cards (migrated singletons) are small, so
    they are tiled in rows above the other visuals instead, in a band of the page. Cards that do
    not fit in that band at the minimum card height are removed from ``visuals`` and returned.
    """
    cards = [visual for visual in visuals if visual.visual_type == VisualType.CARD]
    others = [visual for visual in visuals if visual.visual_type != VisualType.CARD]
    top, bottom = _CONTENT_Y, _CONTENT_Y + _CONTENT_HEIGHT
    overflow: list[Visual] = []
    if cards:
        per_row = min(len(cards), _CARDS_PER_ROW)
        band = _CONTENT_HEIGHT / 3 if others else _CONTENT_HEIGHT
        max_rows = int((band + _VISUAL_GAP) // (_MIN_CARD_HEIGHT + _VISUAL_GAP))
        cards, overflow = cards[: max_rows * per_row], cards[max_rows * per_row :]
        visuals[:] = [visual for visual in visuals if all(visual is not o for o in overflow)]
        rows = -(-len(cards) // per_row)
        height = min(_CARD_HEIGHT, (band - _VISUAL_GAP * (rows - 1)) / rows)
        width = (_CONTENT_WIDTH - _VISUAL_GAP * (per_row - 1)) / per_row
        for index, card in enumerate(cards):
            card.x = _CONTENT_X + (index % per_row) * (width + _VISUAL_GAP)
            card.y = top + (index // per_row) * (height + _VISUAL_GAP)
            card.width = width
            card.height = height
        top += rows * (height + _VISUAL_GAP)
    elif len(others) < 2:
        return overflow
    if not others:
        return overflow
    height = (bottom - top - _VISUAL_GAP * (len(others) - 1)) / len(others)
    for index, visual in enumerate(others):
        visual.x = _CONTENT_X
        visual.y = top + index * (height + _VISUAL_GAP)
        visual.width = _CONTENT_WIDTH
        visual.height = height
    return overflow


def _data_store_query(store: etree._Element) -> str | None:
    """Return the query a Cognos report data store reads, from its ``dsSource``."""
    source = store.find("dsSource")
    if source is None:
        return None
    return next(
        (
            element.get("refQuery")
            for element in source.iter()
            if isinstance(element.tag, str) and element.get("refQuery")
        ),
        None,
    )


def _data_store_items(store: etree._Element) -> set[str]:
    """Return the query data items a report data store exposes (its ``dsV5DataItem`` entries)."""
    source = store.find("dsSource")
    if source is None:
        return set()
    return {
        element.get("refDataItem")
        for element in source.iter()
        if isinstance(element.tag, str)
        and element.tag.endswith("DataItem")
        and element.get("refDataItem")
    }


def _viz_control_type(obj: etree._Element) -> str:
    """Return a vizControl's type without the ``com.ibm.vis.`` prefix of the built-in types."""
    raw = (obj.get("type") or "").strip()
    return raw[len(_VIZ_CONTROL_PREFIX) :] if raw.startswith(_VIZ_CONTROL_PREFIX) else raw


def _interactions(obj: etree._Element) -> list[str]:
    """Return the outermost interactivity elements (``ib...``) of a visualization, by tag."""
    found: list[str] = []
    for element in obj.iter():
        tag = element.tag
        if not (isinstance(tag, str) and tag.startswith("ib") and tag[2:3].isupper()):
            continue
        parent = element.getparent()
        nested = parent is not None and isinstance(parent.tag, str) and parent.tag.startswith("ib")
        if not nested and tag not in found:
            found.append(tag)
    return found


def _viz_aggregation(table: Table, item: str) -> Literal["sum", "count"] | None:
    """Return how a column plotted as a chart value is summarized: summed if numeric, else counted.

    None for a measure, which aggregates itself.
    """
    if any(measure.name == item for measure in table.measures):
        return None
    column = table.column(item)
    return "sum" if column is not None and column.data_type in _NUMERIC_TYPES else "count"


def _effective_aggregate(data_item: etree._Element) -> str | None:
    """Return how a query data item aggregates (``total``, ``average``, ...), if known."""
    aggregate = (data_item.get("aggregate") or "").strip().lower()
    if aggregate and aggregate not in {"none", "automatic", "calculated", "summarize"}:
        return _ROLLUP_NAMES.get(aggregate, aggregate)
    expression = data_item.findtext("expression") or ""
    summary = _simple_summary(expression.strip())
    return _ROLLUP_NAMES.get(summary[0], summary[0]) if summary else None


def _simple_summary(expression: str) -> tuple[str, str] | None:
    """Split ``func([distinct] [ref])`` into ``(aggregate, ref)``; None for anything richer."""
    match = _SIMPLE_SUMMARY_RE.match(expression)
    if not match:
        return None
    func = match.group("func").lower()
    if match.group("distinct"):
        if func != "count":
            return None
        func = "countdistinct"
    if func not in _SUMMARY_FUNCTIONS:
        return None
    return func, match.group("ref")


def _list_separator(root: etree._Element) -> tuple[str, bool]:
    """Return the function-argument separator of the report's ``expressionLocale``, and whether
    the locale was recognized.

    A missing, empty or unrecognized locale assumes ``","``. That fails safe for expressions
    written with ``;``: a ``;`` left in a translation is invalid DAX, so the translator reports
    it as not confident (and the item is flagged for review) rather than emitting wrong DAX.
    A comma between two digits is not safe under that assumption; see ``_QueryContext``.
    """
    locale = (root.get("expressionLocale") or "").strip().lower().replace("_", "-")
    parts = locale.split("-")
    language = _LANGUAGE_ALIASES.get(parts[0], parts[0])
    # Skip a script subtag (sr-latn-rs); a region is two letters or three digits (es-419).
    region = next((part for part in parts[1:] if len(part) == 2 or part.isdigit()), None)
    key = f"{language}-{region}" if region else language
    if key in _SEMICOLON_REGIONS:
        return ";", True
    if key in _COMMA_REGIONS:
        return ",", True
    if language in _SEMICOLON_LANGUAGES:
        return ";", True
    return ",", language in _POINT_LANGUAGES


def _mask_opaque(expression: str) -> str:
    """Blank out string literals, comments and references, keeping every offset in place."""
    return _LITERAL_COMMENT_OR_REFERENCE_RE.sub(lambda match: " " * len(match.group()), expression)


def _expression_separator(expression: str, locale_separator: str) -> str:
    """Return the list separator to translate one expression with.

    IBM documents ``;`` as a list separator and ``.`` as a decimal separator in any locale; only
    ``,`` depends on the locale. An expression with a ``;`` but no ``,`` therefore means the same
    under every locale, and is read with ``;`` even when the locale says ``,``. An expression
    with both keeps the locale's separator, so under a ``,`` locale its ``;`` stays and fails safe.
    """
    masked = _mask_opaque(expression)
    if ";" in masked and "," not in masked:
        return ";"
    return locale_separator


@dataclass
class _TranslationNotes:
    """Semantic caveats of confident translations, collected per report for one flag each."""

    # Severity -> note text -> item labels.
    notes: dict[Severity, dict[str, list[str | None]]] = field(default_factory=dict)

    def add(self, severity: Severity, note: str | None, table: str, item: str) -> None:
        if note:
            self.notes.setdefault(severity, {}).setdefault(note, []).append(f"{table}[{item}]")

    def flag(self, project: MigrationProject) -> None:
        """Add one ``calculation-translation-note`` flag per severity."""
        lead = {
            Severity.WARNING: (
                "calculated column(s)",
                "Use a measure if the value must follow the time the report runs.",
            ),
            Severity.INFO: (
                "measure(s)",
                "Shift the clock if the report must show local time.",
            ),
        }
        for severity in (Severity.WARNING, Severity.INFO):
            by_note = self.notes.get(severity)
            if not by_note:
                continue
            kind, advice = lead[severity]
            items = [item for labels in by_note.values() for item in labels]
            caveats = " ".join(f"{note}." for note in by_note)
            project.add_flag(
                "calculation-translation-note",
                f"{len(items)} {kind} ({_quoted_names(items)}) were translated to DAX with a "
                f"semantic caveat. Caveat: {caveats} {advice}",
                severity,
            )


def _call_arguments(expression: str, start: int, separator: str) -> list[str] | None:
    """Split the arguments of the call whose ``(`` is at ``start``; None if it never closes."""
    arguments: list[str] = []
    depth = 0
    current = start + 1
    position = start
    while position < len(expression):
        char = expression[position]
        if char in "'[":
            close = expression.find("'" if char == "'" else "]", position + 1)
            # A doubled quote escapes a quote; skipping both halves keeps the scan in step.
            position = len(expression) if close < 0 else close + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                arguments.append(expression[current:position])
                return arguments
        elif char == separator and depth == 1:
            arguments.append(expression[current:position])
            current = position + 1
        position += 1
    return None


def _closing_paren(expression: str, start: int) -> int | None:
    """Return the index of the ``)`` that closes the ``(`` at ``start``, outside literals."""
    masked = _mask_opaque(expression)
    depth = 0
    for position in range(start, len(masked)):
        if masked[position] == "(":
            depth += 1
        elif masked[position] == ")":
            depth -= 1
            if depth == 0:
                return position
    return None


def _if_branches(text: str) -> list[str] | None:
    """Split ``if (c) then (a) else (b)`` filling all of ``text`` into ``[a, b]``."""
    match = _IF_OPEN_RE.match(text)
    if match is None:
        return None
    close = _closing_paren(text, match.end() - 1)
    branches: list[str] = []
    for keyword in (_THEN_OPEN_RE, _ELSE_OPEN_RE):
        match = keyword.match(text, close + 1) if close is not None else None
        if match is None:
            return None
        close = _closing_paren(text, match.end() - 1)
        if close is None:
            return None
        branches.append(text[match.end() : close])
    return branches if close is not None and not text[close + 1 :].strip() else None


def _case_branches(text: str) -> list[str] | None:
    """Return the ``then``/``else`` results of a ``case ... end`` filling all of ``text``."""
    branches: list[str] = []
    depth = nesting = 0
    start: int | None = None
    for token in _CASE_TOKEN_RE.finditer(_mask_opaque(text)):
        word = token.group().lower()
        if word in ("(", ")"):
            depth += 1 if word == "(" else -1
            continue
        if depth:
            continue
        if word == "case":
            if nesting == 0 and token.start() != 0:
                return None
            nesting += 1
        elif nesting == 0:
            return None
        elif nesting == 1 and word in ("when", "then", "else", "end"):
            if start is not None:
                branches.append(text[start : token.start()])
            start = token.end() if word in ("then", "else") else None
        if word == "end":
            nesting -= 1
            if nesting == 0:
                return branches if token.end() == len(text) else None
    return None


def _value_branches(expression: str) -> list[str]:
    """Return the sub-expressions that can be the value of ``expression``.

    Parentheses are looked through, and an if/then/else or case yields the values of each of
    its result branches (never a condition or a case subject).
    """
    text = expression.strip()
    if text.startswith("(") and _closing_paren(text, 0) == len(text) - 1:
        return _value_branches(text[1:-1])
    branches = _if_branches(text)
    if branches is None:
        branches = _case_branches(text)
    if branches is None:
        return [text]
    return [value for branch in branches for value in _value_branches(branch)]


def _date_result_type(expression: str) -> DataType | None:
    """Return the type every value of a recognised Cognos date expression has, if one does."""
    types: set[DataType] = set()
    for value in _value_branches(expression):
        if _DATE_CONSTANT_RE.fullmatch(value):
            types.add(DataType.DATE_TIME)
            continue
        call = _LEADING_CALL_RE.match(value)
        result = _DATE_RESULT_TYPES.get(call.group("name").lower()) if call else None
        if call is None or result is None:
            return None
        if _closing_paren(value, call.end() - 1) != len(value) - 1:
            return None
        types.add(result)
    return types.pop() if len(types) == 1 else None


def _date_argument_types(expression: str, separator: str) -> dict[str, DataType]:
    """Type the source columns a Cognos date function constrains, by casefolded column name.

    A reference that can be the value of a date argument (directly, in parentheses or as a
    branch of a conditional) holds dates; references in a purely arithmetic _add_* amount hold
    numbers.
    """
    types: dict[str, DataType] = {}
    for call in _DATE_FUNCTION_CALL_RE.finditer(expression):
        arguments = _call_arguments(expression, call.end() - 1, separator)
        if not arguments:
            continue
        name = call.group("name").lower()
        dates = arguments if name == "_days_between" else arguments[:1]
        for argument in dates:
            for value in _value_branches(argument):
                if _QUALIFIED_REF_RE.fullmatch(value):
                    types[_last_segment(value).casefold()] = DataType.DATE_TIME
        if name.startswith("_add_") and len(arguments) == 2:
            amount = arguments[1]
            if _ARITHMETIC_RE.fullmatch(_QUALIFIED_REF_RE.sub("0", amount)):
                for ref in _QUALIFIED_REF_RE.findall(amount):
                    types.setdefault(_last_segment(ref).casefold(), DataType.DOUBLE)
    return types


def _type_rank(data_type: DataType) -> int:
    """Rank evidence for a base column type: a date beats a number, a number beats the rest."""
    if data_type == DataType.DATE_TIME:
        return 2
    return 1 if data_type in _NUMERIC_TYPES else 0


def _merge_base_column(
    base_columns: dict[str, tuple[str, DataType]], name: str, source: str, data_type: DataType
) -> None:
    """Record one hidden source column, preferring a date, then a numeric type on disagreement."""
    current = base_columns.get(name)
    if current is None or _type_rank(data_type) > _type_rank(current[1]):
        base_columns[name] = (source, data_type)


@dataclass
class _QueryContext:
    """Per-query facts that let one data item resolve references to its sibling items."""

    measure_items: dict[str, str] = field(default_factory=dict)
    # Casefolded names of items that are calculations or measures, not plain source columns.
    derived_names: set[str] = field(default_factory=set)
    # Casefolded source column -> name of the plain-reference column item that already selects it.
    source_owners: dict[str, str] = field(default_factory=dict)
    # Hidden column name -> (source column, data type) needed by translated DAX.
    base_columns: dict[str, tuple[str, DataType]] = field(default_factory=dict)
    # Plain column item -> the type that translated date functions imply for its values, and
    # the type its own Cognos metadata states (None when it states none).
    owner_types: dict[str, DataType] = field(default_factory=dict)
    explicit_types: dict[str, DataType | None] = field(default_factory=dict)
    # The report's function-argument separator, and its report-wide collector of caveats.
    list_separator: str = ","
    notes: _TranslationNotes = field(default_factory=_TranslationNotes)
    # False when the separator is assumed for an unrecognized locale; the report-wide list of
    # items held for review because their reading depends on that locale.
    locale_known: bool = True
    locale_dependent: list[str] = field(default_factory=list)

    @classmethod
    def from_data_items(cls, data_items: list[etree._Element]) -> _QueryContext:
        context = cls()
        for data_item in data_items:
            name = _sanitize_identifier(data_item.get("name") or "Item")
            aggregate = (data_item.get("aggregate") or "none").strip().lower()
            expression_el = data_item.find("expression")
            expression = _strip_comment_macros(
                expression_el.text if expression_el is not None else None
            )
            if aggregate in _SUMMARY_FUNCTIONS and expression:
                context.measure_items[name] = expression
            plain_column = aggregate in {"none", ""} and _is_reference_like(expression)
            if plain_column and not (expression and _contains_summary_expression(expression)):
                source = _reference_source(expression, name)
                context.source_owners.setdefault(source.casefold(), name)
                context.explicit_types.setdefault(name, _explicit_data_type(data_item, expression))
            else:
                context.derived_names.add(name.casefold())
        return context

    def separator_for(self, expression: str) -> str:
        return _expression_separator(expression, self.list_separator)

    def depends_on_locale(self, expression: str, table: str, item: str) -> bool:
        """Whether ``expression`` reads differently by locale while the locale is unknown.

        Under an unrecognized locale ``coalesce(2,5)`` is two arguments or the decimal 2.5, so
        it is recorded and must go to review. A comma after ``]`` or next to a space cannot be a
        decimal comma, and a ``;``-only expression means the same in every locale. Mixed ``;``
        and ``,`` keeps the assumed ``,``, whose stray ``;`` already sends the item to review.
        """
        if self.locale_known or not _POSSIBLE_DECIMAL_COMMA_RE.search(_mask_opaque(expression)):
            return False
        self.locale_dependent.append(f"{table}[{item}]")
        return True

    def redirect_source_references(
        self, expression: str, data_type: DataType
    ) -> tuple[str, dict[str, tuple[str, DataType]], dict[str, DataType]]:
        """Point qualified source references at model columns, without clashing with items.

        A reference whose source column is already selected by a plain item reuses that column.
        Otherwise it needs a hidden base column, named ``<column> (source)`` when the plain name
        belongs to a measure or calculation (which would otherwise reference itself). Returns the
        rewritten expression, the base columns it needs and the types date functions imply for
        the reused plain columns.
        """
        needed: dict[str, tuple[str, DataType]] = {}
        owners: dict[str, tuple[str, DataType]] = {}
        implied_types = _date_argument_types(expression, self.separator_for(expression))

        def repl(match: re.Match[str]) -> str:
            ref = match.group("ref")
            if ref is None or "].[" not in ref:
                return match.group(0)
            source = _last_segment(ref)
            implied = implied_types.get(source.casefold())
            # A date function's signature says more than the type of the whole calculation.
            if implied in _NUMERIC_TYPES and data_type in _NUMERIC_TYPES:
                implied = data_type
            owner = self.source_owners.get(source.casefold())
            if owner is not None:
                if implied is not None:
                    _merge_base_column(owners, owner, source, implied)
                return f"[{owner}]"
            name = f"{source} (source)" if source.casefold() in self.derived_names else source
            _merge_base_column(needed, name, source, implied or data_type)
            return f"[{name}]"

        rewritten = _LITERAL_COMMENT_OR_REFERENCE_RE.sub(repl, expression)
        return rewritten, needed, {owner: implied for owner, (_, implied) in owners.items()}

    def add_base_columns(
        self,
        needed: dict[str, tuple[str, DataType]],
        owner_types: dict[str, DataType] | None = None,
    ) -> None:
        for owner, data_type in (owner_types or {}).items():
            current = self.owner_types.get(owner)
            if current is None or _type_rank(data_type) > _type_rank(current):
                self.owner_types[owner] = data_type
        for name, (source, data_type) in needed.items():
            _merge_base_column(self.base_columns, name, source, data_type)


# Literal kind of a translated filter -> the type of a hidden column added to read it.
_FILTER_LITERAL_TYPES = {
    "string": DataType.STRING,
    "number": DataType.DOUBLE,
    "date": DataType.DATE_TIME,
}


@dataclass(frozen=True)
class _FilterItem:
    """A query data item as a detail filter sees it."""

    name: str
    aggregate: str
    expression: str | None


class _FilterResolver:
    """Resolve the item a detail filter tests to the model column or measure that holds it.

    Cognos evaluates a detail filter on detail rows unless it is marked ``postAutoAggregation``:
    a filter on an aggregated item then tests the item's detail value (measured on Cognos 12:
    ``[Revenue] > 100000`` on a ``total`` item drops individual rows, not groups). So a
    pre-aggregation filter targets the item's underlying column, and only a post-aggregation
    filter targets the measure.
    """

    def __init__(
        self,
        table: Table,
        context: _QueryContext,
        data_items: list[etree._Element],
        model_source: bool,
    ) -> None:
        self.table = table
        self.context = context
        self.model_source = model_source
        self.items: dict[str, _FilterItem] = {}
        # Casefolded source column -> every qualified path the query's items read it from.
        self.paths_by_source: dict[str, set[str]] = {}
        for data_item in data_items:
            name = _sanitize_identifier(data_item.get("name") or "Item")
            expression_el = data_item.find("expression")
            expression = _strip_comment_macros(
                expression_el.text if expression_el is not None else None
            )
            aggregate = (data_item.get("aggregate") or "").strip().lower()
            self.items.setdefault(name, _FilterItem(name, aggregate, expression))
            for ref in _QUALIFIED_REF_RE.findall(_mask_literals(expression or "")):
                if len(ref_segments(ref)) > 1:
                    source = _last_segment(ref).casefold()
                    self.paths_by_source.setdefault(source, set()).add(item_key(ref))

    def resolve(
        self, reference: str, post_aggregation: bool, literal_kinds: set[str]
    ) -> FilterTarget:
        segments = ref_segments(reference)
        if len(segments) == 1:
            return self._resolve_item(segments[0], post_aggregation)
        if len(segments) > 1:
            return self._resolve_reference(reference, post_aggregation, literal_kinds)
        raise UntranslatableFilter("unresolved-item", reference)

    def _find_item(self, segment: str) -> _FilterItem | None:
        name = _sanitize_identifier(segment)
        item = self.items.get(name)
        if item is not None:
            return item
        matches = [value for key, value in self.items.items() if key.casefold() == name.casefold()]
        return matches[0] if len(matches) == 1 else None

    def _column_target(self, column: Column) -> FilterTarget:
        if column.needs_calculation:
            raise UntranslatableFilter("calculated-item", column.name)
        return FilterTarget(table=self.table.name, name=column.name, data_type=column.data_type)

    def _resolve_item(self, segment: str, post_aggregation: bool) -> FilterTarget:
        item = self._find_item(segment)
        if item is None:
            raise UntranslatableFilter("unresolved-item", f"[{segment}]")
        column = self.table.column(item.name)
        if column is not None:
            # On a grouping (non-aggregated) item the post-aggregation filter keeps the same rows.
            return self._column_target(column)
        measure = next((m for m in self.table.measures if m.name == item.name), None)
        if measure is None:
            raise UntranslatableFilter("unresolved-item", f"[{segment}]")
        if post_aggregation:
            if measure.needs_review:
                raise UntranslatableFilter("post-aggregation", f"measure {item.name} needs review")
            return FilterTarget(
                table=self.table.name,
                name=measure.name,
                kind="measure",
                group_by=[column.name for column in self.table.columns if not column.is_hidden],
            )
        if item.aggregate in {"", "none"}:
            raise UntranslatableFilter("summary-item", item.name)
        return self._base_column(item)

    def _base_column(self, item: _FilterItem) -> FilterTarget:
        """The column holding the detail value of an aggregated item, if it is a single column."""
        expression = (item.expression or f"[{self.table.name}].[{item.name}]").strip()
        if not _SIMPLE_REF_RE.match(expression):
            raise UntranslatableFilter("aggregate-base", item.name)
        if len(ref_segments(expression)) == 1:
            inner = self._find_item(ref_segments(expression)[0])
            column = self.table.column(inner.name) if inner is not None else None
            if column is None or column.is_hidden:
                raise UntranslatableFilter("aggregate-base", item.name)
            return self._column_target(column)
        return self._existing_reference(expression, item.name)

    def _existing_reference(self, reference: str, label: str) -> FilterTarget:
        """The column already selecting a qualified reference: its plain item or a hidden base."""
        target = self._reference_column(reference)
        if target is None:
            raise UntranslatableFilter("aggregate-base", label)
        return target

    def _reference_column(self, reference: str) -> FilterTarget | None:
        path = item_key(reference)
        source = _last_segment(reference)
        if self.paths_by_source.get(source.casefold(), {path}) - {path}:
            # Another path ends in the same column name; columns are matched by name alone.
            raise UntranslatableFilter("unresolved-item", f"{reference} is ambiguous")
        for item in self.items.values():
            if item.aggregate not in {"", "none"} or not item.expression:
                continue
            if _SIMPLE_REF_RE.match(item.expression) and item_key(item.expression) == path:
                column = self.table.column(item.name)
                if column is not None and not column.is_calculated:
                    return self._column_target(column)
        for name in (source, f"{source} (source)"):
            column = self.table.column(name)
            if (
                column is not None
                and column.is_hidden
                and not _is_derived_column(column)
                and (column.source_column or "").casefold() == source.casefold()
            ):
                return self._column_target(column)
        return None

    def _resolve_reference(
        self, reference: str, post_aggregation: bool, literal_kinds: set[str]
    ) -> FilterTarget:
        target = self._reference_column(reference)
        if target is not None:
            return target
        if post_aggregation:
            raise UntranslatableFilter("post-aggregation", f"{reference} is not a grouping item")
        source = _last_segment(reference)
        if not self.model_source or source.casefold() in self.context.source_owners:
            raise UntranslatableFilter("unresolved-item", reference)
        taken = {column.name.casefold() for column in self.table.columns}
        taken |= {measure.name.casefold() for measure in self.table.measures}
        taken |= self.context.derived_names
        name = next(
            (
                candidate
                for candidate in (source, f"{source} (source)")
                if candidate.casefold() not in taken
            ),
            None,
        )
        if name is None or len(literal_kinds) != 1:
            raise UntranslatableFilter("unresolved-item", reference)
        # Not selected by any item: a generator that applies the filter adds a hidden column.
        return FilterTarget(
            table=self.table.name,
            name=name,
            data_type=_FILTER_LITERAL_TYPES[next(iter(literal_kinds))],
            source_column=source,
            add_column=True,
        )


def _is_derived_column(column: Column) -> bool:
    return column.is_calculated or column.needs_calculation


def _mask_literals(expression: str) -> str:
    """Blank out string literals and comments so references inside them are not matched."""
    return re.sub(r"'(?:[^']|'')*'|/\*[\s\S]*?\*/", " ", expression)


class CognosReportParser:
    """Parse a Cognos report specification into a :class:`MigrationProject`."""

    def parse_file(self, path: str | Path) -> MigrationProject:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"Cognos report not found: {source}")
        xml_bytes = source.read_bytes()
        project = self.parse_bytes(xml_bytes, name=source.stem)
        project.source_path = str(source)
        return project

    def parse_bytes(self, xml_bytes: bytes, name: str = "MigratedReport") -> MigrationProject:
        parser = etree.XMLParser(remove_blank_text=True, recover=True, resolve_entities=False)
        root = etree.fromstring(xml_bytes, parser=parser)
        if root is None:
            raise ValueError("Could not parse Cognos report XML: empty or invalid document.")
        _strip_namespaces(root)

        project = MigrationProject(name=_sanitize_identifier(name))
        output_queries = self._collect_output_queries(root)
        self._parse_queries(root, project, output_queries)
        self._parse_prompts(root, project)
        self._parse_layouts(root, project)
        self._apply_data_formats(root, project)

        if not project.tables:
            project.add_flag(
                "no-queries",
                "No queries were found in the report specification.",
                Severity.ERROR,
            )
        if not project.pages:
            project.add_flag(
                "no-pages",
                "No report pages were found; a default page was created.",
                Severity.WARNING,
            )
            project.pages.append(ReportPage(name="Page1", display_name="Page 1", visuals=[]))
        return project

    def _collect_output_queries(self, root: etree._Element) -> set[str]:
        """Return the names of queries bound to a layout object (list, crosstab, or chart).

        These are the report's *output* queries. Every ``refQuery`` under a report page marks the
        query it references as user-facing output; queries referenced only by other queries (join
        operands, union operands, ``queryRef`` sources) or by prompt pages are detail queries
        instead.
        """
        output: set[str] = set()
        for report_pages in root.iter("reportPages"):
            for element in report_pages.iter():
                if not isinstance(element.tag, str):
                    continue
                ref = element.get("refQuery")
                if ref:
                    output.add(_sanitize_identifier(ref))
        # A visualization (vizControl) reads its query through a report data store.
        stores = {store.get("name"): store for store in root.iter("reportDataStore")}
        for report_pages in root.iter("reportPages"):
            for data_set in report_pages.iter("vcDataSet"):
                store = stores.get(data_set.get("refDataStore"))
                query = _data_store_query(store) if store is not None else None
                if query:
                    output.add(_sanitize_identifier(query))
        return output

    def _parse_queries(
        self, root: etree._Element, project: MigrationProject, output_queries: set[str]
    ) -> None:
        package_flagged = False
        list_separator, locale_known = _list_separator(root)
        notes = _TranslationNotes()
        locale_dependent: list[str] = []
        for query in root.iter("query"):
            query_name = _sanitize_identifier(query.get("name") or "Query")
            table = Table(name=query_name, source_query=query_name)
            data_items = list(query.iter("dataItem"))
            context = _QueryContext.from_data_items(data_items)
            context.list_separator = list_separator
            context.notes = notes
            context.locale_known = locale_known
            context.locale_dependent = locale_dependent
            for data_item in data_items:
                self._parse_data_item(data_item, table, project, context)
            self._add_base_columns(table, context.base_columns, project)
            self._type_reused_columns(table, context, project)
            if table.columns or table.measures:
                project.tables.append(table)
            self._classify_query(query, query_name, output_queries, project)
            package_flagged = self._parse_query_source(query, table, project, package_flagged)
            self._parse_detail_filters(query, table, project, context, data_items)
        notes.flag(project)
        if locale_dependent:
            locale = (root.get("expressionLocale") or "").strip()
            stated = (
                f"expressionLocale '{locale}' is not recognized"
                if locale
                else "The report states no expressionLocale"
            )
            project.add_flag(
                "expression-locale-unrecognized",
                f"{stated}, so ',' was assumed as the list separator. {len(locale_dependent)} "
                f"item(s) ({_quoted_names([*locale_dependent])}) have a comma between two digits, "
                "which is a decimal comma in a decimal-comma locale, and were held for review. "
                "Confirm the locale the report was authored in.",
                Severity.INFO,
            )

    def _classify_query(
        self,
        query: etree._Element,
        query_name: str,
        output_queries: set[str],
        project: MigrationProject,
    ) -> None:
        """Add this query to the project's query graph as a node plus any join/union/ref edges."""
        graph = project.query_graph
        is_output = query_name in output_queries
        role = QueryRole.UNKNOWN
        source = query.find("source")
        if source is not None:
            if source.find("joinOperation") is not None:
                role = QueryRole.JOIN
                self._add_join_edges(source.find("joinOperation"), query_name, graph)
            elif self._union_source(source) is not None:
                role = QueryRole.UNION
                self._add_union_edges(self._union_source(source), query_name, graph)
            elif source.find("queryRef") is not None:
                role = QueryRole.REFERENCE
                ref = _sanitize_identifier(source.find("queryRef").get("refQuery") or "")
                if ref:
                    graph.edges.append(
                        QueryEdge(from_query=query_name, to_query=ref, kind=QueryEdgeKind.REFERENCE)
                    )
        if role == QueryRole.UNKNOWN:
            role = QueryRole.OUTPUT if is_output else QueryRole.DETAIL
        graph.nodes.append(QueryNode(name=query_name, role=role, is_output=is_output))

    @staticmethod
    def _union_source(source: etree._Element) -> etree._Element | None:
        """Return the set-operation element of a source, if the query is a union query."""
        for tag in _UNION_SOURCE_TAGS:
            element = source.find(tag)
            if element is not None:
                return element
        return None

    def _add_join_edges(self, join_op: etree._Element, query_name: str, graph: QueryGraph) -> None:
        """Record a graph edge from the join query to each query it joins, plus the condition."""
        operands: list[str] = []
        for operand in join_op.iter("joinOperand"):
            query_ref = operand.find("queryRef")
            if query_ref is not None and query_ref.get("refQuery"):
                operands.append(_sanitize_identifier(query_ref.get("refQuery")))
        conditions: list[str] = []
        for join_filter in join_op.iter("joinFilter"):
            expression = join_filter.find("filterExpression")
            if expression is not None and expression.text and expression.text.strip():
                conditions.append(expression.text.strip())
        condition = "; ".join(conditions) or None
        for operand in operands:
            graph.edges.append(
                QueryEdge(
                    from_query=query_name,
                    to_query=operand,
                    kind=QueryEdgeKind.JOIN,
                    condition=condition,
                )
            )

    def _add_union_edges(
        self, union_op: etree._Element, query_name: str, graph: QueryGraph
    ) -> None:
        """Record a graph edge from the union query to each operand query it combines."""
        seen: set[str] = set()
        for element in union_op.iter():
            if not isinstance(element.tag, str):
                continue
            ref = element.get("refQuery")
            if ref:
                operand = _sanitize_identifier(ref)
                if operand not in seen:
                    seen.add(operand)
                    graph.edges.append(
                        QueryEdge(from_query=query_name, to_query=operand, kind=QueryEdgeKind.UNION)
                    )

    def _parse_query_source(
        self,
        query: etree._Element,
        table: Table,
        project: MigrationProject,
        package_flagged: bool,
    ) -> bool:
        """Parse a query source: capture joins as relationships; flag derived/package sources."""
        source = query.find("source")
        if source is None:
            return package_flagged
        join_op = source.find("joinOperation")
        if join_op is not None:
            self._parse_join(join_op, table, project)
            return package_flagged
        union_op = self._union_source(source)
        if union_op is not None:
            operands = [
                edge.to_query
                for edge in project.query_graph.edges
                if edge.from_query == table.name and edge.kind == QueryEdgeKind.UNION
            ]
            joined = ", ".join(operands) if operands else "its operand queries"
            project.add_flag(
                "union-query",
                f"Query '{table.name}' is a Cognos union/set operation over {joined}. It was "
                "materialized as its own table; recreate the union with Power Query "
                "Table.Combine (append) or a DAX UNION, keeping column order and types aligned.",
                Severity.WARNING,
            )
            return package_flagged
        query_ref = source.find("queryRef")
        if query_ref is not None:
            ref = _sanitize_identifier(query_ref.get("refQuery") or "source")
            project.add_flag(
                "derived-query",
                f"Query '{table.name}' is derived from query '{ref}' (a Cognos query reference). "
                "It was materialized as its own table; relate or replace it if you need a single "
                "source of truth.",
                Severity.INFO,
            )
            return package_flagged
        if source.find("model") is not None and not package_flagged:
            project.add_flag(
                "package-source",
                "The report binds to a Cognos package/model rather than a physical table. The "
                "generated tables use parameterized Server/Database placeholders; point each "
                "partition at the real table or view before refreshing.",
                Severity.WARNING,
            )
            return True
        return package_flagged

    def _parse_join(self, join_op: etree._Element, table: Table, project: MigrationProject) -> None:
        cardinalities: dict[str, str] = {}
        for operand in join_op.iter("joinOperand"):
            query_ref = operand.find("queryRef")
            if query_ref is not None:
                ref_name = _sanitize_identifier(query_ref.get("refQuery") or "")
                cardinalities[ref_name] = (operand.get("cardinality") or "").strip()
        for join_filter in join_op.iter("joinFilter"):
            expression = join_filter.find("filterExpression")
            text = expression.text.strip() if expression is not None and expression.text else ""
            self._relationship_from_join(text, cardinalities, table, project)

    def _relationship_from_join(
        self,
        filter_text: str,
        cardinalities: dict[str, str],
        table: Table,
        project: MigrationProject,
    ) -> None:
        match = _JOIN_EQUALITY_RE.match(filter_text)
        if not match:
            if filter_text:
                project.add_flag(
                    "join-needs-review",
                    f"The join for query '{table.name}' uses a condition that could not be mapped "
                    "to a Power BI relationship and needs manual modeling.",
                    Severity.WARNING,
                    source_ref=filter_text,
                )
            return
        left, right = match.group("left"), match.group("right")
        left_parts = re.findall(r"\[([^\[\]]+)\]", left)
        right_parts = re.findall(r"\[([^\[\]]+)\]", right)
        if len(left_parts) < 2 or len(right_parts) < 2:
            return
        left_table = _sanitize_identifier(left_parts[0])
        right_table = _sanitize_identifier(right_parts[0])
        left_col = _sanitize_identifier(left_parts[-1])
        right_col = _sanitize_identifier(right_parts[-1])
        left_many = _cardinality_is_many(cardinalities.get(left_table, ""))
        right_many = _cardinality_is_many(cardinalities.get(right_table, ""))
        if right_many and not left_many:
            from_table, from_col, to_table, to_col = right_table, right_col, left_table, left_col
        else:
            from_table, from_col, to_table, to_col = left_table, left_col, right_table, right_col
        cardinality = (
            Cardinality.MANY_TO_ONE if (left_many or right_many) else Cardinality.ONE_TO_ONE
        )
        project.relationships.append(
            Relationship(
                from_table=from_table,
                from_column=from_col,
                to_table=to_table,
                to_column=to_col,
                cardinality=cardinality,
                name=f"{from_table}_{to_table}",
            )
        )
        project.add_flag(
            "join-relationship",
            f"Added a relationship {from_table}[{from_col}] -> {to_table}[{to_col}] from the "
            f"Cognos join in query '{table.name}'. Verify the cardinality and cross-filter "
            "direction in Power BI.",
            Severity.INFO,
        )

    def _parse_detail_filters(
        self,
        query: etree._Element,
        table: Table,
        project: MigrationProject,
        context: _QueryContext | None = None,
        data_items: list[etree._Element] | None = None,
    ) -> None:
        """Extract each Cognos detail filter with its ``use`` semantics into a structured record.

        The ``use`` attribute drives how Cognos applies a filter:

        - no ``use`` attribute -> mandatory (always applied),
        - ``use="optional"`` -> applied only when its prompt value is supplied,
        - ``use="prohibited"`` -> defined but disabled.

        Each filter becomes a :class:`QueryFilter` on the project (with any referenced prompt
        parameters) and a matching review flag whose wording reflects the ``use`` semantics. A
        mandatory filter is also translated, when it can be, into target-neutral clauses that a
        generator may apply; the flag then stays until a generator actually applies it.
        """
        resolver = _FilterResolver(
            table,
            context or _QueryContext(),
            data_items if data_items is not None else list(query.iter("dataItem")),
            model_source=query.find("source/model") is not None,
        )
        for detail_filters in query.findall("detailFilters"):
            for detail_filter in detail_filters.iter("detailFilter"):
                expression = detail_filter.find("filterExpression")
                text = expression.text.strip() if expression is not None and expression.text else ""
                if not text:
                    continue
                use = FilterUse.from_attribute(detail_filter.get("use"))
                parameters = self._filter_parameters(text)
                record = QueryFilter(
                    query=table.name,
                    expression=text,
                    use=use,
                    parameters=parameters,
                    post_aggregation=(detail_filter.get("postAutoAggregation") or "")
                    .strip()
                    .lower()
                    == "true",
                )
                if use is FilterUse.REQUIRED:
                    self._translate_detail_filter(record, resolver)
                project.filters.append(record)
                self._flag_detail_filter(table.name, text, use, project, record.untranslated_reason)

    @staticmethod
    def _translate_detail_filter(record: QueryFilter, resolver: _FilterResolver) -> None:
        """Fill in the clauses of a mandatory filter, or why it cannot be translated."""
        try:
            parsed = parse_filter(record.expression)
            clauses = [
                FilterClause(
                    item=clause.item,
                    target=resolver.resolve(
                        clause.item,
                        record.post_aggregation,
                        {value.kind for value in literal_values(clause.condition)},
                    ),
                    condition=clause.condition,
                    keeps_null=clause.keeps_null,
                )
                for clause in parsed
            ]
        except UntranslatableFilter as error:
            record.untranslated_code = error.code
            record.untranslated_reason = error.reason
            return
        record.clauses = clauses

    @staticmethod
    def _filter_parameters(expression: str) -> list[str]:
        """Return the distinct prompt parameter names (``?p_x?``) referenced by an expression."""
        seen: list[str] = []
        for match in _PARAM_REF_RE.findall(expression):
            name = match.strip()
            if name and name not in seen:
                seen.append(name)
        return seen

    @staticmethod
    def _flag_detail_filter(
        query_name: str,
        text: str,
        use: FilterUse,
        project: MigrationProject,
        untranslated_reason: str | None = None,
    ) -> None:
        if use is FilterUse.PROHIBITED:
            project.add_flag(
                "detail-filter-prohibited",
                f"Query '{query_name}' has a Cognos detail filter marked use=\"prohibited\" "
                "(disabled in the source). It was captured but not applied; leave it out unless "
                "you intend to re-enable it.",
                Severity.INFO,
                source_ref=text,
            )
            return
        if use is FilterUse.OPTIONAL:
            project.add_flag(
                "detail-filter-optional",
                f"Query '{query_name}' has an optional Cognos detail filter (applied only when "
                "its prompt value is supplied). Recreate it as a parameter-driven Power Query step "
                "or a report/page filter tied to the corresponding parameter.",
                Severity.WARNING,
                source_ref=text,
            )
            return
        project.add_flag(
            "detail-filter",
            not_applied_message(query_name, untranslated_reason),
            Severity.WARNING,
            source_ref=text,
        )

    def _parse_prompts(self, root: etree._Element, project: MigrationProject) -> None:
        """Extract Cognos prompt metadata from ``<promptPages>`` into structured :class:`Prompt`s.

        Any element inside a prompt page that carries a ``parameter`` attribute is treated as a
        prompt control. The control tag classifies the control/prompt type and implied data type;
        attributes and child elements supply the caption, required/multi-select flags, the query
        that supplies selectable values, and default selections. The parameter name is linked back
        to the detail filters that reference it so downstream generators can wire them together.
        """
        seen: set[str] = set()
        for prompt_pages in root.iter("promptPages"):
            for element in prompt_pages.iter():
                if not isinstance(element.tag, str):
                    continue
                param = element.get("parameter")
                if not param:
                    continue
                tag = element.tag
                if tag not in _PROMPT_CONTROLS and not tag.startswith("select"):
                    continue
                param_name = param.strip()
                if not param_name or param_name in seen:
                    continue
                seen.add(param_name)
                project.prompts.append(self._build_prompt(element, param_name, tag, project))
        if project.prompts:
            project.add_flag(
                "prompt-parameters",
                f"Extracted {len(project.prompts)} Cognos prompt parameter(s) from the prompt "
                "pages. Recreate them as RDL ReportParameters or Power BI parameters/slicers; see "
                "the migration metadata for control type, source query, and defaults.",
                Severity.INFO,
            )

    def _build_prompt(
        self,
        element: etree._Element,
        param_name: str,
        tag: str,
        project: MigrationProject,
    ) -> Prompt:
        control_type, data_type = _PROMPT_CONTROLS.get(
            tag, (PromptControlType.SELECT_VALUE, DataType.STRING)
        )
        required = (element.get("required") or "true").strip().lower() != "false"
        multi_select = (
            element.get("multiSelect") or ""
        ).strip().lower() == "true" or tag in _MULTI_SELECT_CONTROLS
        range_prompt = (element.get("range") or "").strip().lower() == "true"
        caption = element.get("caption") or None

        values_query: str | None = None
        value_column: str | None = None
        display_column: str | None = None
        default_values: list[str] = []
        for desc in element.iter():
            if not isinstance(desc.tag, str):
                continue
            ref = desc.get("refQuery")
            if ref and values_query is None:
                values_query = _sanitize_identifier(ref)
            if desc.tag == "useItem" and desc.get("refDataItem") and value_column is None:
                value_column = _sanitize_identifier(desc.get("refDataItem"))
            if desc.tag == "displayItem" and desc.get("refDataItem") and display_column is None:
                display_column = _sanitize_identifier(desc.get("refDataItem"))
            if desc.tag in {"defaultValue", "useValue"}:
                default = (desc.get("useValue") or desc.text or "").strip()
                if default and default not in default_values:
                    default_values.append(default)

        source_query = next((f.query for f in project.filters if param_name in f.parameters), None)
        return Prompt(
            parameter_name=param_name,
            control_type=control_type,
            data_type=data_type,
            caption=caption,
            required=required,
            multi_select=multi_select,
            source_query=source_query,
            values_query=values_query,
            value_column=value_column,
            display_column=display_column,
            default_values=default_values,
            range_prompt=range_prompt,
        )

    @staticmethod
    def _add_base_columns(
        table: Table, base_columns: dict[str, tuple[str, DataType]], project: MigrationProject
    ) -> None:
        """Materialize hidden source columns that translated DAX references but no item selects."""
        existing = {column.name.casefold() for column in table.columns}
        added: list[str] = []
        for name, (source, data_type) in base_columns.items():
            if name.casefold() in existing:
                continue
            table.columns.append(
                Column(name=name, data_type=data_type, source_column=source, is_hidden=True)
            )
            existing.add(name.casefold())
            added.append(name)
        if added:
            project.add_flag(
                "base-column-added",
                f"Added hidden source column(s) {', '.join(added)} to '{table.name}' because its "
                "translated DAX reads them. Provide them in the source query or synthetic data.",
                Severity.INFO,
            )

    @staticmethod
    def _type_reused_columns(
        table: Table, context: _QueryContext, project: MigrationProject
    ) -> None:
        """Type plain columns that translated date functions read, unless metadata types them.

        Stated Cognos metadata wins; when it says a column passed as a date is not a date, the
        confident DAX does date arithmetic on it, so that is flagged instead of overridden.
        """
        conflicts: list[str | None] = []
        for column in table.columns:
            implied = context.owner_types.get(column.name)
            if implied is None or column.is_calculated or column.needs_calculation:
                continue
            explicit = context.explicit_types.get(column.name)
            if explicit is None:
                if _type_rank(implied) > _type_rank(column.data_type):
                    column.data_type = implied
            elif implied == DataType.DATE_TIME and explicit != DataType.DATE_TIME:
                conflicts.append(f"{table.name}[{column.name}] ({explicit.value})")
        if conflicts:
            project.add_flag(
                "date-argument-type-conflict",
                f"{len(conflicts)} column(s) ({_quoted_names(conflicts)}) are passed to Cognos "
                "date functions in translated DAX, but their Cognos metadata declares another "
                "type, which was kept. Check that the source holds dates; DAX date arithmetic on "
                "a number or text gives a wrong result or a refresh error.",
                Severity.WARNING,
            )

    def _parse_data_item(
        self,
        data_item: etree._Element,
        table: Table,
        project: MigrationProject,
        context: _QueryContext | None = None,
    ) -> None:
        item_name = _sanitize_identifier(data_item.get("name") or "Item")
        aggregate = (data_item.get("aggregate") or "none").strip().lower()
        expression_el = data_item.find("expression")
        cognos_expression = (
            expression_el.text.strip() if expression_el is not None and expression_el.text else None
        )
        # Keep the original text for traceability; decide on the macro-free expression.
        expression = _strip_comment_macros(cognos_expression)
        data_type = _infer_data_type(data_item, expression)
        if context is None:
            context = _QueryContext.from_data_items([data_item])

        if aggregate not in {"none", ""}:
            self._add_measure(
                item_name,
                cognos_expression,
                aggregate,
                table,
                project,
                translation_source=expression,
                data_type=data_type,
                context=context,
            )
            return
        simple_summary = _simple_summary(expression) if expression else None
        if simple_summary:
            summary_aggregate, summary_reference = simple_summary
            self._add_measure(
                item_name,
                cognos_expression,
                summary_aggregate,
                table,
                project,
                translation_source=summary_reference,
                data_type=data_type,
                context=context,
            )
            return
        if expression and _contains_summary_expression(expression):
            # The expression, not only the optional aggregate attribute, can define a measure.
            # Keep unimplemented scoped/nested aggregates visible as reviewable placeholders.
            table.measures.append(
                Measure(
                    name=item_name,
                    cognos_expression=cognos_expression,
                    needs_review=True,
                )
            )
            project.add_flag(
                "measure-needs-review",
                f"Summary data item '{item_name}' in '{table.name}' has no aggregate attribute. "
                "It was bound as a measure; its expression still requires DAX translation."
                f"{_placeholder_measure_note(None)}",
                source_ref=cognos_expression,
            )
            return

        # A plain reference (or cast of a reference) becomes a physical column.
        if _is_reference_like(expression):
            table.columns.append(
                Column(
                    name=item_name,
                    data_type=data_type,
                    source_column=_reference_source(expression, item_name),
                    cognos_expression=cognos_expression,
                )
            )
            return

        if data_item.get("aggregate") is None and _references_items(
            expression or "", context.measure_items
        ):
            # Automatic aggregation over aggregated items is computed after aggregation in
            # Cognos; as a calculated column it would read measures row by row.
            project.add_flag(
                "automatic-aggregate-measure",
                f"Calculation '{item_name}' in '{table.name}' has automatic aggregation and uses "
                "aggregated items, so it was translated as a measure that aggregates its operands.",
                Severity.INFO,
                source_ref=cognos_expression,
            )
            self._add_measure(
                item_name,
                cognos_expression,
                "total",
                table,
                project,
                translation_source=expression,
                data_type=data_type,
                context=context,
            )
            return

        # A calculation. Emit a DAX calculated column only when the deterministic translation is
        # confident. Otherwise flag it; the generator renders it as a BLANK() calculated column with
        # the Cognos source commented, so the model loads (the AI stage may translate it later).
        base_type = data_type if data_type in _NUMERIC_TYPES else DataType.STRING
        redirected, needed, owner_types = context.redirect_source_references(
            expression or "", base_type
        )
        translation = translate_measure_expression(
            redirected, table.name, "none", list_separator=context.separator_for(redirected)
        )
        # A calculated column that reads its own name is a circular dependency that blocks load.
        self_reference = bool(
            translation.dax
            and f"{table.name}[{item_name}]".casefold() in translation.dax.casefold()
        )
        confident = translation.confident and not self_reference
        if confident and context.depends_on_locale(redirected, table.name, item_name):
            confident = False
        if confident and translation.dax:
            context.add_base_columns(needed, owner_types)
            context.notes.add(Severity.WARNING, translation.note, table.name, item_name)
            if _explicit_data_type(data_item, expression) is None:
                data_type = _date_result_type(expression or "") or data_type
            table.columns.append(
                Column(
                    name=item_name,
                    data_type=data_type,
                    cognos_expression=cognos_expression,
                    dax_expression=translation.dax,
                    is_calculated=True,
                )
            )
            return
        project.add_flag(
            "calculation-needs-review",
            f"Data item '{item_name}' in query '{table.name}' is a Cognos calculation that has no "
            "deterministic DAX mapping. It was emitted as a BLANK() calculated column with the "
            "Cognos expression as a comment so the model loads; translate it to DAX (or run AI "
            "refinement).",
            Severity.WARNING,
            source_ref=cognos_expression,
        )
        table.columns.append(
            Column(
                name=item_name,
                data_type=data_type,
                cognos_expression=cognos_expression,
                needs_calculation=True,
            )
        )

    def _add_measure(
        self,
        item_name: str,
        cognos_expression: str | None,
        aggregate: str,
        table: Table,
        project: MigrationProject,
        format_string: str | None = None,
        *,
        translation_source: str | None = None,
        data_type: DataType = DataType.STRING,
        context: _QueryContext | None = None,
    ) -> None:
        if context is None:
            context = _QueryContext()
        # A measure without an expression aggregates the source column of the same name.
        source = translation_source or f"[{table.name}].[{item_name}]"
        expansion_complete = True
        if translation_source and aggregate in _SUMMARY_FUNCTIONS:
            # Cognos computes a detail aggregate per row with sibling items inlined, then totals.
            source, expansion_complete = _expand_measure_references(
                source, context.measure_items, frozenset({item_name})
            )
        if aggregate in {"count", "countdistinct"}:
            column_type = DataType.STRING
        elif data_type in _NUMERIC_TYPES or data_type == DataType.DATE_TIME:
            column_type = data_type
        else:
            column_type = DataType.DOUBLE
        source, needed, owner_types = context.redirect_source_references(source, column_type)
        context.add_base_columns(needed)
        translation = translate_measure_expression(
            source, table.name, aggregate, list_separator=context.separator_for(source)
        )
        needs_review = not translation.confident or not expansion_complete
        if not needs_review and context.depends_on_locale(source, table.name, item_name):
            needs_review = True
        if not needs_review:
            context.add_base_columns({}, owner_types)
            context.notes.add(Severity.INFO, translation.note, table.name, item_name)
        if not expansion_complete:
            project.add_flag(
                "measure-needs-review",
                f"Measure '{item_name}' in '{table.name}' references data items in a cycle; the "
                f"cycle was left unexpanded.{_placeholder_measure_note(translation.dax)}",
                Severity.WARNING,
                source_ref=cognos_expression,
            )
        elif needs_review:
            project.add_flag(
                "measure-needs-review",
                f"Measure '{item_name}' uses a Cognos expression that needs review after "
                "deterministic translation to DAX."
                f"{_placeholder_measure_note(translation.dax)}",
                Severity.WARNING,
                source_ref=cognos_expression,
            )
        table.measures.append(
            Measure(
                name=item_name,
                dax_expression=translation.dax,
                cognos_expression=cognos_expression,
                format_string=format_string,
                needs_review=needs_review,
            )
        )

    def _parse_layouts(self, root: etree._Element, project: MigrationProject) -> None:
        """Turn each Cognos report page into a report page, then flag what could not be mapped.

        Prompt pages only collect parameter values before a Cognos report runs. Power BI has no
        such step: their prompts become parameters or slicers (see :meth:`_parse_prompts`), so they
        are not report pages, and any other content on them is flagged instead.
        """
        report_pages = [
            page for page in root.iter("page") if not _has_ancestor(page, "promptPages")
        ]
        stores = {
            store.get("name"): store for store in root.iter("reportDataStore") if store.get("name")
        }
        aggregates = {
            _sanitize_identifier(query.get("name") or "Query"): {
                _sanitize_identifier(item.get("name")): _effective_aggregate(item)
                for item in query.iter("dataItem")
                if item.get("name")
            }
            for query in root.iter("query")
        }
        migrated: set[etree._Element] = set()
        unmigrated_viz: list[tuple[etree._Element, str]] = []
        column_cards: list[str] = []
        overflow_cards: list[str] = []
        for index, page in enumerate(report_pages, start=1):
            page_label = page.get("name") or f"Page{index}"
            report_page = ReportPage(
                name=_sanitize_identifier(page_label),
                display_name=_sanitize_identifier(page_label),
            )
            for layout_tag, visual_type in _LAYOUT_TO_VISUAL.items():
                for obj in page.iter(layout_tag):
                    report_page.visuals.append(self._build_visual(obj, visual_type, project))
            for obj in page.iter("vizControl"):
                visual, reason = self._build_viz_control(obj, stores, aggregates, project)
                if visual is not None:
                    report_page.visuals.append(visual)
                    migrated.add(obj)
                else:
                    unmigrated_viz.append((obj, reason))
            cards: list[tuple[Visual, etree._Element, bool]] = []
            for obj in page.iter("singleton"):
                card, on_column = self._build_singleton_card(obj, project)
                if card is not None:
                    report_page.visuals.append(card)
                    cards.append((card, obj, on_column))
            overflow = _stack_visuals(report_page.visuals)
            for card, obj, on_column in cards:
                name = obj.get("name") or "unnamed"
                if any(card is lost for lost in overflow):
                    overflow_cards.append(name)
                    continue
                migrated.add(obj)
                if on_column:
                    column_cards.append(name)
            self._collect_static_text(page, report_page)
            project.pages.append(report_page)
        # A visualization outside every page, prompt pages aside, is not drawn on a page either.
        unmigrated_viz.extend(
            (obj, "it is not on a report page")
            for obj in _outside_prompt_pages(root, "vizControl")
            if obj not in migrated and all(obj is not seen for seen, _ in unmigrated_viz)
        )
        self._flag_unmigrated_viz_controls(unmigrated_viz, project)
        self._flag_column_cards(column_cards, project)
        self._flag_prompt_pages(root, project)
        self._flag_unmigrated_layout(root, project, migrated, overflow_cards)

    @staticmethod
    def _flag_prompt_pages(root: etree._Element, project: MigrationProject) -> None:
        """Flag the prompt-page content that has no Power BI target, in one flag per report."""
        pages = [
            page for prompt_pages in root.iter("promptPages") for page in prompt_pages.iter("page")
        ]
        lost: Counter[str] = Counter()
        behaviour = False
        external_components = False
        for page in pages:
            for element in page.iter():
                content = _prompt_page_content(element)
                # Prompt-button captions (Next, Finish) belong to the prompt step itself.
                if content is None or _has_ancestor(element, "promptButton"):
                    continue
                lost[content[0]] += 1
                behaviour = behaviour or content[1]
                if element.tag == "layoutComponentRef" and _has_external_report_path(element):
                    external_components = True
        if not lost:
            return
        named = _quoted_names([page.get("name") for page in pages])
        unnamed = sum(1 for page in pages if not page.get("name"))
        names = ", ".join(part for part in (named, f"{unnamed} unnamed" if unnamed else "") if part)
        summary = ", ".join(f"{count} {description}" for description, count in lost.items())
        message = (
            f"{len(pages)} Cognos prompt page(s) ({names}) were not converted to report pages: "
            "Power BI has no prompt step, so prompts become parameters or slicers "
            f"({len(project.prompts)} prompt parameter(s) were extracted). Content on them with no "
            f"Power BI target: {summary}."
        )
        if external_components:
            message += (
                " Prompt controls inside layout components referenced from other reports are not "
                "part of this specification, so they could not be extracted as prompts."
            )
        message += (
            " Place any instructions next to the slicers and re-implement scripted or conditional "
            "prompt behaviour natively, or drop it deliberately."
        )
        project.add_flag(
            "prompt-page-content",
            message,
            Severity.WARNING if behaviour else Severity.INFO,
        )

    def _apply_data_formats(self, root: etree._Element, project: MigrationProject) -> None:
        """Carry Cognos layout data formats to the model column or measure each one displays.

        A format applies when its layout object shows exactly one data item, resolved in the
        query (or data store) of the object's own container. The nearest format wins: a format
        on a text item shadows its list column's, so the outer one does not apply there. Formats
        on objects that show several items (list, crosstab or visualization defaults) are not
        applied: they would format every item, including ones whose inferred type may be wrong.
        Formats on run date/time objects show no data, and formats in conditional styles are
        part of the conditional styling the conditional-style flag reports; both are skipped.
        Nothing else is lost silently: conflicts, dropped parts and unapplied formats are flagged,
        and so is a field that Cognos shows formatted in one place and unformatted in another.
        """
        stores = {store.get("name"): store for store in root.iter("reportDataStore")}
        tables = {table.name: table for table in project.tables}
        formats: list[tuple[etree._Element, etree._Element, _TranslatedFormat]] = []
        not_applied: list[str] = []
        for data_format in _outside_prompt_pages(root, "dataFormat"):
            if any(a.tag in _CONDITIONAL_FORMAT_PARENTS for a in data_format.iterancestors()):
                continue
            element = next((c for c in data_format if isinstance(c.tag, str)), None)
            parent = data_format.getparent()
            owner = parent.getparent() if parent is not None else None
            if element is None or owner is None or owner.tag in {"date", "time"}:
                continue
            if parent is not None and parent.tag not in {"style", "vcValueFormat"}:
                # Cognos keeps formats in layout styles (no data item has a Data Format
                # property), so any other placement is not read; it is reported, not guessed.
                not_applied.append(
                    f"{element.tag} in a dataFormat on {_format_place(parent)}: Cognos layouts "
                    "carry data formats only in a style or a visualization value format"
                )
                continue
            formats.append((owner, element, _translate_data_format(element)))
        holders = {owner for owner, _, _ in formats}
        # A format element with no format property (empty, or only locale separators) is still
        # the object's own Data Format, so like any nearer format it shadows the outer ones; the
        # object then shows the default format. The shadowed outer format is flagged, not applied.
        plain = {
            owner
            for owner, _, translated in formats
            if translated.format_string is None and not translated.dropped
        }
        measure_families = _measure_families(root)

        def store_table(ref: str | None) -> tuple[Table | None, str]:
            store = stores.get(ref) if ref else None
            query = _data_store_query(store) if store is not None else None
            if not query:
                return None, f"its data store '{ref}' reads no query"
            table = tables.get(_sanitize_identifier(query))
            return table, "" if table else f"its query '{query}' was not converted"

        def targets(owner: etree._Element) -> _FormatTargets:
            """Return what a format's object shows; no names and no problem = shadowed."""
            if owner.tag == "vcSlotDsColumn":
                data_set = next((a for a in owner.iterancestors() if a.tag == "vcDataSet"), None)
                table, problem = store_table(
                    data_set.get("refDataStore") if data_set is not None else None
                )
                return _FormatTargets(table, [owner.get("refDsColumn") or ""], problem)
            if owner.tag == "lcColumnBody":
                control = next((a for a in owner.iterancestors() if a.tag == "listControl"), None)
                table, problem = store_table(
                    control.get("refDataStore") if control is not None else None
                )
                column = owner.getparent()
                ref = column.get("refDsColumn") or "" if column is not None else ""
                return _FormatTargets(table, [ref], problem)
            if owner.tag == "vizControl":
                names = [
                    slot.get("refDsColumn") or ""
                    for slot in owner.iter("vcSlotDsColumn")
                    if slot not in holders
                ]
                if not names:
                    return _FormatTargets(None, [])
                unformatted = _quoted_names(names)
                return _FormatTargets(
                    None, [], f"it is the visualization's default format ({unformatted})"
                )
            query_owner = next(
                (a for a in (owner, *owner.iterancestors()) if a.get("refQuery")), None
            )
            if query_owner is None:
                return _FormatTargets(None, [], "it belongs to no query")
            table = tables.get(_sanitize_identifier(query_owner.get("refQuery") or ""))
            if table is None:
                return _FormatTargets(
                    None, [], f"its query '{query_owner.get('refQuery')}' was not converted"
                )
            cell = owner if owner.tag in _CROSSTAB_CELLS else None
            if cell is None and next(owner.iter("cellValue"), None) is not None:
                # A text item showing the cell value formats the cell it sits in.
                cell = next((a for a in owner.iterancestors() if a.tag in _CROSSTAB_CELLS), None)
            if cell is not None:
                inner = [
                    holder
                    for holder in holders
                    if holder is not owner
                    and owner in holder.iterancestors()
                    and next(holder.iter("cellValue"), None) is not None
                ]
                if inner:
                    return _FormatTargets(table, [], shadowed_by=inner)
                measures = self._crosstab_cell_measures(cell, query_owner, table)
                return _FormatTargets(
                    table, measures, "" if measures else "its measure could not be determined"
                )
            shown: list[str] = []
            shadowing: list[etree._Element] = []
            any_value = False
            others: dict[str, list[str]] = {}
            for value in owner.iter("dataItemValue"):
                ancestors = list(value.iterancestors())
                ref = value.get("refDataItem") or ""
                inner_objects = ancestors[: ancestors.index(owner)] if owner in ancestors else []
                nearer = next((a for a in inner_objects if a in holders), None)
                value_query = next((a for a in ancestors if a.get("refQuery")), None)
                if value_query is not query_owner:
                    if nearer is None and value_query is not None:
                        others.setdefault(value_query.get("refQuery") or "", []).append(ref)
                    continue
                any_value = True
                if nearer is None:
                    shown.append(ref)
                else:
                    shadowing.append(nearer)
            nested = "; ".join(
                f"query '{query}': {_quoted_names(list(dict.fromkeys(refs)))}"
                for query, refs in others.items()
            )
            if not any_value:
                if nested:
                    return _FormatTargets(
                        table,
                        [],
                        f"it shows only values of another query in a nested container "
                        f"({nested}), which a format of an outer container is not applied to",
                    )
                member = next(
                    (
                        a
                        for a in (owner, *owner.iterancestors())
                        if a.tag in {"crosstabNodeMember", *_CROSSTAB_CELLS}
                    ),
                    None,
                )
                if member is not None and member.tag == "crosstabNodeMember":
                    return _FormatTargets(
                        table,
                        [],
                        f"it formats the label of crosstab member '{member.get('refDataItem')}', "
                        "not a cell value; only fact cell formats are migrated",
                    )
                return _FormatTargets(table, [], "it shows no data item")
            note = (
                f"the values of another query in a nested container ({nested}) were not formatted"
                if nested
                else ""
            )
            return _FormatTargets(table, list(dict.fromkeys(shown)), "", note, shadowing)

        # Summaries in a query (a list footer's ``dataItemListSummary``) total another data item;
        # Power BI totals a field with the field's own format, so they resolve to that item.
        summaries = {
            (_sanitize_identifier(query.get("name") or "Query"), element.get("name")): (
                element.get("refDataItem") or ""
            )
            for query in root.iter("query")
            for element in query.iter()
            if isinstance(element.tag, str)
            and element.tag != "dataItem"
            and element.get("name")
            and element.get("refDataItem")
        }

        def model_name(table: Table, ref: str) -> str:
            return _sanitize_identifier(summaries.get((table.name, ref), ref))

        applied: dict[tuple[str, str], list[tuple[_TranslatedFormat, str]]] = {}
        objects: dict[tuple[str, str], Column | Measure] = {}
        refused: dict[str, list[str]] = {}
        locale: list[str] = []
        for owner, element, translated in formats:
            label = _format_place(owner)
            found = targets(owner)
            table, names, problem = found.table, found.names, found.problem
            if owner in plain:
                if translated.locale:
                    where = label
                    if table is not None and len(names) == 1 and not problem:
                        where = f"'{table.name}[{model_name(table, names[0])}]'"
                    locale.append(f"{where} ({', '.join(translated.locale)}; no format set)")
                continue
            reason = translated.dropped[0] if translated.dropped else "no format"
            described = translated.format_string or f"{element.tag} ({reason})"
            if not problem and len(names) > 1:
                problem = f"it is the default of several data items ({_quoted_names(names)})"
            if not problem and not names:
                hidden_by = next((h for h in found.shadowed_by if h in plain), None)
                if hidden_by is not None:
                    problem = (
                        f"a nearer format with no format property on {_format_place(hidden_by)} "
                        "replaces it, so Cognos shows the default format there"
                    )
            if problem or table is None or not names:
                if problem:
                    not_applied.append(f"{described} on {label}: {problem}")
                continue
            if found.note:
                not_applied.append(f"{described} on {label}: {found.note}")
            name = model_name(table, names[0])
            target: Column | Measure | None = next(
                (m for m in table.measures if m.name == name), None
            ) or table.column(name)
            item = f"'{table.name}[{name}]'"
            if target is None:
                not_applied.append(f"{described} on {label}: {item} is not in the model")
                continue
            if isinstance(target, Column):
                kind = "date" if target.data_type == DataType.DATE_TIME else "number"
                if target.data_type not in _NUMERIC_TYPES and kind == "number":
                    kind = "text"
                if kind != translated.family:
                    not_applied.append(
                        f"{described} on {label}: {item} is a {target.data_type.value} column"
                    )
                    continue
            else:
                family = measure_families.get((table.name, name))
                if family is None and translated.family != "number":
                    # An untyped measure is modelled as a number, so only number formats fit.
                    not_applied.append(
                        f"{described} on {label}: the type of measure {item} is not known"
                    )
                    continue
                if family is not None and family != translated.family:
                    not_applied.append(f"{described} on {label}: {item} is a {family} measure")
                    continue
            if translated.format_string is None:
                refused.setdefault(item, []).extend(translated.dropped)
                continue
            applied.setdefault((table.name, name), []).append((translated, label))
            objects[(table.name, name)] = target

        # Places that show a field with no format of their own nor any outer one: text items,
        # visualization slots, list control columns and crosstab cells. Each resolves in the
        # query or data store of its own container, as the formats above do.
        unformatted: dict[tuple[str, str], list[str]] = {}

        def shown_unformatted(
            table: Table | None,
            ref: str,
            nearer: Sequence[etree._Element],
            place: etree._Element,
        ) -> None:
            nearest = next((e for e in nearer if e in holders), None)
            if table is None or (nearest is not None and nearest not in plain):
                return
            key = (table.name, model_name(table, ref))
            where = nearest if nearest is not None else place
            unformatted.setdefault(key, []).append(_format_place(where))

        for value in _outside_prompt_pages(root, "dataItemValue"):
            ancestors = list(value.iterancestors())
            query_owner = next((a for a in ancestors if a.get("refQuery")), None)
            table = (
                tables.get(_sanitize_identifier(query_owner.get("refQuery") or ""))
                if (query_owner is not None)
                else None
            )
            place = ancestors[1] if len(ancestors) > 1 else value
            shown_unformatted(table, value.get("refDataItem") or "", ancestors, place)
        for slot in _outside_prompt_pages(root, "vcSlotDsColumn"):
            data_set = next((a for a in slot.iterancestors() if a.tag == "vcDataSet"), None)
            table, _ = store_table(data_set.get("refDataStore") if data_set is not None else None)
            nearer = [slot, *slot.iterancestors()]
            shown_unformatted(table, slot.get("refDsColumn") or "", nearer, slot)
        for column in _outside_prompt_pages(root, "lcColumn"):
            control = next((a for a in column.iterancestors() if a.tag == "listControl"), None)
            table, _ = store_table(control.get("refDataStore") if control is not None else None)
            body = next((c for c in column if c.tag == "lcColumnBody"), column)
            nearer = [body, *body.iterancestors()]
            shown_unformatted(table, column.get("refDsColumn") or "", nearer, body)
        for crosstab in _outside_prompt_pages(root, "crosstab"):
            table = tables.get(_sanitize_identifier(crosstab.get("refQuery") or ""))
            if table is None:
                continue
            measure_names = {measure.name for measure in table.measures}
            for ref, nearer, cell in _crosstab_cells_shown(crosstab, measure_names):
                shown_unformatted(table, ref, nearer, cell)

        def listed(places: list[str]) -> str:
            distinct = list(dict.fromkeys(places))
            return ", ".join(distinct[:3]) + (" and elsewhere" if len(distinct) > 3 else "")

        dropped: dict[str, list[str]] = {item: parts for item, parts in refused.items()}
        for (table_name, name), uses in applied.items():
            kept = uses[0][0]
            item = f"'{table_name}[{name}]'"
            objects[(table_name, name)].format_string = kept.format_string
            dropped.setdefault(item, []).extend(kept.dropped)
            if kept.locale:
                locale.append(f"{item} ({', '.join(kept.locale)})")
            by_format: dict[str, list[str]] = {}
            for translated, place in uses:
                by_format.setdefault(translated.format_string or "", []).append(place)
            places = [f"'{fmt}' on {listed(where)}" for fmt, where in by_format.items()]
            bare = unformatted.get((table_name, name), [])
            if bare:
                places.append(f"no format on {listed(bare)}")
            if len(places) > 1:
                project.add_flag(
                    "data-format-conflict",
                    f"{item} has different Cognos data formats in different places "
                    f"({'; '.join(places)}). Power BI keeps one format per field, so "
                    f"'{kept.format_string}' (the first) was applied everywhere; set a "
                    "visual-level format where another one is needed.",
                    Severity.WARNING,
                    source_ref=item,
                )
        for item, parts in dropped.items():
            if parts:
                project.add_flag(
                    "data-format-dropped",
                    f"Part of the Cognos data format of {item} has no Power BI format string and "
                    f"was dropped: {'; '.join(dict.fromkeys(parts))}. Check how the values read.",
                    Severity.WARNING,
                    source_ref=item,
                )
        if locale:
            project.add_flag(
                "data-format-locale",
                "Cognos data formats pin decimal, grouping, date or time separators for "
                f"{'; '.join(dict.fromkeys(locale))}. A Power BI format string cannot: the "
                "viewer's locale chooses the separators, so they may differ from Cognos.",
                Severity.INFO,
            )
        if not_applied:
            distinct = list(dict.fromkeys(not_applied))
            hidden = len(distinct) - 8
            project.add_flag(
                "data-format-not-applied",
                f"Cognos data formats were not migrated: {'; '.join(distinct[:8])}"
                f"{f' and {hidden} more' if hidden > 0 else ''}. Power BI formats one field at a "
                "time and cannot format text, so set these formats on the field or visual by hand.",
                Severity.WARNING,
            )

    @staticmethod
    def _crosstab_cell_measures(
        cell: etree._Element, crosstab: etree._Element, table: Table
    ) -> list[str]:
        """Return the measures a crosstab cell shows (one, if determined).

        An intersection names its row and column edge members (``edgeLocation``); a measure among
        them is the cell value, otherwise the crosstab's default measure is. The fact cells of a
        measure member show that measure. Other fact cells show every measure of the crosstab.
        """
        measures = {measure.name for measure in table.measures}
        members = {
            member.get("edgeLocation"): member.get("refDataItem") or ""
            for member in crosstab.iter("crosstabNodeMember")
        }
        default = crosstab.find("defaultMeasure")
        defaults = [default.get("refDataItem") or ""] if default is not None else []
        if cell.tag == "crosstabIntersection":
            edges = [members.get(cell.get("row")), members.get(cell.get("column"))]
            shown = [ref for ref in edges if ref and _sanitize_identifier(ref) in measures]
            return list(dict.fromkeys(shown)) or defaults
        member = cell.getparent() if cell.tag == "factCell" else None
        if member is not None and member.tag == "crosstabNodeMember":
            ref = member.get("refDataItem") or ""
            if _sanitize_identifier(ref) in measures:
                return [ref]
        shown = [ref for ref in members.values() if _sanitize_identifier(ref) in measures]
        return list(dict.fromkeys(shown + defaults))

    def _flag_unmigrated_layout(
        self,
        root: etree._Element,
        project: MigrationProject,
        migrated: set[etree._Element] | None = None,
        overflow_cards: list[str] | None = None,
    ) -> None:
        """Flag report constructs that were dropped, once per kind, so none vanish silently.

        ``migrated`` holds the objects of these kinds that did become visuals (singleton cards);
        ``overflow_cards`` names the singletons whose cards did not fit on their page.
        """
        table_names = {table.name for table in project.tables}
        for tag, (code, description, remedy) in _UNMIGRATED_LAYOUT.items():
            elements = [
                element
                for element in _outside_prompt_pages(root, tag)
                if not _reported_as_crosstab_nested(element, table_names)
                and element not in (migrated or set())
            ]
            if not elements:
                continue
            names = _quoted_names([_element_label(element) for element in elements])
            where = "on report pages"
            if tag == "singleton":
                where = _report_page_location(elements)
                if overflow_cards:
                    remedy += (
                        f" {len(overflow_cards)} of them "
                        f"({_quoted_names(overflow_cards, len(overflow_cards))}) did not fit: a "
                        f"page holds at most {_CARDS_PER_ROW} cards per row, at least "
                        f"{_MIN_CARD_HEIGHT:g} pixels tall, in the space left by its other "
                        "visuals."
                    )
            if tag == "layoutComponentRef":
                external = sum(1 for element in elements if _has_external_report_path(element))
                internal = len(elements) - external
                if external:
                    remedy += (
                        f" {external} of them point(s) to other reports, whose content is not part "
                        "of this specification."
                    )
                if internal:
                    remedy += (
                        f" {internal} of them point(s) to objects of this report, which are not "
                        "repeated here: each such object is migrated, if at all, only where it is "
                        "defined."
                    )
            project.add_flag(
                code,
                f"{len(elements)} Cognos {description}{f' ({names})' if names else ''} {where} "
                f"were not migrated. {remedy}",
                Severity.WARNING,
            )
        self._flag_unmapped_charts(root, project, table_names)
        self._flag_report_page_prompts(root, project)
        self._flag_conditional_render(root, project)
        self._flag_conditional_styles(root, project)
        self._flag_report_variables(root, project)
        if next(root.iter("burst"), None) is not None:
            project.add_flag(
                "burst-options",
                "The report defines Cognos burst options (one output per burst recipient), which "
                "were not migrated. Recreate the distribution with Power BI dynamic per-recipient "
                "subscriptions or row-level security.",
                Severity.WARNING,
            )

    @staticmethod
    def _flag_unmapped_charts(
        root: etree._Element, project: MigrationProject, table_names: set[str]
    ) -> None:
        """Flag report-page charts of a type that has no visual mapping, in one flag per report."""
        charts = [
            element
            for element in root.iter()
            if _is_unmapped_chart(element)
            and not _has_ancestor(element, "promptPages")
            and not _reported_as_crosstab_nested(element, table_names)
        ]
        if not charts:
            return
        names = _quoted_names([_element_label(element) for element in charts])
        project.add_flag(
            "layout-object-not-migrated",
            f"{len(charts)} Cognos chart(s) of a type this converter does not map "
            f"({_tag_counts(charts)}{f'; {names}' if names else ''}) on report pages were not "
            "migrated. Rebuild each with the matching Power BI visual.",
            Severity.WARNING,
        )

    @staticmethod
    def _flag_report_page_prompts(root: etree._Element, project: MigrationProject) -> None:
        """Flag prompt controls placed on report pages; only prompt pages yield prompts."""
        controls = [
            element
            for element in root.iter()
            if _is_prompt_control(element) and not _has_ancestor(element, "promptPages")
        ]
        if not controls:
            return
        parameters = [(element.get("parameter") or "").strip() for element in controls]
        extracted = {prompt.parameter_name for prompt in project.prompts}
        missing = [name for name in parameters if name not in extracted]
        covered = [name for name in parameters if name in extracted]
        message = (
            f"{len(controls)} Cognos prompt control(s) on report pages (parameter(s) "
            f"{_quoted_names(parameters)}) were not migrated."
        )
        if missing:
            message += (
                f" Parameter(s) {_quoted_names(missing)} were not extracted as prompt parameters, "
                "which come from prompt pages only: recreate each as a slicer or Power BI "
                "parameter wired to the filters that reference it."
            )
        if covered:
            message += (
                f" Parameter(s) {_quoted_names(covered)} were extracted from a prompt page: wire "
                "the slicer or parameter recreated for them where these controls were."
            )
        project.add_flag("report-page-prompt-control", message, Severity.WARNING)

    @staticmethod
    def _flag_conditional_render(root: etree._Element, project: MigrationProject) -> None:
        """Flag variable-driven rendering, blocks and text on report pages, in one flag per report.

        Power BI has no report variables, so every alternative is migrated side by side.
        """
        renders = _outside_prompt_pages(root, "conditionalRender")
        owners = [render.getparent() for render in renders if render.getparent() is not None]
        families = [
            element
            for element in root.iter()
            if isinstance(element.tag, str)
            and _conditional_family(element)
            and not _has_ancestor(element, "promptPages")
        ]
        blocks = [e for e in families if _conditional_family(e) == "conditionalBlock"]
        sources = [e for e in families if _conditional_family(e) == "conditionalDataSource"]
        # A source placed directly in a contents container stands for itself.
        source_owners = [
            e if e.getparent() is None or e.getparent().tag == "contents" else e.getparent()
            for e in sources
        ]
        if not owners and not blocks and not source_owners:
            return

        def variables_of(elements: list[etree._Element]) -> str:
            names = _quoted_names([element.get("refVariable") for element in elements])
            return f" ({names})" if names else ""

        def family_members(roots: list[etree._Element]) -> list[etree._Element]:
            # A family may carry its variable on an inner element, such as conditionalBlockCases.
            return [
                member
                for root_element in roots
                for member in root_element.iter()
                if _conditional_family_root(member) is root_element
            ]

        sentences = []
        if owners:
            sentences.append(
                f"{len(owners)} Cognos layout object(s) ({_tag_counts(owners)}) render only when "
                f"their report variable{variables_of(renders)} allows it; Power BI shows them "
                "unconditionally."
            )
        alternatives = "Power BI does not choose between the alternatives: each was migrated "
        alternatives += "unconditionally or not at all."
        if blocks:
            names = _quoted_names([_element_label(block) for block in blocks])
            sentences.append(
                f"{len(blocks)} Cognos conditional block(s){f' ({names})' if names else ''} show "
                f"one of several contents depending on their report variable"
                f"{variables_of(family_members(blocks))}. {alternatives}"
            )
        if source_owners:
            sentences.append(
                f"{len(source_owners)} Cognos object(s) ({_tag_counts(source_owners)}) take their "
                f"text or source from their report variable"
                f"{variables_of(family_members(sources))}. {alternatives}"
            )
        message = " ".join(sentences)
        pages = _quoted_names(
            [owner.get("name") or "unnamed" for owner in owners if owner.tag == "page"]
        )
        if pages:
            message += (
                f" This includes whole page(s) {pages}: check whether Cognos ever shows them "
                "before migrating them."
            )
        message += (
            " Recreate the conditions (for example with bookmarks, visual-level filters, or "
            "measure-driven visibility) or remove the objects."
        )
        project.add_flag("conditional-render", message, Severity.WARNING)

    @staticmethod
    def _flag_conditional_styles(root: etree._Element, project: MigrationProject) -> None:
        """Flag conditional styles and conditional chart palettes, in one flag per report."""
        styles = [
            element
            for element in root.iter()
            if _is_conditional_style(element) and not _has_ancestor(element, "promptPages")
        ]
        if not styles:
            return
        names = _quoted_names([element.get("refConditionalStyle") for element in styles])
        style_note = f"; styles {names}" if names else ""
        project.add_flag(
            "conditional-style",
            f"{len(styles)} Cognos conditional style(s) ({_tag_counts(styles)}{style_note}) were "
            "not migrated, so Power BI shows only the base formatting. Recreate them as "
            "conditional formatting rules driven by measures.",
            Severity.WARNING,
        )

    @staticmethod
    def _flag_report_variables(root: etree._Element, project: MigrationProject) -> None:
        """Record the dropped report variables, and any use of them no other flag reports."""
        variables = list(root.iter("reportVariable"))
        if not variables:
            return
        names = _quoted_names([variable.get("name") for variable in variables])
        uses = [
            element
            for element in root.iter()
            if isinstance(element.tag, str) and element.get("refVariable")
        ]
        unflagged = [element for element in uses if not _flagged_variable_use(element)]
        message = (
            f"{len(variables)} Cognos report variable(s) ({names}) were not migrated; Power BI has "
            "no report variables."
        )
        if len(uses) > len(unflagged):
            message += (
                f" {len(uses) - len(unflagged)} use(s) of them in conditional rendering, blocks, "
                "text or styles are flagged separately."
            )
        if unflagged:
            message += (
                f" {len(unflagged)} other use(s) ({_tag_counts(unflagged)}) are not flagged "
                "elsewhere: review what each one changes and recreate it."
            )
        elif not uses:
            message += " No layout object refers to them."
        project.add_flag(
            "report-variables", message, Severity.WARNING if unflagged else Severity.INFO
        )

    @staticmethod
    def _collect_static_text(page: etree._Element, report_page: ReportPage) -> None:
        """Capture layout static text (letterhead/signature) split into before/after the data list.

        Static text that appears before the first list becomes header text; text after it becomes
        footer text. Text inside a list (for example a no-data message) is ignored. Each block keeps
        the font/size/color style Cognos set on its ``textItem``.
        """
        seen_list = False
        for element in page.iter():
            tag = element.tag if isinstance(element.tag, str) else ""
            if tag == "list":
                seen_list = True
                continue
            if tag != "staticValue":
                continue
            text = (element.text or "").strip()
            if not text:
                continue
            if _has_ancestor(element, "list"):
                continue
            style = None
            text_item = element.getparent()
            while text_item is not None and text_item.tag != "textItem":
                text_item = text_item.getparent()
            if text_item is not None:
                style = _child_style(text_item)
            block = TextBlock(text=text, style=style)
            if seen_list:
                report_page.footer_blocks.append(block)
            else:
                report_page.header_blocks.append(block)

    def _build_singleton_card(
        self, obj: etree._Element, project: MigrationProject
    ) -> tuple[Visual | None, bool]:
        """Turn a singleton that shows exactly one data value into a card bound to that item.

        Anything else (no value, several values, other data or static text beside the value, an
        HTML value, an item its query does not define, nested data objects, or a repeating
        container around it) is left to the not-migrated flag, so a card never drops content.
        Returns the card, and whether it shows a column rather than a measure.
        """
        ref_query = obj.get("refQuery")
        if not ref_query or any(_has_ancestor(obj, tag) for tag in _REPEATING_CONTAINERS):
            return None, False
        for node in obj.iter():
            if node is obj or not isinstance(node.tag, str):
                continue
            if node.tag in _SINGLETON_FORMATTING or any(
                _has_ancestor(node, tag) for tag in _SINGLETON_FORMATTING
            ):
                continue
            if node.tag not in _SINGLETON_CARD_PARTS:
                return None, False
            if node.tag == "staticValue" and (node.text or "").strip():
                return None, False
        values = list(obj.iter("dataItemValue"))
        if len(values) != 1:
            return None, False
        source = values[0].getparent()
        holder = source.getparent() if source is not None else None
        if source is None or source.tag != "dataSource" or holder is None:
            return None, False
        if holder.tag != "textItem":
            return None, False
        table_name = _sanitize_identifier(ref_query)
        table = next((t for t in project.tables if t.name == table_name), None)
        name = _sanitize_identifier(values[0].get("refDataItem") or "")
        if table is None:
            return None, False
        is_measure = any(measure.name == name for measure in table.measures)
        column = None if is_measure else table.column(name)
        if not is_measure and column is None:
            return None, False
        aggregation: Literal["sum", "min"] | None = None
        if column is not None:
            aggregation = "sum" if column.data_type in _NUMERIC_TYPES else "min"
        card = Visual(
            title=obj.get("name"),
            visual_type=VisualType.CARD,
            fields=[
                VisualField(table=table.name, name=name, role="values", aggregation=aggregation)
            ],
        )
        return card, not is_measure

    def _build_viz_control(
        self,
        obj: etree._Element,
        stores: dict[str | None, etree._Element],
        aggregates: dict[str, dict[str, str | None]],
        project: MigrationProject,
    ) -> tuple[Visual | None, str]:
        """Convert a Cognos 11+ visualization (vizControl) into the Power BI visual drawing it.

        The visualization reads a report data store (``vcDataSet@refDataStore``), which reads a
        query (``dsV5ListQuery@refQuery``) and exposes some of its data items; each slot names
        those items (``vcSlotDsColumn@refDsColumn``). Returns the visual, or None and the reason
        it was not migrated. What the visual cannot show is flagged per visualization.
        """
        name = obj.get("name") or "unnamed"
        viz_type = _viz_control_type(obj)
        mapping = _VIZ_CONTROL_VISUALS.get(viz_type)
        if mapping is None:
            if obj.get("type", "").startswith(_VIZ_CONTROL_PREFIX):
                return None, "Power BI has no matching built-in visual"
            return None, "a custom visualization, which this converter does not migrate"
        visual_type, family, approximation = mapping
        slot_roles = _VIZ_SLOT_ROLES[family]
        issues: list[str] = [approximation] if approximation else []

        # Resolve every data set to its table; the first that resolves is the visual's table.
        resolved: list[tuple[etree._Element, etree._Element | None, Table | None, str]] = []
        for data_set in obj.iter("vcDataSet"):
            ref = data_set.get("refDataStore")
            store = stores.get(ref) if ref else None
            table: Table | None = None
            if not ref:
                problem = "names no data store"
            elif store is None:
                problem = f"reads data store '{ref}', which does not exist"
            else:
                query = _data_store_query(store)
                table_name = _sanitize_identifier(query) if query else None
                table = next((t for t in project.tables if t.name == table_name), None)
                if not query:
                    problem = f"reads data store '{ref}', which names no query"
                elif table is None:
                    problem = f"reads query '{query}', which was not converted to a table"
                else:
                    problem = ""
            resolved.append((data_set, store, table, problem))
        if not resolved:
            return None, "it has no data set"
        primary = next((table for _, _, table, _ in resolved if table is not None), None)
        if primary is None:
            return None, "; ".join(
                f"its data set '{data_set.get('idDataSet') or 'data'}' {problem}"
                for data_set, _, _, problem in resolved
            )

        measure_names = {measure.name for measure in primary.measures}
        # role -> [(item name, is measure, rollup method)], in slot order, without repeats.
        entries: dict[str, list[tuple[str, bool, str | None]]] = {}
        unmapped: list[str] = []
        not_in_store: list[str] = []
        not_in_table: list[str] = []
        for data_set, store, table, problem in resolved:
            label = data_set.get("idDataSet") or "data"
            if table is None or store is None:
                issues.append(f"its data set '{label}' {problem}, so it was not bound")
                continue
            if table is not primary:
                issues.append(
                    f"its data set '{label}' reads query '{table.name}', not '{primary.name}' "
                    "like the rest of the visualization, so it was not bound"
                )
                continue
            filters = store.find("dsFilters")
            if filters is not None and len(filters):
                issues.append(
                    f"its data store '{store.get('name')}' filters the rows it plots (dsFilters), "
                    "which was not migrated, so it plots unfiltered data"
                )
            exposed = _data_store_items(store)
            for slot in data_set.iter("vcSlotData"):
                slot_id = slot.get("idSlot") or ""
                columns = [
                    column.get("refDsColumn")
                    for column in slot.iter("vcSlotDsColumn")
                    if column.get("refDsColumn")
                ]
                role = slot_roles.get(slot_id)
                if role is None:
                    if columns:
                        unmapped.append(f"{slot_id} ({_quoted_names(columns, len(columns))})")
                    continue
                for column in slot.iter("vcSlotDsColumn"):
                    ref = column.get("refDsColumn")
                    if not ref:
                        continue
                    if ref not in exposed:
                        not_in_store.append(ref)
                        continue
                    item = _sanitize_identifier(ref)
                    is_measure = item in measure_names
                    if not is_measure and primary.column(item) is None:
                        not_in_table.append(ref)
                        continue
                    rollup = (column.get("rollupMethod") or "").strip().lower() or None
                    bucket = entries.setdefault(role, [])
                    if all(existing != item for existing, _, _ in bucket):
                        bucket.append((item, is_measure, rollup))

        roles: dict[str, list[str]] = {}

        def groupings(role: str) -> list[str]:
            """Return a grouping role's columns; measures cannot group and are reported."""
            measures = [item for item, is_measure, _ in entries.get(role, []) if is_measure]
            if measures:
                issues.append(
                    f"measure(s) {_quoted_names(measures, len(measures))} in its {role} slot "
                    "cannot group a Power BI visual and were not bound"
                )
            return [item for item, is_measure, _ in entries.get(role, []) if not is_measure]

        def single(items: list[str], what: str) -> list[str]:
            if len(items) > 1:
                issues.append(
                    f"Power BI allows one {what} here, so {_quoted_names(items[1:], len(items))} "
                    f"beside '{items[0]}' were not bound"
                )
            return items[:1]

        categories = groupings("category")
        if family == "treemap":
            roles["category"] = categories[:1]
            roles["details"] = categories[1:2]
            if len(categories) > 2:
                lost = categories[2:]
                issues.append(
                    "a Power BI treemap shows two levels (group and details), so "
                    f"{_quoted_names(lost, len(lost))} were not bound"
                )
            for item, is_measure, _ in entries.get("color", []):
                if is_measure or item not in categories[:2]:
                    issues.append(f"its colouring by '{item}' was not migrated")
        else:
            roles["category"] = categories
            if len(categories) > 1:
                issues.append(
                    f"its nested categories ({_quoted_names(categories, len(categories))}) "
                    "become a drill-down hierarchy that shows only the top level until expanded"
                )
        series = groupings("series")
        if family in {"axis", "combination"}:
            coloured = [item for item in series if item in categories]
            if coloured:
                series = [item for item in series if item not in categories]
                issues.append(
                    f"it colours by its category {_quoted_names(coloured, len(coloured))}, which "
                    "was not bound as a legend too (that would split each category into its own "
                    "series), so Power BI draws one colour; set per-category colours in the "
                    "format pane if they matter"
                )
        roles["series"] = single(series, "legend field")
        roles["breakdown"] = single(groupings("breakdown"), "breakdown field")
        line_series = groupings("line_series")
        if line_series:
            issues.append(
                "a Power BI combo chart has no legend for its lines, so the line series "
                f"{_quoted_names(line_series, len(line_series))} were not bound"
            )

        one_value = (
            family in {"treemap", "waterfall"}
            or (family == "pie" and bool(categories))
            or bool(roles["series"])
        )
        for role in ("values", "line_values", "x", "y", "size"):
            items = entries.get(role, [])
            if len(items) > 1 and (role in {"x", "y", "size"} or (role == "values" and one_value)):
                kept = items[0][0]
                lost = [item for item, _, _ in items[1:]]
                split = f" split by '{roles['series'][0]}'" if roles["series"] else ""
                issues.append(
                    f"a Power BI {visual_type.value} plots one {role} item{split} here, so only "
                    f"'{kept}' was bound and {_quoted_names(lost, len(lost))} were not"
                )
                items = items[:1]
            roles[role] = [item for item, _, _ in items]
            for item, is_measure, rollup in items:
                issues.extend(self._viz_rollup_issue(item, is_measure, rollup, primary, aggregates))
        if unmapped:
            issues.append(
                f"its slot(s) {'; '.join(unmapped)} have no place in a Power BI "
                f"{visual_type.value} and were not bound"
            )
        if not_in_store:
            issues.append(
                f"data item(s) {_quoted_names(not_in_store, len(not_in_store))} are not exposed "
                "by its data store and were not bound"
            )
        if not_in_table:
            issues.append(
                f"data item(s) {_quoted_names(not_in_table, len(not_in_table))} are not measures "
                f"or columns of '{primary.name}' and were not bound"
            )
        if not any(roles.get(role) for role in _VIZ_VALUE_ROLES):
            causes = [issue for issue in issues if issue != approximation]
            return None, (
                f"none of the values it plots could be bound: {'; '.join(causes)}"
                if causes
                else "it plots no values"
            )
        interactions = _interactions(obj)
        if interactions:
            issues.append(
                f"its interactivity ({', '.join(interactions)}) was not migrated; rebuild it with "
                "Power BI cross-filtering, drill-through or bookmarks"
            )
        containers = [
            ancestor for ancestor in obj.iterancestors() if ancestor.tag in _REPEATING_CONTAINERS
        ]
        if containers:
            container = containers[0]
            label = _element_label(container)
            issues.append(
                f"it sits inside {container.tag}{f' {label!r}' if label else ''}, which repeats "
                "it per row or cell; it was migrated once, without that context"
            )
        if issues:
            project.add_flag(
                "viz-control-needs-review",
                f"Cognos visualization '{name}' ({viz_type}) was converted to a Power BI "
                f"{visual_type.value} that needs review: {'; '.join(issues)}.",
                Severity.WARNING,
                source_ref=name,
            )
        order = ("category", "details", "series", "breakdown", "values", "line_values", "x", "y")
        fields = [
            VisualField(
                table=primary.name,
                name=item,
                role=role,
                aggregation=_viz_aggregation(primary, item) if role in _VIZ_VALUE_ROLES else None,
            )
            for role in (*order, "size")
            for item in roles.get(role, [])
        ]
        return Visual(title=name, visual_type=visual_type, fields=fields), ""

    @staticmethod
    def _viz_rollup_issue(
        item: str,
        is_measure: bool,
        rollup: str | None,
        table: Table,
        aggregates: dict[str, dict[str, str | None]],
    ) -> list[str]:
        """Say how a plotted item's Cognos rollup differs from what Power BI computes."""
        rollup = _ROLLUP_NAMES.get(rollup, rollup) if rollup else None
        if is_measure:
            aggregate = aggregates.get(table.name, {}).get(item)
            if rollup and aggregate and rollup != aggregate:
                return [
                    f"'{item}' rolls up by {rollup} in Cognos, but its Power BI measure "
                    f"aggregates by {aggregate}"
                ]
            return []
        column = table.column(item)
        numeric = column is not None and column.data_type in _NUMERIC_TYPES
        if numeric and rollup in {None, "total"}:
            return []
        if not numeric and rollup == "count":
            return []
        return [
            f"column '{item}' is plotted as a value that Cognos rolls up by "
            f"{rollup or 'its default'}, but Power BI {'sums' if numeric else 'counts'} it"
        ]

    @staticmethod
    def _flag_unmigrated_viz_controls(
        items: list[tuple[etree._Element, str]], project: MigrationProject
    ) -> None:
        """Flag every visualization that did not become a visual, by name, type and reason."""
        if not items:
            return
        described = "; ".join(
            f"'{obj.get('name') or 'unnamed'}' ({_viz_control_type(obj) or 'no type'}: {reason})"
            for obj, reason in items
        )
        project.add_flag(
            "layout-object-not-migrated",
            f"{len(items)} Cognos visualization(s) (vizControl) were not migrated: {described}. "
            "Rebuild each with the matching Power BI visual, or a custom visual where Power BI "
            "has none.",
            Severity.WARNING,
        )

    @staticmethod
    def _flag_column_cards(names: list[str], project: MigrationProject) -> None:
        """Flag cards that show a column: Cognos shows its first row, a card summarizes it."""
        if not names:
            return
        project.add_flag(
            "singleton-card-column",
            f"{len(names)} Cognos singleton(s) ({_quoted_names(list(names), len(names))}) were "
            "migrated as cards that show a query column. Cognos shows the column's value in the "
            "query's first row; a card summarizes the column (the sum of a number, else the first "
            "value in sort order). They agree when the query returns one row; check each.",
            Severity.WARNING,
        )

    def _build_visual(
        self, obj: etree._Element, visual_type: VisualType, project: MigrationProject
    ) -> Visual:
        chart_issues: list[str] = []
        chart_unusable = False
        if obj.tag == "v2_combinationChart":
            visual_type, type_issues = self._combination_chart_type(obj, None)
        ref_query = obj.get("refQuery")
        fields: list[VisualField] = []
        values_on_rows = False
        if ref_query:
            table_name = _sanitize_identifier(ref_query)
            table = next((t for t in project.tables if t.name == table_name), None)
            if table:
                if obj.tag == "crosstab":
                    fields, values_on_rows = self._crosstab_fields(obj, table, project)
                elif obj.tag == "v2_combinationChart":
                    fields, chart_issues, chart_unusable, drawn = self._combination_chart_fields(
                        obj, table
                    )
                    # The chart type follows the series groups whose series were bound.
                    visual_type, type_issues = self._combination_chart_type(obj, drawn)
                else:
                    fields = self._visual_fields(obj, table)
            elif obj.tag == "v2_combinationChart":
                chart_issues.append(f"its query '{ref_query}' was not found, so nothing was bound")
                chart_unusable = True
        else:
            project.add_flag(
                "visual-unbound",
                f"A {visual_type.value} visual has no query reference and needs manual binding.",
                Severity.WARNING,
            )
            if obj.tag == "v2_combinationChart":
                chart_issues.append("it has no query reference, so nothing was bound")
                chart_unusable = True
        if obj.tag == "v2_combinationChart":
            chart_issues = type_issues + chart_issues
            chart_issues.extend(_combination_chart_context_issues(obj))
            if chart_issues:
                project.add_flag(
                    "combination-chart-needs-review",
                    f"Cognos combination chart '{obj.get('name') or 'unnamed'}' was converted to "
                    f"a Power BI {visual_type.value} that needs review: "
                    f"{'; '.join(chart_issues)}.",
                    Severity.ERROR if chart_unusable else Severity.WARNING,
                    source_ref=obj.get("name"),
                )
        # Tables and matrices are the main content of a Cognos list report; size them to fill the
        # page so the layout resembles the source instead of a small default tile.
        if visual_type in {VisualType.TABLE, VisualType.MATRIX}:
            return Visual(
                visual_type=visual_type,
                fields=fields,
                values_on_rows=values_on_rows,
                x=_CONTENT_X,
                y=_CONTENT_Y,
                width=_CONTENT_WIDTH,
                height=_CONTENT_HEIGHT,
            )
        return Visual(visual_type=visual_type, fields=fields)

    @staticmethod
    def _combination_chart_type(
        obj: etree._Element, drawn: list[etree._Element] | None
    ) -> tuple[VisualType, list[str]]:
        """Pick the Power BI visual for the chart's series groups, and say what it cannot show.

        ``drawn`` holds the series groups whose series were bound; None means every group that
        has series (or every group, when none has).
        """
        groups = _combination_chart_groups(obj)
        if not groups:
            return VisualType.LINE_CHART, [
                "it declares no chart type (area, bar or line), so it was drawn as a line chart"
            ]
        shown = _combination_shown_groups(obj)
        issues: list[str] = []
        empty = [group for group in groups if group not in shown]
        default = _combination_default_ref(obj)
        if empty and default is not None:
            # Not verified against Cognos output: an empty group beside populated ones may plot
            # the default measure, so it is reported instead of silently dropped.
            places = ", ".join(_combination_group_place(element, axis) for element, axis in empty)
            issues.append(
                f"its empty series group(s) ({places}) were not converted; Cognos may plot the "
                f"default measure '{default}' in them"
            )
        drawn_groups = [group for group in shown if drawn is None or group[0] in drawn] or shown
        horizontal = obj.get("orientation") == "horizontal"
        labels = [_combination_group_label(element) for element, _ in drawn_groups]
        # Overlaid standard lines are exactly a multi-line chart; any other overlay is not.
        if len(drawn_groups) > 1 and set(labels) != {"line"}:
            issues.append(
                f"it overlays {len(drawn_groups)} series groups ({', '.join(labels)}), which were "
                f"all drawn as one {labels[0]} chart; rebuild the overlay, for example as a Power "
                "BI line and column combo chart"
            )
        secondary = sorted({axis for _, axis in drawn_groups if axis != _COMBINATION_PRIMARY_AXIS})
        if secondary:
            issues.append(
                f"series on a secondary numeric axis ({', '.join(secondary)}) were drawn on the "
                "single primary axis"
            )
        element = drawn_groups[0][0]
        tag = element.tag
        series_type = element.get("seriesType")
        configuration = _COMBINATION_SERIES_TYPES.get(series_type)
        if configuration is None:
            issues.append(
                f"its series type '{series_type}' is not recognised and was drawn as standard"
            )
            configuration = "standard"
        if tag not in _COMBINATION_CHART_TAGS:
            issues.append(f"its chart type '{tag}' is not supported and was drawn as a line chart")
            return VisualType.LINE_CHART, issues
        exact = _COMBINATION_VISUALS.get((tag, horizontal, configuration))
        if exact is not None:
            return exact, issues
        if horizontal:
            issues.append(
                f"Power BI has no horizontal {_COMBINATION_CHART_TAGS[tag]} chart, so it is drawn "
                "vertically"
            )
            exact = _COMBINATION_VISUALS.get((tag, False, configuration))
            if exact is not None:
                return exact, issues
        visual_type, approximation = _COMBINATION_APPROXIMATIONS[(tag, configuration)]
        issues.append(approximation)
        return visual_type, issues

    @staticmethod
    def _combination_chart_fields(
        obj: etree._Element, table: Table
    ) -> tuple[list[VisualField], list[str], bool, list[etree._Element]]:
        """Bind a Cognos combination chart's category axis, series and measures.

        Every series Cognos draws pairs one measure with the dimensions that split it. The
        measure is the one on the series' own path, else the category's, else the default
        measure, which applies only where the edges do not determine the measure (IBM, "Specify
        the default measure"). One Power BI chart draws several unsplit measures, or one measure
        split by one legend field; any other series are named in the issues, never re-paired.
        Returns the fields, the issues, whether no measure was bound, and the series groups whose
        series were bound.
        """
        column_names = {column.name for column in table.columns}
        measure_names = {measure.name for measure in table.measures}
        missing: list[str] = []
        issues: list[str] = []

        def add(items: list[str], name: str) -> None:
            if name not in items:
                items.append(name)

        def resolve(path: list[etree._Element]) -> tuple[list[str], list[str], bool]:
            measures: list[str] = []
            dims: list[str] = []
            unresolved = False
            for member in path:
                ref = member.get("refDataItem")
                if not ref:
                    continue
                name = _sanitize_identifier(ref)
                if name in measure_names:
                    add(measures, name)
                elif name in column_names:
                    add(dims, name)
                else:
                    unresolved = True
                    missing.append(ref)
            return measures, dims, unresolved

        categories = [
            resolve(path)
            for path in _chart_node_paths(obj.find("v2_commonAxis"))
            if _path_has_ref(path)
        ]
        category_dims: list[str] = []
        category_measures: list[str] = []
        for measures, dims, unresolved in categories:
            for dim in dims:
                add(category_dims, dim)
            for measure in [] if unresolved else measures:
                add(category_measures, measure)
        known = [measures for measures, _, unresolved in categories if not unresolved]
        # Where a category item is not a measure, the default measure fills the gap.
        free_category = not known or any(not measures for measures in known)

        shown = _combination_shown_groups(obj)
        leaves: list[tuple[etree._Element | None, str, list[str], list[str]]] = []
        # Resolved dimensions of the series paths that did not resolve as a whole.
        unresolved_series: list[list[str]] = []
        for element, axis in shown:
            for path in _chart_node_paths(element):
                if not _path_has_ref(path):
                    continue
                measures, dims, unresolved = resolve(path)
                if unresolved:
                    unresolved_series.append(dims)
                else:
                    leaves.append((element, axis, measures, dims))
        substituted = not leaves and bool(unresolved_series)
        if not leaves:
            # No resolvable series: each group draws one series of the edge or default measure.
            leaves = [(element, axis, [], []) for element, axis in shown] or [
                (None, _COMBINATION_PRIMARY_AXIS, [], [])
            ]

        # (measure, splitting dimensions, group, axis) for every series Cognos draws.
        pairs: list[tuple[str, tuple[str, ...], etree._Element | None, str]] = []
        for element, axis, measures, dims in leaves:
            plotted = measures or [
                *category_measures,
                *([_COMBINATION_DEFAULT] if free_category else []),
            ]
            pairs.extend((measure, tuple(dims), element, axis) for measure in plotted)

        default_name: str | None = None
        default_ref = _combination_default_ref(obj)
        if default_ref and any(pair[0] == _COMBINATION_DEFAULT for pair in pairs):
            name = _sanitize_identifier(default_ref)
            if name in measure_names:
                default_name = name
            elif name in column_names:
                issues.append(f"default measure '{name}' is a column, not a measure")
            else:
                missing.append(default_ref)

        def value(pair: tuple[str, tuple[str, ...], etree._Element | None, str]) -> str | None:
            return default_name if pair[0] == _COMBINATION_DEFAULT else pair[0]

        def measure_text(pair: tuple[str, tuple[str, ...], etree._Element | None, str]) -> str:
            default = " (default measure)" if pair[0] == _COMBINATION_DEFAULT else ""
            return f"'{value(pair)}'{default}"

        def describe(pair: tuple[str, tuple[str, ...], etree._Element | None, str]) -> str:
            _, dims, element, axis = pair
            text = measure_text(pair) + (f" by {_quoted_names(list(dims))}" if dims else "")
            places = [_combination_group_label(element)] if element is not None else []
            if axis != _COMBINATION_PRIMARY_AXIS:
                places.append(axis)
            return f"{text} ({', '.join(places)})" if places else text

        plotted_pairs = [pair for pair in pairs if value(pair)]
        unmeasured = [pair for pair in pairs if not value(pair) and pair[1]]
        split = [pair for pair in plotted_pairs if pair[1]]
        split_measures = [name for name in dict.fromkeys(value(pair) for pair in split) if name]
        series: list[str] = []
        if len(split_measures) == 1:
            series = [split[0][1][0]]
            kept = [pair for pair in split if pair[1][0] == series[0]]
            dropped = [pair for pair in plotted_pairs if pair not in kept]
            if dropped:
                issues.append(
                    "one Power BI chart draws one measure split by one legend field, so only "
                    f"{measure_text(kept[0])} by '{series[0]}' was "
                    "bound and these Cognos series were not: "
                    f"{'; '.join(describe(pair) for pair in dropped)}; rebuild them, for example "
                    "as a Power BI line and column combo chart"
                )
        else:
            kept = plotted_pairs
            if split:
                issues.append(
                    "Power BI cannot split several measures by a legend, so the series "
                    f"{_quoted_names([dim for pair in split for dim in pair[1]])} were not bound"
                )
        values = [name for name in dict.fromkeys(value(pair) for pair in kept) if name]
        if unmeasured and values:
            issues.append(
                f"the series {_quoted_names([dim for pair in unmeasured for dim in pair[1]])} "
                "have no measure to plot (no usable default measure), so they were not bound"
            )
        elif unmeasured:
            # Nothing is plotted either way; keep the legend so the chart's shape is visible.
            series = [unmeasured[0][1][0]]
            kept = [pair for pair in unmeasured if pair[1][0] == series[0]]
        nested = [dim for pair in kept for dim in pair[1][1:]]
        if series and nested:
            issues.append(
                "Power BI allows one legend field, so the nested series "
                f"{_quoted_names(list(nested))} were not bound"
            )
        if unmeasured and not values:
            others = [pair[1][0] for pair in unmeasured if pair[1][0] != series[0]]
            if others:
                issues.append(
                    "Power BI allows one legend field, so the series "
                    f"{_quoted_names(list(others))} beside '{series[0]}' were not bound"
                )

        bound_category_measures = [name for name in category_measures if name in values]
        if bound_category_measures:
            issues.append(
                f"measure(s) {_quoted_names(list(bound_category_measures))} on the category axis "
                "were bound as Y values, so they are not categories in Power BI"
            )
        unbound_category_measures = [name for name in category_measures if name not in values]
        if unbound_category_measures:
            issues.append(
                f"measure(s) {_quoted_names(list(unbound_category_measures))} on the category axis "
                "were not bound"
            )
        if missing:
            issues.append(
                f"data item(s) {_quoted_names(list(missing))} are not measures or columns of "
                f"'{table.name}' and were not bound"
            )
        if substituted:
            plotted = list(dict.fromkeys(measure_text(pair) for pair in kept if value(pair)))
            text = (
                f"none of its series resolved, so {', '.join(plotted)} was plotted in their place "
                "with no legend or series split"
                if plotted
                else "none of its series resolved, so no series were bound"
            )
            lost = [dim for dims in unresolved_series for dim in dims]
            if lost:
                text += f"; the series split by {_quoted_names(list(lost))} was lost"
            issues.append(text)
        if categories and not category_dims and not category_measures:
            issues.append("none of its category axis items resolved, so it has no category axis")
        elif len(category_dims) > 1:
            dim_paths = [dims for _, dims, _ in categories if dims]
            nested_categories = any(len(dims) > 1 for dims in dim_paths)
            side_by_side = len({dims[0] for dims in dim_paths}) > 1
            names = _quoted_names(list(category_dims))
            hierarchy = (
                f"were bound as a drill-down hierarchy that shows only '{category_dims[0]}' "
                "until expanded"
            )
            if nested_categories and side_by_side:
                issues.append(
                    f"its categories ({names}) mix nested and side-by-side items, but {hierarchy}"
                )
            elif side_by_side:
                issues.append(
                    f"its categories ({names}) are side by side on the Cognos axis, not nested, "
                    f"but {hierarchy}"
                )
            else:
                issues.append(
                    f"its nested categories ({names}) become a drill-down hierarchy that shows "
                    "only the top level until expanded"
                )
        no_values = not values
        if no_values:
            issues.append("no measure resolved, so it plots no values")
        fields = [
            VisualField(table=table.name, name=name, role=role)
            for role, names in (("category", category_dims), ("series", series), ("values", values))
            for name in names
        ]
        drawn = list(dict.fromkeys(pair[2] for pair in kept if pair[2] is not None))
        return fields, issues, no_values, drawn

    def _crosstab_fields(
        self, obj: etree._Element, table: Table, project: MigrationProject
    ) -> tuple[list[VisualField], bool]:
        """Extract axes parent-first, and bind only explicitly displayed cell values."""
        fields: list[VisualField] = []
        seen: set[tuple[str, str]] = set()
        measures = {measure.name for measure in table.measures}
        columns = {column.name for column in table.columns}
        values_on_rows = False

        def add(ref: str | None, role: str) -> None:
            nonlocal values_on_rows
            if not ref:
                return
            name = _sanitize_identifier(ref)
            if name in measures:
                if role == "rows":
                    values_on_rows = True
                role = "values"
            elif name not in columns or role == "values":
                project.add_flag(
                    "crosstab-binding-needs-review",
                    f"Crosstab '{obj.get('name')}' references '{name}' as {role} in "
                    f"'{table.name}', but it has no corresponding "
                    f"{'measure' if role == 'values' else 'field'}. "
                    "Resolve the cell expression or edge summary explicitly.",
                    source_ref=name,
                )
                return
            if (role, name) not in seen:
                seen.add((role, name))
                fields.append(VisualField(table=table.name, name=name, role=role))

        def axis(parent: etree._Element | None, role: str) -> None:
            if parent is None:
                return
            for node in parent.findall("crosstabNode"):
                for member in node.findall("crosstabNodeMembers/crosstabNodeMember"):
                    add(member.get("refDataItem"), role)
                # Cognos often serializes nested nodes before their parent's member.
                axis(node.find("crosstabNestedNodes"), role)

        axis(obj.find("crosstabRows"), "rows")
        axis(obj.find("crosstabColumns"), "columns")
        default_measure = obj.find("defaultMeasure")
        if default_measure is not None:
            add(default_measure.get("refDataItem"), "values")
        for value in obj.iter("dataItemValue"):
            ancestors = list(value.iterancestors())
            owner = next((element for element in ancestors if element.get("refQuery")), None)
            if owner is None or owner is not obj:
                continue
            add(value.get("refDataItem"), "values")
            if any(element.tag == "crosstabNodeMember" for element in ancestors):
                project.add_flag(
                    "crosstab-header-value",
                    f"Inline header value '{value.get('refDataItem')}' in "
                    f"'{obj.get('name')}' requires layout review; resolved measures "
                    "are emitted as matrix values rather than inline header text.",
                    source_ref=value.get("refDataItem"),
                )
        for nested in obj.iterdescendants():
            if nested.get("refQuery"):
                message = (
                    f"Nested {nested.tag} with query '{nested.get('refQuery')}' in "
                    f"'{obj.get('name')}' was not embedded in the matrix. "
                    "Rebuild its master-detail layout and interaction separately."
                )
                owner = _query_owner(nested)
                if owner is not None and owner.tag == "customControl":
                    # This flag replaces custom-control for it, so keep the JavaScript warning.
                    label = _element_label(owner)
                    named = f" '{label}'" if label else ""
                    message += (
                        f" It belongs to custom control{named}, which "
                        "runs JavaScript that Power BI does not run; re-implement the behaviour "
                        "natively or drop it deliberately."
                    )
                project.add_flag(
                    "crosstab-nested-content",
                    message,
                    source_ref=nested.get("refQuery"),
                )
        if not any(field.role == "values" for field in fields):
            project.add_flag(
                "crosstab-values-missing",
                f"Crosstab '{obj.get('name')}' has no resolved measure. "
                "No arbitrary query columns were substituted for its cell values.",
                Severity.ERROR,
                source_ref=table.name,
            )
        return fields, values_on_rows

    def _visual_fields(self, obj: etree._Element, table: Table) -> list[VisualField]:
        """Bind the visual to exactly the columns the Cognos layout shows, in their shown order.

        A Cognos list declares its columns (and order) via ``listColumn`` entries. When present we
        honor that selection and order, and carry each column's title/body style through so the
        generated report matches the source alignment, font, and weight; otherwise we fall back to
        every column then measure.
        """
        column_names = {column.name for column in table.columns}
        measure_names = {measure.name for measure in table.measures}
        fields: list[VisualField] = []
        seen: set[str] = set()
        for ref, header_style, cell_style in self._layout_columns(obj):
            if ref in seen or (ref not in column_names and ref not in measure_names):
                continue
            seen.add(ref)
            role = "values" if ref in measure_names else "rows"
            fields.append(
                VisualField(
                    table=table.name,
                    name=ref,
                    role=role,
                    header_style=header_style,
                    cell_style=cell_style,
                )
            )
        if fields:
            return fields
        for column in table.columns:
            fields.append(VisualField(table=table.name, name=column.name, role="rows"))
        for measure in table.measures:
            fields.append(VisualField(table=table.name, name=measure.name, role="values"))
        return fields

    @staticmethod
    def _layout_columns(obj: etree._Element) -> list[tuple[str, Style | None, Style | None]]:
        """Return ordered ``(dataItem, titleStyle, bodyStyle)`` tuples for a list's columns."""
        out: list[tuple[str, Style | None, Style | None]] = []
        for column in obj.iter("listColumn"):
            ref = None
            for tag in ("dataItemValue", "dataItemLabel"):
                cell = column.find(f".//{tag}")
                if cell is not None and cell.get("refDataItem"):
                    ref = _sanitize_identifier(cell.get("refDataItem"))
                    break
            if not ref:
                continue
            title = column.find("listColumnTitle")
            body = column.find("listColumnBody")
            title_style = _child_style(title) if title is not None else None
            body_style = _child_style(body) if body is not None else None
            out.append((ref, title_style, body_style))
        return out


def _combination_chart_groups(obj: etree._Element) -> list[tuple[etree._Element, str]]:
    """Return each series group (area, bar or line element) of a chart with its axis tag."""
    return [
        (element, chart_types.getparent().tag)
        for chart_types in obj.iter("v2_combinationChartTypes")
        for element in chart_types
        if isinstance(element.tag, str)
    ]


def _combination_shown_groups(obj: etree._Element) -> list[tuple[etree._Element, str]]:
    """Return the series groups that have series, or every group when none has."""
    groups = _combination_chart_groups(obj)
    shown = [group for group in groups if next(group[0].iter("chartNodeMember"), None) is not None]
    return shown or groups


def _combination_default_ref(obj: etree._Element) -> str | None:
    """Return the data item a chart names as its default measure, if any."""
    return next(
        (ref for ref in (d.get("refDataItem") for d in obj.iter("v2_defaultChartMeasure")) if ref),
        None,
    )


def _combination_group_place(element: etree._Element, axis: str) -> str:
    """Describe a series group with its axis, for example ``line on v2_topRightAxis``."""
    return f"{_combination_group_label(element)} on {axis}"


def _chart_node_paths(parent: etree._Element | None) -> list[list[etree._Element]]:
    """Return the member path to every leaf of a chartNodes tree; sibling members branch."""
    if parent is None:
        return []
    paths: list[list[etree._Element]] = []
    for node in parent.findall("chartNodes/chartNode"):
        nested = _chart_node_paths(node)
        members = node.findall("chartNodeMembers/chartNodeMember")
        for prefix in [[member] for member in members] or [[]]:
            paths.extend([prefix + path for path in nested] if nested else [prefix])
    return paths


def _path_has_ref(path: list[etree._Element]) -> bool:
    return any(member.get("refDataItem") for member in path)


def _combination_group_label(element: etree._Element) -> str:
    """Describe a series group, for example ``bar (stacked)``."""
    kind = _COMBINATION_CHART_TAGS.get(element.tag, element.tag)
    series_type = element.get("seriesType")
    configuration = _COMBINATION_SERIES_TYPES.get(series_type, series_type)
    return kind if configuration == "standard" else f"{kind} ({configuration})"


def _combination_chart_context_issues(obj: etree._Element) -> list[str]:
    """Describe what a chart's surrounding layout does that a single Power BI visual does not."""
    links = obj.find("masterDetailLinks")
    count = 0 if links is None else len(links.findall("masterDetailLink"))
    if not count:
        return []
    return [
        f"it is filtered to each row of its container by {count} master-detail link(s), but was "
        "emitted once and unfiltered; rebuild it with small multiples, drill-through or a filter"
    ]


def _cardinality_is_many(cardinality: str) -> bool:
    """Return True when a Cognos join cardinality string denotes a many side (n, *, or 1:n)."""
    text = cardinality.strip().lower()
    if not text:
        return False
    right = text.split(":")[-1] if ":" in text else text
    return right in {"n", "*", "many"} or right not in {"0", "1"}


def parse_report(path: str | Path) -> MigrationProject:
    """Convenience wrapper to parse a Cognos report file into a migration project."""
    return CognosReportParser().parse_file(path)
