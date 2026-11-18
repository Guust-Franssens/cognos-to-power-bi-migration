"""
purpose: Numeric parity gate for one report unit of a migration run. Runs every check of an agreed oracle
         file against the model loaded in a Power BI Desktop instance and prints PASS/FAIL per check.
         Before connecting it verifies the unit against its parent run.json and proves that --pid was
         opened on the unit's working .pbip, so another unit, the baseline or a stale copy can never pass.
         The gate is read-only: it never refreshes, saves or edits the model.

         Exit codes: 0 every check passed; 1 at least one check failed (mismatch, missing row, an unexpected
         row when the check sets exact_rows, DAX error, a result value that could not be read); 2 refused,
         nothing was checked (invalid oracle, oracle inside the run root or
         this checkout or hard-linked, unit verification failed, pid could not be inspected or is not
         serving the unit, data source not a text parameter naming --data-path, missing ADOMD.NET client
         or pythonnet, connection failed); 3 crashed: an unexpected error, printed on stderr as one
         "CRASHED: <type>: <message>" line.

oracle:  a JSON file kept OUTSIDE the run root and outside this checkout, with a single hard link:
           {"checks": [{"id": "sales_by_month", "title": "...", "source": "cognos-capture",
                        "dax": "EVALUATE ...", "keys": ["Month"], "round": 2, "exact_rows": true,
                        "expected": {"jan": [936.0, 464.0], "feb": [...]}}]}
         Duplicate keys in any object, NaN and Infinity are refused; expected values must be finite
         numbers, text or null. The DAX returns the key columns first, then the value columns. A row's
         key is its key values joined with "|" (null as "", dates as ISO 8601, numbers as Python str(),
         except that a whole-number float below 2**53 in size is written without ".0", so a Decimal
         year 2024.0 matches the key "2024" exactly like an integer year does); a check without keys
         uses "". "round" (optional) rounds both sides at that many decimals, ties away from zero
         (2.345 to 2.35, -0.5 to -1; Python's decimal ROUND_HALF_UP), otherwise numbers must agree
         within 1e-9 * max(1, |expected|), that is an absolute 1e-9 when
         |expected| < 1 and a relative 1e-9 otherwise. "exact_rows" (optional, default false) also fails
         on rows the oracle does not list; an empty "expected" is only accepted with exact_rows true.
         Every "dax" must be one read-only query (see desktop_gate.is_read_only). With
         --data-expression the named shared M expression must be a text parameter (one string literal,
         optionally with meta [...]) whose path equals --data-path.
usage:   uv run --with pythonnet python scripts/parity_check.py --unit <run>/units/<key> --pid <pid>
             --oracle <path outside the run>/parity.json [--data-expression DataFolder --data-path <dir>]
             [--adomd <AdomdClient.dll>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
from collections.abc import Callable
from decimal import MAX_EMAX, MIN_EMIN, ROUND_HALF_UP, Context, Decimal
from pathlib import Path

import desktop_gate
from desktop_gate import DEFAULT_ADOMD, GateRefusal, Unit

LOG = logging.getLogger("parity_check")


# A finite double's shortest repr has at most 17 significant digits and no digit below 1e-341, so rounding
# to more places than this cannot change it; capping keeps the decimal context bounded for any "round".
MAX_ROUND_PLACES = 400


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise GateRefusal(f"oracle repeats the key {key!r} in one object; JSON would keep only the last")
        result[key] = value
    return result


def _no_constant(name: str):
    raise GateRefusal(f"oracle holds {name}, which is not a JSON number")


def _is_expected_value(value) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def load_oracle(path: Path, run_root: Path, repo_root: Path) -> tuple[list[dict], str]:
    """Validate the oracle's location and shape; return its checks and SHA-256, else raise GateRefusal."""
    if not path.is_file():
        raise GateRefusal(f"oracle file not found: {path}")
    for root, label in ((run_root, "run root"), (repo_root, "checkout")):
        if desktop_gate.is_within(path, root):
            raise GateRefusal(f"the oracle must be kept outside the {label} ({root}), not at {path.resolve()}")
    links = path.stat().st_nlink
    if links > 1:
        raise GateRefusal(
            f"oracle file has {links} hard links and the location of the other links cannot be proven; "
            "copy it to a file of its own"
        )
    raw = path.read_bytes()
    try:
        spec = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_no_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateRefusal(f"oracle is not UTF-8 JSON: {exc}") from exc
    checks = spec.get("checks") if isinstance(spec, dict) else None
    if not isinstance(checks, list) or not checks:
        raise GateRefusal("oracle has no checks; an empty oracle proves nothing")
    seen: set[str] = set()
    for index, check in enumerate(checks):
        where = f"oracle check {index}"
        if not isinstance(check, dict):
            raise GateRefusal(f"{where} is not an object")
        cid = check.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise GateRefusal(f"{where} has no id")
        where = f"oracle check {cid!r}"
        if cid in seen:
            raise GateRefusal(f"{where} is duplicated")
        seen.add(cid)
        if not isinstance(check.get("dax"), str) or not desktop_gate.is_read_only(check["dax"]):
            raise GateRefusal(f"{where} dax is not a read-only EVALUATE or DEFINE ... EVALUATE query")
        keys = check.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise GateRefusal(f"{where} keys must be a list of column names")
        expected = check.get("expected")
        if not isinstance(expected, dict) or not all(isinstance(v, list) and v for v in expected.values()):
            raise GateRefusal(f"{where} expected must map row keys to non-empty value lists")
        if not all(_is_expected_value(value) for row in expected.values() for value in row):
            raise GateRefusal(f"{where} expected values must be finite numbers, text or null")
        if len({len(v) for v in expected.values()}) > 1:
            raise GateRefusal(f"{where} expected rows have different numbers of values")
        digits = check.get("round")
        if digits is not None and (isinstance(digits, bool) or not isinstance(digits, int) or digits < 0):
            raise GateRefusal(f"{where} round must be a non-negative integer or null")
        if not isinstance(check.get("exact_rows", False), bool):
            raise GateRefusal(f"{where} exact_rows must be true or false")
        if not expected and not check.get("exact_rows", False):
            raise GateRefusal(f"{where} expects no rows without exact_rows, so it could never fail")
        for field in ("title", "source"):
            if not isinstance(check.get(field, ""), str):
                raise GateRefusal(f"{where} {field} must be text")
    return checks, hashlib.sha256(raw).hexdigest()


