"""
purpose: Run one read-only DAX query (EVALUATE or DEFINE ... EVALUATE) against the model of one report unit
         loaded in Power BI Desktop and print the result as JSON lines, one object per row (dates as ISO
         8601), written as UTF-8. A column name that repeats an earlier one gets a "#2", "#3", ... suffix
         so no column is lost. Uses the same guard as parity_check.py: the unit must match its parent
         run.json and --pid must have been opened on the unit's working .pbip.

         Only one query is allowed: outside comments and literals it starts with EVALUATE or DEFINE, holds
         at least one EVALUATE and no ';'. With --data-expression the named shared M expression must be a
         text parameter (one string literal, optionally with meta [...]) whose path equals --data-path.

         Exit codes: 0 query ran; 1 DAX error or a result value that could not be read; 2 refused, nothing
         was queried (--file unreadable, not a read-only query, unit verification failed, pid could not
         be inspected or is not serving the unit, data source mismatch, missing ADOMD.NET or pythonnet,
         connection failed); 3 crashed: an unexpected error, printed on stderr as one
         "CRASHED: <type>: <message>" line.
usage:   uv run --with pythonnet python scripts/dax_query.py --unit <run>/units/<key> --pid <pid>
             (--dax "EVALUATE ..." | --file query.dax) [--max-rows 200]
             [--data-expression DataFolder --data-path <dir>] [--adomd <AdomdClient.dll>]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import desktop_gate
from desktop_gate import DEFAULT_ADOMD, GateRefusal

LOG = logging.getLogger("dax_query")


def unique_names(names: list[str]) -> list[str]:
    """Column names with every repeat suffixed "#2", "#3", ...; a suffix never takes a name the result holds."""
    originals = set(names)
    taken: set[str] = set()
    result = []
    for name in names:
        candidate, number = name, 1
        while candidate in taken or (candidate != name and candidate in originals):
            number += 1
            candidate = f"{name}#{number}"
        taken.add(candidate)
        result.append(candidate)
    return result


def main(argv: list[str] | None = None) -> int:
    desktop_gate.utf8_output()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unit", required=True, type=Path, help="report unit, <run>/units/<key>")
    ap.add_argument("--pid", required=True, type=int, help="PBIDesktop.exe process id opened on the working .pbip")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--dax", help="the DAX query text")
    source.add_argument("--file", type=Path, help="file holding the DAX query")
    ap.add_argument("--max-rows", type=int, default=200, help="stop printing after this many rows")
    ap.add_argument("--data-expression", help="shared M expression that must point at --data-path, e.g. DataFolder")
    ap.add_argument("--data-path", type=Path, help="data folder the model must read")
    ap.add_argument("--adomd", type=Path, default=DEFAULT_ADOMD, help="Microsoft.AnalysisServices.AdomdClient.dll")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    if (args.data_expression is None) != (args.data_path is None):
        ap.error("--data-expression and --data-path go together")
    try:
        return _query(args)
    except Exception as exc:  # noqa: BLE001  (exit 3 must not read as a DAX error)
        return desktop_gate.crashed(exc)


def _query(args: argparse.Namespace) -> int:
    if args.dax is not None:
        text = args.dax
    else:
        try:
            text = args.file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            LOG.error("refusing: cannot read --file %s: %s", args.file, exc)
            return 2
    if not desktop_gate.is_read_only(text):
        LOG.error("refusing: only one EVALUATE or DEFINE ... EVALUATE query is allowed")
        return 2
    try:
        unit = desktop_gate.verified_unit(args.unit)
        conn, _ = desktop_gate.connect_verified(unit, args.pid, args.adomd)
    except GateRefusal as exc:
        LOG.error("refusing to query: %s", exc)
        return 2
    try:
        if args.data_expression is not None:
            desktop_gate.require_data_source(conn, args.data_expression, args.data_path)
        try:
            names, rows = desktop_gate.read(conn, text)
        except desktop_gate.UnreadableValue as exc:
            LOG.error("the query ran but a result value could not be read: %s", exc)
            return 1
        except Exception as exc:  # noqa: BLE001  (ADOMD raises .NET exceptions)
            LOG.error("DAX error: %s", desktop_gate.dotnet_message(exc))
            return 1
    except GateRefusal as exc:
        LOG.error("refusing to query: %s", exc)
        return 2
    finally:
        conn.Close()
    names = unique_names(names)
    for row in rows[: max(args.max_rows, 0)]:
        print(json.dumps(dict(zip(names, row)), ensure_ascii=False))
    LOG.info("%d rows%s", len(rows), f" (printed {args.max_rows})" if len(rows) > args.max_rows else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
