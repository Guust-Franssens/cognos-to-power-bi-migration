"""Resolve query-local measure names before emitting a model-wide Power BI namespace."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from cognos2powerbi.core.ir.models import Column, Measure, MigrationProject, Severity

# Keep token offsets so ignoring trivia for identity resolution never changes source text.
DAX_TOKEN = re.compile(
    r"(?P<trivia>\s+|//[^\r\n]*|--[^\r\n]*|/\*[\s\S]*?\*/)"
    r'|(?P<literal>"(?:[^"]|"")*")'
    r"|(?P<table>'(?:[^']|'')*')"
    r"|(?P<reference>\[(?:[^\]]|\]\])+\])"
    r"|(?P<word>[^\W\d]\w*)|."
)
_COLUMN_AGGREGATE = re.compile(
    r"SUM|AVERAGE|MIN|MAX|COUNT|COUNTA|COUNTBLANK|DISTINCTCOUNT|DISTINCTCOUNTNOBLANK",
    re.IGNORECASE,
)
DAX_KEYWORDS = {"var", "return", "in", "not", "and", "or", "true", "false"}


def normalize_measure_names(project: MigrationProject) -> None:
    """Update names, DAX and visual references atomically and idempotently in the IR.

    All members of a collision receive a table-qualified name, independent of query order.
    Existing names are reserved before allocation so a generated suffix cannot steal one.
    """
    entries = [(table, measure) for table in project.tables for measure in table.measures]
    keys = [(table.name.casefold(), measure.name.casefold()) for table, measure in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate measure names within a table cannot be resolved unambiguously.")
    counts = Counter(measure.name.casefold() for _, measure in entries)
    used = set(counts)
    columns = {
        table.name.casefold(): {column.name.casefold() for column in table.columns}
        for table in project.tables
    }
    names: dict[tuple[str, str], str] = {}
    for table, measure in sorted(
        entries, key=lambda item: (item[0].name.casefold(), item[1].name.casefold())
    ):
        key = (table.name.casefold(), measure.name.casefold())
        candidate = measure.name
        if counts[key[1]] > 1 or key[1] in columns[key[0]]:
            base = f"{measure.name} ({table.name})"
            candidate = base
            suffix = 2
            while candidate.casefold() in used or candidate.casefold() in columns[key[0]]:
                candidate = f"{base} {suffix}"
                suffix += 1
        names[key] = candidate
        used.add(candidate.casefold())
    if all(names[key] == measure.name for key, (_, measure) in zip(keys, entries, strict=True)):
        return

    by_name: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key in names:
        by_name[key[1]].append(key)

    def rewrite(expression: str | None, home: str) -> str | None:
        if not expression:
            return expression

        tokens = [token for token in DAX_TOKEN.finditer(expression) if token.lastgroup != "trivia"]
        parts: list[str] = []
        end = 0
        for index, match in enumerate(tokens):
            if match.lastgroup != "reference":
                continue
            ref = match.group("reference")[1:-1].replace("]]", "]").casefold()
            previous = tokens[index - 1] if index else None
            qualifier = (
                previous.group()
                if previous is not None
                and (
                    previous.lastgroup == "table"
                    or (
                        previous.lastgroup == "word"
                        and previous.group().casefold() not in DAX_KEYWORDS
                    )
                )
                else None
            )
            table_key = (
                (
                    qualifier[1:-1].replace("''", "'") if qualifier.startswith("'") else qualifier
                ).casefold()
                if qualifier
                else home.casefold()
            )
            # These functions require a physical column, even when the source parser has
            # not materialized that column yet. Do not turn SUM(T[Amount]) into a measure.
            reference_start = index - 1 if qualifier else index
            if (
                ref in columns.get(table_key, set())
                and (qualifier or (table_key, ref) not in names)
            ) or (
                reference_start >= 2
                and tokens[reference_start - 1].group() == "("
                and _COLUMN_AGGREGATE.fullmatch(tokens[reference_start - 2].group())
                and index + 1 < len(tokens)
                and tokens[index + 1].group() == ")"
            ):
                continue
            key = (table_key, ref)
            if key not in names and not qualifier:
                candidates = by_name.get(ref, [])
                if len(candidates) > 1:
                    raise ValueError(
                        f"Ambiguous unqualified measure [{ref}] in table '{home}'; "
                        "supply its owning table before renaming."
                    )
                if candidates:
                    key = candidates[0]
            if key not in names:
                continue
            new_ref = "[" + names[key].replace("]", "]]") + "]"
            parts.extend((expression[end : match.start()], new_ref))
            end = match.end()

        parts.append(expression[end:])
        return "".join(parts)

    # Resolve everything before changing the project, so ambiguous input leaves it untouched.
    expressions: list[tuple[Column | Measure, str | None]] = []
    for table in project.tables:
        items: list[Column | Measure] = [*table.columns, *table.measures]
        expressions.extend((item, rewrite(item.dax_expression, table.name)) for item in items)
    visual_names = [
        (field, names.get((field.table.casefold(), field.name.casefold()), field.name))
        for page in project.pages
        for visual in page.visuals
        for field in visual.fields
    ]
    for item, expression in expressions:
        item.dax_expression = expression
    for field, name in visual_names:
        field.name = name
    for key, (table, measure) in zip(keys, entries, strict=True):
        new_name = names[key]
        if new_name != measure.name:
            project.add_flag(
                "measure-name-renamed",
                f"Measure '{measure.name}' in '{table.name}' was renamed to '{new_name}' "
                "to satisfy Power BI's model-wide name uniqueness.",
                Severity.INFO,
                source_ref=f"{table.name}[{measure.name}]",
            )
            measure.name = new_name