def _quantize(value: float, digits: int) -> Decimal:
    """Round ties away from zero at digits places, in a decimal context precise enough for any finite double."""
    exact = Decimal(repr(value))
    places = min(digits, MAX_ROUND_PLACES)
    context = Context(prec=max(exact.adjusted(), 0) + places + 3, rounding=ROUND_HALF_UP, Emax=MAX_EMAX, Emin=MIN_EMIN)
    return exact.quantize(Decimal(1).scaleb(-places, context), context=context)


def same(actual, expected, digits: int | None) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(expected, str) or isinstance(actual, str):
        return str(actual) == str(expected)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and actual == expected
    got, want = float(actual), float(expected)
    if not (math.isfinite(got) and math.isfinite(want)):
        return got == want
    if digits is not None:
        return _quantize(got, digits) == _quantize(want, digits)
    return abs(got - want) <= 1e-9 * max(1.0, abs(want))


def _key_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return str(int(value))
    return str(value)


def row_key(values: list) -> str:
    return "|".join(_key_text(v) for v in values)


def run_check(fetch: Callable[[str], list[list]], check: dict) -> list[str]:
    """Compare one check's query result with its expected rows; return the problems found."""
    try:
        rows = fetch(check["dax"])
    except desktop_gate.UnreadableValue as exc:
        return [f"the query ran but a result value could not be read: {exc}"]
    except Exception as exc:  # noqa: BLE001  (ADOMD raises .NET exceptions)
        return [f"DAX error: {desktop_gate.dotnet_message(exc)}"]
    nkeys = len(check["keys"])
    actual: dict[str, list] = {}
    problems = []
    for row in rows:
        key = row_key(row[:nkeys])
        if key in actual:
            problems.append(f"duplicate row [{key}] in the query result")
        actual[key] = row[nkeys:]
    digits = check.get("round")
    for key, exp in check["expected"].items():
        if key not in actual:
            problems.append(f"missing row [{key}] expected {exp}")
        elif len(actual[key]) != len(exp) or not all(same(a, e, digits) for a, e in zip(actual[key], exp)):
            problems.append(f"[{key}] got {actual[key]} expected {exp}")
    if check.get("exact_rows", False):
        problems += [f"unexpected row [{k}] = {v}" for k, v in actual.items() if k not in check["expected"]]
    return problems


