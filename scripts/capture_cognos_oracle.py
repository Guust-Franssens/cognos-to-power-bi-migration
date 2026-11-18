"""
purpose: capture a Cognos Analytics oracle per report, headless, over the documented API-key login:
         the report spec, whole-report PDF / CSV / XLSX, a render per data container (the RDS HTML fragment,
         plus a PNG per chart image), a DataSetJSON data oracle per table-like container (lists, crosstabs;
         RDS serves a chart only as an image and does not address singletons, so those have their query named),
         and lineage (spec data items -> data module query items -> sources). One stable output folder
         per report: a capture is staged and swapped in only when it finished normally; before the swap, every
         complete output group (all files of one leg) that was ok in an earlier capture of that report (<id>,
         <id>.previous, <id>.incoming, newest first) for a leg this run did not capture ok is copied into it and
         named in the entry's "carried" (with its own captured_at; "outside_run" when the leg is not in this
         run's plan: a format not requested, a container no longer in the spec); a failed leg itself stays
         failed. Files of a leg that failed or was cut short in that earlier capture are never carried. A capture
         cut short (stop, crash, Ctrl+C) is kept as <id>.incoming and the old folder is left untouched; one cut
         short mid-swap puts the old folder back, or keeps it as <id>.previous (named, never deleted). A swap
         deletes the old folder and the <id>.incoming / <id>.previous it supersedes, naming in "dropped" every
         file of theirs neither replaced by this run's ok output nor carried in; otherwise a run deletes only
         what it created, and every other leftover (an older .incoming moved aside, .trash and .staging folders
         of earlier runs) is kept and named. Each report folder's capture.json records its own entry ("report":
         every leg with status and reason, carried, dropped; paths relative to that folder).
         manifest.json is rewritten by every run: it classifies every leg of this run's reports, and
         "earlier_reports" lists every other report folder in --out with its recorded results, checked by hash;
         a folder without a record adopts one from the manifest.json this run replaces (read first, named by
         recorded_in, saved into its capture.json only when every file matched), else "recorded": false.
         Folders and packages are walked recursively; a shortcut is resolved to its target and captured when
         that is a report; every other child (reportView, module, unresolved shortcut, ...) is named in
         discovery_skipped so it is never mistaken for coverage.
usage:   set COGNOS_API_KEY (or pass --api-key-file), then
             uv run --no-project --with requests python scripts\\capture_cognos_oracle.py ^
                 --server https://<host> --namespace <CAMNamespace> --out <dir> ^
                 [--folder-id <storeID> ...] [--report-id <storeID> ...] [--formats pdf,csv,xlsx] [--parts auto]
         ids must be Cognos storeIDs (i + 32 hex digits), deduplicated case-insensitively; every path is checked
         to stay inside --out.
exit:    0 every requested leg captured, or recorded unsupported (usage; chart data; viz data and an empty viz
           render; the measured singleton answers; unnamed containers; custom-control renders; Active Report
           viewer outputs, Active Report data controls and data tables), each named in data_unestablished /
           render_unestablished; skipped discovery children (discovery_skipped), unresolved lineage
           (lineage_unestablished) and other report folders in --out (other_captures) are named, not failed;
         1 some legs failed or were refused, including prompt_required and incomplete (see manifest.json), or
           no report was found to capture (manifest status "no_reports");
         2 stopped: login refused or the account lacks a capability (a human must act), or a usage error;
         3 crashed: an unexpected error; manifest status "crashed" with the error named;
         130 interrupted (Ctrl+C); manifest status "interrupted". A Ctrl+C or error after the last report
           (cleanup, stderr lines, the final manifest write) never escapes: an interrupt exits 130; an error
           exits 3 unless the run already stopped (2) or was interrupted (130); a cleanup error is named in
           cleanup_error.
deadline: --timeout (default 120 s) is a wall-clock budget per request, redirect hops included: the body is
         streamed and every socket read is limited to the time left (a server dripping its headers is only held
         to it per read). A leg makes at most 4 attempts (3 on transient errors or a viewer run still working
         after its poll, which gets one more attempt only, and one more after a re-login); a viewer attempt
         also polls up to 120 s; a render leg makes one request per chart image. The worst case
         per leg, and what each chart image adds, is printed at startup and stored in manifest.json (deadline).
secrets: the key is read from env or a file and redacted, plain or escaped (JSON, HTML entities, %-encoding),
         from the manifest, stdout, stderr and every TEXT output stored, judged by its extension (json, xml, html,
         csv, svg, txt; any svg content type is text; read as its BOM says, else as UTF-8, UTF-16LE, UTF-16BE
         and latin-1 in turn; the encoding is kept; counted in the leg's "redacted"); server text is redacted
         before it is cut into a reason; a part name holding it gets a hashed file name; binary outputs
         (pdf, xlsx, raster images) are
         stored as served, since rewriting them would corrupt them; outputs hold no session cookies or tokens;
         redirects are followed only on the --server host, so cookies and tokens never leave it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import sys
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import requests
import urllib3

SCHEMA = "cognos.oracle.v1"
LINEAGE_SCHEMA = "cognos.lineage.v1"
MAX_ATTEMPTS = 3
BACKOFF_S = (2.0, 5.0)
DEFAULT_TIMEOUT_S, LOGIN_TIMEOUT_S, MAX_REDIRECTS = 120.0, 60.0, 5
VIEWER_WAIT_S, VIEWER_POLL_S = 120.0, 1.5
CHUNK_BYTES = 65536
EXIT_INTERRUPTED = 130  # the shell convention for SIGINT (128 + 2)
STORE_ID_RE = re.compile(r"^i[0-9A-Fa-f]{32}$")
REPORT_TYPES = {"report", "interactiveReport"}
CONTAINER_TYPES = {"folder", "package"}
VIEWER_FORMATS = {"pdf": ("PDF", "report.pdf"), "csv": ("CSV", "report.csv"), "xlsx": ("spreadsheetML", "report.xlsx")}
PROMPT_TAGS = {
    "selectValue",
    "selectWithSearch",
    "selectWithTree",
    "selectDate",
    "selectTime",
    "selectDateTime",
    "selectInterval",
    "textBox",
    "dateBox",
    "timeBox",
    "dateTimeBox",
    "intervalBox",
    "generatedPrompt",
    "promptButton",
}
NON_PART_TAGS = {
    "page",
    "pageSet",
    "reportPage",
    "promptPage",
    # carry refQuery but feed a container or a query; measured in the 128 IBM sample specs
    "conditionalDataItem",
    "dsV5ListQuery",
    "queryRef",
    "defaultValue",
    "vcBaseline",
}
VIZ_TAGS = {"vizControl", "viz"}
CUSTOM_TAGS = {"customControl"}
# Active Report data controls: they carry a refQuery but filter other controls in the browser (spec census below)
APP_CONTROL_TAGS = {"appDataDropDownList", "appDataListBox", "appDataDeck"}
MEASURED_APP_CONTROL = "appDataDropDownList"
# a "Data table" reaches its query through refDataStore on the control itself (no refQuery, no *DataSet child)
LIST_CONTROL_TAGS = {"listControl"}
LIST_CONTROL_FACT = (
    'measured live on pub-hospital-admissions (Data table2): RDS answered DataSetJSON with {"dataSet":null} and '
    'HTMLFragment with an empty <div></div>, and the PDF viewer output prints "Data table rendering is not supported '
    'for this output type." in its place'
)
ERR_CODE_RE = re.compile(r"\b(CV-\d{4}|[A-Z]{2,4}-[A-Z]{2,4}-\d{4})\b")
# component prefixes of Cognos error codes; a fragment whose text OPENS with one of these is an error, not data
COGNOS_ERR_PREFIXES = {"CV", "RQP", "RSV", "XQE", "CNC", "CAM", "CM", "RDS", "QE", "UDA", "DPR", "PRS", "CCL", "DMB"}
# "has expired" or "timed out" as a message; a bare "Session expired" can be a header and a value
SESSION_EXPIRED_RE = re.compile(r"(?:your\s+|the\s+)?session\s+(?:has\s+(?:expired|timed\s+out)|timed\s+out)\b", re.I)
NBSP_RE = re.compile(r"&(?:nbsp|#160|#x0*a0);", re.I)
# the viewer page of an Active Report (measured on 12.1.2): complete, no b_action=rc URL, opens irot.htm instead
# irot.htm is anchored: it must open a path segment (measured as '../irot/irot.htm?v=...'), so airot/ never counts
ACTIVE_REPORT_RE = re.compile(
    r"""AR_Parameters\s*\[\s*['"]generatedOutputPath['"]\s*\]|(?<![^/'"\s=])irot/irot\.htm\b"""
)
ACTIVE_REPORT_REASON = (
    "Active Report: the viewer delivers this output only through irot.htm in a browser (the page names "
    "AR_Parameters generatedOutputPath and no b_action=rc output URL); measured on 12.1.2, generatedOutputPath "
    "served 0 bytes headlessly and RDS outputFormat answered RDS-ERR-1022, so no headless route yields it"
)
RENAME_ATTEMPTS, RENAME_BACKOFF_S = 3, 0.5
# stored outputs are redacted by what they ARE (their extension), not by what the server called them
TEXT_EXT = {".json", ".xml", ".html", ".csv", ".svg", ".txt"}
BINARY_EXT = {".pdf", ".xlsx", ".png", ".jpg", ".gif"}
TEXT_VIEWS = ("utf-8", "utf-16-le", "utf-16-be", "latin-1")
# which leg wrote a file of a capture folder; parts/* files are owned through their part's file stem
FILE_OWNERS = {
    "report.pdf": "pdf",
    "report.csv": "csv",
    "report.xlsx": "xlsx",
    "object.json": "object",
    "spec.xml": "spec",
    "lineage.json": "lineage",
    "module.json": "lineage",
}
# the pointer held_in() writes into a reason; stripped again when the output it names was lost
HELD_IN_RE = re.compile(r"its (values|render) (?:are|is) in report\.\w+(?: and report\.\w+)?, captured on this run")
VIZ_CSV_NOTE = (
    "it is the first data container, but report.csv holds the first table-like one (measured to skip a leading "
    "vizControl), so whether report.csv holds this viz is unverified; "
)
STATUS_RE = re.compile(r"""m_sStatus["']?\s*[:=]\s*["'](\w+)""")
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/svg+xml": ".svg"}
LIMITS = [
    "usage: the Cognos REST API exposes none; it lives in the audit database (COGIPF_RUNREPORT) when auditing is on",
    "lineage: module reports resolve to module query items and their sources; package (Framework Manager) "
    "reports stop at the package reference; references that do not resolve (macros, package items) are named "
    "in lineage_unestablished",
    "report.csv holds one data container only, the first table-like one (measured); parts/*.json is the "
    "per-visual data oracle",
    "viewer outputs: a run still working is polled (ui.action=wait) for up to 120 s; one still working then gets one "
    "more attempt (backoff first, within the leg's attempts), then is recorded incomplete",
    "prompts: the viewer and RDS run with prompt=false, so optional prompts take their defaults; a report that "
    "needs prompt values is recorded prompt_required (RDS-ERR-1021, or viewer status prompting)",
    "charts: RDS returns a chart as an image URL in DataSetJSON, DataSet and layoutDataXML (measured on vizControl), "
    "so a chart has a render oracle but no data oracle; its query is named so a sibling container can stand in",
    "viz (11.x visualizations): RDS DataSetJSON gave no answer in 60 s, once past 4 minutes (measured), so viz data "
    "is not requested and is recorded unsupported with its query; the HTMLFragment answers with one image whose src "
    "is empty (measured), recorded unsupported; any other viz fragment is judged as usual",
    "vizControl: DataSetJSON answered with an image link when measured, so its data is still requested; a hang like "
    "viz is not excluded and would cost up to 3 attempts of --timeout before the leg fails",
    "singletons: RDS does not address them (selection returns dataSet null and an empty fragment; the unselected "
    "DataSetJSON omits them; measured on 7 table-nested singletons); only those exact answers are recorded "
    "unsupported, any other answer is judged as for any part",
    "unnamed containers: RDS selects a container by name, so an unnamed one has no per-part oracle; it is recorded "
    "unsupported with its tag and query",
    "where a part reason says its values or render are in a viewer output, it names only report.pdf / report.xlsx "
    "legs captured ok on this run; otherwise it says no output captured on this run holds them",
    "custom controls: rendered in the browser by their JavaScript module, so the render leg is recorded unsupported; "
    "their data set is requested and judged like a list (RDS behaviour unmeasured)",
    'Active Reports (spec root application="true"): the viewer page is complete but names '
    "AR_Parameters generatedOutputPath and irot.htm instead of an output URL, for PDF, spreadsheetML, CSV, HTML and "
    "MHT alike; that path served 0 bytes headlessly and RDS outputFormat answered RDS-ERR-1022 (measured on 12.1.2), "
    "so each viewer leg that shows this page is recorded unsupported; a complete page with neither an output URL nor "
    "this marker is refused",
    "Active Report data controls (appDataDropDownList, appDataListBox, appDataDeck) filter other controls in the "
    "browser: RDS answered appDataDropDownList with no dataSet and an empty <div></div> (measured on 12.1.2), so "
    "neither leg is requested and both are recorded unsupported with the query; appDataListBox and appDataDeck are "
    "inferred to behave alike (unmeasured: their only sample reports need the absent Audit data source, "
    "XQE-DS-0009); appList (an Active Report data list) is requested and judged like a list (unmeasured)",
    "data tables (listControl, query through refDataStore): every one is discovered and both legs are recorded "
    "unsupported with the query and never requested: RDS answered a null dataSet and an empty <div></div> and the "
    "PDF prints a placeholder instead of it (measured on one report, pub-hospital-admissions; listControl occurs in 4 "
    "of the 149 specs of a local corpus outside this repo, 3 distinct reports; the repo holds only test fixtures); "
    "an ok pdf leg names them under omits, also with --parts none; "
    "whether report.xlsx or report.csv holds them is unmeasured",
    "renders: a fragment is credited only when it holds a <table> or an <img> and either has text once its tags are "
    "stripped or links server-rendered chart images, each fetched and validated (so an image-only chart with no text "
    "is credited through its images); a chart part must link at least one; all 99 fragments captured live passed; "
    "a fragment whose text opens with a Cognos error code or a session-expired message is refused",
    "discovery: folders are listed through /api/v1/content and a nested package through /bi/v1/objects (both measured "
    "to list a package); a content listing that answers 404 falls back to /bi/v1/objects; listings are read as one "
    "page (paging unmeasured); a shortcut is resolved in its own discovery leg (resolve:<id>, retried like any leg) "
    "through ?fields=target and its relative /bi/v1/path?path= URL (measured) and captured when its target is a "
    "report; report views, other non-report children and shortcuts to a non-report are named in discovery_skipped; "
    "a shortcut that does not resolve is named there too and fails its leg",
    "store: a capture is swapped in only when it finished normally; a rename refused by a transient lock is retried "
    f"{RENAME_ATTEMPTS} times with a short backoff; before the swap, each complete output group (every file of one "
    "leg) of an earlier capture of that id (<id>, <id>.previous, <id>.incoming, newest first) whose leg this run did "
    "not capture ok is copied in and named in carried with its own captured_at (a failed leg stays failed; a leg "
    "outside this run's plan, such as a format not requested or a container no longer in the spec, is marked "
    "outside_run); complete means ok in its source: listed under that leg in that folder's capture.json ok_files "
    "with a matching sha256 and whole by content, or, in a folder from an older version without that record, whole "
    "by content alone (a lineage naming its model kind, a module lineage with exactly the module.json it names, a "
    "render with one html and every chart image it links); a group whose leg failed or was cut short there is never "
    "carried; the swap removes the old folder and the <id>.incoming and <id>.previous it supersedes (named in "
    "superseded) and names in dropped every file of theirs neither replaced by this run's ok output nor carried in; "
    "otherwise a run deletes nothing it did not create: an older <id>.incoming "
    "met by a capture cut short is moved to .trash and named (earlier_incoming), a .staging/<id> left by a killed "
    "run is moved to .trash and named (earlier_staging), and .trash and .staging leftovers of earlier runs are named "
    "in other_captures; each report folder's capture.json records its own entry (report), and manifest.json, "
    "rewritten every run, lists every report folder this run did not capture in earlier_reports with that record "
    "checked by hash, or recorded false when it has none",
    "deadline: --timeout is a wall-clock budget per request, redirect hops included, with every body read limited to "
    "the time left (headers dripped slowly are only held to it per read); a leg makes at most 4 attempts (a viewer "
    "run still working counts as one of them); a viewer "
    "attempt also polls up to 120 s; a render leg adds one request per chart image; see manifest deadline",
]

SESSION_LOST, TRANSIENT, CAPABILITY = "session_lost", "transient", "capability"
REPORT_ERROR, NOT_FOUND, REFUSED = "report_error", "not_found", "refused"
PROMPT_REQUIRED, INCOMPLETE, UNSUPPORTED = "prompt_required", "incomplete", "unsupported"
STORE_ERROR = "store_error"


class Failure(Exception):
    """A classified failure of one leg."""

    def __init__(self, kind: str, reason: str):
        super().__init__(f"{kind}: {reason}")
        self.kind, self.reason = kind, reason


class Stop(Exception):
    """The whole run must stop: a human has to act (login refused, missing capability)."""


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def is_store_id(value: Any) -> bool:
    return isinstance(value, str) and bool(STORE_ID_RE.fullmatch(value))


def secret_pattern(secret: str) -> re.Pattern[str]:
    """The key as written plainly or in any escaped form a server or serializer may use for one of its characters:
    JSON (``\\u003d``, ``\\/``, ``\\"``, ``\\\\``), Python repr (``\\'``), HTML entities (``&#43;``, ``&#x2B;``,
    ``&amp;``) and %-encoding. Hex digits match in either case."""
    named = {
        "&": ["&amp;"],
        "<": ["&lt;"],
        ">": ["&gt;"],
        '"': ["&quot;", '\\"'],
        "'": ["&apos;", "\\'"],
        "\\": ["\\\\"],
        "/": ["\\/"],
    }
    parts = []
    for ch in secret:
        n = ord(ch)
        alts = [re.escape(ch), *(re.escape(a) for a in named.get(ch, []))]
        alts += [rf"&#0*{n};", rf"&#[xX]0*(?i:{n:x});"]
        if n < 0x10000:
            alts.append(rf"\\u(?i:{n:04x})")
        if n < 0x80:
            alts.append(rf"%(?i:{n:02x})")
        parts.append("(?:" + "|".join(alts) + ")")
    return re.compile("".join(parts))


def text_views(body: bytes) -> tuple[bytes, list[str]]:
    """A text body's byte-order mark and the encodings to read it with, the BOM's own first (the viewer CSV is
    UTF-16LE with a BOM, measured). Without a BOM every view is tried, latin-1 last (lossless for any byte), so a
    BOM-less UTF-16 body served without a charset is still searched for the key."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if body.startswith(bom):
            return bom, [enc, *(e for e in TEXT_VIEWS if e != enc)]
    return b"", list(TEXT_VIEWS)


def redact_text(text: str, pattern: re.Pattern[str] | None) -> tuple[str, int]:
    return pattern.subn("<key>", text) if pattern else (text, 0)


# the key of the running capture; server text is scrubbed with it BEFORE it is cut into a reason, since a cut
# through the key leaves a prefix that the full pattern no longer matches when the manifest is written
_SECRET_RE: re.Pattern[str] | None = None


def scrub(text: str) -> str:
    return redact_text(text, _SECRET_RE)[0]


def drop_viewer_pointers(reason: str) -> str:
    """A reason that names a viewer output, rewritten once that output was lost."""
    reason = reason.replace(VIZ_CSV_NOTE, "")
    return HELD_IN_RE.sub(lambda m: f"no output captured on this run holds its {m.group(1)}", reason)


def inside(base: Path, path: Path) -> Path:
    """Refuse any path that resolves outside base, before anything is written or deleted there."""
    root, target = base.resolve(), path.resolve()
    if target == root or root not in target.parents:
        raise Failure(REFUSED, f"path escapes the output folder: {path.name!r}")
    return target


def is_reparse_point(path: Path) -> bool:
    """Whether a path is a symlink, junction or other Windows reparse point."""
    try:
        info = path.lstat()
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def worst_case_leg_s(timeout: float, chart_images: int = 0) -> float:
    """Announced deadline of one leg: 4 attempts of its slowest shape, plus backoff and two re-login calls.

    A viewer attempt is a page, polls for VIEWER_WAIT_S (plus one sleep and one in-flight poll) and the output;
    a render attempt is the fragment plus one request per chart image. Each request is held to ``timeout``.
    """
    attempts = MAX_ATTEMPTS + 1  # transient retries, plus one more after a re-login
    viewer = 3 * timeout + VIEWER_WAIT_S + VIEWER_POLL_S
    render = (1 + chart_images) * timeout
    backoff = sum(BACKOFF_S[min(i, len(BACKOFF_S) - 1)] for i in range(MAX_ATTEMPTS - 1))
    return round(attempts * max(viewer, render) + backoff + 2 * LOGIN_TIMEOUT_S)


def error_snippet(text: str, code: str) -> str:
    text = scrub(text)
    i = max(text.find(code), 0)
    s = re.sub(r"<[^>]+>", " ", text[i : i + 300])
    return re.sub(r"\s+", " ", s).strip()[:200]


def classify(status: int, text: str) -> tuple[str, str]:
    """Classify a failed response by its Cognos error code, then by HTTP status."""
    codes = ERR_CODE_RE.findall(text or "")
    reason = error_snippet(text, codes[0]) if codes else f"HTTP {status}"
    if "CV-0015" in codes:
        return CAPABILITY, reason
    # RDS-ERR-1020 was measured on a session missing its passport cookies; CAM-AAA is inferred
    if status == 401 or "RDS-ERR-1020" in codes or any(c.startswith("CAM-AAA-") for c in codes):
        return SESSION_LOST, reason
    if status in (429, 502, 503, 504):
        return TRANSIENT, reason
    if "RDS-ERR-1021" in codes:
        return PROMPT_REQUIRED, reason
    if codes:
        return REPORT_ERROR, reason
    if status == 404:
        return NOT_FOUND, reason
    if status >= 500:
        return TRANSIENT, reason
    return REPORT_ERROR, reason


def amp(s: str) -> str:
    # Cognos escapes only '&' in attributes; a full html.unescape would turn '&not=' into a symbol
    return s.replace("&amp;", "&").replace("&#38;", "&")


def to_bi_path(url: str | None, host: str) -> str | None:
    """Reduce a Cognos URL to a /bi-relative path on OUR server, or None (cookies never go elsewhere)."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme and parts.scheme not in ("http", "https"):
        return None
    if parts.netloc and (parts.hostname or "").lower() != host.lower():
        return None
    if not parts.path.startswith("/bi/"):
        return None
    return parts.path[3:] + (f"?{parts.query}" if parts.query else "")


def viewer_status(page: str) -> str | None:
    m = STATUS_RE.search(page)
    return m.group(1) if m else None


def viewer_error(page: str) -> Failure:
    """Classify a viewer page that is not complete, prompting or working.

    Only reached once the status has been read, so a code-shaped report title on a good page never decides
    the outcome; the page title is dropped because it carries the report name, not the error.
    """
    body = re.sub(r"(?is)<title\b.*?</title>", " ", page)
    if ERR_CODE_RE.search(body):
        return Failure(*classify(200, body))
    return Failure(REPORT_ERROR, f"viewer status {viewer_status(page) or 'missing'}")


def viewer_field(page: str, key: str) -> str | None:
    """A string field of the viewer's initViewer({...}) state, e.g. the conversation token a wait needs."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
    return m.group(1) if m else None


def extract_rc_path(page: str, host: str) -> str | None:
    """The viewer page embeds the output URL: a pdf.js 'file=' parameter, or a quoted b_action=rc URL."""
    m = re.search(r"pdf\.js/web/viewer\.html\?file=([^\"'&\s]+)", page)
    if m:
        return to_bi_path(unquote(amp(m.group(1))), host)
    m = re.search(r"[\"'](/bi/v1/disp\?b_action=rc[^\"']+)[\"']", page)
    return to_bi_path(amp(m.group(1)), host) if m else None


def is_active_report_page(page: str) -> bool:
    """A viewer page that hands its output to irot.htm (an Active Report) instead of embedding an output URL."""
    return bool(ACTIVE_REPORT_RE.search(page))


def is_active_report_spec(spec_xml: str) -> bool:
    """Cognos marks an Active Report with application="true" on the spec root (5 of the 128 IBM samples)."""
    try:
        return ET.fromstring(spec_xml).get("application") == "true"
    except ET.ParseError:
        return False


def fragment_text(fragment: str) -> str:
    """The visible text of a fragment: tags dropped, non-breaking spaces as spaces, whitespace collapsed."""
    return re.sub(r"\s+", " ", NBSP_RE.sub(" ", re.sub(r"<[^>]*>", " ", fragment))).strip()


def fragment_error(text: str) -> str | None:
    """An error message in the error position: the fragment's text OPENS with a Cognos error code or a
    session-expired message. A list or crosstab opens with its headers, so a code-like value further in is data."""
    text = scrub(text)
    m = ERR_CODE_RE.match(text)
    if m and m.group(1).split("-")[0] in COGNOS_ERR_PREFIXES:
        return text[:120]
    if SESSION_EXPIRED_RE.match(text):
        return text[:120]
    return None


def held_in(part: dict, what: str) -> str:
    """Where a part's values or render can be read instead: only viewer outputs captured ok on this run."""
    names = ("report.pdf", "report.xlsx") if what == "values" else ("report.pdf",)
    outs = [n for n in names if n in (part.get("viewer_ok") or [])]
    if outs:
        verb = "are" if what == "values" else "is"
        return f"its {what} {verb} in {' and '.join(outs)}, captured on this run"
    return f"no output captured on this run holds its {what}"


def app_control_reason(part: dict) -> str:
    """Why an Active Report data control has no per-part oracle; measured for the drop-down only."""
    tag, query = part["tag"], part.get("refQuery")
    if tag == MEASURED_APP_CONTROL:
        why = (
            "measured on 12.1.2 (Data drop-down list1): RDS answers DataSetJSON with no dataSet and HTMLFragment "
            "with an empty <div></div>"
        )
    else:
        why = (
            f"inferred from {MEASURED_APP_CONTROL}, not measured (the sample reports holding it fail on the trial "
            "with XQE-DS-0009)"
        )
    return (
        f"Active Report data control {tag}: an interaction control filled in the browser, not a server-rendered "
        f"container; {why}; not requested; query {query}; {held_in(part, 'values')}"
    )


def list_control_reason(part: dict) -> str:
    """Why a Data table (listControl) has no per-part oracle; never points at report.pdf, which omits it."""
    return (
        f"data table {part['tag']}: {LIST_CONTROL_FACT}; so neither leg is requested; query {part.get('refQuery')}; "
        "report.pdf does not hold it (it prints that placeholder), and whether report.xlsx or report.csv does is "
        "unmeasured"
    )


def image_paths(fragment: str, host: str) -> list[str]:
    """Server-rendered chart images in an HTMLFragment (static decoration images are not under /bi)."""
    out: list[str] = []
    for src in re.findall(r"<img\b[^>]*?\bsrc=\"([^\"]+)\"", fragment):
        p = to_bi_path(amp(src), host)
        if p and p.startswith("/v1/disp") and p not in out:
            out.append(p)
    return out


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def is_chart(tag: str) -> bool:
    return tag in VIZ_TAGS or tag.startswith("v2_") or tag.lower().endswith(("chart", "gauge", "map"))


def discover_parts(spec_xml: str) -> list[dict]:
    """Data containers (lists, crosstabs, singletons, charts, visualizations, custom controls, data tables);
    prompts excluded. A data table (listControl) names its data store on itself and is always a part.

    A ``*DataSet`` element (vcDataSet, vizDataSet, customControlDataSet, appListDataSet) is never a part itself:
    it feeds the container that holds it, directly or through a ``*DataSets`` wrapper. A container without a
    name is returned with ``name`` None (RDS cannot select it), so it is recorded rather than dropped.
    """
    root = ET.fromstring(spec_xml)
    parent = {child: el for el in root.iter() for child in el}
    # a vizControl reaches its query indirectly: vcDataSet@refDataStore -> reportDataStore -> dsV5ListQuery@refQuery
    stores = {
        el.get("name"): next((d.get("refQuery") for d in el.iter() if d.get("refQuery")), None)
        for el in root.iter()
        if local(el.tag) == "reportDataStore" and el.get("name")
    }

    def query_of(el: ET.Element) -> str | None:
        return el.get("refQuery") or stores.get(el.get("refDataStore"))

    hosts: dict[ET.Element, str | None] = {}
    for el in root.iter():
        if not local(el.tag).endswith("DataSet"):
            continue
        host = parent.get(el)
        if host is not None and local(host.tag).endswith("DataSets"):
            host = parent.get(host)
        if host is not None and not hosts.get(host):
            hosts[host] = query_of(el)

    parts: list[dict] = []
    seen: set[str] = set()
    unnamed: dict[str, int] = {}
    for el in root.iter():
        tag, name = local(el.tag), el.get("name")
        if tag in PROMPT_TAGS or tag in NON_PART_TAGS or tag.endswith("DataSet"):
            continue
        if not (el.get("refQuery") or tag in VIZ_TAGS or tag in LIST_CONTROL_TAGS or el in hosts):
            continue
        query = el.get("refQuery") or hosts.get(el) or next((q for d in el.iter() if (q := query_of(d))), None)
        if name:
            if name in seen:
                continue
            seen.add(name)
            label = name
        else:
            unnamed[tag] = unnamed.get(tag, 0) + 1
            label = f"unnamed:{tag}:{unnamed[tag]}"
        parts.append({"name": name, "label": label, "tag": tag, "refQuery": query, "chart": is_chart(tag)})
    return parts


def safe_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "part"
    return s if s == name else f"{s}-{hashlib.sha1(name.encode()).hexdigest()[:6]}"


def unique_stem(name: str, used: set[str]) -> str:
    """A file stem for a part, unique case-insensitively (List1 and list1 are one file on Windows and macOS)."""
    stem = safe_name(name)
    if stem.lower() in used:
        stem = f"{stem}-{hashlib.sha1(name.encode()).hexdigest()[:6]}"
    base, n = stem, 1
    while stem.lower() in used:
        n += 1
        stem = f"{base}-{n}"
    used.add(stem.lower())
    return stem


def validate(kind: str, content_type: str, body: bytes) -> None:
    """Refuse empty or wrong-type successes instead of crediting them as evidence."""
    ct = (content_type or "").lower()
    if not body:
        raise Failure(REFUSED, "empty body")
    if kind == "pdf" and ("pdf" not in ct or not body.startswith(b"%PDF")):
        raise Failure(REFUSED, f"not a PDF ({ct or 'no content type'})")
    if kind == "xlsx" and ("spreadsheetml" not in ct or not body.startswith(b"PK")):
        raise Failure(REFUSED, f"not an XLSX ({ct or 'no content type'})")
    if kind == "csv" and (not ("csv" in ct or ct.startswith("text/plain")) or b"<html" in body[:600].lower()):
        raise Failure(REFUSED, f"not a CSV ({ct or 'no content type'})")
    if kind == "image" and (not ct.startswith("image/") or (ct == "image/png" and not body.startswith(b"\x89PNG"))):
        raise Failure(REFUSED, f"not an image ({ct or 'no content type'})")


def validate_fragment(content_type: str, text: str) -> None:
    """Refuse a render that is not an RDS HTML fragment (measured: text/xml, a body that opens with <div>)."""
    ct = (content_type or "").lower()
    if "xml" not in ct and "html" not in ct:
        raise Failure(REFUSED, f"not an HTML fragment ({ct or 'no content type'})")
    body = text.lstrip()
    if not body.lower().startswith("<div") or re.search(r"<(html|head|body|form)\b", body[:4000], re.I):
        raise Failure(REFUSED, "not an HTML fragment (a page, not a <div> fragment)")


def dataset_rows(payload: dict) -> int:
    tables = (payload.get("dataSet") or {}).get("dataTable") or []
    return sum(len(t.get("row") or []) for t in tables if isinstance(t, dict))


def image_only(payload: dict, host: str) -> bool:
    """True when every row is a lone link to a rendered image: how RDS returns a chart instead of its data."""
    tables = (payload.get("dataSet") or {}).get("dataTable") or []
    rows = [row for t in tables if isinstance(t, dict) for row in t.get("row") or []]
    return bool(rows) and all(
        isinstance(row, dict)
        and set(row) == {"value"}
        and (to_bi_path(str(row["value"]), host) or "").startswith("/v1/disp")
        and "SM=query" in str(row["value"])
        for row in rows
    )


# ---------------------------------------------------------------- lineage


def model_refs(expression: str) -> list[str]:
    """Model-qualified references ([ns].[subject].[item]) as dotted ids, the form module metadata uses."""
    refs = []
    for m in re.finditer(r"(?:\[[^\[\]]+\]\.)+\[[^\[\]]+\]", expression or ""):
        rid = ".".join(re.findall(r"\[([^\[\]]+)\]", m.group(0)))
        if rid not in refs:
            refs.append(rid)
    return refs


def module_index(meta: dict) -> dict[str, dict]:
    """idForExpression -> query item / calculation facts, with the source each query subject reads."""
    uses = meta.get("useSpec") or []
    uses = uses if isinstance(uses, list) else [uses]
    by_id = {u.get("identifier"): u for u in uses if isinstance(u, dict)}

    def sources(subject: dict | None) -> list[dict]:
        out = []
        for ref in (subject or {}).get("ref") or []:
            use_id, _, table = str(ref).partition(".")
            u = by_id.get(use_id, {})
            out.append({"use": use_id, "table": table, "type": u.get("type"), "searchPath": u.get("searchPath")})
        return out

    index: dict[str, dict] = {}

    def walk(node: Any, subject: dict | None) -> None:
        if isinstance(node, list):
            for v in node:
                walk(v, subject)
            return
        if not isinstance(node, dict):
            return
        if "idForExpression" in node and "item" in node:
            subject = node
            index[node["idForExpression"]] = {"kind": "querySubject", "label": node.get("label")}
            index[node["idForExpression"]]["sources"] = sources(node)
        qi = node.get("queryItem")
        if isinstance(qi, dict) and qi.get("idForExpression"):
            index[qi["idForExpression"]] = {
                "kind": "queryItem",
                **{k: qi.get(k) for k in ("label", "datatype", "usage", "regularAggregate", "expression")},
                "querySubject": (subject or {}).get("idForExpression"),
                "sources": sources(subject),
            }
        for v in node.values():
            walk(v, subject)

    walk({k: v for k, v in meta.items() if k != "calculation"}, None)
    calcs = meta.get("calculation") or []
    for c in calcs if isinstance(calcs, list) else [calcs]:
        if isinstance(c, dict) and c.get("idForExpression"):
            index[c["idForExpression"]] = {
                "kind": "calculation",
                "label": c.get("label"),
                "expression": c.get("expression"),
            }
    return index


def build_lineage(report_id: str, spec_xml: str, model: dict, meta: dict | None) -> dict:
    root = ET.fromstring(spec_xml)
    queries, all_refs = [], []
    for q in (e for e in root.iter() if local(e.tag) == "query"):
        items, filters = [], []
        for e in q.iter():
            tag = local(e.tag)
            expr = next((c.text or "" for c in e if local(c.tag) in ("expression", "filterExpression")), None)
            if expr is None or tag not in ("dataItem", "detailFilter", "summaryFilter"):
                continue
            refs = model_refs(expr)
            all_refs += [r for r in refs if r not in all_refs]
            local_refs = sorted(set(re.findall(r"(?<![\]\.])\[([^\[\]]+)\](?!\.\[)", expr)))
            rec = {"expression": expr.strip(), "model_refs": refs}
            if tag == "dataItem":
                items.append({"name": e.get("name"), "aggregate": e.get("aggregate"), **rec, "item_refs": local_refs})
            else:
                filters.append({"kind": tag, "use": e.get("use"), **rec})
        queries.append({"name": q.get("name"), "data_items": items, "filters": filters})
    index = module_index(meta) if meta else {}
    resolved = {r: index.get(r) for r in all_refs}
    return {
        "schema": LINEAGE_SCHEMA,
        "report_id": report_id,
        "model": model,
        "containers": [{k: p[k] for k in ("name", "tag", "refQuery")} for p in discover_parts(spec_xml)],
        "queries": queries,
        "model_items": resolved,
        "resolved": sum(1 for v in resolved.values() if v),
        "referenced": len(resolved),
        "unresolved": [r for r, v in resolved.items() if not v],
    }


def search_path_names(search_path: str) -> list[str]:
    return [n.replace("''", "'") for n in re.findall(r"@name='((?:[^']|'')*)'", search_path or "")]


# ---------------------------------------------------------------- client


def fetch(session: requests.Session, method: str, url: str, deadline: float, **kw: Any) -> requests.Response:
    """One HTTP exchange held to a wall-clock deadline (time.monotonic()).

    A requests timeout applies per socket read, so a server dripping a byte a second ran 7 s past timeout=3
    (measured by the reviewer). The body is therefore streamed, the deadline checked between chunks, and each
    read limited to the time left.
    """

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failure(TRANSIENT, "request exceeded the --timeout deadline")
        return remaining

    try:
        resp = session.request(method, url, timeout=left(), allow_redirects=False, stream=True, **kw)
    except requests.RequestException as e:
        raise Failure(TRANSIENT, type(e).__name__) from e
    try:
        raw = resp.raw
        # the live socket: the pooled connection's, or (once http.client let go of it on Connection: close) the
        # one under the response's own file object
        sock = getattr(getattr(raw, "connection", None), "sock", None) or getattr(
            getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None
        )
        # read1 returns after one socket read, so a dripping body cannot hold a 64 KB read past the deadline
        read = getattr(raw, "read1", None) or raw.read
        chunks = []
        while True:
            budget = left()
            if sock is not None and sock.fileno() != -1:  # http.client closes it once a closing body is read
                sock.settimeout(budget)
            chunk = read(CHUNK_BYTES, decode_content=True)
            if not chunk:
                break
            chunks.append(chunk)
        release = getattr(raw, "release_conn", None)
        if release is not None:
            release()
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as e:
        resp.close()
        raise Failure(TRANSIENT, type(e).__name__) from e
    except Failure:
        resp.close()
        raise
    resp._content = b"".join(chunks)  # what .content, .text and .json() read once the stream is consumed
    return resp


class Client:
    """Documented API-key login, then /api/v1 and /bi calls that forward ALL session cookies.

    Login cookies are path-scoped to /api; /bi routes need them sent explicitly (measured: without the
    capability cookies the viewer answers CV-0015 and RDS answers RDS-ERR-1020/1022).
    """

    def __init__(self, server: str, namespace: str, key: str, timeout: float = DEFAULT_TIMEOUT_S):
        self.server, self.namespace, self._key, self.timeout = server.rstrip("/"), namespace, key, timeout
        base = urlsplit(self.server)
        self.host, self.origin = base.hostname or "", f"{base.scheme}://{base.netloc}"
        self.session: requests.Session | None = None

    def login(self) -> None:
        s = requests.Session()
        try:
            fetch(s, "GET", f"{self.server}/api/v1/session", time.monotonic() + LOGIN_TIMEOUT_S)
            body = {
                "parameters": [
                    {"name": "CAMNamespace", "value": self.namespace},
                    {"name": "CAMAPILoginKey", "value": self._key},
                ]
            }
            # fetch never follows a redirect: a 307/308 would resend the key in the body to wherever it points
            r = fetch(
                s,
                "PUT",
                f"{self.server}/api/v1/session",
                time.monotonic() + LOGIN_TIMEOUT_S,
                json=body,
                headers={"X-XSRF-TOKEN": s.cookies.get("XSRF-TOKEN") or ""},
            )
        except Failure as e:
            raise Stop(f"login failed: {e.reason}") from e
        if r.status_code != 201:
            codes = ERR_CODE_RE.findall(r.text.replace(self._key, "<key>"))
            raise Stop(f"login refused: HTTP {r.status_code} {codes[0] if codes else ''}".strip())
        self.session = s

    def logout(self) -> int | None:
        if self.session is None:
            return None
        try:
            return self.request("DELETE", "/api/v1/session").status_code
        except Failure:
            return None

    def request(self, method: str, path: str, **kw: Any) -> requests.Response:
        """One call on OUR server. Redirects are followed by hand and only on this host, so the cookie jar
        and the XSRF token are never sent elsewhere; a redirect to another host is refused. Every hop is
        charged to one deadline of --timeout."""
        if self.session is None:
            raise Failure(SESSION_LOST, "not logged in")
        url = f"{self.server}{path}"
        deadline = time.monotonic() + self.timeout
        for _ in range(MAX_REDIRECTS + 1):
            # rebuilt every hop: a cookie set by a redirect hop (it lands in the session jar) goes on the next one
            jar = "; ".join(f"{c.name}={c.value}" for c in self.session.cookies)
            xsrf = self.session.cookies.get("XSRF-TOKEN") or ""
            headers = {"Cookie": jar, "X-XSRF-TOKEN": xsrf, "Accept": "*/*"}
            before = {(c.name, c.domain, c.path): c.value for c in self.session.cookies}
            resp = fetch(self.session, method, url, deadline, headers=headers, **kw)
            self._drop_superseded(before)
            if not 300 <= resp.status_code < 400 or not resp.headers.get("location"):
                break
            target = urljoin(url, resp.headers["location"])
            parts = urlsplit(target)
            if (parts.hostname or "").lower() != self.host.lower():
                raise Failure(REFUSED, f"redirect to another host ({parts.hostname or 'unknown'})")
            # keep the configured scheme and port: a Location may carry a bogus ':80' on https (measured on img src)
            url = f"{self.origin}{parts.path}" + (f"?{parts.query}" if parts.query else "")
            if resp.status_code in (301, 302, 303) and method != "HEAD":
                method = "GET"
                kw = {k: v for k, v in kw.items() if k not in ("data", "json", "files")}
        else:
            raise Failure(REFUSED, f"more than {MAX_REDIRECTS} redirects")
        final = (urlsplit(resp.url or url).hostname or "").lower()
        if final != self.host.lower():
            raise Failure(REFUSED, f"response came from another host ({final or 'unknown'})")
        return resp

    def _drop_superseded(self, before: dict[tuple, str]) -> None:
        """A cookie a response just set replaces any same-named one on another path or domain (login sets
        Path=/api; a /bi hop may set it again elsewhere), so the stale value is never sent ahead of the new one."""
        jar = self.session.cookies  # type: ignore[union-attr]  # request() checked the session
        fresh = [c for c in jar if before.get((c.name, c.domain, c.path)) != c.value]
        for new in fresh:
            for old in list(jar):
                if old.name == new.name and (old.domain, old.path) != (new.domain, new.path):
                    jar.clear(old.domain, old.path, old.name)

    def api(self, method: str, path: str, **kw: Any) -> requests.Response:
        return self.request(method, f"/api/v1{path}", **kw)

    def bi(self, method: str, path: str, **kw: Any) -> requests.Response:
        return self.request(method, f"/bi{path}", **kw)


def ok(resp: requests.Response) -> requests.Response:
    """Raise a classified Failure for an HTTP error or an RDS error body (RDS can answer 200 with one)."""
    head = resp.content[:4000]
    if not 200 <= resp.status_code < 300 or b"rds:error" in head or b"<rds:message" in head:
        raise Failure(*classify(resp.status_code, resp.text))
    return resp


def json_body(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError as e:
        raise Failure(REFUSED, "response is not JSON") from e


# ---------------------------------------------------------------- capture


class Capturer:
    def __init__(
        self,
        client: Client,
        out: Path,
        formats: list[str],
        parts: str,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client, self.formats, self.parts_mode, self.sleep = client, formats, parts, sleep
        # resolved once, like every path inside() returns, so a relative or 8.3 short --out relativizes the same
        self.out = Path(out).resolve()
        self.reauths = 0
        self._modules: list[dict] | None = None
        self._meta: dict[str, dict | None] = {}
        # the API key, redacted from every text output this run stores (a server could echo it back)
        self._secret: str | None = getattr(client, "_key", None) or None
        self._secret_re = secret_pattern(self._secret) if self._secret else None
        global _SECRET_RE  # noqa: PLW0603 - classify() and fragment_error() scrub server text before cutting it
        _SECRET_RE = self._secret_re
        # .trash entries this run keeps (an old capture not put back, an earlier .incoming or .staging moved aside)
        self.kept_trash: set[str] = set()
        # staging and .trash folders this run created: the only ones the end of the run may delete
        self.created: list[Path] = []
        # per report id: the folder each carried file came from, so a swap can name what it drops ({src: {rel}})
        self.carried_from: dict[str, dict[str, set[str]]] = {}

    # ---- legs

    @staticmethod
    def new_leg(name: str) -> dict:
        return {"leg": name, "status": "pending", "retries": 0, "retry_reasons": [], "reauths": 0}

    def run_leg(self, rec: dict, fn: Callable[[], Any]) -> Any:
        """Run one leg: back off on transient errors and once on a viewer still working, re-login once on a lost
        session, record the rest. Every retry shares the same attempt budget, so the announced deadline holds."""
        start, reauthed, attempt, waited = time.monotonic(), False, 0, False
        rec["captured_at"] = now()
        try:
            while True:
                attempt += 1
                try:
                    try:
                        result = fn()
                    except ET.ParseError as e:
                        raise Failure(REPORT_ERROR, f"spec is not parseable XML: {e}") from e
                    rec["status"] = "ok"
                    return result
                except Failure as f:
                    again = f.kind == TRANSIENT or (f.kind == INCOMPLETE and not waited)
                    if again and attempt < MAX_ATTEMPTS:
                        waited = waited or f.kind == INCOMPLETE
                        rec["retries"] += 1
                        rec["retry_reasons"].append(f"{f.kind}: {f.reason}")
                        self.sleep(BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)])
                        continue
                    if f.kind == SESSION_LOST and not reauthed:
                        reauthed = True
                        rec["retries"] += 1
                        rec["retry_reasons"].append(f"{f.kind}: {f.reason}")
                        try:
                            self.client.login()
                        except Stop:
                            rec.update({"status": "failed", "class": SESSION_LOST, "reason": "re-login refused"})
                            raise
                        self.reauths += 1
                        rec["reauths"] += 1
                        continue
                    status = {REFUSED: "refused", UNSUPPORTED: "unsupported"}.get(f.kind, "failed")
                    rec.update({"status": status, "class": f.kind})
                    rec["reason"] = f.reason
                    if f.kind == CAPABILITY:
                        raise Stop(f"{rec['leg']}: {f.reason}") from f
                    return None
        finally:
            rec["elapsed_s"] = round(time.monotonic() - start, 3)

    def store(self, rec: dict, stage: Path, rel: str, body: bytes, content_type: str | None) -> None:
        path = inside(self.out, stage / rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        body, redacted = self.redact_body(body, content_type, rel)
        path.write_bytes(body)
        rec.update(
            {
                "path": f"{stage.name}/{rel}",
                "content_type": content_type,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        if redacted:
            rec["redacted"] = redacted

    def redact_body(self, body: bytes, content_type: str | None, rel: str = "") -> tuple[bytes, int]:
        """Replace the API key, plain or escaped, in a text output (json, xml, html, csv, svg), decided by the
        stored file's extension first and its content type only for an unknown extension; binary outputs (pdf,
        xlsx, raster images) are stored as served, since rewriting them would corrupt them. The body keeps its
        encoding and byte-order mark, and is rewritten only when the key was found."""
        ct = (content_type or "").lower()
        ext = Path(rel).suffix.lower()
        text = ext in TEXT_EXT or (
            ext not in BINARY_EXT and any(t in ct for t in ("json", "xml", "html", "csv", "svg", "text/"))
        )
        if not self._secret_re or not text:
            return body, 0
        bom, views = text_views(body)
        raw = body[len(bom) :]
        for enc in views:
            data, tail = raw, b""
            if enc.startswith("utf-16") and len(raw) % 2:
                # a truncated UTF-16 body ends in half a code unit: search the rest and keep that byte as served
                data, tail = raw[:-1], raw[-1:]
            try:
                decoded = data.decode(enc)
            except UnicodeDecodeError:
                continue
            new, count = redact_text(decoded, self._secret_re)
            if count:
                return bom + new.encode(enc) + tail, count
        return body, 0

    def holds_secret(self, text: str) -> bool:
        return bool(self._secret_re and self._secret_re.search(text))

    def rename(self, src: Path, dst: Path) -> None:
        """Rename, retrying a PermissionError a few times: on Windows a freshly written folder can be held
        briefly by the indexer or a virus scanner (4 of 120 back-to-back reruns, measured by the reviewer)."""
        for attempt in range(RENAME_ATTEMPTS):
            try:
                src.rename(dst)
                return
            except PermissionError:
                if attempt == RENAME_ATTEMPTS - 1:
                    raise
                self.sleep(RENAME_BACKOFF_S * (attempt + 1))

    # ---- discovery

    def list_children(self, cid: str, route: str) -> list:
        """Children of a folder (/api/v1/content) or of a package or a folder inside one (/bi/v1/objects)."""
        if route == "content":
            data = json_body(ok(self.client.api("GET", f"/content/{cid}/items")))
        else:
            data = json_body(ok(self.client.bi("GET", f"/v1/objects/{cid}/items?fields=defaultName,type,id")))
        kids = data.get("content", data.get("data")) if isinstance(data, dict) else None
        if not isinstance(kids, list):
            raise Failure(REFUSED, "listing holds no content or data array")
        return kids

    def resolve_shortcut(self, sid: str) -> dict:
        """A shortcut's target in two calls (measured): ``?fields=target`` names the target by search path with an
        empty id, and its relative ``/bi/v1/path`` URL answers with the object. Any other URL is never followed."""
        data = json_body(ok(self.client.api("GET", f"/content/{sid}?fields=target")))
        targets = data.get("target") if isinstance(data, dict) else None
        if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
            raise Failure(REFUSED, "shortcut holds no single target")
        url: Any = targets[0]
        for key in ("_meta", "links", "path", "url"):
            url = url.get(key) if isinstance(url, dict) else None
        parts = urlsplit(url) if isinstance(url, str) and "\\" not in url else None
        if (
            not parts
            or parts.scheme
            or parts.netloc
            or parts.fragment
            or parts.path != "/bi/v1/path"
            or not parse_qs(parts.query).get("path")
        ):
            raise Failure(REFUSED, f"target path URL is not a relative /bi/v1/path?path= URL: {scrub(str(url))[:80]!r}")
        data = json_body(ok(self.client.bi("GET", f"/v1/path?{parts.query}")))
        objs = data.get("data") if isinstance(data, dict) else None
        if not isinstance(objs, list) or len(objs) != 1 or not isinstance(objs[0], dict):
            raise Failure(REFUSED, "target path did not resolve to exactly one object")
        if not is_store_id(objs[0].get("id")):
            raise Failure(REFUSED, f"target {objs[0].get('type')!r} has no Cognos storeID: {objs[0].get('id')!r}")
        return objs[0]

    def discover(
        self,
        folder_ids: Iterable[str],
        report_ids: Iterable[str],
        legs: list[dict] | None = None,
        skipped: list[dict] | None = None,
        found: list[tuple[str, str]] | None = None,
    ) -> tuple[list[tuple[str, str]], list[dict], list[dict]]:
        """Reports below each folder or package, recursively; every other child is named in ``skipped``.

        Ids are deduplicated case-insensitively. ``legs``, ``skipped`` and ``found`` are filled in place, so a
        stop or a crash in the middle of discovery still leaves them on record.
        """
        legs = [] if legs is None else legs
        skipped = [] if skipped is None else skipped
        found = [] if found is None else found
        visited: set[str] = set()

        def add(rid: str, origin: str) -> None:
            if all(rid.lower() != r.lower() for r, _ in found):
                found.append((rid, origin))

        for r in report_ids:
            add(r, "report-id")

        def listing(cid: str, route: str) -> tuple[str, list]:
            try:
                return route, self.list_children(cid, route)
            except Failure as f:
                # the content route listed a package live; a 404 there still falls back to /bi/v1/objects
                if f.kind == NOT_FOUND and route == "content":
                    return "objects", self.list_children(cid, "objects")
                raise

        def shortcut(kid: str, name: Any, parent: str, origin: str) -> None:
            rec = {"id": kid, "type": "shortcut", "name": name, "parent": parent}
            # its own leg, so a timeout is retried, a lost session re-logs in and CV-0015 stops the run
            leg = self.new_leg(f"resolve:{kid}")
            legs.append(leg)
            target = self.run_leg(leg, lambda: self.resolve_shortcut(kid))
            if target is None:
                reason = (
                    f"shortcut: target not resolved ({leg.get('class')}: {leg.get('reason')}; leg resolve:{kid}); "
                    "capture it with --report-id"
                )
                skipped.append({**rec, "reason": reason})
                return
            tid, ttype = target.get("id"), target.get("type")
            leg.update({"target_id": tid, "target_type": ttype})
            if ttype in REPORT_TYPES:
                add(tid, f"{origin}/shortcut:{kid}")
                return
            reason = f"shortcut to a {ttype} ({target.get('defaultName')!r}): not a report; not followed"
            skipped.append({**rec, "target_id": tid, "target_type": ttype, "reason": reason})

        def walk(cid: str, origin: str, route: str) -> None:
            if cid.lower() in visited:
                return
            visited.add(cid.lower())
            rec = self.new_leg(f"list:{cid}")
            legs.append(rec)
            res = self.run_leg(rec, lambda: listing(cid, route))
            if res is None:
                return
            used, kids = res
            rec.update({"route": used, "items": len(kids)})
            for k in kids:
                k = k if isinstance(k, dict) else {}
                kid, kind, name = k.get("id"), k.get("type"), k.get("defaultName") or k.get("name")
                if not is_store_id(kid):
                    reason = "id is not a Cognos storeID; not followed"
                    skipped.append({"id": kid, "type": kind, "name": name, "parent": cid, "reason": reason})
                elif kind in REPORT_TYPES:
                    add(kid, origin)
                elif kind in CONTAINER_TYPES:
                    # a nested package and everything inside it is listed through /bi/v1/objects
                    walk(kid, origin, "objects" if kind == "package" or used == "objects" else "content")
                elif kind == "shortcut":
                    shortcut(kid, name, cid, origin)
                else:
                    reason = {
                        "reportView": "report view: runs another report's spec with its own settings; not captured",
                    }.get(str(kind), f"type {kind}: not a report")
                    skipped.append({"id": kid, "type": kind, "name": name, "parent": cid, "reason": reason})

        for fid in folder_ids:
            walk(fid, f"folder:{fid}", "content")
        return found, legs, skipped

    # ---- report legs

    def get_object(self, rid: str, stage: Path, rec: dict) -> dict:
        fields = "defaultName,type,ancestors,modificationTime,module,metadataModelPackage"
        data = json_body(ok(self.client.bi("GET", f"/v1/objects/{rid}?fields={fields}")))
        obj = (data.get("data") or [None])[0] if isinstance(data, dict) else None
        if not isinstance(obj, dict):
            raise Failure(REFUSED, "object response has no data")
        clean = strip_meta(obj)
        self.store(rec, stage, "object.json", json.dumps(clean, indent=1).encode(), "application/json")
        return clean

    def get_spec(self, rid: str, stage: Path, rec: dict) -> str:
        data = json_body(ok(self.client.api("GET", f"/content/{rid}?fields=specification")))
        spec = data.get("specification") if isinstance(data, dict) else None
        if not spec and isinstance(data, dict) and data.get("data"):
            spec = data["data"][0].get("specification")
        if not spec:
            raise Failure(REFUSED, "no specification (not a report, or no read access)")
        self.store(rec, stage, "spec.xml", spec.encode("utf-8"), "application/xml")
        return spec

    def get_viewer(self, rid: str, fmt: str, stage: Path, rec: dict) -> None:
        out_fmt, rel = VIEWER_FORMATS[fmt]
        obj = quote(f'storeID("{rid}")', safe="")
        page = ok(
            self.client.bi(
                "GET",
                f"/v1/disp?b_action=cognosViewer&ui.action=run&ui.object={obj}"
                f"&run.outputFormat={out_fmt}&run.prompt=false",
            )
        ).text
        page = self.wait_viewer(page)
        status = viewer_status(page)
        if status == "prompting":
            raise Failure(PROMPT_REQUIRED, "report needs prompt values")
        if status in ("working", "stillWorking"):
            raise Failure(INCOMPLETE, f"viewer still {status} after {VIEWER_WAIT_S:.0f} s of polling")
        if status != "complete":
            raise viewer_error(page)
        rc = extract_rc_path(page, self.client.host)
        if not rc:
            if is_active_report_page(page):
                rec["active_report"] = True
                raise Failure(UNSUPPORTED, ACTIVE_REPORT_REASON)
            raise Failure(REFUSED, "viewer page holds no output URL")
        resp = ok(self.client.bi("GET", rc))
        ct = resp.headers.get("content-type", "")
        validate(fmt, ct, resp.content)
        self.store(rec, stage, rel, resp.content, ct)

    def wait_viewer(self, page: str) -> str:
        """Poll a still-working viewer run with ui.action=wait (measured: the next page carries the output URL)."""
        deadline = time.monotonic() + VIEWER_WAIT_S
        for _ in range(int(VIEWER_WAIT_S / VIEWER_POLL_S)):
            if viewer_status(page) not in ("working", "stillWorking") or time.monotonic() >= deadline:
                break
            tokens = {
                "ui.conversation": viewer_field(page, "m_sConversation"),
                "m_tracking": viewer_field(page, "m_sTracking"),
                "cv.actionState": viewer_field(page, "m_sActionState"),
            }
            if not tokens["ui.conversation"]:
                raise Failure(REPORT_ERROR, "viewer is working but its page holds no conversation to wait on")
            self.sleep(VIEWER_POLL_S)
            form = {"b_action": "cognosViewer", "ui.action": "wait", "ui.primaryAction": "run", "cv.id": "_NS_"}
            form.update({k: v for k, v in tokens.items() if v})
            page = ok(self.client.bi("POST", "/v1/disp", data=form)).text
        return page

    def get_part_data(self, rid: str, part: dict, stage: Path, rec: dict) -> None:
        if part["tag"] == "viz":
            # measured: no answer in 60 s (once past 4 minutes), so the request is never sent
            csv_ok = "report.csv" in (part.get("viewer_ok") or [])
            where = VIZ_CSV_NOTE + held_in(part, "values") if part.get("first") and csv_ok else held_in(part, "values")
            raise Failure(
                UNSUPPORTED,
                f"RDS DataSetJSON does not answer for viz; not requested; query {part.get('refQuery')}; {where}",
            )
        sel = quote(part["name"], safe="")
        resp = ok(
            self.client.bi(
                "GET", f"/v1/disp/rds/reportData/report/{rid}?fmt=DataSetJSON&async=off&prompt=false&selection={sel}"
            )
        )
        payload = json_body(resp)
        # measured on 7 table-nested singletons: exactly {"dataSet": null}; any other shape is judged as usual
        if part["tag"] == "singleton" and payload == {"dataSet": None}:
            raise Failure(
                UNSUPPORTED, f"RDS does not address singletons; query {part.get('refQuery')}; {held_in(part, 'values')}"
            )
        if not isinstance(payload, dict) or not payload.get("dataSet"):
            raise Failure(REFUSED, "no dataSet in DataSetJSON (an unknown selection returns dataSet null)")
        if image_only(payload, self.client.host):
            # measured on charts only; a list, crosstab or singleton answering with an image is not data
            if not part["chart"]:
                raise Failure(REFUSED, f"{part['tag']} answered with an image link, not data")
            raise Failure(UNSUPPORTED, f"RDS returned an image link, not data; query {part.get('refQuery')}")
        rows = dataset_rows(payload)
        if rows == 0:
            raise Failure(REFUSED, "empty dataset")
        rec["rows"] = rows
        stem = part.get("file") or safe_name(part["name"])
        self.store(rec, stage, f"parts/{stem}.json", resp.content, resp.headers.get("content-type"))

    def get_part_render(self, rid: str, part: dict, stage: Path, rec: dict) -> None:
        sel = quote(part["name"], safe="")
        resp = ok(
            self.client.bi(
                "GET", f"/v1/disp/rds/reportData/report/{rid}?fmt=HTMLFragment&async=off&prompt=false&selection={sel}"
            )
        )
        # measured on 7 table-nested singletons: exactly <div></div>; any other shape is judged as usual
        if part["tag"] == "singleton" and resp.text.strip() == "<div></div>":
            raise Failure(UNSUPPORTED, f"RDS does not address singletons; {held_in(part, 'render')}")
        validate_fragment(resp.headers.get("content-type") or "", resp.text)
        text = resp.text
        imgs = re.findall(r"<img\b[^>]*>", text, re.I)
        visible = fragment_text(text)
        bare = visible.replace(" ", "")
        # measured on viz: exactly one image with src="" and nothing else; any other viz fragment is judged as usual
        empty_viz = len(imgs) == 1 and re.search(r'\ssrc=""', imgs[0]) and not bare and "<table" not in text.lower()
        if part["tag"] == "viz" and empty_viz:
            raise Failure(
                UNSUPPORTED, f"RDS does not render viz (one image with an empty src); {held_in(part, 'render')}"
            )
        if not re.search(r"<(table|img)\b", text, re.I):
            raise Failure(REFUSED, "fragment holds no table or image (an unknown selection returns <div></div>)")
        err = fragment_error(visible)
        if err:
            raise Failure(REFUSED, f"fragment holds an error message, not a render: {err!r}")
        paths = image_paths(text, self.client.host)
        if not bare and not paths:
            raise Failure(REFUSED, "empty fragment: no text and no chart image")
        base = f"parts/{part.get('file') or safe_name(part['name'])}"
        if part["chart"] and not paths:
            raise Failure(REFUSED, "chart part rendered no image")
        # chart images are only served to the session that rendered the fragment (other sessions: 304);
        # every image is fetched and validated before anything is written, so a refused render leaves no file
        fetched = []
        for path in paths:
            img = ok(self.client.bi("GET", path))
            ct = (img.headers.get("content-type") or "").split(";")[0].strip().lower()
            validate("image", ct, img.content)
            fetched.append((ct, img.content))
        self.store(rec, stage, f"{base}.html", resp.content, resp.headers.get("content-type"))
        images = []
        for i, (ct, body) in enumerate(fetched):
            sub: dict = {}
            ext = IMAGE_EXT.get(ct) or (".svg" if "svg" in ct else ".img")  # any svg is text: redacted like one
            self.store(sub, stage, f"{base}_{i}{ext}", body, ct)
            images.append(sub)
        rec["images"] = images

    def get_lineage(self, rid: str, spec: str, obj: dict | None, stage: Path, rec: dict) -> None:
        model = self.resolve_model(obj, spec)
        if model["kind"] == "module" and not model.get("id"):
            # raised before anything is stored: a failed leg writes no file, so an earlier lineage is carried whole
            raise Failure(NOT_FOUND, f"module not found: {model.get('searchPath')}")
        meta = self.module_metadata(model["id"]) if model["kind"] == "module" else None
        if meta is not None:
            model["metadata"] = "module.json"
        lineage = build_lineage(rid, spec, model, meta)
        if meta is not None:
            # stored per report (staged with it), so a later run can never leave a stale shared copy behind
            sub: dict = {}
            self.store(sub, stage, "module.json", json.dumps(meta, indent=1).encode(), "application/json")
            rec["module"] = sub
        self.store(rec, stage, "lineage.json", json.dumps(lineage, indent=1).encode(), "application/json")
        rec.update(
            {
                "model_kind": model["kind"],
                "resolved": lineage["resolved"],
                "referenced": lineage["referenced"],
                "unresolved": lineage["unresolved"],
            }
        )

    def resolve_model(self, obj: dict | None, spec: str) -> dict:
        obj = obj or {}
        mod, pkg = obj.get("module") or [], obj.get("metadataModelPackage") or []
        if mod:
            return {"kind": "module", "searchPath": mod[0].get("searchPath"), "id": mod[0].get("id")}
        if pkg:
            return {"kind": "package", "searchPath": pkg[0].get("searchPath"), "id": pkg[0].get("id")}
        mp = next((e for e in ET.fromstring(spec).iter() if local(e.tag) == "modelPath"), None)
        path = (mp.text or "").strip() if mp is not None else ""
        if "/module[@name=" in path or (mp is not None and mp.get("type") == "module"):
            return {"kind": "module", "searchPath": path, "id": self.module_id(path)}
        if path:
            return {"kind": "package", "searchPath": path, "id": None}
        return {"kind": "unknown", "searchPath": None, "id": None}

    def module_id(self, search_path: str) -> str | None:
        if self._modules is None:
            data = json_body(ok(self.client.api("GET", "/modules")))
            self._modules = data.get("modules", []) if isinstance(data, dict) else []
        want = search_path_names(search_path)
        for m in self._modules:
            names = [a.get("name") for a in (m.get("ancestors") or [])[1:]] + [m.get("name")]
            if names == want:
                return m.get("id")
        return None

    def module_metadata(self, mid: str) -> dict:
        if not is_store_id(mid):
            raise Failure(REFUSED, f"module id is not a Cognos storeID: {scrub(str(mid))[:40]!r}")
        if mid not in self._meta:
            self._meta[mid] = json_body(ok(self.client.api("GET", f"/modules/{mid}/metadata")))
        return self._meta[mid] or {}

    def capture_report(self, rid: str, origin: str, reports: list[dict]) -> dict:
        """Capture one report into a staging folder, then swap it in as <out>/<id> (one stable folder).

        The entry is appended to ``reports`` first, so a Stop mid-report still leaves its legs on record.
        """
        entry: dict = {"id": rid, "origin": origin, "name": None, "path": None, "parts": [], "legs": []}
        reports.append(entry)
        if not is_store_id(rid):
            reason = "id is not a Cognos storeID; nothing written"
            entry["legs"].append({**self.new_leg("report"), "status": "refused", "class": REFUSED, "reason": reason})
            return entry
        stage = inside(self.out, self.out / ".staging" / rid)
        if stage.exists() or stage.is_symlink():
            # left by a run that was killed mid-swap: it may hold the only copy of a capture, so keep it and name it
            earlier = inside(self.out, self.out / ".trash" / f"{rid}.staging-{uuid.uuid4().hex[:8]}")
            try:
                earlier.parent.mkdir(parents=True, exist_ok=True)
                self.rename(stage, earlier)
            except OSError as e:
                reason = (
                    f"an earlier run's .staging/{rid} could not be moved aside ({type(e).__name__}); nothing written"
                )
                entry["legs"].append(
                    {**self.new_leg("store"), "status": "failed", "class": STORE_ERROR, "reason": reason}
                )
                return entry
            note = f"left in .staging by an earlier run that was killed; {presence(self.out, rid)}"
            entry["earlier_staging"] = {"path": earlier.relative_to(self.out).as_posix(), "note": note}
        stage.mkdir(parents=True)
        self.created.append(stage)
        # who wrote this folder and when: lets a later run name the captures it did not touch (other_captures);
        # ok_files, added before the swap or when the capture is kept aside, names the files of each ok leg
        stamp = {"schema": SCHEMA, "id": rid, "origin": origin, "captured_at": now(), "server": self.client.server}
        self.store({}, stage, "capture.json", json.dumps(stamp, indent=1).encode(), "application/json")

        def leg(name: str, fn: Callable[[dict], Any], **extra: Any) -> Any:
            rec = {**self.new_leg(name), **extra}
            entry["legs"].append(rec)
            return self.run_leg(rec, lambda: fn(rec))

        def skip(name: str, reason: str) -> None:
            entry["legs"].append({**self.new_leg(name), "status": "skipped", "reason": reason})

        try:
            obj = leg("object", lambda r: self.get_object(rid, stage, r))
            if obj:
                entry["name"] = obj.get("defaultName")
                entry["path"] = "/".join(a.get("defaultName", "?") for a in obj.get("ancestors") or [])
            spec = leg("spec", lambda r: self.get_spec(rid, stage, r))
            if spec and is_active_report_spec(spec):
                entry["active_report"] = 'spec root carries application="true" (information only; not a short-cut)'
            for fmt in self.formats:
                leg(fmt, lambda r, f=fmt: self.get_viewer(rid, f, stage, r))
            if spec is None:
                if self.parts_mode != "none":
                    skip("parts", "spec unavailable")
                skip("lineage", "spec unavailable")
            else:
                self.capture_parts(rid, spec, stage, entry, leg)
                leg("lineage", lambda r: self.get_lineage(rid, spec, obj, stage, r))
            entry["legs"].append({**self.new_leg("usage"), "status": "unsupported", "reason": LIMITS[0]})
            carry_error = self.carry_forward(stage, rid, entry)
        except BaseException as e:
            # a capture cut short must never replace a complete one: keep it aside, leave the old folder alone
            why = f"the run ended before this capture finished ({type(e).__name__}); the old capture is untouched"
            self.keep_incoming(stage, rid, entry, why, cut_class(e))
            raise
        if carry_error:
            why = f"earlier outputs could not be carried in ({carry_error}); the old capture is untouched"
            self.keep_incoming(stage, rid, entry, why, STORE_ERROR, finished=True)
            return entry
        self.swap(stage, rid, entry)
        return entry

    def carry_forward(self, stage: Path, rid: str, entry: dict) -> str | None:
        """Copy in each complete output group (all files of one leg) of an earlier capture of this id whose leg
        this run did not capture ok. Sources are <id>, <id>.previous and <id>.incoming, newest capture first; a
        group is taken from one source only, and only when it was ok there (see complete_groups): a group whose leg
        failed, was cut short or whose copy broke off is never carried, so it can never replace a complete one in
        an older source. A failed leg's own files are replaced by the carried group. Each copy is named in
        ``carried`` with its own captured_at; its leg keeps this run's status. The stage's capture.json records
        ok_files (this run's ok legs and every group carried whole), so a later run can tell the same.

        Returns None, or why a copy failed (the caller then keeps this capture aside and leaves <id> alone).
        """
        labels = {p["file"]: p["label"] for p in entry["parts"] if p.get("file")}
        taken = {r["leg"] for r in entry["legs"] if r.get("status") == "ok"}
        planned = {r["leg"] for r in entry["legs"]}
        parts_known = any(r["leg"] == "parts" and r.get("status") == "ok" for r in entry["legs"]) or bool(labels)
        taken_from: dict[str, set[str]] = self.carried_from.setdefault(rid, {})
        sources = []
        for suffix in ("", ".previous", ".incoming"):
            src = inside(self.out, self.out / f"{rid}{suffix}")
            if src.is_dir() and not src.is_symlink():
                sources.append((str(read_stamp(src).get("captured_at") or ""), src))
        sources.sort(key=lambda s: s[0], reverse=True)  # ISO UTC stamps sort as text; unstamped last
        carried: list[dict] = entry.setdefault("carried", [])
        whole: dict[str, dict[str, str]] = {}
        try:
            for stamped, src in sources:
                stamp = read_stamp(src)
                dates = stamp.get("carried")
                dates = dates if isinstance(dates, dict) else {}
                for owner, rels in complete_groups(src, stamp, labels, self.client.host).items():
                    if owner in taken or owner.startswith("file:"):  # not an output this script writes
                        continue
                    taken.add(owner)
                    self.drop_failed_files(stage, rid, entry, owner, labels)
                    group: dict[str, str] = {}
                    for rel in rels:
                        dst = inside(stage, stage / rel)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        tmp = dst.with_name(dst.name + ".part")
                        shutil.copyfile(src / rel, tmp)
                        os.replace(tmp, dst)
                        body = dst.read_bytes()
                        group[rel] = hashlib.sha256(body).hexdigest()
                        taken_from.setdefault(src.name, set()).add(rel)
                        item = {
                            "path": f"{rid}/{rel}",
                            "leg": owner,
                            "bytes": len(body),
                            "sha256": group[rel],
                            "captured_at": dates.get(rel) or stamped or None,
                            "from": src.name,
                        }
                        if owner not in planned:
                            item["outside_run"] = outside_run(owner, parts_known)
                        carried.append(item)
                    whole[owner] = group
        except OSError as e:
            return f"{type(e).__name__}: {e.strerror or e}"
        finally:
            if not carried:
                entry.pop("carried", None)
            try:
                stamp = read_stamp(stage)
                stamp["ok_files"] = {**ok_files(entry, rid), **whole}
                if carried:
                    stamp["carried"] = {c["path"][len(rid) + 1 :]: c["captured_at"] for c in carried}
                stamp["report"] = report_record(entry, rid)
                self.store({}, stage, "capture.json", json.dumps(stamp, indent=1).encode(), "application/json")
            except OSError:
                pass  # without the record a later run judges this folder by its content, as an older one
        return None

    def drop_failed_files(self, stage: Path, rid: str, entry: dict, owner: str, labels: dict[str, str]) -> None:
        """Before a group is carried in: remove what this run's failed ``owner`` leg left in the stage, and the
        paths that pointed at it, so the manifest never names a file whose bytes it does not describe."""
        for rel in capture_files(stage):
            if owner_of(rel, labels) == owner:
                (stage / rel).unlink()
        for rec in entry["legs"]:
            if rec["leg"] == owner:
                for r in files_of(rec):
                    if str(r.get("path") or "").startswith(f"{rid}/"):
                        r["path"] = None

    def keep_incoming(self, stage: Path, rid: str, entry: dict, why: str, cls: str, finished: bool = False) -> None:
        """Keep the staged capture as <id>.incoming, point every path in the entry there, record a failed
        ``store`` leg, and leave <out>/<id> untouched, so the manifest never lists files that do not exist.

        An older <id>.incoming is moved to .trash and named (earlier_incoming), never deleted. If a move fails, every
        leg that had written a file is failed (store_error): its file is gone, and no reason may still say a viewer
        output holds a part's values. An interrupt in any of these steps is raised only once the entry matches the
        disk. ``finished`` marks a capture whose legs all ran (a later run names it as such).
        """
        incoming = inside(self.out, self.out / f"{rid}.incoming")
        pending: BaseException | None = None

        def guarded(fn: Callable[[], Any]) -> bool:
            nonlocal pending
            try:
                fn()
                return True
            except OSError:
                return False
            except BaseException as e:  # noqa: BLE001 - raised below, once the entry matches the disk
                pending = pending or e
                return False

        def move(src: Path, dst: Path) -> bool:
            # an interrupt in the backoff gets one immediate attempt; the disk, not the call, says if it moved
            if not guarded(lambda: self.rename(src, dst)) and pending is not None and src.exists():
                guarded(lambda: src.rename(dst))
            return dst.exists() and not src.exists()

        def mark() -> None:
            stamp = read_stamp(stage)
            stamp["kept_aside"] = {"finished": finished, "class": cls, "why": why, "at": now()}
            # the groups carry_forward copied whole are already recorded; add this run's ok legs (paths are <id>/...)
            record = stamp.get("ok_files")
            stamp["ok_files"] = {**(record if isinstance(record, dict) else {}), **ok_files(entry, rid)}
            # the entry as it will read once kept: a leg still pending is cut short, and the store leg says where
            kept = report_record(entry, rid)
            for rec in kept["legs"]:
                if rec.get("status") == "pending":
                    rec.update({"status": "failed", "class": cls, "reason": "the run ended before this leg finished"})
            store = f"{why}; new capture kept in {incoming.name}"
            kept["legs"].append({**self.new_leg("store"), "status": "failed", "class": cls, "reason": store})
            stamp["report"] = kept
            self.store({}, stage, "capture.json", json.dumps(stamp, indent=1).encode(), "application/json")

        guarded(mark)
        blocked = False
        if incoming.exists() or incoming.is_symlink():
            earlier = inside(self.out, self.out / ".trash" / f"{incoming.name}-{uuid.uuid4().hex[:8]}")
            note = incoming_note(incoming)
            guarded(lambda: earlier.parent.mkdir(parents=True, exist_ok=True))
            if move(incoming, earlier):
                rel = earlier.relative_to(self.out).as_posix()
                entry["earlier_incoming"] = {"path": rel, "note": f"{note}; moved aside, {presence(self.out, rid)}"}
            else:
                blocked = True
                entry["stale_incoming"] = (
                    f"{incoming.name} holds an earlier capture, not this run's; it could not be moved aside"
                )
        moved = not blocked and move(stage, incoming)
        prefix = f"{rid}/"
        for rec in entry["legs"]:
            lost = False
            for r in files_of(rec):
                if moved and str(r.get("path", "")).startswith(prefix):
                    r["path"] = f"{incoming.name}/{r['path'][len(prefix) :]}"
                elif not moved and r.get("path"):
                    r["path"] = None
                    lost = True
            if lost and rec.get("status") == "ok":
                rec.update({"status": "failed", "class": STORE_ERROR, "reason": "captured, but its file was lost"})
        if moved:
            for c in entry.get("carried", []):
                c["path"] = f"{incoming.name}/{c['path'][len(prefix) :]}"
        else:
            entry.pop("carried", None)
            for rec in entry["legs"]:
                if rec.get("reason"):
                    rec["reason"] = drop_viewer_pointers(rec["reason"])
        entry["stored_as"] = incoming.name if moved else None
        where = f"kept in {incoming.name}" if moved else "lost: the staging folder could not be moved either"
        reason = f"{why}; new capture {where}"
        entry["legs"].append({**self.new_leg("store"), "status": "failed", "class": cls, "reason": reason})
        if pending is not None:
            raise pending

    def swap(self, stage: Path, rid: str, entry: dict) -> None:
        """Move the old capture aside, then the new one in; never delete the old one before that succeeded.

        If a rename fails (a file in the old folder is open, for example) or the run is cut short in a rename
        backoff (Ctrl+C), the old capture is put back (when even that fails it is kept under a visible name, named in
        the entry and never deleted), the new capture is kept as <id>.incoming (see keep_incoming), and an interrupt
        is raised again. On success the <id>.incoming and <id>.previous it supersedes (their outputs were carried in
        first) are removed and named in ``superseded``.
        """
        final = inside(self.out, self.out / rid)
        aside: Path | None = None
        had_old = final.exists()
        try:
            aside = inside(self.out, self.out / ".trash" / f"{rid}-{uuid.uuid4().hex[:8]}")
            if had_old:
                aside.parent.mkdir(parents=True, exist_ok=True)
                self.created.append(aside)
                self.rename(final, aside)
            self.rename(stage, final)
            entry.update({"stored_as": rid, "replaced_old": had_old})
        except BaseException as e:
            if aside is None:
                if isinstance(e, Failure):
                    why = f"old capture folder could not be replaced ({type(e).__name__}: {e}); the old capture is untouched"
                    self.keep_incoming(stage, rid, entry, why, STORE_ERROR, finished=True)
                    return
                why = f"the run ended before the capture could be swapped in ({type(e).__name__}); the old capture is untouched"
                self.keep_incoming(stage, rid, entry, why, cut_class(e), finished=True)
                raise
            if final.exists() and not stage.exists():
                # the new capture is in place: the run was cut short after the last rename; only cleanup is left
                entry.update({"stored_as": rid, "replaced_old": had_old})
                self.finish_swap(aside, rid, entry)
                raise
            kept, pending = self.restore_aside(aside, final, rid)
            if kept is not None:
                rel = kept.relative_to(self.out).as_posix()
                entry["previous_capture"] = {"path": rel, "note": "the old capture; it could not be put back in place"}
                old = f"the old capture could not be put back and is kept as {rel}"
            else:
                old = "the old capture is untouched" if had_old else "there was no old capture"
            if isinstance(e, OSError):
                why = f"old capture folder could not be replaced ({type(e).__name__}: {e.strerror or e}); {old}"
                self.keep_incoming(stage, rid, entry, why, STORE_ERROR, finished=True)
                if pending is not None:
                    raise pending from e
                return
            why = f"the run ended while this capture was being swapped in ({type(e).__name__}); {old}"
            self.keep_incoming(stage, rid, entry, why, cut_class(e), finished=True)
            raise
        assert aside is not None
        self.finish_swap(aside, rid, entry)

    def restore_aside(self, aside: Path, final: Path, rid: str) -> tuple[Path | None, BaseException | None]:
        """Put a set-aside old capture back in place.

        Returns where the old capture is when it could not be put back (None when it is in place or never moved),
        and an interrupt that arrived meanwhile, for the caller to raise once the entry is recorded. An interrupt in
        the backoff does not end the restore: one immediate attempt follows, then a visible <id>.previous, then it
        stays in .trash and is never deleted by this run.
        """
        if not aside.exists():
            return None, None
        pending: BaseException | None = None

        def attempt(fn: Callable[[], Any]) -> bool:
            nonlocal pending
            try:
                fn()
                return True
            except OSError:
                return False
            except BaseException as e:  # noqa: BLE001 - the caller raises it once the entry is recorded
                pending = pending or e
                return not aside.exists()

        if attempt(lambda: self.rename(aside, final)) or attempt(lambda: aside.rename(final)):
            return None, pending
        previous = inside(self.out, self.out / f"{rid}.previous")
        if not previous.exists() and attempt(lambda: aside.rename(previous)):
            return previous, pending
        self.kept_trash.add(aside.name)
        return aside, pending

    def finish_swap(self, aside: Path, rid: str, entry: dict) -> None:
        """The new capture is in place: drop the old one and the <id>.incoming and <id>.previous it supersedes
        (carry_forward copied in every complete group of theirs that was ok there and that neither this run nor a
        newer capture had), naming each. Every file they held that is neither replaced by this run's ok output of
        its leg nor carried in is named in ``dropped`` before it is deleted; the report's capture.json record is then
        rewritten with what was superseded and dropped."""
        if aside.is_dir() and not aside.is_symlink():
            self.note_dropped(aside, rid, rid, entry)
        shutil.rmtree(aside, ignore_errors=True)
        for suffix, origin in ((".incoming", "an earlier run cut short"), (".previous", "an earlier failed swap")):
            stale = inside(self.out, self.out / f"{rid}{suffix}")
            if not (stale.exists() or stale.is_symlink()):
                continue
            dropped = len(entry.get("dropped", []))
            if stale.is_dir() and not stale.is_symlink():
                self.note_dropped(stale, stale.name, rid, entry)
            shutil.rmtree(stale, ignore_errors=True)
            if stale.exists():
                entry[f"stale_{suffix[1:]}"] = f"{stale.name} (from {origin}) could not be removed"
                del entry.get("dropped", [])[dropped:]  # still on disk, so not dropped
                if not entry.get("dropped"):
                    entry.pop("dropped", None)
            else:
                note = (
                    f"from {origin}; removed: each complete output of it that this run did not capture ok was "
                    f"carried into {rid} (unless a newer capture had it); every other file it held is named in "
                    "dropped"
                )
                entry.setdefault("superseded", []).append({"folder": stale.name, "note": note})
        if entry.get("dropped") or entry.get("superseded"):
            final = inside(self.out, self.out / rid)
            try:
                stamp = read_stamp(final)
                if isinstance(stamp.get("report"), dict):
                    for k in ("superseded", "dropped"):
                        if entry.get(k):
                            stamp["report"][k] = entry[k]
                    self.store({}, final, "capture.json", json.dumps(stamp, indent=1).encode(), "application/json")
            except OSError:
                pass  # the manifest of this run still names them

    def note_dropped(self, folder: Path, name: str, rid: str, entry: dict) -> None:
        """Name, in ``dropped``, each file of an old capture folder that the swap deletes without a replacement:
        not of a leg this run captured ok, and not carried in from that folder."""
        labels = {p["file"]: p["label"] for p in entry["parts"] if p.get("file")}
        ok_legs = {r["leg"] for r in entry["legs"] if r.get("status") == "ok"}
        carried = self.carried_from.get(rid, {})
        final = self.out / rid
        dropped = entry.setdefault("dropped", [])
        for rel in capture_files(folder):
            owner = owner_of(rel, labels)
            if (owner in ok_legs and (final / rel).is_file()) or rel in carried.get(name, set()):
                continue
            elsewhere = sorted(src for src, rels in carried.items() if src != name and rel in rels)
            if owner.startswith("file:"):
                why = "not a file this script writes"
            elif owner in ok_legs:
                why = "its leg was captured ok again on this run, without this file"
            elif elsewhere:
                why = f"a newer copy of this output was carried in from {elsewhere[0]}"
            else:
                why = "not carried: its leg had not completed there, or the file changed since it was captured"
            f = folder / rel
            size = f.stat().st_size if f.is_file() else None
            dropped.append({"folder": name, "path": rel, "bytes": size, "sha256": sha256_of(f), "why": why})
        if not dropped:
            entry.pop("dropped", None)

    def note_pdf_omits(self, parts: list[dict], entry: dict) -> None:
        """An ok pdf leg is the PDF Cognos renders, but it prints a placeholder for each Data table: name them."""
        tables = [p["label"] for p in parts if p["tag"] in LIST_CONTROL_TAGS]
        pdf = next((r for r in entry["legs"] if r["leg"] == "pdf" and r["status"] == "ok"), None)
        if tables and pdf is not None:
            pdf["omits"] = {"parts": tables, "reason": f"data tables printed as a placeholder; {LIST_CONTROL_FACT}"}

    def capture_parts(self, rid: str, spec: str, stage: Path, entry: dict, leg: Callable) -> None:
        try:
            parts = discover_parts(spec)
        except ET.ParseError as e:
            if self.parts_mode != "none":
                rec = {**self.new_leg("parts"), "status": "failed", "class": REPORT_ERROR, "reason": str(e)}
                entry["legs"].append(rec)
            return
        self.note_pdf_omits(parts, entry)
        if self.parts_mode == "none":
            return
        for i, p in enumerate(parts):
            p["first"] = i == 0  # report.csv is measured to hold one container only; the viz data reason names this
        # a part reason may only name a viewer output that this run actually captured
        viewer_ok = [
            VIEWER_FORMATS[r["leg"]][1] for r in entry["legs"] if r["leg"] in VIEWER_FORMATS and r["status"] == "ok"
        ]
        for p in parts:
            p["viewer_ok"] = viewer_ok
        if self.parts_mode != "auto":
            wanted = [n.strip() for n in self.parts_mode.split(",") if n.strip()]
            by_name = {p["name"]: p for p in parts if p["name"]}
            for n in wanted:
                if n not in by_name:
                    rec = {**self.new_leg(f"part:{n}"), "part": n, "status": "failed", "class": NOT_FOUND}
                    entry["legs"].append({**rec, "reason": "no data container with that name in the spec"})
            parts = [by_name[n] for n in wanted if n in by_name]
        stems: set[str] = set()
        for p in parts:
            if p["name"]:
                # a name holding the key would be redacted in the manifest but not in the file name: hash it instead
                name = p["name"]
                stem = f"part-{hashlib.sha1(name.encode()).hexdigest()[:10]}" if self.holds_secret(name) else name
                p["file"] = unique_stem(stem, stems)
        entry["parts"] = [{k: p.get(k) for k in ("name", "label", "tag", "refQuery", "chart", "file")} for p in parts]
        for p in parts:
            label = p["label"]

            def unsupported(reason: str, label: str = label) -> None:
                for kind in ("data", "render"):
                    rec = {**self.new_leg(f"part:{label}:{kind}"), "part": label, "captured_at": now()}
                    entry["legs"].append({**rec, "status": "unsupported", "class": UNSUPPORTED, "reason": reason})

            # the kind checks come first: an unnamed data table is still a data table, and the pdf does not hold it
            if p["tag"] in APP_CONTROL_TAGS:
                unsupported(app_control_reason(p))
                continue
            if p["tag"] in LIST_CONTROL_TAGS:
                unsupported(list_control_reason(p))
                continue
            if not p["name"]:
                unsupported(
                    f"unnamed {p['tag']} (query {p['refQuery']}): RDS selects a container by name, so it has no "
                    f"per-part oracle; {held_in(p, 'values')}"
                )
                continue
            leg(f"part:{label}:data", lambda r, p=p: self.get_part_data(rid, p, stage, r), part=label)
            if p["tag"] in CUSTOM_TAGS:
                reason = "custom control: rendered in the browser by its JavaScript module; no server render credited"
                rec = {**self.new_leg(f"part:{label}:render"), "part": label, "captured_at": now()}
                entry["legs"].append({**rec, "status": "unsupported", "class": UNSUPPORTED, "reason": reason})
                continue
            leg(f"part:{label}:render", lambda r, p=p: self.get_part_render(rid, p, stage, r), part=label)


def strip_meta(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: strip_meta(v) for k, v in node.items() if k not in ("_meta", "owner")}
    if isinstance(node, list):
        return [strip_meta(v) for v in node]
    return node


def cut_class(e: BaseException) -> str:
    """The store-leg class for a run cut short by ``e``."""
    return "stopped" if isinstance(e, Stop) else "interrupted" if isinstance(e, KeyboardInterrupt) else "crashed"


def files_of(rec: dict) -> list[dict]:
    """A leg record and the sub-records of every other file it wrote (chart images, module metadata)."""
    images = rec.get("images")
    return [
        rec,
        *(images if isinstance(images, list) else []),
        *([rec["module"]] if isinstance(rec.get("module"), dict) else []),
    ]


def read_stamp(folder: Path) -> dict:
    """A capture folder's capture.json, or {} when it has none or it is unreadable."""
    try:
        stamp = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return stamp if isinstance(stamp, dict) else {}


def capture_files(folder: Path) -> list[str]:
    """Every file of a capture folder (relative, posix), except its metadata stamp."""
    return sorted(
        f.relative_to(folder).as_posix() for f in folder.rglob("*") if f.is_file() and f.name != "capture.json"
    )


def owner_of(rel: str, labels: dict[str, str]) -> str:
    """The leg that wrote a capture file; ``labels`` maps a part's file stem to its label."""
    if rel in FILE_OWNERS:
        return FILE_OWNERS[rel]
    if rel.startswith("parts/"):
        name = rel[len("parts/") :]
        m = re.fullmatch(r"(.+)\.(json|html)", name) or re.fullmatch(r"(.+)_\d+\.[^./]+", name)
        if m:
            kind = "data" if name.endswith(".json") else "render"
            stem = m.group(1)
            return f"part:{labels[stem]}:{kind}" if stem in labels else f"part-file:{stem}:{kind}"
    return f"file:{rel}"


def presence(out: Path, rid: str) -> str:
    """Where else the outputs of a capture moved aside (into .trash) may be: never in <id>, which holds another."""
    if (out / rid).is_dir():
        return f"{rid} holds a different capture; an output only this folder holds is not in it"
    return f"{rid} is missing, so this may be the only copy"


def outside_run(owner: str, parts_known: bool) -> str:
    """Why a carried output's leg is not part of this run's plan, so it is never read as a current output."""
    if owner.startswith(("part:", "part-file:")):
        if parts_known:
            return "outside this run's scope: no container of this run's spec writes this file (renamed or removed)"
        return "outside this run's scope: this run did not establish its spec's containers"
    if owner in VIEWER_FORMATS:
        return "outside this run's scope: this format was not requested on this run"
    return "outside this run's scope: this leg was not attempted on this run"


# the keys of an entry recorded in its own capture.json; the rest name folders relative to --out, or this run
RECORD_KEYS = ("id", "origin", "name", "path", "active_report", "parts", "legs", "carried")


def report_record(entry: dict, folder: str) -> dict:
    """An entry as its capture folder records it (capture.json "report"): every leg, failed and unsupported ones
    with their reasons included, and every carried file, with each file path relative to that folder."""
    prefix = f"{folder}/"

    def rel(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: v[len(prefix) :] if k == "path" and isinstance(v, str) and v.startswith(prefix) else rel(v)
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [rel(v) for v in node]
        return node

    record = {k: entry[k] for k in RECORD_KEYS if k in entry}
    for k in ("legs", "carried"):
        if k in record:
            record[k] = rel(record[k])
    return json.loads(json.dumps(record))


LEG_STATUSES = ("ok", "failed", "refused", "skipped", "unsupported")


def recorded_source(value: Any, fallback: dict) -> dict:
    """Where a record adopted from a manifest was first recorded: only its string fields, else ``fallback``."""
    if not isinstance(value, dict):
        return fallback
    kept = {k: value[k] for k in ("file", "started_at", "status") if isinstance(value.get(k), str)}
    return kept if kept.get("file") else fallback


def previous_reports(data: Any) -> dict[str, dict]:
    """The per-report records an earlier manifest.json (``data``, parsed; any shape is tolerated) holds:
    {id lower: {RECORD_KEYS..., "recorded_in": {file, started_at, status}}}, file paths relative to --out.

    Taken from its reports (entries with legs), its earlier_reports items recorded from a report's own folder, and
    the records it inherited itself, so a description survives a chain of runs; for a manifest still "running" (a
    run that was killed) the inherited ones come first, since its own entries may describe a capture never swapped
    in. Nothing is trusted: a leg that is not a dict with a leg name is dropped, and a leg still pending, or with a
    status this script does not write, becomes failed, so an adopted record never claims more than it recorded."""
    if not isinstance(data, dict):
        return {}
    here = {"file": "manifest.json"}
    here.update({k: data[k] for k in ("started_at", "status") if isinstance(data.get(k), str)})

    def listed(key: str) -> list:
        value = data.get(key)
        return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []

    own = [(e, here) for e in listed("reports")]
    earlier = [
        (i["report"], recorded_source(i.get("recorded_in"), here))
        for i in listed("earlier_reports")
        if i.get("recorded") is True and i.get("folder") == i.get("id") and isinstance(i.get("report"), dict)
    ]
    inherited = [(e, recorded_source(e.get("recorded_in"), here)) for e in listed("inherited")]
    if data.get("status") == "running":
        order = inherited + earlier + own
    else:
        confirmed = [(e, source) for e, source in own if e.get("stored_as") == e.get("id")]
        uncertain = [(e, source) for e, source in own if e.get("stored_as") != e.get("id")]
        order = confirmed + inherited + earlier + uncertain
    found: dict[str, dict] = {}
    for entry, source in order:
        rid = entry.get("id")
        if not is_store_id(rid) or str(rid).lower() in found or not isinstance(entry.get("legs"), list):
            continue
        if "stored_as" in entry and entry["stored_as"] != rid:
            continue  # a capture this run did not swap in: the entry does not describe the <id> folder
        if not any(isinstance(leg, dict) and leg.get("leg") != "store" for leg in entry["legs"]):
            continue  # a store-only entry wrote no capture folder and cannot describe the existing one
        record = json.loads(json.dumps({k: entry[k] for k in RECORD_KEYS if k in entry}))
        legs = [r for r in record["legs"] if isinstance(r, dict) and isinstance(r.get("leg"), str)]
        for rec in legs:
            if rec.get("status") not in LEG_STATUSES:
                was = rec.get("status")
                reason = f"recorded as {was!r} by a run that did not finish it" if isinstance(was, str) else None
                rec.update({"status": "failed", "class": STORE_ERROR, "reason": reason or "recorded without a status"})
        record["legs"] = legs
        found[str(rid).lower()] = {**record, "recorded_in": source}
    return found


def read_previous_manifest(out: Path) -> tuple[dict[str, dict], dict]:
    """The records of the manifest.json an earlier run left in ``out`` (see previous_reports), read before this
    run replaces it, and what was read: {started_at, status, records} or {error} (missing, unreadable, not JSON)."""
    path = out / "manifest.json"
    if not path.is_file():
        return {}, {"error": "no manifest.json in --out"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as e:
        return {}, {"error": f"manifest.json not read ({type(e).__name__})"}
    pending = [(data, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 900:
            return {}, {"error": "manifest.json not read (malformed data)"}
        if isinstance(node, dict):
            pending.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            pending.extend((value, depth + 1) for value in node)
    try:
        records = previous_reports(data)
    except (RecursionError, TypeError, ValueError):
        return {}, {"error": "manifest.json not read (malformed data)"}
    meta: dict = {
        k: data[k] for k in ("started_at", "status") if isinstance(data, dict) and isinstance(data.get(k), str)
    }
    return records, {**meta, "records": len(records)}


def ok_files(entry: dict, rid: str) -> dict[str, dict[str, str]]:
    """The files of each ok leg of an entry still in staging (paths <id>/...): {leg: {relative path: sha256}}."""
    record: dict[str, dict[str, str]] = {}
    for rec in entry["legs"]:
        if rec.get("status") != "ok":
            continue
        for r in files_of(rec):
            path = str(r.get("path") or "")
            if path.startswith(f"{rid}/") and r.get("sha256"):
                record.setdefault(rec["leg"], {})[path[len(rid) + 1 :]] = str(r["sha256"])
    return record


def sha256_of(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def owned_by(rels: list[str], leg: str, labels: dict[str, str]) -> bool:
    """Whether every file of a recorded group is written by ``leg``. A part file whose label this run does not know
    (its spec leg failed) is accepted for a part leg of the same kind, as long as the group is one part's files."""
    owners = {owner_of(rel, labels) for rel in rels}
    if owners == {leg}:
        return True
    m = re.fullmatch(r"part:.+:(data|render)", leg)
    if not m:
        return False
    loose = {owner_of(rel, {}) for rel in rels}
    return len(loose) == 1 and all(o.startswith("part-file:") and o.endswith(f":{m.group(1)}") for o in owners)


def complete_groups(src: Path, stamp: dict, labels: dict[str, str], host: str) -> dict[str, list[str]]:
    """The output groups of a capture folder that are safe to carry: {owner leg: its files}.

    A folder written by this version records ok_files; only a group recorded there counts, and only when every
    file is present with the recorded sha256, every file belongs to the leg it is recorded under, and the group is
    whole by the content rule below as well (a leg that failed or was cut short is not recorded). A folder without
    the record (an older version, or a record that could not be written) is judged by its content alone: a
    lineage needs lineage.json naming its model kind and, for a module, the module.json it names; a render needs
    exactly one html and every chart image it links."""
    record = stamp.get("ok_files")
    groups: dict[str, list[str]] = {}
    if isinstance(record, dict):
        for leg, files in record.items():
            if not isinstance(files, dict) or not files:
                continue
            rels = sorted(str(rel) for rel in files)
            if not owned_by(rels, str(leg), labels):
                continue  # a group recorded under another leg, or spanning two, is not that leg's output
            try:
                whole = all(sha256_of(inside(src, src / str(rel))) == sha for rel, sha in files.items())
            except Failure:
                whole = False
            if whole and legacy_complete(src, str(leg), rels, host):
                groups[str(leg)] = rels
        return groups
    owned: dict[str, list[str]] = {}
    for rel in capture_files(src):
        owned.setdefault(owner_of(rel, labels), []).append(rel)
    for owner, rels in owned.items():
        if legacy_complete(src, owner, rels, host):
            groups[owner] = rels
    return groups


def legacy_complete(src: Path, owner: str, rels: list[str], host: str) -> bool:
    """Whether a group holds every file its leg writes when ok: a lineage names its model kind, and a module lineage
    names the module.json it holds (a module lineage without one, as a failed or older one may be, is incomplete);
    a render holds one html and every chart image it links."""
    try:
        if owner == "lineage":
            if "lineage.json" not in rels:
                return False
            model = json.loads((src / "lineage.json").read_text(encoding="utf-8")).get("model")
            if not isinstance(model, dict) or model.get("kind") not in ("module", "package", "unknown"):
                return False
            meta = model.get("metadata")
            if model["kind"] == "module":
                return meta == "module.json" and set(rels) == {"lineage.json", "module.json"}
            return not meta and rels == ["lineage.json"]
        if owner.endswith(":render"):
            htmls = [rel for rel in rels if rel.endswith(".html")]
            if len(htmls) != 1:
                return False
            text = (src / htmls[0]).read_text(encoding="utf-8", errors="replace")
            return len(rels) - 1 == len(image_paths(text, host))
    except (OSError, ValueError, AttributeError):
        return False
    return True


def incoming_note(folder: Path) -> str:
    """What an <id>.incoming is, from the marker keep_incoming wrote into it."""
    kept = read_stamp(folder).get("kept_aside")
    if isinstance(kept, dict) and kept.get("finished"):
        return f"a finished capture not swapped in ({kept.get('class')}); its legs all ran"
    if isinstance(kept, dict):
        return f"an interrupted capture ({kept.get('class')}), kept aside; not a complete capture"
    return "an interrupted capture (or one from an older version of this script), kept aside; not a complete capture"


def part_of(rec: dict, suffix: str) -> str:
    return rec.get("part") or rec["leg"][len("part:") : -len(suffix)]


def render_unestablished(reports: list[dict]) -> list[dict]:
    """Reports or parts without a usable render, named so nobody mistakes absence for a pass."""
    out = []
    for r in reports:
        for rec in r["legs"]:
            reason = rec.get("reason", rec["status"])
            if rec["leg"] in ("pdf", "parts") and rec["status"] != "ok":
                out.append({"report": r["id"], "part": None, "leg": rec["leg"], "reason": reason})
            if rec["leg"] == "pdf" and rec["status"] == "ok" and isinstance(rec.get("omits"), dict):
                # an ok PDF that prints a placeholder for a data table is no render of that table
                for part in rec["omits"]["parts"]:
                    out.append({"report": r["id"], "part": part, "leg": "pdf", "reason": rec["omits"]["reason"]})
            if rec["leg"].endswith(":render") and rec["status"] != "ok":
                out.append({"report": r["id"], "part": part_of(rec, ":render"), "leg": rec["leg"], "reason": reason})
    return out


def data_unestablished(reports: list[dict]) -> list[dict]:
    """Parts without a data oracle (charts, unnamed, failures), so absence is never read as a pass."""
    out = []
    for r in reports:
        for rec in r["legs"]:
            if rec["leg"] == "parts" and rec["status"] != "ok":
                out.append({"report": r["id"], "part": None, "status": rec["status"], "reason": rec.get("reason")})
            if rec["leg"].endswith(":data") and rec["status"] != "ok":
                part = part_of(rec, ":data")
                out.append({"report": r["id"], "part": part, "status": rec["status"], "reason": rec.get("reason")})
    return out


def lineage_unestablished(reports: list[dict]) -> list[dict]:
    """Reports whose lineage does not reach the model for every reference (macros, packages, failures)."""
    out = []
    for r in reports:
        for rec in r["legs"]:
            if rec["leg"] != "lineage":
                continue
            if rec["status"] != "ok":
                out.append({"report": r["id"], "status": rec["status"], "reason": rec.get("reason")})
            elif rec.get("unresolved") or rec.get("model_kind") != "module":
                out.append(
                    {
                        "report": r["id"],
                        "status": rec["status"],
                        "model_kind": rec.get("model_kind"),
                        "resolved": rec.get("resolved"),
                        "referenced": rec.get("referenced"),
                        "unresolved": rec.get("unresolved", []),
                        "reason": {
                            "package": "lineage stops at the package",
                            "unknown": "the spec names no data module or package",
                        }.get(
                            str(rec.get("model_kind")),
                            f"{len(rec.get('unresolved') or [])} of {rec.get('referenced')} references unresolved",
                        ),
                    }
                )
    return out


def other_captures(out: Path, touched: set[str], named: frozenset[str] = frozenset()) -> list[dict]:
    """Folders in ``out`` that this run did not write: other reports' captures (legitimate in a shared out
    folder, never deleted), stale leftovers and the .trash and .staging folders earlier runs left (this run deletes
    only what it created), each named with when it was captured and what it is, so none passes unnoticed.
    ``named`` holds the leftovers an entry of this run already names."""
    found = []

    def stamped(d: Path, rid: str | None, note: str) -> dict:
        stamp = read_stamp(d)
        if not stamp:
            note += "; no capture.json (written by an older version of this script, or not by it)"
        return {
            "folder": d.relative_to(out).as_posix(),
            "id": rid,
            "captured_at": stamp.get("captured_at"),
            "note": note,
        }

    for d in sorted(p for p in out.iterdir() if p.is_dir()):
        suffix = next((s for s in (".incoming", ".previous") if d.name.endswith(s)), "")
        rid = d.name[: -len(suffix)] if suffix else d.name
        # a report folder this run wrote is in its entry; so is a leftover it names, but not one it left alone
        mine = rid.lower() in touched and (not suffix or d.name in named)
        if is_store_id(rid) and not mine:
            note = "a capture of a report this run did not touch; its recorded results are in earlier_reports"
            if suffix == ".incoming":
                note = incoming_note(d)
            elif suffix == ".previous":
                note = "an old capture that an earlier run could not put back in place; superseded or stale"
            found.append(stamped(d, rid, note))
        elif d.name == "modules":
            note = "module metadata from an older version of this script; module.json now lives in each report folder"
            found.append({"folder": d.name, "id": None, "captured_at": None, "note": note})
    for kind in (".trash", ".staging"):
        base = out / kind
        for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
            rel = d.relative_to(out).as_posix()
            if rel in named:
                continue
            m = re.match(r"(i[0-9A-Fa-f]{32})((?:\.incoming|\.staging)?)", d.name)
            rid = m.group(1) if m else None
            if kind == ".staging":
                what = "a capture left in staging by a run that was killed"
            elif m and m.group(2) == ".incoming":
                what = f"{incoming_note(d)}, moved aside by a later run"
            elif m and m.group(2) == ".staging":
                what = "a capture left in staging by a run that was killed, moved aside by a later run"
            else:
                what = "an old capture set aside during a swap that was not finished"
            found.append(stamped(d, rid, f"{what}; {presence(out, rid) if rid else 'not a capture of a known id'}"))
    return found


def from_out(node: Any, folder: str, original_id: str | None = None) -> Any:
    """A record with each file path relative to ``out`` made relative to ``folder``; a path in another folder is
    left as it is, so it is checked (and found missing) inside ``folder``, never followed."""
    prefix = f"{(original_id or folder)}/".lower()
    if isinstance(node, dict):
        return {
            k: v[len(prefix) :]
            if k == "path" and isinstance(v, str) and v.lower().startswith(prefix)
            else from_out(v, folder, original_id)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [from_out(v, folder, original_id) for v in node]
    return node


def earlier_reports(
    out: Path, touched: set[str], previous: dict[str, dict] | None = None, secret: str | None = None
) -> list[dict]:
    """The report folders in ``out`` (<id>, <id>.incoming, <id>.previous) of every report this run did not
    capture, each with the results its own capture.json records, so the manifest still describes the whole folder.

    Each recorded file path is made relative to ``out`` and checked: a file that is missing or no longer matches
    its recorded sha256 loses its path, and an ok leg holding one is reported failed (store_error).

    An <id> folder whose capture.json holds no record takes its record from ``previous`` (the manifest.json this
    run replaces, see previous_reports), checked the same way and named by recorded_in. When every file it names is
    present with its recorded sha256 and the folder's capture.json is a stamp of the same id, the record is also
    written into that capture.json (atomically, the key redacted), so the folder describes itself on later runs;
    record_saved says whether it was. A folder with neither record is named as such (recorded false), never
    treated as complete."""
    found = []
    folders = sorted(p for p in out.iterdir() if p.is_dir() and not p.is_symlink())
    canonical_ids = {p.name.lower() for p in folders if is_store_id(p.name)}
    for d in folders:
        if d.name == ".trash" and is_reparse_point(d):
            continue
        if d.name == ".trash":
            for candidate in sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink()):
                if is_reparse_point(candidate):
                    continue
                stamp = read_stamp(candidate)
                rid = stamp.get("id")
                if (
                    is_store_id(rid)
                    and rid.lower() not in canonical_ids
                    and re.fullmatch(re.escape(rid) + r"-[0-9a-fA-F]{8}", candidate.name, re.IGNORECASE)
                ):
                    folders.append(candidate)
            continue
        suffix = next((s for s in (".incoming", ".previous") if d.name.endswith(s)), "")
        is_trash = d.parent.name == ".trash"
        rid = read_stamp(d).get("id") if is_trash else d.name[: -len(suffix)] if suffix else d.name
        if not is_store_id(rid) or rid.lower() in touched:
            continue
        folder = d.relative_to(out).as_posix()
        if is_reparse_point(d):
            found.append(
                {
                    "folder": folder,
                    "id": rid,
                    "captured_at": None,
                    "recorded": False,
                    "note": "folder is a reparse point; not read or written",
                }
            )
            continue
        stamp = read_stamp(d)
        item: dict = {"folder": folder, "id": rid, "captured_at": stamp.get("captured_at")}
        record = stamp.get("report")
        adopted = None
        if not isinstance(record, dict) or not isinstance(record.get("legs"), list):
            canonical_missing = rid.lower() not in canonical_ids
            adopted = None if suffix and suffix != ".previous" else (previous or {}).get(rid.lower())
            if suffix == ".previous" and not canonical_missing:
                adopted = None
            if adopted is None:
                note = (
                    "its capture.json records no per-report results (written by an older version of this script, "
                    "or not by it), and no earlier manifest.json read by this run does either: which legs ran, "
                    "failed or were unsupported is unknown, so it is not known to be complete"
                )
                found.append({**item, "recorded": False, "note": note})
                continue
            record = from_out({k: v for k, v in adopted.items() if k != "recorded_in"}, folder, rid)
        source = adopted["recorded_in"] if adopted else stamp.get("recorded_in")
        report = json.loads(json.dumps(record))
        intact = True

        def check(r: dict, required: bool = False) -> bool:
            nonlocal intact
            path = r.get("path")
            if path is None:
                if required:
                    r["path"] = None
                    r["changed_since"] = "its recorded path is missing"
                    intact = False
                    return False
                return True
            if not isinstance(path, str) or not path:
                r["path"] = None
                r["changed_since"] = "its recorded path is not a file name"
                intact = False
                return False
            try:
                parts = Path(path).parts
                metadata_alias = (
                    ":" in path
                    or any(part.endswith((".", " ")) for part in parts)
                    or Path(path).name.lower() in ("capture.json", "manifest.json")
                )
                if metadata_alias or Path(path).suffix.lower() in (".part", ".tmp"):
                    raise Failure(REFUSED, "metadata or temporary file is not a report output")
                f = inside(d, d / path)
                for metadata in (d / "capture.json", out / "manifest.json"):
                    if metadata.exists() and os.path.samefile(f, metadata):
                        raise Failure(REFUSED, "metadata is not a report output")
            except (Failure, ValueError, OSError):
                f = None
            expected = r.get("sha256")
            if f is None or not f.is_file() or not isinstance(expected, str) or sha256_of(f) != expected:
                r["path"] = None
                r["changed_since"] = f"{path} is missing or changed since it was recorded"
                intact = False
                return False
            r["path"] = f"{folder}/{path}"
            return True

        try:
            for rec in [r for r in report["legs"] if isinstance(r, dict)]:
                checks = [check(r, required=rec.get("status") == "ok") for r in files_of(rec) if isinstance(r, dict)]
                whole = all(checks)
                if not whole and rec.get("status") == "ok":
                    reason = "recorded ok when captured, but a file of it is missing or changed since"
                    rec.update({"status": "failed", "class": STORE_ERROR, "reason": reason})
            carried = report.get("carried")
            for c in [c for c in carried if isinstance(c, dict)] if isinstance(carried, list) else []:
                check(c)
            report["legs"] = [r for r in report["legs"] if isinstance(r, dict) and "leg" in r and "status" in r]
            unestablished = {
                "render_unestablished": render_unestablished([report]),
                "data_unestablished": data_unestablished([report]),
                "lineage_unestablished": lineage_unestablished([report]),
            }
        except (TypeError, ValueError, KeyError, AttributeError):
            note = "its recorded results are malformed, so it is not known to be complete"
            found.append({**item, "recorded": False, "note": note})
            continue
        item.update({"recorded": True, "report": report})
        if isinstance(source, dict):
            item["recorded_in"] = source
        if suffix:
            item["note"] = incoming_note(d) if suffix == ".incoming" else "an old capture not put back in place"
        if adopted is not None:
            when = source.get("started_at") or "an unknown time"
            item["note"] = (
                f"its capture.json records no per-report results; they are taken from the {source.get('file')} of "
                f"the run started {when}, each file checked against its recorded sha256"
            )
            item["record_saved"] = False
            same = is_store_id(stamp.get("id")) and str(stamp.get("id")).lower() == rid.lower()
            if intact and same and suffix != ".previous":
                try:
                    destination = inside(out, d / "capture.json")
                    write_json(destination, {**stamp, "report": record, "recorded_in": source}, secret)
                    item["record_saved"] = True
                except (Failure, OSError, ValueError) as e:
                    item["note"] += f"; not written into its capture.json ({type(e).__name__})"
                    (d / "capture.tmp").unlink(missing_ok=True)
            else:
                why = "a file it names is missing or changed" if not intact else "its capture.json is not a stamp"
                item["note"] += f"; not written into its capture.json: {why}"
        item.update(unestablished)
        found.append(item)
    return found


def capture_state(entry: dict) -> str | None:
    """What this run did to <out>/<id> for one report, from the entry (None when it changed no capture folder)."""
    if "stored_as" not in entry:
        return None
    rid, where = entry["id"], entry["stored_as"]
    if where == rid:
        return f"{rid} swapped in, replacing the old capture" if entry.get("replaced_old") else f"{rid} written"
    kept = entry.get("previous_capture")
    old = f"its old capture is kept as {kept['path']}" if kept else f"{rid} is as it was before this run"
    new = f"new capture kept as {where}" if where else "new capture lost"
    return f"{rid}: {new}; {old}"


def write_json(path: Path, data: Any, secret: str | None = None) -> None:
    text = json.dumps(data, indent=1, ensure_ascii=False)
    if secret:
        text = redact_text(text, secret_pattern(secret))[0]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    # a report name the console cannot encode (cp1252 and Chinese, say) must never crash the run
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("COGNOS_SERVER"), help="https://host (env COGNOS_SERVER)")
    ap.add_argument(
        "--namespace", default=os.environ.get("COGNOS_NAMESPACE"), help="CAMNamespace (env COGNOS_NAMESPACE)"
    )
    ap.add_argument("--api-key-file", type=Path, help="file holding the API key (else env COGNOS_API_KEY)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--report-id", action="append", default=[], help="a report storeID (i + 32 hex digits)")
    ap.add_argument(
        "--folder-id", action="append", default=[], help="a folder or package storeID; every report below it"
    )
    ap.add_argument("--formats", default="pdf,csv,xlsx", help="comma list of pdf,csv,xlsx, or none")
    ap.add_argument("--parts", default="auto", help="auto | none | comma list of data container names")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds per request (default 120)")
    args = ap.parse_args(argv)

    key = (
        args.api_key_file.read_text(encoding="utf-8").strip() if args.api_key_file else os.environ.get("COGNOS_API_KEY")
    )
    formats = [] if args.formats == "none" else [f.strip() for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in VIEWER_FORMATS]
    if not (args.server and args.namespace and key) or bad or not (args.report_id or args.folder_id):
        ap.error("need --server, --namespace, an API key, valid --formats and at least one --report-id/--folder-id")
    bad_ids = [i for i in args.report_id + args.folder_id if not is_store_id(i)]
    if bad_ids:
        ap.error(f"not a Cognos storeID (i + 32 hex digits): {', '.join(repr(i) for i in bad_ids)}")
    if args.parts not in ("auto", "none") and not [n for n in args.parts.split(",") if n.strip()]:
        ap.error("--parts needs auto, none or at least one data container name")
    if not args.timeout > 0:
        ap.error("--timeout must be positive")

    key_re = secret_pattern(key)

    def redact(text: str) -> str:
        return redact_text(text, key_re)[0]

    args.out.mkdir(parents=True, exist_ok=True)
    client = Client(args.server, args.namespace, key, args.timeout)
    cap = Capturer(client, args.out, formats, args.parts)
    deadline = {"request_timeout_s": args.timeout, "max_attempts_per_leg": MAX_ATTEMPTS + 1}
    deadline["worst_case_leg_s"] = worst_case_leg_s(args.timeout)
    # a render leg makes one request per chart image, so each image adds this much to its worst case
    deadline["per_chart_image_s"] = (MAX_ATTEMPTS + 1) * args.timeout
    manifest: dict = {
        "schema": SCHEMA,
        "server": client.server,
        "namespace": args.namespace,
        "started_at": now(),
        "status": "running",
        "formats": formats,
        "parts": args.parts,
        "limits": LIMITS,
        "deadline": deadline,
        "discovery": [],
        "discovery_skipped": [],
        "reports": [],
    }
    print(
        f"deadline: {args.timeout:.0f} s per request, at most {MAX_ATTEMPTS + 1} attempts per leg; "
        f"worst case about {deadline['worst_case_leg_s']:.0f} s per leg; a render leg with N chart images "
        f"can take up to (N + 1) x {deadline['per_chart_image_s']:.0f} s plus backoff and re-login",
        flush=True,
    )
    exit_code = 0
    targets: list[tuple[str, str]] = []
    attempted = 0
    outcome: BaseException | None = None

    def not_attempted() -> None:
        seen = {r["id"] for r in manifest["reports"]}
        manifest["reports"] += [
            {"id": r, "origin": o, "status": "not_attempted"} for r, o in targets[attempted:] if r not in seen
        ]

    def fail_pending(cls: str, reason: str) -> None:
        legs = manifest["discovery"] + [leg for r in manifest["reports"] for leg in r.get("legs", [])]
        for leg in legs:
            if leg["status"] == "pending":
                leg.update({"status": "failed", "class": cls, "reason": reason})

    previous: dict[str, dict] = {}

    def read_previous() -> None:
        # read before this run first writes manifest.json, which replaces the one an earlier run left
        if "previous_manifest" not in manifest:
            records, meta = read_previous_manifest(args.out)
            previous.update(records)
            manifest["inherited"] = list(records.values())
            manifest["previous_manifest"] = meta

    try:
        read_previous()
        client.login()
        cap.discover(args.folder_id, args.report_id, manifest["discovery"], manifest["discovery_skipped"], targets)
        for rid, origin in targets:
            attempted += 1
            entry = cap.capture_report(rid, origin, manifest["reports"])
            done = sum(1 for leg in entry["legs"] if leg["status"] == "ok")
            carried = f", {len(entry['carried'])} earlier outputs carried" if entry.get("carried") else ""
            print(redact(f"{rid} {entry['name']!r}: {done}/{len(entry['legs'])} legs ok{carried}"), flush=True)
            manifest["reauths"] = cap.reauths
            write_json(args.out / "manifest.json", manifest, key)
    except BaseException as e:  # noqa: BLE001 - classified below, outside this handler, so nothing chains to it
        outcome = e

    # Everything after the capture loop runs under one guard: a real Ctrl+C only sets a flag (SIGINT is deferred),
    # and each step catches what escapes it (an interrupt is noted, any other error is named as a crash), so no
    # exception, and no unredacted message chained to one, can leave main.
    cut = {"cleanup": False}
    cleanup_error: list[str] = []

    def on_sigint(signum: int, frame: Any) -> None:
        cut["cleanup"] = True

    try:
        previous_handler: Any = signal.signal(signal.SIGINT, on_sigint)
    except (ValueError, OSError, AttributeError):
        previous_handler = None  # not the main thread: each step's guard still catches the interrupt

    def guard(fn: Callable[[], Any], attempts: int = 1) -> Any:
        for _ in range(attempts):
            try:
                return fn()
            except KeyboardInterrupt:
                cut["cleanup"] = True
            except Exception as e:  # noqa: BLE001 - named in the manifest, never raised out of main
                cleanup_error.append(redact(f"{type(e).__name__}: {e}")[:300])
                return None
        return None

    def say(text: str) -> None:
        guard(lambda: print(redact(text), file=sys.stderr))

    def classify() -> None:
        nonlocal exit_code
        if outcome is None:
            legs = manifest["discovery"] + [leg for r in manifest["reports"] for leg in r.get("legs", [])]
            exit_code = 1 if any(leg["status"] in ("failed", "refused") for leg in legs) else 0
            manifest["status"] = "partial" if exit_code else "ok"
            if not targets:
                # an empty folder, or one holding only report views or unresolved shortcuts, is not a capture
                exit_code = 1
                manifest["status"] = "no_reports"
        elif isinstance(outcome, Stop):
            not_attempted()
            fail_pending("stopped", "the run stopped")
            manifest.update({"status": "stopped", "stop_reason": redact(str(outcome))})
            exit_code = 2
        elif isinstance(outcome, KeyboardInterrupt):
            not_attempted()
            fail_pending("interrupted", "KeyboardInterrupt")
            manifest["status"] = "interrupted"
            exit_code = EXIT_INTERRUPTED
        else:  # any other error is recorded as a crash, never as "some legs failed"
            not_attempted()
            fail_pending("crashed", type(outcome).__name__)
            manifest.update({"status": "crashed", "crash": redact(f"{type(outcome).__name__}: {outcome}")[:300]})
            exit_code = 3

    def states() -> str:
        return "; ".join(s for s in (capture_state(r) for r in manifest["reports"]) if s)

    def logout() -> None:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - a failed logout is no failure of the capture
            pass

    def rmdir_if_empty(d: Path) -> None:
        if is_reparse_point(d):
            return
        try:
            d.rmdir()
        except OSError:
            pass

    def list_out(fn: Callable[[], list[dict]]) -> list[dict]:
        try:
            return fn()
        except OSError as e:
            return [{"error": f"out folder not listed ({type(e).__name__})"}]

    def wrap_up() -> None:
        guard(classify, attempts=3)
        if isinstance(outcome, Stop):
            say(f"STOPPED: {outcome}")
        elif isinstance(outcome, KeyboardInterrupt):
            guard(lambda: say(f"INTERRUPTED: {states() or 'no capture folder was changed'}"))
        elif outcome is not None:
            say("".join(traceback.format_exception(outcome)))
        guard(logout)
        # only what this run created: leftovers of earlier runs may hold the only copy of a capture
        for d in cap.created:
            if d.name not in cap.kept_trash:
                guard(lambda d=d: shutil.rmtree(d, ignore_errors=True), attempts=3)
        for d in (args.out / ".staging", args.out / ".trash"):
            guard(lambda d=d: rmdir_if_empty(d), attempts=3)
        kinds = ("previous_capture", "earlier_incoming", "earlier_staging")
        named = frozenset(
            str(r[k]["path"]) for r in manifest["reports"] for k in kinds if isinstance(r.get(k), dict)
        ) | {str(r["stored_as"]) for r in manifest["reports"] if r.get("stored_as")}
        for r in manifest["reports"]:
            for k in kinds:
                if isinstance(r.get(k), dict):
                    say(f"KEPT: {r['id']} {k} in {r[k]['path']}: {r[k]['note']}")
        manifest["reauths"] = cap.reauths
        captured = [r for r in manifest["reports"] if "legs" in r]
        touched = {r["id"].lower() for r in captured}
        for name, fn in (
            ("render_unestablished", render_unestablished),
            ("data_unestablished", data_unestablished),
            ("lineage_unestablished", lineage_unestablished),
        ):
            manifest[name] = guard(lambda fn=fn: fn(captured), attempts=3)
        found = guard(lambda: list_out(lambda: other_captures(args.out, touched, named)), attempts=3)
        manifest["other_captures"] = found if found is not None else [{"error": "out folder not listed"}]
        guard(read_previous, attempts=3)
        earlier = guard(lambda: list_out(lambda: earlier_reports(args.out, touched, previous, key)), attempts=3)
        manifest["earlier_reports"] = earlier if earlier is not None else [{"error": "out folder not listed"}]
        swapped = {r["id"].lower() for r in captured if r.get("stored_as") == r["id"]}
        inherited = [e for e in manifest.get("inherited", []) if e["id"].lower() not in swapped]
        if inherited:
            manifest["inherited"] = inherited
        else:
            manifest.pop("inherited", None)
        if earlier is not None and not any("error" in e for e in earlier):
            # an untouched report is described in earlier_reports now; an inherited record is kept only for a
            # report this run captured without swapping it in, whose <id> folder that record still describes
            kept = [e for e in manifest.get("inherited", []) if e["id"].lower() in touched - swapped]
            manifest["inherited"] = kept
            if not kept:
                manifest.pop("inherited", None)
        for o in manifest["other_captures"]:
            folder = str(o.get("folder") or "")
            if "/" in folder or folder.endswith((".incoming", ".previous")):
                say(f"KEPT: {folder}: {o.get('note')}")

    try:
        guard(wrap_up)
        if manifest["status"] == "running":  # classify itself was cut short three times or failed
            manifest["status"] = "interrupted" if cut["cleanup"] else "crashed"
            exit_code = EXIT_INTERRUPTED if cut["cleanup"] else 3
        if cleanup_error:
            manifest["cleanup_error"] = cleanup_error
            if exit_code in (0, 1):
                manifest.update({"status": "crashed", "crash": f"after the capture: {cleanup_error[0]}"})
                say(f"CRASHED: after the capture: {cleanup_error[0]}")
                exit_code = 3
        if cut["cleanup"] and exit_code != EXIT_INTERRUPTED:
            manifest["status"] = "interrupted"
            say(f"INTERRUPTED: during the final cleanup; {states() or 'no capture folder was changed'}")
            exit_code = EXIT_INTERRUPTED
        manifest["finished_at"] = now()
        written = False
        for _ in range(3):
            try:
                write_json(args.out / "manifest.json", manifest, key)
                written = True
                break
            except KeyboardInterrupt:
                # the interrupt is never lost: the manifest is written again saying so, and the exit code is 130
                if exit_code != EXIT_INTERRUPTED:
                    manifest["status"] = "interrupted"
                    exit_code = EXIT_INTERRUPTED
            except Exception as e:  # noqa: BLE001 - a manifest that cannot be written is a crash, not a partial run
                if exit_code not in (2, EXIT_INTERRUPTED):
                    manifest.update({"status": "crashed", "crash": redact(f"manifest not written: {type(e).__name__}")})
                    exit_code = 3
                say(f"CRASHED: manifest.json not written ({type(e).__name__}: {e})")
        if cut["cleanup"] and exit_code != EXIT_INTERRUPTED:  # a Ctrl+C deferred while the manifest was written
            manifest["status"] = "interrupted"
            exit_code = EXIT_INTERRUPTED
            written = bool(guard(lambda: write_json(args.out / "manifest.json", manifest, key) or True))
        if not written:
            message = (
                f"INTERRUPTED: manifest.json may not be final; {states() or 'no capture folder was changed'}"
                if exit_code == EXIT_INTERRUPTED
                else f"CRASHED: manifest.json may not be final; {states() or 'no capture folder was changed'}"
            )
            say(message)
        skipped = len(manifest["discovery_skipped"])
        guard(
            lambda: print(
                redact(
                    f"{manifest['status']}: {len(manifest['reports'])} reports, {skipped} discovery children skipped "
                    f"-> {args.out / 'manifest.json'}"
                )
            )
        )
    finally:
        if previous_handler is not None:
            try:
                signal.signal(signal.SIGINT, previous_handler)
            except (ValueError, OSError):
                pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
