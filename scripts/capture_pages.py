"""
purpose: Capture every report page of one report unit's working .pbip open in Power BI Desktop, for render
         review against the approved Cognos evidence. It verifies the unit against its parent run.json and
         refuses unless --pid was opened on the unit's working .pbip. It resizes that pid's main window to a
         usable size WITHOUT activating it (a small window captures only the Filters pane), then runs
         `powerbi-desktop screenshot-all` and compares the number of PNGs with the pages listed in the
         working report's <name>.Report/definition/pages/pages.json (pageOrder, hidden pages included).

         Captures go to <unit>/captures/working unless --out is given. Inside the unit's run root --out
         must be below <unit>/captures/ (the validator uses <unit>/captures/validation), never inside any
         unit's baseline or working folder, and no folder or file on its path below the run root may be
         a link, junction or other reparse point (the captures folder itself included). It may never be
         inside this checkout or inside another migration run (any folder holding a run.json). Elsewhere
         it is free. An existing output folder is only cleared when it
         holds nothing but its capture_pages.json record and the PNGs that record names as this script's
         outputs; only those files and the record are deleted. The record's status ends as ok, suspect,
         incomplete, failed, crashed or interrupted (Ctrl+C: the PNGs written so far are recorded, then
         the interrupt is raised again). Anything else in the folder, or PNGs left beside a record still
         "running" (it names no pages) because the process was killed outright, refuses the next run
         (exit 2) and deletes nothing: look at the files, then move them away or choose another --out.

         Exit codes: 0 every page captured; 1 capture failed or is suspect: the report lists no pages, the
         number of PNGs differs from the page count, the CLI failed or timed out, no window, or a PNG is
         suspiciously small (inspect it: a sparse page can be genuine); 2 refused, nothing was captured;
         3 crashed: an unexpected error, printed on stderr as one "CRASHED: <type>: <message>" line, with
         the record finalised as crashed when it had been started.
usage:   python scripts/capture_pages.py --unit <run>/units/<key> --pid <pid> [--out <dir>]
             [--size 1600x1000] [--min-bytes 12000]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

import desktop_gate
from desktop_gate import GateRefusal

LOG = logging.getLogger("capture_pages")
RECORD = "capture_pages.json"
CLI_TIMEOUT_S = 300
SWP_NOZORDER, SWP_NOACTIVATE, GW_OWNER = 0x0004, 0x0010, 4
USER32 = ctypes.windll.user32 if sys.platform == "win32" else None


def output_folder(unit: desktop_gate.Unit, requested: Path | None, repo_root: Path) -> Path:
    """Resolve the capture folder; inside the run root only below <unit>/captures, never in the checkout."""
    captures = unit.unit_dir / "captures"
    target = requested if requested is not None else captures / "working"
    link = desktop_gate.reparse_point_on_path(target, unit.run_root)
    if link is not None:
        raise GateRefusal(f"captures may not be written through a link, junction or reparse point in the run: {link}")
    out = target.resolve()
    if desktop_gate.is_within(out, repo_root):
        raise GateRefusal(f"captures may not be written inside the checkout: {out}")
    if desktop_gate.is_within(out, unit.run_root):
        if desktop_gate.reparse_point_on_path(captures, unit.run_root) is not None:
            raise GateRefusal(f"the unit's captures folder is a link, junction or reparse point: {captures}")
        if not desktop_gate.is_within(out.parent, captures):
            raise GateRefusal(
                f"captures may not be written inside the run root except below {captures.resolve()}: {out}"
            )
        for project in (*unit.run_root.glob("units/*/baseline"), *unit.run_root.glob("units/*/working")):
            if desktop_gate.is_within(out, project):
                raise GateRefusal(f"captures may never be written inside a unit's baseline or working folder: {out}")
    other_run = next((folder for folder in (out, *out.parents) if (folder / "run.json").is_file()), None)
    if other_run is not None and not desktop_gate.is_within(other_run, unit.run_root):
        raise GateRefusal(f"captures may not be written inside another migration run ({other_run}): {out}")
    if out.exists() and not out.is_dir():
        raise GateRefusal(f"capture output is not a folder: {out}")
    return out


def expected_pages(unit: desktop_gate.Unit) -> list[str]:
    """The pageOrder of the working report's PBIR pages.json."""
    pages = unit.working_pbip.with_suffix(".Report") / "definition" / "pages" / "pages.json"
    try:
        spec = json.loads(pages.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateRefusal(f"cannot read the working report's page list {pages} (PBIR pages.json): {exc}") from exc
    order = spec.get("pageOrder") if isinstance(spec, dict) else None
    if not isinstance(order, list) or not all(isinstance(name, str) for name in order):
        raise GateRefusal(f"{pages} has no pageOrder list of page names")
    return order


def _recorded_outputs(out: Path) -> set[str] | None:
    """PNG names the folder's record lists as this script's outputs; None when the record is unusable."""
    try:
        record = json.loads((out / RECORD).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    pages = record.get("pages", []) if isinstance(record, dict) else None
    if not isinstance(pages, list):
        return None
    names = set()
    for page in pages:
        name = page.get("file") if isinstance(page, dict) else None
        if not isinstance(name, str) or Path(name).name != name or not name.casefold().endswith(".png"):
            return None
        names.add(name)
    return names


def clear_previous(out: Path) -> None:
    """Delete an earlier run's capture from out; refuse, deleting nothing, when it holds anything else."""
    if not out.exists():
        return
    entries = list(out.iterdir())
    if not entries:
        return
    named = _recorded_outputs(out)
    if named is None:
        raise GateRefusal(
            f"{out} holds files but no readable {RECORD} naming them as this script's outputs; "
            "choose another --out or move them away"
        )
    foreign = [
        p.name for p in entries if p.name != RECORD and (p.name not in named or p.is_symlink() or not p.is_file())
    ]
    if foreign:
        raise GateRefusal(
            f"{out} holds files this script did not write ({', '.join(sorted(foreign)[:5])}); "
            "choose another --out or move them away"
        )
    for path in entries:
        path.unlink()


def write_record(out: Path, record: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def captured_pages(out: Path, min_bytes: int) -> list[dict]:
    return [
        {"file": p.name, "bytes": p.stat().st_size, "suspect": p.stat().st_size < min_bytes}
        for p in sorted(out.glob("*.png"))
    ]


def main_window(pid: int) -> int | None:
    """Largest visible, unowned top-level window of the process."""
    found: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and USER32.IsWindowVisible(hwnd) and not USER32.GetWindow(hwnd, GW_OWNER):
            rect = wintypes.RECT()
            USER32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append(((rect.right - rect.left) * (rect.bottom - rect.top), hwnd))
        return True

    USER32.EnumWindows(visit, 0)
    return max(found)[1] if found else None


def resize_window(hwnd: int, width: int, height: int) -> None:
    USER32.SetWindowPos(hwnd, None, 0, 0, width, height, SWP_NOZORDER | SWP_NOACTIVATE)


def main(argv: list[str] | None = None) -> int:
    desktop_gate.utf8_output()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--unit", required=True, type=Path, help="report unit, <run>/units/<key>")
    ap.add_argument("--pid", required=True, type=int, help="PBIDesktop.exe process id opened on the working .pbip")
    ap.add_argument("--out", type=Path, help="output folder (default: <unit>/captures/working)")
    ap.add_argument("--size", default="1600x1000", help="window size WIDTHxHEIGHT before capture")
    ap.add_argument("--min-bytes", type=int, default=12000, help="smaller PNGs are reported as suspect")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        width, height = (int(v) for v in args.size.lower().split("x"))
    except ValueError:
        ap.error("--size must be WIDTHxHEIGHT, e.g. 1600x1000")

    started: dict = {}
    try:
        return _capture(args, width, height, started)
    except KeyboardInterrupt as exc:
        _finalise(started, args.min_bytes, "interrupted", f"interrupted ({type(exc).__name__})")
        raise
    except Exception as exc:  # noqa: BLE001  (exit 3 must not read as a failed capture)
        code = desktop_gate.crashed(exc)
        _finalise(started, args.min_bytes, "crashed", f"{type(exc).__name__}: {exc}")
        return code


def _finalise(started: dict, min_bytes: int, status: str, reason: str) -> None:
    """Close a started record so the PNGs already written are named as this script's outputs."""
    if not started:
        return
    try:
        pages = captured_pages(started["out"], min_bytes)
        write_record(started["out"], {**started["record"], "status": status, "reason": reason, "pages": pages})
    except Exception as again:  # noqa: BLE001
        print(f"CRASHED: the record could not be finalised either: {again}", file=sys.stderr)


def _capture(args: argparse.Namespace, width: int, height: int, started: dict) -> int:
    try:
        if USER32 is None:
            raise GateRefusal("page capture needs Windows and Power BI Desktop")
        repository = desktop_gate.repository_root()
        unit = desktop_gate.verified_unit(args.unit, repository)
        out = output_folder(unit, args.out, repository)
        pages = expected_pages(unit)
        desktop_gate.require_desktop(args.pid, unit.working_pbip)
        cli = shutil.which("powerbi-desktop")
        if not cli:
            raise GateRefusal("powerbi-desktop CLI not found on PATH")
        clear_previous(out)
    except GateRefusal as exc:
        LOG.error("refusing to capture: %s", exc)
        return 2

    record = {
        "unit": unit.key,
        "project": str(unit.working_pbip),
        "pid": args.pid,
        "expectedPages": len(pages),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }
    write_record(out, record)
    started.update(out=out, record=record)

    def failed(reason: str, status: str = "failed") -> int:
        LOG.error("%s", reason)
        write_record(out, {**record, "status": status, "reason": reason, "pages": captured_pages(out, args.min_bytes)})
        return 1

    if not pages:
        return failed("the working report lists no pages (pageOrder in pages.json is empty); nothing to capture")
    hwnd = main_window(args.pid)
    if hwnd is None:
        return failed(f"pid {args.pid} has no visible main window")
    resize_window(hwnd, width, height)
    time.sleep(2)

    cmd = [cli, "screenshot-all", "--pid", str(args.pid), "--output-dir", str(out), "--scale", "1", "--settle", "2000"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return failed(f"screenshot-all did not finish within {CLI_TIMEOUT_S} s")
    except OSError as exc:
        return failed(f"screenshot-all could not be started: {exc}")
    if done.returncode != 0:
        return failed(f"screenshot-all exit {done.returncode}: {(done.stderr or done.stdout or '').strip()[-600:]}")
    captured = captured_pages(out, args.min_bytes)
    for page in captured:
        LOG.info("%s %-40s %7d bytes", "SUSPECT" if page["suspect"] else "ok     ", page["file"], page["bytes"])
    if len(captured) != len(pages):
        return failed(
            f"captured {len(captured)} PNGs but the working report has {len(pages)} pages; inspect before trusting",
            "incomplete",
        )
    suspect = [page for page in captured if page["suspect"]]
    if suspect:
        return failed(
            f"{len(suspect)} of {len(captured)} captures are smaller than {args.min_bytes} bytes; "
            "inspect before trusting",
            "suspect",
        )
    write_record(out, {**record, "status": "ok", "pages": captured})
    LOG.info("%d pages captured to %s", len(captured), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