def check_all(fetch: Callable[[str], list[list]], checks: list[dict]) -> int:
    """Run and log every check; return the number that failed."""
    failed = 0
    for check in checks:
        problems = run_check(fetch, check)
        cells = sum(len(v) for v in check["expected"].values())
        status = "PASS" if not problems else "FAIL"
        LOG.info(
            "%s %-28s %-17s %5d values  %s", status, check["id"], check.get("source", ""), cells, check.get("title", "")
        )
        for problem in problems[:8]:
            LOG.info("       %s", problem)
        if len(problems) > 8:
            LOG.info("       ... %d more", len(problems) - 8)
        failed += bool(problems)
    return failed


def main(argv: list[str] | None = None) -> int:
    desktop_gate.utf8_output()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unit", required=True, type=Path, help="report unit, <run>/units/<key>")
    ap.add_argument("--pid", required=True, type=int, help="PBIDesktop.exe process id opened on the working .pbip")
    ap.add_argument("--oracle", required=True, type=Path, help="oracle JSON kept outside the run and checkout")
    ap.add_argument("--data-expression", help="shared M expression that must point at --data-path, e.g. DataFolder")
    ap.add_argument("--data-path", type=Path, help="data folder the oracle was computed from")
    ap.add_argument("--adomd", type=Path, default=DEFAULT_ADOMD, help="Microsoft.AnalysisServices.AdomdClient.dll")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if (args.data_expression is None) != (args.data_path is None):
        ap.error("--data-expression and --data-path go together")
    try:
        return _check(args)
    except Exception as exc:  # noqa: BLE001  (exit 3 must not read as a failed check)
        return desktop_gate.crashed(exc)


def _check(args: argparse.Namespace) -> int:
    try:
        repository = desktop_gate.repository_root()
        unit: Unit = desktop_gate.verified_unit(args.unit, repository)
        checks, digest = load_oracle(args.oracle, unit.run_root, repository)
        conn, instance = desktop_gate.connect_verified(unit, args.pid, args.adomd)
    except GateRefusal as exc:
        LOG.error("refusing to check: %s", exc)
        return 2
    try:
        if args.data_expression is not None:
            desktop_gate.require_data_source(conn, args.data_expression, args.data_path)
        LOG.info(
            "unit %s: pid %s port %s serves %s", unit.key, instance["pid"], instance["ports"][0], unit.working_pbip
        )
        LOG.info("oracle %s sha256 %s, %d checks", args.oracle.resolve(), digest, len(checks))
        if args.data_expression is None:
            LOG.info("data source not checked (no --data-expression/--data-path)")
        failed = check_all(lambda text: desktop_gate.read(conn, text)[1], checks)
    except GateRefusal as exc:
        LOG.error("refusing to check: %s", exc)
        return 2
    finally:
        conn.Close()
    LOG.info("%d/%d checks passed", len(checks) - failed, len(checks))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
