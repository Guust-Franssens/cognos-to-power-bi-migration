"""
purpose: offline tests for scripts/capture_cognos_oracle.py (synthetic fixtures, no server, no real tokens)
usage:   uv run --no-project --with pytest --with requests pytest scripts\\tests -q -p no:cacheprovider
"""

from __future__ import annotations

import contextlib
import ctypes
import gzip
import hashlib
import io
import http.server
import json
import os
import re
import shutil
import sys
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capture_cognos_oracle as cco  # noqa: E402

HOST = "cognos.example.test"
NS = 'xmlns="http://developer.cognos.com/schemas/report/16.2/"'
# real storeIDs are i + 32 hex digits (all 366 ids in the trial sample tree)
RID, SID, FID, MID, XID = ("i" + c * 32 for c in "ABFDC")
PKG, SUB = "i" + "E" * 32, "i" + "1" * 32


class Resp:
    def __init__(self, status: int = 200, body: bytes | str = b"", ctype: str = "application/json"):
        self.status_code = status
        self.content = body.encode() if isinstance(body, str) else body
        self.headers = {"content-type": ctype}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


class FakeClient:
    """Routes api()/bi() calls through a handler; counts logins."""

    def __init__(self, handler):
        self.host, self.handler, self.logins, self.calls, self.posts = HOST, handler, 0, [], []
        self.server = f"https://{HOST}"

    def login(self):
        self.logins += 1

    def logout(self):
        return 204

    def api(self, method, path, **kw):
        self.calls.append(("api", path))
        return self.handler("api", path)

    def bi(self, method, path, **kw):
        self.calls.append(("bi", path))
        if method == "POST":
            self.posts.append(dict(kw.get("data") or {}))
        return self.handler("bi", path)


def capturer(tmp_path, handler, formats=(), parts="auto"):
    sleeps: list[float] = []
    cap = cco.Capturer(FakeClient(handler), tmp_path, list(formats), parts, sleep=sleeps.append)
    return cap, sleeps


SPEC = f"""<report {NS}>
<modelPath type="module">/content/folder[@name='Team']/module[@name='Bank mod']</modelPath>
<queries><query name="Query1"><selection>
  <dataItem name="Region"><expression>[C].[Bank].[Loss].[Region]</expression></dataItem>
  <dataItem name="Total"><expression>total([Amount])</expression></dataItem>
  <dataItem name="Amount" aggregate="total"><expression>[C].[Bank].[Loss].[Amount]</expression></dataItem>
</selection><detailFilter use="required"><filterExpression>[C].[Bank].[Loss].[Year] = 2024</filterExpression>
</detailFilter></query></queries>
<layouts><layout><reportPages><page name="Page1" refQuery="Query1"><pageBody><contents>
  <list name="List1" refQuery="Query1"/>
  <crosstab name="Crosstab1" refQuery="Query1"/>
  <selectValue name="Prompt1" refQuery="Query1"/>
  <vizControl name="Chart1"><vcDataSets><vcDataSet refQuery="Query1"/></vcDataSets></vizControl>
  <combinationChart name="Legacy1" refQuery="Query1"/>
  <list name="List1" refQuery="Query1"/>
  <textItem name="Title"/>
</contents></pageBody></page></reportPages></layout></layouts></report>"""

CHART_URL = f"https://{HOST}:443/bi/v1/disp/?SM=query&search=storeID(%22iX%22)&ITEM=data&ES=&EM="
CHART_DATA = {"dataSet": {"dataTable": [{"row": [{"value": CHART_URL}]}]}}
TABLE_DATA = {"dataSet": {"dataTable": [{"id": "List1", "row": [{"Region": "North", "Amount": "1.5"}]}]}}


# ---------------------------------------------------------------- pure helpers


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}:80/bi/v1/disp?SM=query&x=1", "/v1/disp?SM=query&x=1"),
        (f"https://{HOST.upper()}/bi/v1/disp", "/v1/disp"),
        ("/bi/v1/disp?b_action=rc", "/v1/disp?b_action=rc"),
        ("https://evil.example.test/bi/v1/disp?x=1", None),
        ("javascript:alert(1)", None),
        (f"https://{HOST}/samples/images/logo.png", None),
        ("", None),
        (None, None),
    ],
)
def test_to_bi_path_only_ever_targets_our_bi_server(url, expected):
    assert cco.to_bi_path(url, HOST) == expected


def test_extract_rc_path_reads_both_viewer_shapes_and_refuses_foreign_hosts():
    js = "var sURL = '/bi/v1/disp?b_action=rc&amp;rsid=1&amp;f=CSV&amp;k=SECRET';"
    assert cco.extract_rc_path(js, HOST) == "/v1/disp?b_action=rc&rsid=1&f=CSV&k=SECRET"
    pdf = '<iframe src="/bi/pdf.js/web/viewer.html?file=%2Fbi%2Fv1%2Fdisp%3Fb_action%3Drc%26rsid%3D2">'
    assert cco.extract_rc_path(pdf, HOST) == "/v1/disp?b_action=rc&rsid=2"
    foreign = "pdf.js/web/viewer.html?file=https%3A%2F%2Fevil.example.test%2Fbi%2Fv1%2Fdisp%3Fb_action%3Drc"
    assert cco.extract_rc_path(foreign, HOST) is None
    assert cco.extract_rc_path("<html>no output url</html>", HOST) is None


@pytest.mark.parametrize(
    ("status", "text", "kind"),
    [
        (200, "<error>CV-0015 capability missing</error>", cco.CAPABILITY),
        (200, "<rds:message>RDS-ERR-1020 session</rds:message>", cco.SESSION_LOST),
        (403, "CAM-AAA-0101 expired passport", cco.SESSION_LOST),
        (401, "", cco.SESSION_LOST),
        (503, "", cco.TRANSIENT),
        (429, "", cco.TRANSIENT),
        (500, "RDS-ERR-1000 bad report", cco.REPORT_ERROR),
        (200, "<rds:message>RDS-ERR-1021 unanswered prompts</rds:message>", cco.PROMPT_REQUIRED),
        (404, "", cco.NOT_FOUND),
        (500, "", cco.TRANSIENT),
        (400, "", cco.REPORT_ERROR),
    ],
)
def test_classify(status, text, kind):
    assert cco.classify(status, text)[0] == kind


def test_discover_parts_finds_containers_skips_prompts_pages_and_duplicates():
    parts = cco.discover_parts(SPEC)
    assert [p["name"] for p in parts] == ["List1", "Crosstab1", "Chart1", "Legacy1"]
    by = {p["name"]: p for p in parts}
    assert not by["List1"]["chart"] and not by["Crosstab1"]["chart"]
    assert by["Chart1"]["chart"] and by["Legacy1"]["chart"]
    assert by["Chart1"]["refQuery"] == "Query1"  # read from the nested vcDataSet


def test_discover_parts_resolves_a_chart_query_through_its_data_store():
    # measured shape: vcDataSet@refDataStore -> reportDataStore@name -> dsV5ListQuery@refQuery
    spec = f"""<report {NS}><layouts><layout><reportPages><page name="P"><pageBody><contents>
      <vizControl name="Line"><vcDataSets><vcDataSet idDataSet="data" refDataStore="Data store2"/></vcDataSets>
      </vizControl></contents></pageBody></page></reportPages></layout></layouts>
      <reportDataStores>
        <reportDataStore name="Data store1"><dsSource><dsV5ListQuery refQuery="qBar"/></dsSource></reportDataStore>
        <reportDataStore name="Data store2"><dsSource><dsV5ListQuery refQuery="qLine"/></dsSource></reportDataStore>
      </reportDataStores></report>"""
    assert cco.discover_parts(spec) == [
        {"name": "Line", "label": "Line", "tag": "vizControl", "refQuery": "qLine", "chart": True}
    ]


@pytest.mark.parametrize(
    ("kind", "ctype", "body"),
    [
        ("pdf", "application/pdf", b""),
        ("pdf", "text/html", b"%PDF-1.7"),
        ("pdf", "application/pdf", b"<html>"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"<html>"),
        ("csv", "text/html", b"a,b"),
        ("csv", "text/csv", b"<html><body>login</body></html>"),
        ("image", "image/png", b"GIF89a"),
        ("image", "text/html", b"\x89PNG"),
    ],
)
def test_validate_refuses_empty_or_wrong_type(kind, ctype, body):
    with pytest.raises(cco.Failure) as e:
        cco.validate(kind, ctype, body)
    assert e.value.kind == cco.REFUSED


def test_validate_accepts_real_signatures():
    cco.validate("pdf", "application/pdf", b"%PDF-1.7")
    cco.validate("image", "image/png", b"\x89PNG\r\n")
    cco.validate("csv", "text/csv; charset=UTF-16LE", b"\xff\xfea\x00")


def test_image_only_detects_chart_links_and_nothing_else():
    assert cco.image_only(CHART_DATA, HOST)
    assert not cco.image_only(TABLE_DATA, HOST)
    assert not cco.image_only({"dataSet": {"dataTable": [{"row": [{"value": "42"}]}]}}, HOST)
    foreign = json.loads(json.dumps(CHART_DATA).replace(HOST, "evil.example.test"))
    assert not cco.image_only(foreign, HOST)
    mixed = {"dataSet": {"dataTable": CHART_DATA["dataSet"]["dataTable"] + TABLE_DATA["dataSet"]["dataTable"]}}
    assert not cco.image_only(mixed, HOST)
    assert not cco.image_only({"dataSet": {"dataTable": []}}, HOST)


def test_lineage_resolves_spec_refs_to_module_items_and_sources():
    meta = {
        "useSpec": {"identifier": "M1", "type": "file", "searchPath": "uploadedFile[@name='bank.xlsx']"},
        "querySubject": [
            {
                "idForExpression": "C.Bank.Loss",
                "label": "Loss",
                "ref": ["M1.Loss_Events"],
                "item": [
                    {"queryItem": {"idForExpression": "C.Bank.Loss.Region", "label": "Region", "datatype": "VARCHAR"}},
                    {"queryItem": {"idForExpression": "C.Bank.Loss.Amount", "label": "Amount", "usage": "fact"}},
                ],
            }
        ],
    }
    lin = cco.build_lineage(RID, SPEC, {"kind": "module", "id": MID}, meta)
    assert lin["referenced"] == 3 and lin["resolved"] == 2  # Year is not in the module: an honest gap
    region = lin["model_items"]["C.Bank.Loss.Region"]
    assert region["querySubject"] == "C.Bank.Loss"
    assert region["sources"] == [
        {"use": "M1", "table": "Loss_Events", "type": "file", "searchPath": "uploadedFile[@name='bank.xlsx']"}
    ]
    assert lin["model_items"]["C.Bank.Loss.Year"] is None
    total = next(i for i in lin["queries"][0]["data_items"] if i["name"] == "Total")
    assert total["item_refs"] == ["Amount"] and total["model_refs"] == []
    assert lin["queries"][0]["filters"][0]["use"] == "required"


def test_search_path_names_unescapes_quotes():
    assert cco.search_path_names("/content/folder[@name='It''s']/module[@name='M']") == ["It's", "M"]


# ---------------------------------------------------------------- retry engine


def raising(*outcomes):
    it = iter(outcomes)

    def fn():
        o = next(it)
        if isinstance(o, Exception):
            raise o
        return o

    return fn


def test_run_leg_backs_off_on_transient_then_succeeds(tmp_path):
    cap, sleeps = capturer(tmp_path, None)
    rec = cap.new_leg("x")
    fn = raising(cco.Failure(cco.TRANSIENT, "503"), cco.Failure(cco.TRANSIENT, "503"), "done")
    assert cap.run_leg(rec, fn) == "done"
    assert rec["status"] == "ok" and rec["retries"] == 2 and sleeps == [2.0, 5.0]


def test_run_leg_gives_up_after_max_attempts(tmp_path):
    cap, sleeps = capturer(tmp_path, None)
    rec = cap.new_leg("x")
    assert cap.run_leg(rec, raising(*[cco.Failure(cco.TRANSIENT, "503")] * 5)) is None
    assert rec["status"] == "failed" and rec["class"] == cco.TRANSIENT and len(sleeps) == cco.MAX_ATTEMPTS - 1


def test_run_leg_relogs_in_once_then_records_failure(tmp_path):
    cap, _ = capturer(tmp_path, None)
    rec = cap.new_leg("x")
    assert cap.run_leg(rec, raising(*[cco.Failure(cco.SESSION_LOST, "RDS-ERR-1020")] * 5)) is None
    assert cap.client.logins == 1 and cap.reauths == 1 and rec["reauths"] == 1
    assert rec["status"] == "failed" and rec["class"] == cco.SESSION_LOST


def test_run_leg_relogin_recovers(tmp_path):
    cap, _ = capturer(tmp_path, None)
    rec = cap.new_leg("x")
    assert cap.run_leg(rec, raising(cco.Failure(cco.SESSION_LOST, "x"), "done")) == "done"
    assert rec["status"] == "ok" and cap.client.logins == 1


def test_run_leg_capability_stops_the_run(tmp_path):
    cap, _ = capturer(tmp_path, None)
    rec = cap.new_leg("x")
    with pytest.raises(cco.Stop):
        cap.run_leg(rec, raising(cco.Failure(cco.CAPABILITY, "CV-0015")))
    assert rec["status"] == "failed" and rec["class"] == cco.CAPABILITY


@pytest.mark.parametrize(
    ("kind", "status"),
    [(cco.UNSUPPORTED, "unsupported"), (cco.REFUSED, "refused"), (cco.REPORT_ERROR, "failed")],
)
def test_run_leg_records_unsupported_and_refused_distinctly(tmp_path, kind, status):
    cap, _ = capturer(tmp_path, None)
    rec = cap.new_leg("x")
    cap.run_leg(rec, raising(cco.Failure(kind, "r")))
    assert rec["status"] == status


# ---------------------------------------------------------------- legs and orchestration


def part(name, chart=False):
    return {"name": name, "tag": "vizControl" if chart else "list", "refQuery": "Query1", "chart": chart}


def run_part(cap, tmp_path, getter, p):
    rec = cap.new_leg("leg")
    cap.run_leg(rec, lambda: getter(RID, p, tmp_path, rec))
    return rec


def test_chart_data_leg_is_unsupported_and_writes_nothing(tmp_path):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=json.dumps(CHART_DATA)))
    rec = run_part(cap, tmp_path, cap.get_part_data, part("Chart1", True))
    assert rec["status"] == "unsupported" and "Query1" in rec["reason"]
    assert not (tmp_path / "parts").exists()


def test_table_data_leg_stores_rows(tmp_path):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=json.dumps(TABLE_DATA)))
    rec = run_part(cap, tmp_path, cap.get_part_data, part("List1"))
    assert rec["status"] == "ok" and rec["rows"] == 1 and (tmp_path / "parts" / "List1.json").exists()


@pytest.mark.parametrize("body", ['{"dataSet":null}', '{"dataSet":{"dataTable":[{"row":[]}]}}', "<html/>"])
def test_unknown_selection_or_empty_data_is_refused(tmp_path, body):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body))
    assert run_part(cap, tmp_path, cap.get_part_data, part("Nope"))["status"] == "refused"


@pytest.mark.parametrize(("code", "kind"), [("RDS-ERR-1000", cco.REPORT_ERROR), ("RDS-ERR-1021", cco.PROMPT_REQUIRED)])
def test_rds_error_inside_http_200_is_a_failure(tmp_path, code, kind):
    body = f"<rds:error><rds:message>{code} the report failed</rds:message></rds:error>"
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    rec = run_part(cap, tmp_path, cap.get_part_data, part("List1"))
    assert rec["status"] == "failed" and rec["class"] == kind and code in rec["reason"]


def test_rds_legs_run_without_prompting(tmp_path):
    # measured: without prompt=false RDS refuses a report whose prompts are all optional (RDS-ERR-1021)
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=json.dumps(TABLE_DATA)))
    run_part(cap, tmp_path, cap.get_part_data, part("List1"))
    run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    rds = [p for _, p in cap.client.calls if "/rds/" in p]
    assert len(rds) == 2 and all("prompt=false" in p for p in rds)


def viewer_page(status, conv="CONV1"):
    state = f'"m_sStatus": "{status}", "m_sConversation": "{conv}", "m_sTracking": "T-{conv}", "m_sActionState": "A"'
    url = "var sURL = '/bi/v1/disp?b_action=rc&amp;rsid=1';" if status == "complete" else ""
    return Resp(body=f"<script>oCV.initViewer({{ {state} }});{url}</script>", ctype="text/html")


def viewer_server(pages):
    """The first viewer GET and every wait POST return the next page; the rc URL returns a PDF."""
    queue = list(pages)

    def handler(kind, path):
        if "b_action=rc" in path:
            return Resp(body=b"%PDF-1.7 test", ctype="application/pdf")
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


def run_viewer(tmp_path, pages):
    cap, sleeps = capturer(tmp_path, viewer_server(pages))
    rec = cap.new_leg("pdf")
    cap.run_leg(rec, lambda: cap.get_viewer(RID, "pdf", tmp_path, rec))
    return cap, rec, sleeps


def test_viewer_polls_a_working_run_with_the_latest_tokens_until_complete(tmp_path):
    pages = [viewer_page("working"), viewer_page("stillWorking", "CONV2"), viewer_page("complete")]
    cap, rec, sleeps = run_viewer(tmp_path, pages)
    assert rec["status"] == "ok" and (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")
    assert [p["ui.conversation"] for p in cap.client.posts] == ["CONV1", "CONV2"]
    assert all(p["ui.action"] == "wait" and p["m_tracking"].startswith("T-") for p in cap.client.posts)
    assert sleeps == [cco.VIEWER_POLL_S] * 2


def test_viewer_still_working_after_the_wait_budget_is_incomplete(tmp_path):
    cap, rec, sleeps = run_viewer(tmp_path, [viewer_page("working")])
    assert rec["status"] == "failed" and rec["class"] == cco.INCOMPLETE
    # one more attempt only (H1b), after one backoff: two full poll budgets, never a third
    polls = int(cco.VIEWER_WAIT_S / cco.VIEWER_POLL_S)
    assert len(cap.client.posts) == 2 * polls and len(sleeps) == 2 * polls + 1
    assert sleeps.count(cco.BACKOFF_S[0]) == 1 and rec["retries"] == 1
    assert not (tmp_path / "report.pdf").exists()


@pytest.mark.parametrize(
    ("page", "kind"),
    [
        (viewer_page("working", conv=""), cco.REPORT_ERROR),
        (viewer_page("prompting"), cco.PROMPT_REQUIRED),
    ],
)
def test_viewer_without_a_conversation_or_needing_prompts_fails_without_polling(tmp_path, page, kind):
    cap, rec, _ = run_viewer(tmp_path, [page])
    assert rec["status"] == "failed" and rec["class"] == kind and cap.client.posts == []


def test_render_refuses_an_empty_fragment(tmp_path):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body="<div></div>", ctype="text/xml"))
    assert run_part(cap, tmp_path, cap.get_part_render, part("Nope"))["status"] == "refused"


@pytest.mark.parametrize(("leg", "body"), [("get_part_data", '{"dataSet":null}'), ("get_part_render", "<div></div>")])
def test_singleton_legs_are_unsupported_not_refused(tmp_path, leg, body):
    # measured: RDS does not address singletons, so a known spec singleton must not fail the run as unknown
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    singleton = {"name": "Singleton1", "tag": "singleton", "refQuery": "qYtY", "chart": False}
    rec = run_part(cap, tmp_path, getattr(cap, leg), singleton)
    assert rec["status"] == "unsupported" and "singleton" in rec["reason"]
    assert not (tmp_path / "parts").exists()


def test_render_refuses_a_chart_without_image(tmp_path):
    body = "<div><table><tr><td>no chart</td></tr></table></div>"
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("Chart1", True))
    assert rec["status"] == "refused" and "no image" in rec["reason"]


def test_render_fetches_only_our_chart_images(tmp_path):
    frag = (
        f'<div><img src="https://{HOST}:80/bi/v1/disp?SM=query&amp;search=storeID(1)"/>'
        '<img src="/samples/images/logo.png"/><img src="https://evil.example.test/bi/v1/disp?SM=query"/></div>'
    )

    def handler(kind, path):
        if "HTMLFragment" in path:
            return Resp(body=frag, ctype="text/xml")
        return Resp(body=b"\x89PNG\r\n\x1a\n", ctype="image/png")

    cap, _ = capturer(tmp_path, handler)
    rec = run_part(cap, tmp_path, cap.get_part_render, part("Chart1", True))
    assert rec["status"] == "ok" and len(rec["images"]) == 1
    assert [p for _, p in cap.client.calls if "HTMLFragment" not in p] == ["/v1/disp?SM=query&search=storeID(1)"]


def test_capture_report_without_spec_skips_dependent_legs_and_keeps_one_folder(tmp_path):
    def handler(kind, path):
        if path.startswith("/v1/objects/"):
            return Resp(body=json.dumps({"data": [{"defaultName": "R", "owner": "CAMID(secret)", "ancestors": []}]}))
        return Resp(status=404, body="")

    cap, _ = capturer(tmp_path, handler)
    (tmp_path / RID).mkdir()
    (tmp_path / RID / "stale.txt").write_text("old run")
    reports: list[dict] = []
    entry = cap.capture_report(RID, "report-id", reports)
    status = {leg["leg"]: leg["status"] for leg in entry["legs"]}
    assert status == {
        "object": "ok",
        "spec": "failed",
        "parts": "skipped",
        "lineage": "skipped",
        "usage": "unsupported",
    }
    assert reports == [entry]
    assert sorted(p.name for p in (tmp_path / RID).iterdir()) == ["capture.json", "object.json"]
    assert "owner" not in json.loads((tmp_path / RID / "object.json").read_text())
    assert not (tmp_path / ".staging" / RID).exists()


def fake_server(spec=SPEC, chart_body=CHART_DATA):
    modules = {"modules": [{"name": "Bank mod", "id": MID, "ancestors": [{}, {"name": "Team"}]}]}
    items = {"content": [{"id": RID, "type": "report"}, {"id": XID, "type": "dataSet2"}]}

    def handler(kind, path):
        if path.startswith(f"/content/{FID}/items"):
            return Resp(body=json.dumps(items))
        if path.startswith("/v1/objects/"):
            return Resp(body=json.dumps({"data": [{"defaultName": "R", "ancestors": [], "module": None}]}))
        if path.startswith(f"/content/{RID}?fields=specification"):
            return Resp(body=json.dumps({"specification": spec}))
        if path == "/modules":
            return Resp(body=json.dumps(modules))
        if path == f"/modules/{MID}/metadata":
            return Resp(body=json.dumps({"querySubject": []}))
        if "fmt=DataSetJSON" in path:
            chart = "Chart1" in path or "Legacy1" in path
            return Resp(body=json.dumps(chart_body if chart else TABLE_DATA))
        if "fmt=HTMLFragment" in path:
            img = f'<img src="https://{HOST}/bi/v1/disp?SM=query&amp;n=1"/>'
            return Resp(body=f"<div>{img}</div>", ctype="text/xml")
        if "SM=query" in path:
            return Resp(body=b"\x89PNG\r\n\x1a\n", ctype="image/png")
        return Resp(status=404)

    return handler


def test_main_charts_without_data_are_named_but_do_not_fail_the_run(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, fake_server())
    assert code == 0 and m["status"] == "ok"
    assert [r["id"] for r in m["reports"]] == [RID]
    assert {d["part"] for d in m["data_unestablished"]} == {"Chart1", "Legacy1"}
    assert all(d["status"] == "unsupported" for d in m["data_unestablished"])
    assert m["render_unestablished"] == []
    lineage = json.loads((tmp_path / RID / "lineage.json").read_text(encoding="utf-8"))
    assert lineage["model"]["id"] == MID
    assert lineage["model"]["metadata"] == "module.json" and (tmp_path / RID / "module.json").exists()
    assert not (tmp_path / "modules").exists()
    assert "not-a-real-key" not in (tmp_path / "manifest.json").read_text(encoding="utf-8")


def test_main_exits_1_when_a_leg_fails(tmp_path, monkeypatch):
    broken = {"dataSet": {"dataTable": [{"row": []}]}}
    code, m = run_main(tmp_path, monkeypatch, fake_server(chart_body=broken))
    assert code == 1 and m["status"] == "partial"
    assert {d["status"] for d in m["data_unestablished"]} == {"refused"}


def test_main_stops_with_exit_2_and_records_the_rest_as_not_attempted(tmp_path, monkeypatch):
    base = fake_server()

    def handler(kind, path):
        if path.startswith(f"/content/{FID}/items"):
            items = [{"id": RID, "type": "report"}, {"id": SID, "type": "report"}]
            return Resp(body=json.dumps({"content": items}))
        if "b_action=cognosViewer" in path:  # measured: the viewer answers 200 with the CV-0015 page
            return Resp(body="<html>CV-0015 no capability</html>", ctype="text/html")
        return base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler, formats="pdf")
    assert code == 2 and m["status"] == "stopped"
    pdf = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "pdf")
    assert pdf["class"] == cco.CAPABILITY
    assert m["reports"][1] == {"id": SID, "origin": f"folder:{FID}", "status": "not_attempted"}
    assert not (tmp_path / ".staging").exists()


# ---------------------------------------------------------------- review hardening: ids and paths


def no_client(*a, **k):
    raise AssertionError("a usage error must stop before any client is built")


@pytest.mark.parametrize("flag", ["--report-id", "--folder-id"])
@pytest.mark.parametrize("bad", ["..", "", "..\\escaped", "iR", "i" + "G" * 32, RID + "x"])
def test_main_refuses_ids_that_are_not_store_ids_and_touches_nothing(tmp_path, monkeypatch, flag, bad):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("keep")
    (tmp_path / "sibling.txt").write_text("sibling")
    monkeypatch.setattr(cco, "Client", no_client)
    monkeypatch.setenv("COGNOS_API_KEY", "not-a-real-key")
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(out), flag, bad]
    with pytest.raises(SystemExit) as e:
        cco.main(argv)
    assert e.value.code == 2
    assert (out / "keep.txt").exists() and (tmp_path / "sibling.txt").exists()
    assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]


@pytest.mark.parametrize("parts", ["", " , ", ","])
def test_main_refuses_a_parts_list_without_names(tmp_path, monkeypatch, parts):
    monkeypatch.setattr(cco, "Client", no_client)
    monkeypatch.setenv("COGNOS_API_KEY", "k")
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(tmp_path), "--report-id", RID]
    with pytest.raises(SystemExit) as e:
        cco.main(argv + ["--parts", parts])
    assert e.value.code == 2


def test_inside_refuses_every_path_that_leaves_the_output_folder(tmp_path):
    out = tmp_path / "out"
    assert cco.inside(out, out / "a" / "b.json") == (out / "a" / "b.json").resolve()
    for escape in (out, out / "..", out / ".." / "x", out / "a" / ".." / ".." / "x", tmp_path / "outside"):
        with pytest.raises(cco.Failure) as e:
            cco.inside(out, escape)
        assert e.value.kind == cco.REFUSED


@pytest.mark.parametrize("rid", ["..", "", "..\\escaped", "iR"])
def test_capture_report_refuses_an_invalid_id_and_writes_or_deletes_nothing(tmp_path, rid):
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "sibling.txt").write_text("sibling")
    cap = cco.Capturer(FakeClient(lambda kind, path: Resp(status=404)), out, [], "auto", sleep=lambda s: None)
    entry = cap.capture_report(rid, "folder:x", [])
    assert [(leg["leg"], leg["status"]) for leg in entry["legs"]] == [("report", "refused")]
    assert cap.client.calls == [] and list(out.iterdir()) == [] and (tmp_path / "sibling.txt").exists()


def test_module_metadata_refuses_an_invalid_module_id_and_writes_nothing(tmp_path):
    out = tmp_path / "out"
    cap = cco.Capturer(FakeClient(lambda kind, path: Resp(body="{}")), out, [], "auto")
    with pytest.raises(cco.Failure) as e:
        cap.module_metadata("..\\..\\pwned")
    assert e.value.kind == cco.REFUSED and cap.client.calls == []
    assert not list(tmp_path.rglob("pwned*"))


# ---------------------------------------------------------------- discovery


def listing_server(content=None, objects=None):
    """content: folder id -> children over /api/v1/content (missing -> 404); objects: id -> children over /bi."""
    content, objects = content or {}, objects or {}

    def handler(kind, path):
        m = re.match(r"/content/(i\w+)/items$", path)
        if kind == "api" and m:
            kids = content.get(m.group(1))
            return Resp(status=404) if kids is None else Resp(body=json.dumps({"content": kids}))
        m = re.match(r"/v1/objects/(i\w+)/items\?fields=defaultName,type,id$", path)
        if kind == "bi" and m:
            kids = objects.get(m.group(1))
            return Resp(status=404) if kids is None else Resp(body=json.dumps({"data": kids}))
        return Resp(status=404)

    return handler


def kid(i, kind, name="x"):
    return {"id": i, "type": kind, "defaultName": name}


def test_discover_recurses_into_subfolders(tmp_path):
    srv = listing_server(content={FID: [kid(SUB, "folder"), kid(RID, "report")], SUB: [kid(SID, "report")]})
    cap, _ = capturer(tmp_path, srv)
    found, legs, skipped = cap.discover([FID], [])
    assert found == [(SID, f"folder:{FID}"), (RID, f"folder:{FID}")]  # depth first
    assert [(leg["leg"], leg["status"], leg["route"], leg["items"]) for leg in legs] == [
        (f"list:{FID}", "ok", "content", 2),
        (f"list:{SUB}", "ok", "content", 1),
    ]
    assert skipped == []


def test_discover_walks_a_package_through_bi_objects_and_names_every_skipped_child(tmp_path):
    # a package is always listed through /bi/v1/objects (measured 200); /api/v1/content/{package}/items is not
    # used for one, whatever it answers (here 404; live it answered 200, see the G4 test below)
    shortcut, view, module, inter = ("i" + c * 32 for c in "2345")
    pkg_kids = [
        kid(XID, "report", "R in package"),
        kid(SUB, "folder", "Sub"),
        kid(shortcut, "shortcut", "Short"),
        kid(view, "reportView", "View"),
        kid(module, "module", "Mod"),
        kid(inter, "interactiveReport", "IR"),
        kid("..\\escaped", "report", "evil"),
        "not-a-dict",
    ]
    srv = listing_server(content={FID: [kid(PKG, "package")]}, objects={PKG: pkg_kids, SUB: [kid(SID, "report")]})
    cap, _ = capturer(tmp_path, srv)
    found, legs, skipped = cap.discover([FID], [RID])
    assert found == [(RID, "report-id"), (XID, f"folder:{FID}"), (SID, f"folder:{FID}"), (inter, f"folder:{FID}")]
    lists = [(leg.get("route"), leg["status"]) for leg in legs if leg["leg"].startswith("list:")]
    assert lists == [("content", "ok"), ("objects", "ok"), ("objects", "ok")]
    # the shortcut lookup answered 404: its own failed discovery leg, not a silent skip
    assert [(leg["leg"], leg["status"]) for leg in legs if leg["leg"].startswith("resolve:")] == [
        (f"resolve:{shortcut}", "failed")
    ]
    # a folder inside a package is listed through /bi/v1/objects too, never through /api/v1/content
    assert ("api", f"/content/{SUB}/items") not in cap.client.calls
    by_type = {s["type"]: s for s in skipped}
    assert set(by_type) == {"shortcut", "reportView", "module", "report", None}
    assert by_type["shortcut"] == {
        "id": shortcut,
        "type": "shortcut",
        "name": "Short",
        "parent": PKG,
        "reason": by_type["shortcut"]["reason"],
    }
    assert "target" in by_type["shortcut"]["reason"] and by_type["report"]["id"] == "..\\escaped"
    assert all(s["parent"] == PKG and s["reason"] for s in skipped)


def test_discover_falls_back_to_bi_objects_when_the_folder_id_is_a_package(tmp_path):
    cap, _ = capturer(tmp_path, listing_server(objects={PKG: [kid(RID, "report")]}))
    found, legs, _ = cap.discover([PKG], [])
    assert found == [(RID, f"folder:{PKG}")] and legs[0]["route"] == "objects" and legs[0]["status"] == "ok"


@pytest.mark.parametrize(
    ("resp", "status"),
    [
        (Resp(body="<html>login</html>", ctype="text/html"), "refused"),
        (Resp(body=json.dumps({"something": []})), "refused"),
        (Resp(body=json.dumps({"content": "nope"})), "refused"),
        (Resp(status=400, body="CM-REQ-4012 bad request"), "failed"),
    ],
)
def test_discover_never_credits_a_listing_it_could_not_read(tmp_path, resp, status):
    cap, _ = capturer(tmp_path, lambda kind, path: resp)
    found, legs, skipped = cap.discover([FID], [])
    assert found == [] and skipped == [] and [leg["status"] for leg in legs] == [status]


def run_main(tmp_path, monkeypatch, handler, formats="none", ids=None, parts=None):
    monkeypatch.setattr(cco, "Client", lambda *a, **k: FakeClient(handler))
    monkeypatch.setenv("COGNOS_API_KEY", "not-a-real-key")
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(tmp_path), "--formats", formats]
    argv += ids if ids is not None else ["--folder-id", FID]
    argv += ["--parts", parts] if parts is not None else []
    try:
        code = cco.main(argv)
    except KeyboardInterrupt:
        pytest.fail("Ctrl+C escaped main")
    return code, json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))


def test_main_exits_1_when_a_folder_listing_fails(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, lambda kind, path: Resp(body="<html/>", ctype="text/html"))
    assert code == 1 and m["status"] == "no_reports" and m["reports"] == []
    assert [leg["status"] for leg in m["discovery"]] == ["refused"]


def test_main_records_skipped_children_without_failing(tmp_path, monkeypatch):
    base = fake_server()
    module = {"data": [{"id": MID, "type": "module", "defaultName": "Mod"}]}
    srv = shortcut_server(resolved=module, extra=[kid(RID, "report"), kid(SID, "reportView", "V")])

    def handler(kind, path):
        shortcut = "/items" in path or "fields=target" in path or path.startswith("/v1/path?")
        return srv(kind, path) if shortcut else base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler)
    assert code == 0 and m["status"] == "ok" and [r["id"] for r in m["reports"]] == [RID]
    assert [(s["id"], s["type"]) for s in m["discovery_skipped"]] == [(SC, "shortcut"), (SID, "reportView")]


# ---------------------------------------------------------------- exit codes and unestablished lists


VIEWER_BODIES = {
    "PDF": (b"%PDF-1.7 test", "application/pdf"),
    "CSV": ("a,b\n1,2\n".encode("utf-16"), "text/csv; charset=UTF-16"),
    "spreadsheetML": (b"PK\x03\x04 xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


def all_viewers(base):
    """Every viewer format completes with its own, valid output."""
    last = {"fmt": "PDF"}

    def handler(kind, path):
        if "b_action=cognosViewer" in path:
            last["fmt"] = re.search(r"run\.outputFormat=(\w+)", path).group(1)
            return viewer_page("complete")
        if "b_action=rc" in path:
            body, ctype = VIEWER_BODIES[last["fmt"]]
            return Resp(body=body, ctype=ctype)
        return base(kind, path)

    return handler


def with_viewer(base, page):
    def handler(kind, path):
        if "b_action=cognosViewer" in path:
            return page
        if "b_action=rc" in path:
            return Resp(body=b"%PDF-1.7 test", ctype="application/pdf")
        return base(kind, path)

    return handler


@pytest.mark.parametrize(
    ("page", "kind"),
    [
        (viewer_page("prompting"), cco.PROMPT_REQUIRED),
        (viewer_page("working"), cco.INCOMPLETE),
        (Resp(status=400, body="RDS-ERR-1000 the report failed", ctype="text/html"), cco.REPORT_ERROR),
    ],
)
def test_main_exits_1_for_every_failed_class(tmp_path, monkeypatch, page, kind):
    monkeypatch.setattr(cco, "VIEWER_WAIT_S", 0.0)  # no polling: a working page stays working
    code, m = run_main(tmp_path, monkeypatch, with_viewer(fake_server(), page), formats="pdf")
    pdf = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "pdf")
    assert pdf["status"] == "failed" and pdf["class"] == kind
    assert code == 1 and m["status"] == "partial"
    assert {"report": RID, "part": None, "leg": "pdf", "reason": pdf["reason"]} in m["render_unestablished"]


def test_main_ok_viewer_run_is_not_in_render_unestablished(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), formats="pdf")
    assert code == 0 and m["render_unestablished"] == []
    assert (tmp_path / RID / "report.pdf").read_bytes().startswith(b"%PDF")


def test_main_names_a_failed_part_render(tmp_path, monkeypatch):
    base = fake_server()

    def handler(kind, path):
        if "HTMLFragment" in path and "List1" in path:
            return Resp(status=400, body="RDS-ERR-1000 broken", ctype="text/xml")
        return base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler)
    assert code == 1
    assert [(d["part"], d["leg"]) for d in m["render_unestablished"]] == [("List1", "part:List1:render")]


def test_unestablished_lists_name_every_non_ok_leg():
    legs = [
        {"leg": "pdf", "status": "failed", "reason": "x"},
        {"leg": "parts", "status": "skipped", "reason": "spec unavailable"},
        {"leg": "part:A:render", "part": "A", "status": "refused", "reason": "y"},
        {"leg": "part:A:data", "part": "A", "status": "failed", "reason": "z"},
        {"leg": "part:B:render", "part": "B", "status": "ok"},
        {"leg": "part:B:data", "part": "B", "status": "ok"},
        {"leg": "csv", "status": "failed"},
    ]
    reports = [{"id": RID, "legs": legs}]
    assert [(d["leg"], d["part"]) for d in cco.render_unestablished(reports)] == [
        ("pdf", None),
        ("parts", None),
        ("part:A:render", "A"),
    ]
    assert [(d["part"], d["status"]) for d in cco.data_unestablished(reports)] == [(None, "skipped"), ("A", "failed")]


@pytest.mark.parametrize(
    ("leg", "listed"),
    [
        ({"status": "ok", "model_kind": "module", "resolved": 3, "referenced": 3, "unresolved": []}, False),
        ({"status": "ok", "model_kind": "module", "resolved": 9, "referenced": 13, "unresolved": ["#m#"]}, True),
        ({"status": "ok", "model_kind": "package", "resolved": 0, "referenced": 4, "unresolved": []}, True),
        ({"status": "failed", "reason": "module not found"}, True),
        ({"status": "skipped", "reason": "spec unavailable"}, True),
    ],
)
def test_lineage_unestablished_names_partial_or_missing_lineage(leg, listed):
    out = cco.lineage_unestablished([{"id": RID, "legs": [{"leg": "lineage", **leg}]}])
    assert bool(out) == listed
    if listed and leg["status"] != "ok":
        assert out[0]["status"] == leg["status"] and out[0]["reason"] == leg["reason"]


def test_main_lineage_with_unresolved_refs_is_named_but_not_failed(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, fake_server())
    assert code == 0
    [lin] = m["lineage_unestablished"]
    assert lin["report"] == RID and lin["resolved"] == 0 and lin["referenced"] == 3 and len(lin["unresolved"]) == 3


def test_main_crash_is_exit_3_and_never_reads_as_partial(tmp_path, monkeypatch, capsys):
    base = fake_server()

    def handler(kind, path):
        if "fields=specification" in path:
            raise RuntimeError("boom not-a-real-key")
        return base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler)
    assert code == 3 and m["status"] == "crashed"
    assert m["crash"].startswith("RuntimeError") and "not-a-real-key" not in m["crash"]
    spec = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "spec")
    assert spec["status"] == "failed" and spec["class"] == "crashed"
    assert all(leg["status"] != "pending" for r in m["reports"] for leg in r.get("legs", []))
    assert "not-a-real-key" not in (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert not (tmp_path / ".staging").exists() and not (tmp_path / ".trash").exists()
    printed = capsys.readouterr()
    assert "RuntimeError: boom <key>" in printed.err  # the traceback is printed, with the key redacted
    assert "not-a-real-key" not in printed.err and "not-a-real-key" not in printed.out


def test_main_announces_its_deadline(tmp_path, monkeypatch, capsys):
    _, m = run_main(tmp_path, monkeypatch, fake_server())
    assert m["deadline"]["request_timeout_s"] == cco.DEFAULT_TIMEOUT_S
    assert m["deadline"]["worst_case_leg_s"] == cco.worst_case_leg_s(cco.DEFAULT_TIMEOUT_S)
    assert "deadline:" in capsys.readouterr().out


# ---------------------------------------------------------------- staging and swap


def report_server(spec=SPEC, seen=None, final=None):
    base = fake_server(spec)

    def handler(kind, path):
        if seen is not None and "fields=specification" in path:
            seen.append(sorted(p.name for p in final.iterdir()))
        return base(kind, path)

    return handler


def test_capture_writes_to_staging_and_leaves_the_old_capture_until_the_swap(tmp_path):
    final = tmp_path / RID
    final.mkdir()
    (final / "stale.txt").write_text("old run")
    seen: list = []
    cap, _ = capturer(tmp_path, report_server(seen=seen, final=final))
    entry = cap.capture_report(RID, "report-id", [])
    assert seen == [["stale.txt"]]  # mid-capture the old folder is untouched and holds no new file
    assert not (final / "stale.txt").exists() and (final / "object.json").exists() and (final / "spec.xml").exists()
    assert all(leg["status"] != "failed" for leg in entry["legs"])
    assert not (tmp_path / ".staging" / RID).exists()
    assert not (tmp_path / ".trash").exists() or not list((tmp_path / ".trash").iterdir())


@pytest.mark.parametrize("locked", ["old", "new"])
def test_swap_keeps_the_old_capture_when_it_cannot_be_replaced(tmp_path, monkeypatch, locked):
    final = tmp_path / RID
    final.mkdir()
    (final / "report.pdf").write_text("old pdf")
    real_rename = Path.rename

    def rename(self, target):
        # "old": the old folder holds an open file; "new": the staged capture cannot take its place
        staged = self.parent.name == ".staging"
        if (locked == "old" and self == final) or (locked == "new" and staged and Path(target) == final):
            raise PermissionError(13, "in use")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    cap, _ = capturer(tmp_path, report_server())
    entry = cap.capture_report(RID, "report-id", [])
    assert (final / "report.pdf").read_text() == "old pdf" and not (final / "object.json").exists()
    store = entry["legs"][-1]
    assert store["leg"] == "store" and store["status"] == "failed" and store["class"] == "store_error"
    obj = next(leg for leg in entry["legs"] if leg["leg"] == "object")
    assert obj["path"] == f"{RID}.incoming/object.json"
    assert (tmp_path / obj["path"]).exists()


# ---------------------------------------------------------------- parts


def spec_with(contents):
    return f"""<report {NS}><queries><query name="Query1"/></queries><layouts><layout><reportPages>
      <page name="P"><pageBody><contents>{contents}</contents></pageBody></page>
      </reportPages></layout></layouts></report>"""


def test_discover_parts_names_a_viz_and_never_its_data_set():
    # real shape: By industry/Insurance/Customer claim analysis
    spec = spec_with(
        '<viz name="Clustered bar1"><vizDataSets>'
        '<vizDataSet name="Clustered bar: Tabular data set" refQuery="qLocation"/></vizDataSets></viz>'
        '<customControl name="Custom1"><customControlDataSets>'
        '<customControlDataSet name="DS1" refQuery="qC"/></customControlDataSets></customControl>'
        '<customControl name="Button1"/>'
        '<appList name="App1"><appListDataSet refQuery="qA"/></appList>'
    )
    parts = cco.discover_parts(spec)
    assert [(p["name"], p["tag"], p["refQuery"], p["chart"]) for p in parts] == [
        ("Clustered bar1", "viz", "qLocation", True),
        ("Custom1", "customControl", "qC", False),
        ("App1", "appList", "qA", False),
    ]


def test_discover_parts_records_unnamed_containers():
    parts = cco.discover_parts(spec_with('<list refQuery="Query1"/><singleton refQuery="Query1"/><list/>'))
    assert [(p["name"], p["label"], p["tag"]) for p in parts] == [
        (None, "unnamed:list:1", "list"),
        (None, "unnamed:singleton:1", "singleton"),
    ]


def test_main_unnamed_container_is_unsupported_and_named_in_both_lists(tmp_path, monkeypatch):
    spec = spec_with('<list name="List1" refQuery="Query1"/><list refQuery="Query1"/>')
    code, m = run_main(tmp_path, monkeypatch, fake_server(spec=spec))
    assert code == 0
    assert [(d["part"], d["status"]) for d in m["data_unestablished"]] == [("unnamed:list:1", "unsupported")]
    assert [d["part"] for d in m["render_unestablished"]] == ["unnamed:list:1"]


def test_custom_control_data_is_attempted_and_its_render_recorded_unsupported(tmp_path):
    spec = spec_with(
        '<customControl name="Custom1"><customControlDataSets><customControlDataSet refQuery="Query1"/>'
        "</customControlDataSets></customControl>"
    )
    cap, _ = capturer(tmp_path, fake_server(spec=spec))
    entry = cap.capture_report(RID, "report-id", [])
    status = {leg["leg"]: leg["status"] for leg in entry["legs"]}
    assert status["part:Custom1:data"] == "ok" and status["part:Custom1:render"] == "unsupported"
    assert not any("HTMLFragment" in p for _, p in cap.client.calls)


def test_part_files_are_unique_case_insensitively(tmp_path):
    spec = spec_with('<list name="List1" refQuery="Query1"/><list name="list1" refQuery="Query1"/>')
    cap, _ = capturer(tmp_path, fake_server(spec=spec))
    entry = cap.capture_report(RID, "report-id", [])
    files = [p["file"] for p in entry["parts"]]
    assert len({f.lower() for f in files}) == 2
    assert len(list((tmp_path / RID / "parts").glob("*.json"))) == 2
    paths = [leg["path"] for leg in entry["legs"] if leg["leg"].startswith("part:")]
    assert len({p.lower() for p in paths}) == 4


def test_unique_stem_disambiguates_case_and_repeats():
    used: set[str] = set()
    stems = [cco.unique_stem(n, used) for n in ("List1", "list1", "LIST1", "List1")]
    assert len({s.lower() for s in stems}) == 4 and stems[0] == "List1"
    # a clash takes a suffix derived from the name itself, so the file name does not depend on spec order
    assert stems[2] == "LIST1-" + __import__("hashlib").sha1(b"LIST1").hexdigest()[:6]


SINGLETON = {"name": "Singleton1", "label": "Singleton1", "tag": "singleton", "refQuery": "q", "chart": False}


@pytest.mark.parametrize("body", ["{}", "[]", '{"error": "x"}', '{"dataSet": {}}', '{"dataSet": null, "x": 1}'])
def test_singleton_data_shapes_other_than_the_measured_one_are_refused(tmp_path, body):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body))
    assert run_part(cap, tmp_path, cap.get_part_data, SINGLETON)["status"] == "refused"


@pytest.mark.parametrize(
    ("body", "ctype"),
    [("<div><div></div></div>", "text/xml"), ('{"error": "x"}', "application/json"), ("", "text/xml")],
)
def test_singleton_render_shapes_other_than_the_measured_one_are_refused(tmp_path, body, ctype):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype=ctype))
    assert run_part(cap, tmp_path, cap.get_part_render, SINGLETON)["status"] == "refused"


@pytest.mark.parametrize(
    ("body", "ctype"),
    [
        ("<html><head><title>Log in</title></head><body><form>sign in</form></body></html>", "text/html"),
        ('{"error": "something went wrong"}', "application/json"),
        ("<div><table><tr><td>1</td></tr></table></div>", "application/json"),
        ("<div><form action='/login'>sign in</form></div>", "text/html"),
        ("plain text", "text/plain"),
    ],
)
def test_table_render_refuses_anything_but_an_html_fragment(tmp_path, body, ctype):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype=ctype))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    assert rec["status"] == "refused" and not (tmp_path / "parts").exists()


def test_table_render_accepts_the_measured_fragment(tmp_path):
    body = "<div><div><table><tr><td>North</td></tr></table></div></div>"
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml; charset=UTF-8"))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    assert rec["status"] == "ok" and (tmp_path / "parts" / "List1.html").exists()


# ---------------------------------------------------------------- viewer


def test_viewer_runs_without_prompting(tmp_path):
    cap, rec, _ = run_viewer(tmp_path, [viewer_page("complete")])
    first = cap.client.calls[0][1]
    assert rec["status"] == "ok" and "b_action=cognosViewer" in first and "run.prompt=false" in first


def test_viewer_output_that_is_not_the_requested_format_is_refused(tmp_path):
    def handler(kind, path):
        if "b_action=rc" in path:
            return Resp(body="<html><body>session expired</body></html>", ctype="text/html")
        return viewer_page("complete")

    cap, _ = capturer(tmp_path, handler)
    rec = cap.new_leg("pdf")
    cap.run_leg(rec, lambda: cap.get_viewer(RID, "pdf", tmp_path, rec))
    assert rec["status"] == "refused" and not (tmp_path / "report.pdf").exists()


@pytest.mark.parametrize("title", ["KPI-FIN-2024", "CAM-AAA-0101", "RDS-ERR-1021 overview", "CV-0015"])
def test_a_code_shaped_report_title_never_overrides_a_complete_status(tmp_path, title):
    page = viewer_page("complete")
    page = Resp(body=f"<html><head><title>{title}</title></head>{page.text}</html>", ctype="text/html")
    cap, rec, _ = run_viewer(tmp_path, [page])
    assert rec["status"] == "ok" and cap.client.logins == 0


def test_a_failed_viewer_page_is_classified_from_its_body_not_its_title(tmp_path):
    page = Resp(body="<html><head><title>CAM-AAA-0101</title></head><body>no status</body></html>", ctype="text/html")
    cap, rec, _ = run_viewer(tmp_path, [page])
    assert rec["status"] == "failed" and rec["class"] == cco.REPORT_ERROR and cap.client.logins == 0


# ---------------------------------------------------------------- Client over a fake transport

XSRF, PASSPORT, KEY = "SECRET-XSRF-7f3a", "SECRET-PASSPORT-91c2", "SECRET-APIKEY-0d4e"


class Raw:
    """The urllib3 body stream that fetch() reads: read1 answers at most one chunk per call."""

    def __init__(self, body: bytes):
        self.body = body

    def read1(self, amt, decode_content=True):
        chunk, self.body = self.body[:amt], self.body[amt:]
        return chunk


class Wire(Resp):
    def __init__(self, status=200, body=b"", ctype="application/json", url="", location=None):
        super().__init__(status, body, ctype)
        self.url, self.raw = url, Raw(self.content)
        if location:
            self.headers["location"] = location

    def close(self):
        pass


class FakeSession:
    """Stands in for requests.Session: records every request; ``route`` answers by full URL."""

    def __init__(self, route=None):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.route, self.sent = route, []

    def _send(self, method, url, **kw):
        self.sent.append({"method": method, "url": url, **kw})
        resp = self.route(self, method, url, kw)
        resp.url = resp.url or url
        return resp

    def get(self, url, **kw):
        return self._send("GET", url, **kw)

    def put(self, url, **kw):
        return self._send("PUT", url, **kw)

    def request(self, method, url, **kw):
        return self._send(method, url, **kw)


def logged_in(route):
    client = cco.Client(f"https://{HOST}", "ns", KEY)
    client.session = FakeSession(route)
    client.session.cookies.set("XSRF-TOKEN", XSRF)
    client.session.cookies.set("cam_passport", PASSPORT)
    return client


def test_client_forwards_cookies_and_xsrf_and_never_lets_requests_redirect():
    client = logged_in(lambda s, m, u, kw: Wire(body="{}"))
    client.bi("GET", "/v1/objects/x")
    [sent] = client.session.sent
    assert sent["url"] == f"https://{HOST}/bi/v1/objects/x" and sent["allow_redirects"] is False
    assert PASSPORT in sent["headers"]["Cookie"] and XSRF in sent["headers"]["Cookie"]
    assert sent["headers"]["X-XSRF-TOKEN"] == XSRF


def test_client_follows_a_same_host_redirect_and_switches_a_303_post_to_get():
    def route(s, method, url, kw):
        if "first" in url:
            return Wire(303, location=f"https://{HOST}:80/bi/second?a=1")
        return Wire(body="done")

    client = logged_in(route)
    assert client.bi("POST", "/first", data={"x": "1"}).text == "done"
    first, second = client.session.sent
    assert second["method"] == "GET" and "data" not in second
    assert second["url"] == f"https://{HOST}/bi/second?a=1"  # configured origin, never the Location's port
    assert second["headers"]["X-XSRF-TOKEN"] == XSRF


def test_client_keeps_method_and_body_on_a_307():
    def route(s, method, url, kw):
        return Wire(307, location="/bi/second") if "first" in url else Wire(body="ok")

    client = logged_in(route)
    client.bi("POST", "/first", data={"x": "1"})
    assert client.session.sent[1]["method"] == "POST" and client.session.sent[1]["data"] == {"x": "1"}


@pytest.mark.parametrize(
    "location", ["https://evil.example.test/bi/x", "//evil.example.test/bi/x", f"https://{HOST}.evil.test/x"]
)
def test_client_refuses_a_redirect_to_another_host_without_contacting_it(location):
    client = logged_in(lambda s, m, u, kw: Wire(302, location=location))
    with pytest.raises(cco.Failure) as e:
        client.bi("GET", "/v1/disp")
    assert e.value.kind == cco.REFUSED and len(client.session.sent) == 1


def test_client_refuses_a_response_whose_final_url_is_another_host():
    client = logged_in(lambda s, m, u, kw: Wire(body="{}", url="https://evil.example.test/x"))
    with pytest.raises(cco.Failure) as e:
        client.bi("GET", "/v1/disp")
    assert e.value.kind == cco.REFUSED


def test_client_refuses_a_redirect_loop():
    client = logged_in(lambda s, m, u, kw: Wire(302, location="/bi/again"))
    with pytest.raises(cco.Failure) as e:
        client.bi("GET", "/v1/disp")
    assert e.value.kind == cco.REFUSED and len(client.session.sent) == cco.MAX_REDIRECTS + 1


def test_login_never_follows_a_redirect_and_never_echoes_the_key(monkeypatch):
    def route(s, method, url, kw):
        if method == "GET":
            s.cookies.set("XSRF-TOKEN", XSRF)
            return Wire(body="{}")
        return Wire(307, body=f"CAM-AAA-0190 bad key {KEY}", location="https://evil.example.test/")

    sessions = []
    monkeypatch.setattr(cco.requests, "Session", lambda: sessions.append(FakeSession(route)) or sessions[-1])
    client = cco.Client(f"https://{HOST}", "ns", KEY)
    with pytest.raises(cco.Stop) as e:
        client.login()
    put = sessions[0].sent[1]
    assert put["allow_redirects"] is False and put["headers"]["X-XSRF-TOKEN"] == XSRF
    assert KEY not in str(e.value) and len(sessions[0].sent) == 2


def wire_server(handler):
    """Route the real Client's requests to a fake_server-style handler; login sets the secret cookies."""

    def route(s, method, url, kw):
        path = url.split(HOST, 1)[1]
        if path == "/api/v1/session" and method == "GET":
            s.cookies.set("XSRF-TOKEN", XSRF)
            return Wire(body="{}")
        if path == "/api/v1/session" and method == "PUT":
            assert kw["json"]["parameters"][1]["value"] == KEY
            s.cookies.set("cam_passport", PASSPORT)
            return Wire(201, body="{}")
        if path == "/api/v1/session" and method == "DELETE":
            return Wire(204)
        cookie = kw["headers"]["Cookie"]
        assert PASSPORT in cookie and kw["headers"]["X-XSRF-TOKEN"] == XSRF
        kind, rest = ("api", path[len("/api/v1") :]) if path.startswith("/api/v1") else ("bi", path[len("/bi") :])
        r = handler(kind, rest)
        return Wire(r.status_code, r.content, r.headers.get("content-type", ""))

    return route


def test_main_end_to_end_writes_no_secret_anywhere(tmp_path, monkeypatch, capsys):
    page = viewer_page("complete")
    base = with_viewer(fake_server(), page)

    def handler(kind, path):
        if "b_action=rc" in path and "CSV" not in path:
            return Resp(body=b"%PDF-1.7 test", ctype="application/pdf")
        return base(kind, path)

    sessions = []
    monkeypatch.setattr(
        cco.requests, "Session", lambda: sessions.append(FakeSession(wire_server(handler))) or sessions[-1]
    )
    key_file = tmp_path / "key.txt"
    key_file.write_text(KEY)
    out = tmp_path / "out"
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--api-key-file", str(key_file), "--out", str(out)]
    code = cco.main(argv + ["--report-id", RID, "--formats", "pdf"])
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert code == 0, m
    assert (out / RID / "report.pdf").exists() and (out / RID / "parts" / "List1.json").exists()
    printed = capsys.readouterr()
    blobs = [p.read_bytes() for p in out.rglob("*") if p.is_file()] + [printed.out.encode(), printed.err.encode()]
    assert len(blobs) > 8
    for secret in (XSRF, PASSPORT, KEY):
        assert not [b for b in blobs if secret.encode() in b], secret
    assert sessions and sessions[-1].sent[-1]["method"] == "DELETE"


# ---------------------------------------------------------------- round three: a capture cut short (A, B, E, F)


def cut_short(how):
    """A handler that ends the run on the spec call: a Stop (viewer CV-0015 first), a crash or Ctrl+C."""
    base = with_viewer(fake_server(), Resp(body="<html>CV-0015 no capability not-a-real-key</html>", ctype="text/html"))

    def handler(kind, path):
        if "fields=specification" in path and how != "stop":
            raise RuntimeError("boom not-a-real-key") if how == "crash" else KeyboardInterrupt()
        return base(kind, path)

    return handler


@pytest.mark.parametrize(
    ("how", "code", "status"), [("stop", 2, "stopped"), ("crash", 3, "crashed"), ("interrupt", 130, "interrupted")]
)
def test_a_run_cut_short_keeps_the_old_capture_and_the_new_one_as_incoming(
    tmp_path, monkeypatch, capsys, how, code, status
):
    (tmp_path / RID).mkdir()
    (tmp_path / RID / "report.pdf").write_text("old pdf")
    formats = "pdf" if how == "stop" else "none"
    got, m = run_main(tmp_path, monkeypatch, cut_short(how), formats=formats, ids=["--report-id", RID])
    assert got == code and m["status"] == status and m["finished_at"]
    assert sorted(p.name for p in (tmp_path / RID).iterdir()) == ["report.pdf"]
    assert (tmp_path / RID / "report.pdf").read_text() == "old pdf"
    legs = m["reports"][0]["legs"]
    obj = next(leg for leg in legs if leg["leg"] == "object")
    assert obj["path"] == f"{RID}.incoming/object.json" and (tmp_path / obj["path"]).exists()
    assert legs[-1]["leg"] == "store" and legs[-1]["status"] == "failed"
    assert all(leg["status"] != "pending" for leg in m["discovery"] + legs)
    printed = capsys.readouterr()
    for blob in (printed.out, printed.err, (tmp_path / "manifest.json").read_text(encoding="utf-8")):
        assert "not-a-real-key" not in blob


def test_a_stop_echoing_the_key_never_prints_or_writes_it(tmp_path, monkeypatch, capsys):
    code, m = run_main(tmp_path, monkeypatch, cut_short("stop"), formats="pdf", ids=["--report-id", RID])
    err = capsys.readouterr().err
    assert code == 2 and "STOPPED" in err and "CV-0015" in err and "<key>" in err
    assert "not-a-real-key" not in err and "not-a-real-key" not in m["stop_reason"]


@pytest.mark.parametrize(
    ("exc", "code", "cls"), [(RuntimeError, 3, "crashed"), (KeyboardInterrupt, 130, "interrupted")]
)
def test_a_run_cut_short_during_discovery_leaves_no_pending_leg(tmp_path, monkeypatch, exc, code, cls):
    def handler(kind, path):
        raise exc()

    got, m = run_main(tmp_path, monkeypatch, handler)
    assert got == code and m["finished_at"]
    assert [(leg["status"], leg["class"]) for leg in m["discovery"]] == [("failed", cls)]


def test_a_crash_records_every_report_it_did_not_reach(tmp_path, monkeypatch):
    base = fake_server()

    def handler(kind, path):
        if path.startswith(f"/content/{FID}/items"):
            return Resp(body=json.dumps({"content": [kid(RID, "report"), kid(SID, "report")]}))
        if f"/content/{RID}?fields=specification" in path:
            raise RuntimeError("boom")
        return base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler)
    assert code == 3 and m["reports"][1] == {"id": SID, "origin": f"folder:{FID}", "status": "not_attempted"}


# ---------------------------------------------------------------- round three: renders (C) and viz


@pytest.mark.parametrize(
    "body",
    [
        "<div/>",
        '<div class="x"></div>',
        "<div>Your session has expired, please log in again</div>",
        "<div><table></table></div>",
        "<div><span> </span>&nbsp;</div>",
        '<div><img src="/samples/images/logo.png"/></div>',
    ],
)
def test_render_refuses_a_fragment_without_content(tmp_path, body):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/html"))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    assert rec["status"] == "refused" and not (tmp_path / "parts").exists()


@pytest.mark.parametrize("body", ["<div/>", '<div class="x"></div>'])
def test_singleton_render_only_the_literal_empty_div_is_unsupported(tmp_path, body):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    assert run_part(cap, tmp_path, cap.get_part_render, SINGLETON)["status"] == "refused"


VIZ = {"name": "Clustered bar1", "label": "Clustered bar1", "tag": "viz", "refQuery": "qLocation", "chart": True}
# measured on interactiveReport Customer claim analysis: 200, text/xml, 154 bytes
VIZ_EMPTY = (
    '<div><div><img id="Clustered__bar1" src="" style="font-family:IBM Plex Sans; font-size:14.000000px; '
    'color:#343334; text-align:left; " alt=""/></div></div>'
)


ALL_VIEWERS = ["report.pdf", "report.csv", "report.xlsx"]


@pytest.mark.parametrize(
    ("first", "viewer_ok", "where", "never"),
    [
        (True, ALL_VIEWERS, "first data container", "no output captured"),
        (False, ALL_VIEWERS, "its values are in report.pdf and report.xlsx, captured on this run", "first data"),
        # L3: the viewer outputs were not captured (an Active Report), so no reason may name them
        (True, [], "no output captured on this run holds its values", "report."),
        (True, ["report.pdf", "report.xlsx"], "its values are in report.pdf and report.xlsx", "report.csv"),
        (False, ["report.xlsx"], "its values are in report.xlsx, captured", "report.pdf"),
    ],
)
def test_viz_data_is_never_requested_and_names_its_query(tmp_path, first, viewer_ok, where, never):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=json.dumps(TABLE_DATA)))
    rec = run_part(cap, tmp_path, cap.get_part_data, {**VIZ, "first": first, "viewer_ok": viewer_ok})
    assert rec["status"] == "unsupported" and "qLocation" in rec["reason"] and where in rec["reason"]
    assert never not in rec["reason"]
    assert cap.client.calls == [] and rec["retries"] == 0 and not (tmp_path / "parts").exists()


def test_viz_data_wording_follows_the_spec_order_not_the_parts_filter(tmp_path):
    viz = '<viz name="V1"><vizDataSets><vizDataSet refQuery="Query1"/></vizDataSets></viz>'
    spec = spec_with(f'{viz}<list name="List1" refQuery="Query1"/><viz name="V2" refQuery="Query1"/>')
    cap, _ = capturer(tmp_path, all_viewers(fake_server(spec=spec)), formats=("pdf", "csv", "xlsx"), parts="V2,V1")
    entry = cap.capture_report(RID, "report-id", [])
    reason = {leg["leg"]: leg.get("reason", "") for leg in entry["legs"]}
    status = {leg["leg"]: leg["status"] for leg in entry["legs"]}
    assert [status[f] for f in ("pdf", "csv", "xlsx")] == ["ok"] * 3
    assert "first data container" in reason["part:V1:data"]
    assert "first data container" not in reason["part:V2:data"]
    assert "its values are in report.pdf and report.xlsx" in reason["part:V2:data"]


def test_viz_render_of_the_measured_empty_image_is_unsupported(tmp_path):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=VIZ_EMPTY, ctype="text/xml; charset=UTF-8"))
    rec = run_part(cap, tmp_path, cap.get_part_render, {**VIZ, "viewer_ok": ["report.pdf"]})
    assert rec["status"] == "unsupported" and "is in report.pdf" in rec["reason"] and not (tmp_path / "parts").exists()
    # L3: without a captured report.pdf the reason must not send the reader to it
    rec = run_part(cap, tmp_path, cap.get_part_render, {**VIZ, "viewer_ok": ["report.csv"]})
    assert rec["status"] == "unsupported" and "report.pdf" not in rec["reason"]
    assert "no output captured on this run holds its render" in rec["reason"]


def test_viz_render_with_a_real_chart_image_is_captured(tmp_path):
    frag = f'<div><img src="https://{HOST}/bi/v1/disp?SM=query&amp;n=1"/></div>'

    def handler(kind, path):
        if "HTMLFragment" in path:
            return Resp(body=frag, ctype="text/xml")
        return Resp(body=b"\x89PNG\r\n\x1a\n", ctype="image/png")

    cap, _ = capturer(tmp_path, handler)
    rec = run_part(cap, tmp_path, cap.get_part_render, VIZ)
    assert rec["status"] == "ok" and len(rec["images"]) == 1


@pytest.mark.parametrize(
    ("p", "body"),
    [
        (VIZ, '<div><img src=""/>Total</div>'),
        (VIZ, '<div><img src=""/><img src=""/></div>'),
        (VIZ, '<div><img src=""/><table><tr><td></td></tr></table></div>'),
        ({**VIZ, "tag": "vizControl"}, VIZ_EMPTY),
        (part("Chart1", True), VIZ_EMPTY),
    ],
)
def test_other_empty_image_fragments_are_refused(tmp_path, p, body):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    assert run_part(cap, tmp_path, cap.get_part_render, p)["status"] == "refused"


# ---------------------------------------------------------------- round three: chart images and error bodies


def chart_with_image(img):
    frag = f'<div><img src="https://{HOST}/bi/v1/disp?SM=query&amp;n=1"/></div>'
    return lambda kind, path: Resp(body=frag, ctype="text/xml") if "HTMLFragment" in path else img


@pytest.mark.parametrize(
    "img",
    [
        Resp(body=b"", ctype="image/png"),
        Resp(body=b"", ctype="image/gif"),
        Resp(body=b"<html>login</html>", ctype="text/html"),
        Resp(body=b"GIF89a", ctype="image/png"),
        Resp(status=404, body=b"", ctype="text/html"),
    ],
)
def test_a_bad_chart_image_refuses_the_render_and_writes_nothing(tmp_path, img):
    cap, _ = capturer(tmp_path, chart_with_image(img))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("Chart1", True))
    assert rec["status"] in ("refused", "failed") and "images" not in rec and not (tmp_path / "parts").exists()


def test_a_bad_second_chart_image_writes_no_file_and_no_path(tmp_path):
    frag = (
        f'<div><img src="https://{HOST}/bi/v1/disp?SM=query&amp;n=1"/><img src="/bi/v1/disp?SM=query&amp;n=2"/></div>'
    )

    def handler(kind, path):
        if "HTMLFragment" in path:
            return Resp(body=frag, ctype="text/xml")
        return Resp(body=b"\x89PNG\r\n\x1a\n" if path.endswith("n=1") else b"", ctype="image/png")

    cap, _ = capturer(tmp_path, handler)
    rec = run_part(cap, tmp_path, cap.get_part_render, part("Chart1", True))
    assert rec["status"] == "refused" and "path" not in rec and not (tmp_path / "parts").exists()


def test_a_decoration_image_is_not_a_chart(tmp_path):
    # a static image served from /bi, but not a rendered chart (those are /bi/v1/disp?SM=query)
    body = '<div><img src="/bi/samples/images/logo.png"/>Bank</div>'
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("Chart1", True))
    assert rec["status"] == "refused" and cap.client.calls == [cap.client.calls[0]]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ('<?xml version="1.0"?><error>RDS failed</error>', "refused"),
        ('<?xml version="1.0"?><error><table><tr><td>RDS failed</td></tr></table></error>', "refused"),
        ("<rds:error><rds:message>RDS-ERR-1000 failed</rds:message></rds:error>", "failed"),
    ],
)
def test_a_render_that_is_an_error_document_is_never_credited(tmp_path, body, status):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    assert run_part(cap, tmp_path, cap.get_part_render, part("List1"))["status"] == status


@pytest.mark.parametrize(
    "resp",
    [
        Resp(404, "not found"),
        Resp(500, "<html>error</html>", "text/html"),
        Resp(302, ""),
        Resp(200, "<x><rds:message>RDS-ERR-1000 boom</rds:message></x>", "text/xml"),
        Resp(200, "<rds:error/>", "text/xml"),
    ],
)
def test_ok_refuses_every_error_body(resp):
    with pytest.raises(cco.Failure):
        cco.ok(resp)


def test_client_returns_a_redirect_without_location_after_one_request_and_ok_refuses_it():
    client = logged_in(lambda s, m, u, kw: Wire(302, body=""))
    resp = client.bi("GET", "/v1/disp")
    assert len(client.session.sent) == 1 and resp.status_code == 302
    with pytest.raises(cco.Failure):
        cco.ok(resp)


# ---------------------------------------------------------------- round three: main-level absence


@pytest.mark.parametrize("answer", [Resp(status=404), Resp(body="{}"), Resp(body='{"data": [{}]}')])
def test_main_without_a_spec_exits_1(tmp_path, monkeypatch, answer):
    base = fake_server()

    def handler(kind, path):
        return answer if "fields=specification" in path else base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler)
    assert code == 1 and m["status"] == "partial"
    assert next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "spec")["status"] in ("failed", "refused")


def test_main_with_an_unknown_parts_name_exits_1(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, fake_server(), parts="List1,Nope")
    legs = {leg["leg"]: leg for leg in m["reports"][0]["legs"]}
    assert code == 1 and legs["part:Nope"]["status"] == "failed" and legs["part:List1:data"]["status"] == "ok"


def test_main_module_lineage_without_a_module_id_exits_1(tmp_path, monkeypatch):
    base = fake_server()

    def handler(kind, path):
        return Resp(body=json.dumps({"modules": []})) if path == "/modules" else base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler)
    lineage = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "lineage")
    assert code == 1 and lineage["status"] == "failed" and lineage["class"] == cco.NOT_FOUND


def test_main_unparseable_spec_is_a_failed_leg_not_a_crash(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, fake_server(spec="<report"))
    legs = {leg["leg"]: leg for leg in m["reports"][0]["legs"]}
    assert code == 1 and m["status"] == "partial"
    assert legs["parts"]["status"] == "failed" and legs["lineage"]["status"] == "failed"
    assert legs["lineage"]["class"] == cco.REPORT_ERROR


# ---------------------------------------------------------------- round three: ids deduplicated (D)


@pytest.mark.parametrize("second", [RID, RID.lower()])
def test_main_captures_a_repeated_report_id_once(tmp_path, monkeypatch, second):
    code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID, "--report-id", second])
    assert code == 0 and [r["id"] for r in m["reports"]] == [RID]


def test_discover_lists_a_folder_once_in_any_letter_case(tmp_path):
    cap, _ = capturer(tmp_path, listing_server(content={FID.lower(): [kid(RID, "report")], FID: []}))
    found, legs, _ = cap.discover([FID.lower(), FID], [RID.lower()])
    assert found == [(RID.lower(), "report-id")] and len(legs) == 1


def test_main_lists_a_repeated_folder_once_and_captures_a_repeated_child_once(tmp_path, monkeypatch):
    base = fake_server()

    def handler(kind, path):
        if path.startswith(f"/content/{FID}/items"):
            return Resp(body=json.dumps({"content": [kid(RID, "report"), kid(RID.lower(), "report")]}))
        return base(kind, path)

    ids = ["--folder-id", FID, "--folder-id", FID.lower(), "--report-id", RID]
    code, m = run_main(tmp_path, monkeypatch, handler, ids=ids)
    assert code == 0 and [r["id"] for r in m["reports"]] == [RID] and len(m["discovery"]) == 1


# ---------------------------------------------------------------- round three: shortcuts


SC, TID = "i" + "6" * 32, "i" + "7" * 32
PATH_URL = "/bi/v1/path?path=.public_folders%2FSamples%2FHospitals"


def shortcut_server(target=None, path_url=PATH_URL, resolved=None, extra=()):
    """FID holds one shortcut (and extra children); the shortcut resolves in two calls as measured."""
    srv = listing_server(content={FID: [kid(SC, "shortcut", "Short"), *extra]})
    if target is None:
        meta = {"links": {"path": {"url": path_url}, "self": {"url": "/x"}}}
        target = {"target": [{"searchPath": "/content/folder[@name='S']", "id": "", "type": "nil", "_meta": meta}]}
    if resolved is None:
        resolved = {"data": [{"id": TID, "type": "report", "defaultName": "Hospitals"}]}

    def handler(kind, path):
        if kind == "api" and path == f"/content/{SC}?fields=target":
            return target if isinstance(target, Resp) else Resp(body=json.dumps(target))
        if kind == "bi" and path.startswith("/v1/path?"):
            return resolved if isinstance(resolved, Resp) else Resp(body=json.dumps(resolved))
        return srv(kind, path)

    return handler


@pytest.mark.parametrize("ttype", ["report", "interactiveReport"])
def test_a_shortcut_to_a_report_is_captured_with_its_origin(tmp_path, ttype):
    resolved = {"data": [{"id": TID, "type": ttype, "defaultName": "Hospitals"}]}
    cap, _ = capturer(tmp_path, shortcut_server(resolved=resolved))
    found, legs, skipped = cap.discover([FID], [])
    assert found == [(TID, f"folder:{FID}/shortcut:{SC}")] and skipped == []
    assert ("bi", "/v1/path?path=.public_folders%2FSamples%2FHospitals") in cap.client.calls


def test_a_shortcut_target_already_found_is_captured_once(tmp_path):
    cap, _ = capturer(tmp_path, shortcut_server(extra=[kid(TID.lower(), "report")]))
    found, _, _ = cap.discover([FID], [TID])
    assert found == [(TID, "report-id")]


def test_a_shortcut_to_a_module_is_named_with_its_target_type(tmp_path):
    resolved = {"data": [{"id": MID, "type": "module", "defaultName": "Mod"}]}
    cap, _ = capturer(tmp_path, shortcut_server(resolved=resolved))
    found, _, [skip] = cap.discover([FID], [])
    assert found == [] and skip["id"] == SC and skip["target_type"] == "module" and skip["target_id"] == MID
    assert "module" in skip["reason"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.test/bi/v1/path?path=x",
        "//evil.example.test/bi/v1/path?path=x",
        f"https://{HOST}/bi/v1/path?path=x",
        "/bi/v1/other?path=x",
        "/x/v1/path?path=a",
        "https:/bi/v1/path?path=x",
        "/bi/v1/path",
        "/bi/v1/path?x=1",
        "/bi/v1/path?path=",
        "/bi/v1/path?path=a\\b",
        "/bi/v1/path?path=x#frag",
        "\\\\evil\\bi\\v1\\path?path=x",
        None,
        42,
    ],
)
def test_a_shortcut_path_url_that_is_not_relative_bi_path_is_never_followed(tmp_path, url):
    cap, _ = capturer(tmp_path, shortcut_server(path_url=url))
    found, _, [skip] = cap.discover([FID], [])
    assert found == [] and skip["id"] == SC and "not resolved" in skip["reason"]
    assert not [c for c in cap.client.calls if c[1].startswith("/v1/path")]


@pytest.mark.parametrize(
    ("target", "resolved"),
    [
        (Resp(400, "CM-REQ-4012 bad"), None),
        (Resp(body="<html/>", ctype="text/html"), None),
        ({"target": []}, None),
        ({"target": [{}, {}]}, None),
        ("twice", None),
        (None, Resp(400, "bad")),
        (None, {"data": []}),
        (None, {"data": [{"id": TID, "type": "report"}, {"id": SID, "type": "report"}]}),
        (None, {"data": [{"id": "", "type": "report"}]}),
    ],
)
def test_a_shortcut_that_does_not_resolve_is_a_named_skip(tmp_path, target, resolved):
    if target == "twice":
        one = json.loads(shortcut_server()("api", f"/content/{SC}?fields=target").content)["target"][0]
        target = {"target": [one, one]}
    cap, _ = capturer(tmp_path, shortcut_server(target=target, resolved=resolved))
    found, legs, [skip] = cap.discover([FID], [])
    assert found == [] and skip["id"] == SC and skip["reason"] and legs[0]["status"] == "ok"
    assert legs[1]["leg"] == f"resolve:{SC}" and legs[1]["status"] in ("failed", "refused")


# ---------------------------------------------------------------- round three: the deadline is a wall clock


@contextlib.contextmanager
def loopback(do_get, protocol="HTTP/1.0"):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = protocol

        def do_GET(self):  # noqa: N802 - the http.server hook name
            try:
                do_get(self)
            except OSError:
                pass  # the client gave up mid-body

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def session():
    s = requests.Session()
    s.trust_env = False  # never a proxy for loopback
    return s


def test_fetch_caps_a_dripping_body_at_the_deadline():
    def drip(h):
        h.send_response(200)
        h.send_header("Content-Length", "30")
        h.end_headers()
        for _ in range(30):
            h.wfile.write(b"x")
            h.wfile.flush()
            time.sleep(0.2)

    with loopback(drip) as url:
        start = time.monotonic()
        with pytest.raises(cco.Failure) as e:
            cco.fetch(session(), "GET", url, time.monotonic() + 1.0)
        assert e.value.kind == cco.TRANSIENT and time.monotonic() - start < 1.8


# HTTP/1.0 closes, so http.client drops the connection's socket; HTTP/1.1 keeps it on the pooled connection
@pytest.mark.parametrize("protocol", ["HTTP/1.0", "HTTP/1.1"])
def test_fetch_cuts_a_stalled_body_at_the_deadline_not_at_the_first_read_timeout(protocol):
    def stall(h):
        time.sleep(0.6)
        h.send_response(200)
        h.send_header("Content-Length", "30")
        h.end_headers()
        h.wfile.write(b"x")
        h.wfile.flush()
        time.sleep(3)

    with loopback(stall, protocol) as url:
        start = time.monotonic()
        with pytest.raises(cco.Failure) as e:
            cco.fetch(session(), "GET", url, time.monotonic() + 1.0)
        assert e.value.kind == cco.TRANSIENT and time.monotonic() - start < 1.4


@pytest.mark.parametrize("protocol", ["HTTP/1.0", "HTTP/1.1"])
def test_fetch_reads_a_whole_body_within_the_deadline(protocol):
    def answer(h):
        h.send_response(200)
        h.send_header("Content-Length", "200000")
        h.end_headers()
        h.wfile.write(b"y" * 200000)

    with loopback(answer, protocol) as url:
        resp = cco.fetch(session(), "GET", url, time.monotonic() + 5.0)
    assert resp.status_code == 200 and resp.content == b"y" * 200000


def test_fetch_decodes_a_chunked_gzip_body():
    body = gzip.compress(b'{"dataSet": "z"}' * 5000)

    def answer(h):
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.send_header("Content-Encoding", "gzip")
        h.end_headers()
        for i in range(0, len(body), 1000):
            piece = body[i : i + 1000]
            h.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        h.wfile.write(b"0\r\n\r\n")

    with loopback(answer, "HTTP/1.1") as url:
        resp = cco.fetch(session(), "GET", url, time.monotonic() + 5.0)
    assert resp.content == b'{"dataSet": "z"}' * 5000


def test_redirect_hops_share_one_deadline():
    def hop(h):
        time.sleep(0.4)
        h.send_response(302)
        h.send_header("Location", "/bi/next")
        h.send_header("Content-Length", "0")
        h.end_headers()

    with loopback(hop) as url:
        client = cco.Client(url, "ns", KEY, timeout=1.0)
        client.session = session()
        start = time.monotonic()
        with pytest.raises(cco.Failure) as e:
            client.bi("GET", "/first")
        assert e.value.kind == cco.TRANSIENT and time.monotonic() - start < 1.5


def test_each_chart_image_adds_a_full_request_to_the_worst_case(tmp_path, monkeypatch):
    t = 100.0
    assert cco.worst_case_leg_s(t, 5) - cco.worst_case_leg_s(t, 4) == (cco.MAX_ATTEMPTS + 1) * t
    _, m = run_main(tmp_path, monkeypatch, fake_server())
    assert m["deadline"]["per_chart_image_s"] == (cco.MAX_ATTEMPTS + 1) * cco.DEFAULT_TIMEOUT_S


def test_fetch_checks_the_deadline_between_chunks_without_a_socket() -> None:
    # no socket to time out: only the check between chunks can stop a body that keeps coming after the deadline
    class SlowRaw:
        left = 8  # a finite body, so a fetch without the check ends (late) instead of hanging the test

        def read1(self, n: int, decode_content: bool = True) -> bytes:
            time.sleep(0.15)
            self.left -= 1
            return b"x" if self.left >= 0 else b""

    class Resp:
        raw = SlowRaw()
        closed = False

        def close(self) -> None:
            self.closed = True

    resp = Resp()

    class Session:
        def request(self, *a: object, **kw: object) -> Resp:
            return resp

    start = time.monotonic()
    with pytest.raises(cco.Failure) as e:
        cco.fetch(Session(), "GET", "https://h/x", time.monotonic() + 0.5)  # type: ignore[arg-type]
    assert e.value.kind == cco.TRANSIENT and "deadline" in str(e.value)
    assert time.monotonic() - start < 1.0 and resp.closed


# ---------------------------------------------------------------- round four: Active Reports (L1, L2, L3)

# measured live on Customer claim analysis (interactiveReport, spec root application="true"); tokens replaced
AR_SCRIPT = (
    "var AR_Parameters = {};AR_Parameters['generatedOutputPath'] = "
    "'/bi/v1/disp/rc/2026-10-30-09.16.51.889057:12172/tok/tok/tok/content';"
    "var sURL = '../irot/irot.htm?v=4063517421';window.open(sURL, '', '');"
)


def complete_page(script):
    state = '"m_sStatus": "complete", "m_sConversation": "C", "m_sTracking": "T", "m_sActionState": "A"'
    return Resp(body=f"<script>oCV.initViewer({{ {state} }});{script}</script>", ctype="text/html")


@pytest.mark.parametrize("fmt", ["pdf", "csv", "xlsx"])
@pytest.mark.parametrize(
    "script",
    [
        AR_SCRIPT,
        "var AR_Parameters = {};AR_Parameters['generatedOutputPath'] = '/x';",
        "window.open('../irot/irot.htm');",
    ],
)
def test_an_active_report_viewer_page_is_unsupported_with_the_measured_reason(tmp_path, fmt, script):
    cap, _ = capturer(tmp_path, lambda kind, path: complete_page(script))
    rec = cap.new_leg(fmt)
    cap.run_leg(rec, lambda: cap.get_viewer(RID, fmt, tmp_path, rec))
    assert rec["status"] == "unsupported" and rec["class"] == cco.UNSUPPORTED
    assert rec["reason"] == cco.ACTIVE_REPORT_REASON and rec["active_report"] is True
    assert "irot.htm" in rec["reason"] and "0 bytes" in rec["reason"] and "RDS-ERR-1022" in rec["reason"]
    # one viewer request, never the generatedOutputPath (measured: 200 with 0 bytes) and no file
    assert len(cap.client.calls) == 1 and "path" not in rec and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("script", ["", "var sURL = '/bi/v1/disp?b_action=other';", "AR_Parameters = {};"])
def test_a_complete_viewer_page_without_an_output_url_or_active_report_marker_stays_refused(tmp_path, script):
    # V5: the boundary L1 must not move; a genuinely broken page is still refused, never credited
    cap, _ = capturer(tmp_path, lambda kind, path: complete_page(script))
    rec = cap.new_leg("pdf")
    cap.run_leg(rec, lambda: cap.get_viewer(RID, "pdf", tmp_path, rec))
    assert rec["status"] == "refused" and rec["reason"] == "viewer page holds no output URL"
    assert "active_report" not in rec and "path" not in rec and list(tmp_path.iterdir()) == []


AR_SPEC = f"""<report {NS} application="true"><queries><query name="qClaims"/></queries>
<layouts><layout><reportPages><page name="P"><pageBody><contents>
  <appDataDropDownList name="Data drop-down list1" refQuery="qClaims"/>
  <appDataListBox name="Data list box1" refQuery="qClaims"/>
  <appDataDeck name="Data deck1" refQuery="qClaims"><appDataCard/></appDataDeck>
  <appList name="Data list1"><appListDataSet refQuery="qClaims"/></appList>
  <viz name="Clustered bar1"><vizDataSets><vizDataSet refQuery="qClaims"/></vizDataSets></viz>
  <list refQuery="qClaims"/>
</contents></pageBody></page></reportPages></layout></layouts></report>"""


def active_report_server():
    base = fake_server(spec=AR_SPEC)

    def handler(kind, path):
        if "b_action=cognosViewer" in path:
            return complete_page(AR_SCRIPT)
        return base(kind, path)

    return handler


def test_main_active_report_is_unsupported_not_refused_and_names_no_missing_file(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, active_report_server(), formats="pdf,csv,xlsx", ids=["--report-id", RID])
    assert code == 0 and m["status"] == "ok", m
    [entry] = m["reports"]
    assert "application" in entry["active_report"]
    legs = {leg["leg"]: leg for leg in entry["legs"]}
    for fmt in ("pdf", "csv", "xlsx"):
        assert legs[fmt]["status"] == "unsupported" and legs[fmt]["reason"] == cco.ACTIVE_REPORT_REASON
    # L2: the three interaction controls are named unsupported, never requested; appList is attempted
    for name in ("Data drop-down list1", "Data list box1", "Data deck1"):
        for kind in ("data", "render"):
            leg = legs[f"part:{name}:{kind}"]
            assert leg["status"] == "unsupported" and leg["class"] == cco.UNSUPPORTED and "qClaims" in leg["reason"]
    assert "measured on 12.1.2" in legs["part:Data drop-down list1:data"]["reason"]
    for name in ("Data list box1", "Data deck1"):
        reason = legs[f"part:{name}:data"]["reason"]
        assert "inferred" in reason and "not measured" in reason and "measured on 12.1.2" not in reason
    assert legs["part:Data list1:data"]["status"] == "ok" and legs["part:Data list1:render"]["status"] == "ok"
    # L3: no reason names a viewer output this run did not capture
    for leg in entry["legs"]:
        for name in ("report.pdf", "report.xlsx", "report.csv"):
            assert name not in leg.get("reason", ""), (leg["leg"], leg.get("reason"))
    viz = legs["part:Clustered bar1:data"]["reason"]
    assert "no output captured on this run holds its values" in viz and "first data container" not in viz
    unnamed = [leg["reason"] for leg in entry["legs"] if leg.get("reason", "").startswith("unnamed list")]
    assert len(unnamed) == 2 and all("no output captured on this run holds its values" in r for r in unnamed)
    assert {u["leg"] for u in m["render_unestablished"]} >= {"pdf", "part:Data drop-down list1:render"}


def test_active_report_controls_are_never_requested(tmp_path):
    cap, _ = capturer(tmp_path, active_report_server())
    cap.capture_report(RID, "report-id", [])
    rds = [p for _, p in cap.client.calls if "/rds/" in p]
    assert rds and not [p for p in rds if any(n in p for n in ("drop-down", "list%20box", "deck"))]
    assert [p for p in rds if "Data%20list1" in p]


def test_an_app_control_reason_names_the_viewer_output_only_when_it_was_captured():
    p = {"tag": "appDataListBox", "refQuery": "q", "viewer_ok": ["report.pdf", "report.xlsx"]}
    assert "its values are in report.pdf and report.xlsx" in cco.app_control_reason(p)
    assert "report." not in cco.app_control_reason({**p, "viewer_ok": []})


def test_is_active_report_spec_reads_only_the_root_application_attribute():
    assert cco.is_active_report_spec(AR_SPEC)
    assert not cco.is_active_report_spec(SPEC)
    assert not cco.is_active_report_spec(f'<report {NS} application="false"/>')
    assert not cco.is_active_report_spec("<report")


def test_a_singleton_or_unnamed_reason_names_only_captured_viewer_outputs(tmp_path):
    single = {**SINGLETON, "viewer_ok": []}
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body='{"dataSet":null}'))
    rec = run_part(cap, tmp_path, cap.get_part_data, single)
    assert rec["status"] == "unsupported" and "report." not in rec["reason"]
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body="<div></div>", ctype="text/xml"))
    rec = run_part(cap, tmp_path, cap.get_part_render, {**single, "viewer_ok": ["report.pdf"]})
    assert rec["status"] == "unsupported" and "its render is in report.pdf" in rec["reason"]


# ---------------------------------------------------------------- round four: the store (N1, N2, N3, N5)


def test_a_rerun_after_an_interrupt_removes_the_stale_incoming(tmp_path, monkeypatch):
    got, _ = run_main(tmp_path, monkeypatch, cut_short("interrupt"), ids=["--report-id", RID])
    assert got == 130 and (tmp_path / f"{RID}.incoming").exists()
    code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID])
    assert code == 0 and m["status"] == "ok"
    assert not (tmp_path / f"{RID}.incoming").exists() and (tmp_path / RID / "object.json").exists()
    assert "stale_incoming" not in m["reports"][0] and m["other_captures"] == []


def test_a_stale_incoming_that_cannot_be_removed_is_named(tmp_path, monkeypatch):
    (tmp_path / f"{RID}.incoming").mkdir()
    real = cco.shutil.rmtree

    def rmtree(path, *a, **k):
        if Path(path).name != f"{RID}.incoming":
            real(path, *a, **k)

    monkeypatch.setattr(cco.shutil, "rmtree", rmtree)
    cap, _ = capturer(tmp_path, report_server())
    entry = cap.capture_report(RID, "report-id", [])
    assert f"{RID}.incoming" in entry["stale_incoming"]


def two_report_server(ids):
    base = fake_server()

    def handler(kind, path):
        if path.startswith(f"/content/{FID}/items"):
            return Resp(body=json.dumps({"content": [kid(i, "report") for i in ids]}))
        return base(kind, path.replace(SID, RID))  # SID is served as a second copy of the RID report

    return handler


def test_a_report_folder_this_run_did_not_touch_is_named_in_other_captures(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    assert code == 0 and m["other_captures"] == []
    first = json.loads((tmp_path / SID / "capture.json").read_text(encoding="utf-8"))
    assert first["id"] == SID and first["origin"] == f"folder:{FID}" and first["captured_at"]
    (tmp_path / "modules").mkdir()
    (tmp_path / XID).mkdir()  # a folder some other tool, or an older version, wrote: no capture.json
    (tmp_path / "notes").mkdir()
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]))
    assert code == 0 and [r["id"] for r in m["reports"]] == [RID]
    others = {o["folder"]: o for o in m["other_captures"]}
    assert set(others) == {SID, XID, "modules"}  # never deleted, never the report captured now, never "notes"
    assert others[SID]["captured_at"] == first["captured_at"] and (tmp_path / SID).exists()
    assert others[XID]["captured_at"] is None and "capture.json" in others[XID]["note"]
    assert "module.json" in others["modules"]["note"]


def test_a_report_not_attempted_is_named_as_another_capture(tmp_path, monkeypatch):
    (tmp_path / SID).mkdir()
    base = two_report_server([RID, SID])

    def handler(kind, path):
        if f"/content/{RID}?fields=specification" in path:
            raise RuntimeError("boom")
        return base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler)
    assert code == 3 and [o["folder"] for o in m["other_captures"]] == [SID]


def every_staging_rename_fails(monkeypatch):
    real = Path.rename

    def rename(self, target):
        if self.parent.name == ".staging":
            raise PermissionError(13, "in use")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def test_when_the_staged_capture_cannot_be_moved_every_written_leg_is_failed(tmp_path, monkeypatch):
    every_staging_rename_fails(monkeypatch)
    cap, sleeps = capturer(tmp_path, report_server())
    entry = cap.capture_report(RID, "report-id", [])
    by = {leg["leg"]: leg for leg in entry["legs"]}
    store = entry["legs"][-1]
    assert store["leg"] == "store" and store["class"] == cco.STORE_ERROR and "lost" in store["reason"]
    for name in ("object", "spec", "lineage", "part:List1:data", "part:List1:render", "part:Chart1:render"):
        assert by[name]["status"] == "failed" and by[name]["class"] == cco.STORE_ERROR, name
        assert by[name]["path"] is None and all(i["path"] is None for i in by[name].get("images", []))
    assert by["lineage"]["module"]["path"] is None
    assert not [leg for leg in entry["legs"] if leg["status"] == "ok"]
    renders = {u["leg"] for u in cco.render_unestablished([entry])}
    assert {"part:List1:render", "part:Chart1:render"} <= renders
    assert {"List1"} <= {d["part"] for d in cco.data_unestablished([entry])}
    # each rename was retried with a backoff before it was given up
    assert sleeps.count(cco.RENAME_BACKOFF_S) == 2 and sleeps.count(2 * cco.RENAME_BACKOFF_S) == 2


def test_main_exits_1_when_a_capture_is_lost(tmp_path, monkeypatch):
    every_staging_rename_fails(monkeypatch)
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID])
    assert code == 1 and m["status"] == "partial"
    assert "part:List1:render" in {u["leg"] for u in m["render_unestablished"]}


@pytest.mark.parametrize("fails", [1, 2])
def test_a_rename_held_briefly_is_retried_and_the_capture_swapped_in(tmp_path, monkeypatch, fails):
    real, count = Path.rename, {"n": 0}

    def rename(self, target):
        if self.parent.name == ".staging" and count["n"] < fails:
            count["n"] += 1
            raise PermissionError(13, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    cap, sleeps = capturer(tmp_path, report_server())
    entry = cap.capture_report(RID, "report-id", [])
    assert all(leg["status"] != "failed" for leg in entry["legs"]) and (tmp_path / RID / "object.json").exists()
    assert sleeps == [cco.RENAME_BACKOFF_S * (i + 1) for i in range(fails)]


def test_a_rename_that_keeps_failing_gives_up_after_its_attempts(tmp_path):
    calls = []

    class Locked:
        def rename(self, target):
            calls.append(target)
            raise PermissionError(13, "in use")

    cap, sleeps = capturer(tmp_path, report_server())
    with pytest.raises(PermissionError):
        cap.rename(Locked(), tmp_path / "x")
    assert len(calls) == cco.RENAME_ATTEMPTS == 3 and len(sleeps) == 2


@pytest.mark.parametrize(("how", "cls"), [("stop", "stopped"), ("crash", "crashed"), ("interrupt", "interrupted")])
def test_the_store_leg_of_a_capture_cut_short_names_what_cut_it(tmp_path, monkeypatch, how, cls):
    formats = "pdf" if how == "stop" else "none"
    _, m = run_main(tmp_path, monkeypatch, cut_short(how), formats=formats, ids=["--report-id", RID])
    store = m["reports"][0]["legs"][-1]
    assert store["leg"] == "store" and store["class"] == cls


# ---------------------------------------------------------------- round four: discovery (N4, N7, G4, S6)


def test_a_shortcut_lookup_that_keeps_failing_is_a_failed_leg_and_exit_1(tmp_path, monkeypatch):
    monkeypatch.setattr(cco, "BACKOFF_S", (0.0, 0.0))
    srv = shortcut_server(target=Resp(503, "busy"))
    code, m = run_main(tmp_path, monkeypatch, srv)
    resolve = [leg for leg in m["discovery"] if leg["leg"] == f"resolve:{SC}"]
    assert code == 1 and m["status"] == "no_reports"
    assert resolve[0]["status"] == "failed" and resolve[0]["class"] == cco.TRANSIENT
    assert resolve[0]["retries"] == cco.MAX_ATTEMPTS - 1
    assert [s["id"] for s in m["discovery_skipped"]] == [SC] and f"resolve:{SC}" in m["discovery_skipped"][0]["reason"]


def test_a_shortcut_lookup_that_recovers_captures_its_report(tmp_path, monkeypatch):
    monkeypatch.setattr(cco, "BACKOFF_S", (0.0, 0.0))
    srv, base, answers = shortcut_server(), fake_server(), [Resp(503, "busy")]

    def handler(kind, path):
        if path == f"/content/{SC}?fields=target" and answers:
            return answers.pop()
        if "fields=target" in path or path.startswith("/v1/path?") or "/items" in path:
            return srv(kind, path)
        return base(kind, path.replace(TID, RID))

    code, m = run_main(tmp_path, monkeypatch, handler)
    assert code == 0 and m["status"] == "ok" and [r["id"] for r in m["reports"]] == [TID]
    resolve = next(leg for leg in m["discovery"] if leg["leg"] == f"resolve:{SC}")
    assert resolve["status"] == "ok" and resolve["retries"] == 1 and resolve["target_id"] == TID


def test_a_shortcut_lookup_whose_session_is_lost_relogs_in(tmp_path):
    answers = [Resp(401, "")]
    srv = shortcut_server()

    def handler(kind, path):
        if path == f"/content/{SC}?fields=target" and answers:
            return answers.pop()
        return srv(kind, path)

    cap, _ = capturer(tmp_path, handler)
    found, legs, _ = cap.discover([FID], [])
    assert found == [(TID, f"folder:{FID}/shortcut:{SC}")] and cap.client.logins == 1 and legs[1]["reauths"] == 1


def test_a_shortcut_lookup_without_the_capability_stops_the_run(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, shortcut_server(target=Resp(403, "CV-0015 no capability")))
    assert code == 2 and m["status"] == "stopped"
    assert [leg["class"] for leg in m["discovery"] if leg["leg"].startswith("resolve:")] == [cco.CAPABILITY]


@pytest.mark.parametrize("kids", [[], [kid(SID, "reportView", "V")]])
def test_main_without_any_report_exits_1_with_a_named_status(tmp_path, monkeypatch, kids):
    code, m = run_main(tmp_path, monkeypatch, listing_server(content={FID: kids}))
    assert code == 1 and m["status"] == "no_reports" and m["reports"] == []


def test_a_package_is_listed_through_bi_objects_even_when_content_answers_200(tmp_path):
    # G4: measured live, /api/v1/content/{package}/items answered 200; the package must still go through objects
    srv = listing_server(
        content={FID: [kid(PKG, "package")], PKG: [kid(SID, "report")]}, objects={PKG: [kid(XID, "report")]}
    )
    cap, _ = capturer(tmp_path, srv)
    found, legs, _ = cap.discover([FID], [])
    assert found == [(XID, f"folder:{FID}")] and legs[1]["route"] == "objects"
    assert ("api", f"/content/{PKG}/items") not in cap.client.calls


# ---------------------------------------------------------------- round four: legs that must not be credited


def test_an_object_answer_without_data_is_refused_and_writes_no_file(tmp_path):
    # GO1
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=json.dumps({"data": []})))
    rec = cap.new_leg("object")
    cap.run_leg(rec, lambda: cap.get_object(RID, tmp_path, rec))
    assert rec["status"] == "refused" and "path" not in rec and not (tmp_path / "object.json").exists()


@pytest.mark.parametrize("tag", ["list", "crosstab", "singleton"])
def test_a_non_chart_answering_with_an_image_link_is_refused(tmp_path, tag):
    # N8: the image-only rule was measured on charts only
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=json.dumps(CHART_DATA)))
    rec = run_part(cap, tmp_path, cap.get_part_data, {**part("List1"), "tag": tag})
    assert rec["status"] == "refused" and "image link" in rec["reason"] and not (tmp_path / "parts").exists()


@pytest.mark.parametrize(
    "body",
    [
        '<div><img src="/bi/samples/logo.png"/>Your session has expired</div>',
        "<div><table><tr><td>RQP-DEF-0177 An error occurred while performing operation</td></tr></table></div>",
        "<div><table><tr><td>&#xa0;XQE-DS-0009 data source unavailable</td></tr></table></div>",
        "<div><table><tr><td>session timed out</td></tr></table></div>",
        "<div><table><tr><td>CV-0015 no capability</td></tr></table></div>",
        "<div><table><tr><td>&#xa0;</td><td>&#XA0;&#160;&nbsp;</td></tr></table></div>",
    ],
)
def test_a_render_holding_an_error_or_nothing_is_refused(tmp_path, body):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    assert rec["status"] == "refused" and not (tmp_path / "parts").exists()


@pytest.mark.parametrize(
    "body",
    [
        "<div><table><tr><td>Code</td></tr><tr><td>RQP-DEF-0177</td></tr></table></div>",
        "<div><table><tr><td>KPI-FIN-2024</td><td>12</td></tr></table></div>",
        "<div><table><tr><td>Session</td><td>expired</td></tr></table></div>",
    ],
)
def test_a_render_with_a_code_like_value_after_its_headers_is_credited(tmp_path, body):
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    assert rec["status"] == "ok" and (tmp_path / "parts" / "List1.html").exists()


def test_a_fragment_wrapped_in_a_form_is_not_a_fragment(tmp_path):
    # VF3
    body = "<div><form><table><tr><td>User</td></tr></table></form></div>"
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=body, ctype="text/xml"))
    assert run_part(cap, tmp_path, cap.get_part_render, part("List1"))["status"] == "refused"


def test_image_only_needs_the_chart_query_marker():
    # IO1
    row = {"value": f"https://{HOST}/bi/v1/disp?b_action=xts.run"}
    assert not cco.image_only({"dataSet": {"dataTable": [{"row": [row]}]}}, HOST)
    assert cco.image_only(CHART_DATA, HOST)


@pytest.mark.parametrize("url", ["ftp:/bi/v1/disp", "file:///bi/v1/disp", "mailto:/bi/v1/disp"])
def test_to_bi_path_refuses_a_non_http_scheme(url):
    # H2
    assert cco.to_bi_path(url, HOST) is None


def test_discover_parts_includes_a_viz_without_any_query():
    # DP3
    assert [(p["name"], p["tag"], p["refQuery"]) for p in cco.discover_parts(spec_with('<viz name="V0"/>'))] == [
        ("V0", "viz", None)
    ]


def test_resolve_model_reads_a_module_path_without_its_type_attribute(tmp_path):
    # LN8
    spec = SPEC.replace('<modelPath type="module">', "<modelPath>")
    cap, _ = capturer(tmp_path, fake_server(spec))
    model = cap.resolve_model(None, spec)
    assert model["kind"] == "module" and model["id"] == MID


def test_resolve_model_prefers_the_objects_package(tmp_path):
    # LN9
    cap, _ = capturer(tmp_path, fake_server())
    obj = {"metadataModelPackage": [{"searchPath": "/content/package[@name='P']", "id": PKG}]}
    assert cap.resolve_model(obj, SPEC) == {"kind": "package", "searchPath": "/content/package[@name='P']", "id": PKG}


def test_viewer_wait_posts_carry_the_action_state(tmp_path):
    # W6
    cap, rec, _ = run_viewer(tmp_path, [viewer_page("working"), viewer_page("complete")])
    assert rec["status"] == "ok" and [p["cv.actionState"] for p in cap.client.posts] == ["A"]


@pytest.mark.parametrize("timeout", ["0", "-1", "nan"])
def test_main_refuses_a_timeout_that_is_not_positive(tmp_path, monkeypatch, timeout):
    # M19
    monkeypatch.setattr(cco, "Client", no_client)
    monkeypatch.setenv("COGNOS_API_KEY", "k")
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(tmp_path), "--report-id", RID]
    with pytest.raises(SystemExit) as e:
        cco.main(argv + ["--timeout", timeout])
    assert e.value.code == 2


def test_the_worst_case_covers_a_whole_viewer_attempt():
    # M29
    assert cco.worst_case_leg_s(1.0) >= (cco.MAX_ATTEMPTS + 1) * (3 * 1.0 + cco.VIEWER_WAIT_S)


def test_safe_name_keeps_distinct_names_distinct():
    # N2 (mutant): "a b" and "a_b" must never share a file
    assert cco.safe_name("a_b") == "a_b"
    assert cco.safe_name("a b") != "a_b" and cco.safe_name("a b").startswith("a_b-")


def test_the_recorded_sha256_is_that_of_the_stored_file(tmp_path):
    # A15
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body=json.dumps(TABLE_DATA)))
    rec = run_part(cap, tmp_path, cap.get_part_data, part("List1"))
    stored = (tmp_path / "parts" / "List1.json").read_bytes()
    assert rec["sha256"] == hashlib.sha256(stored).hexdigest() and rec["bytes"] == len(stored)


# ---------------------------------------------------------------- round four: run control (L3, R6, M14, M12, M17)


def test_a_login_that_cannot_reach_the_server_stops_the_run(tmp_path, monkeypatch):
    # L3 (mutant): a transient login failure is a Stop (exit 2), never a crash (exit 3)
    def route(s, method, url, kw):
        raise requests.ConnectionError("unreachable")

    monkeypatch.setattr(cco.requests, "Session", lambda: FakeSession(route))
    monkeypatch.setenv("COGNOS_API_KEY", KEY)
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(tmp_path), "--report-id", RID]
    code = cco.main(argv)
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert code == 2 and m["status"] == "stopped" and "login failed" in m["stop_reason"]


def test_a_refused_relogin_fails_the_leg_and_stops(tmp_path):
    # R6
    class NoLogin(FakeClient):
        def login(self):
            raise cco.Stop("login refused")

    cap = cco.Capturer(NoLogin(lambda kind, path: Resp()), tmp_path, [], "auto", sleep=lambda s: None)
    rec = cap.new_leg("x")
    with pytest.raises(cco.Stop):
        cap.run_leg(rec, raising(cco.Failure(cco.SESSION_LOST, "401")))
    assert rec["status"] == "failed" and rec["class"] == cco.SESSION_LOST and rec["reason"] == "re-login refused"


def test_a_stop_from_inside_a_leg_fails_it_instead_of_leaving_it_pending(tmp_path, monkeypatch):
    # M14
    base = fake_server()

    def handler(kind, path):
        if "fields=specification" in path:
            raise cco.Stop("stopped from the server side")
        return base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler, ids=["--report-id", RID])
    spec = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "spec")
    assert code == 2 and spec["status"] == "failed" and spec["class"] == "stopped"


def test_a_manifest_write_cut_short_keeps_the_previous_manifest(tmp_path, monkeypatch):
    # M12
    target = tmp_path / "manifest.json"
    target.write_text('{"status": "ok"}', encoding="utf-8")
    real = Path.write_text

    def write_text(self, text, *a, **k):
        real(self, text[:5], *a, **k)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_text", write_text)
    with pytest.raises(OSError):
        cco.write_json(target, {"status": "running", "reports": []})
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "ok"}


def test_the_manifest_is_written_after_every_report(tmp_path, monkeypatch):
    # M17
    base, seen = two_report_server([RID, SID]), []

    def handler(kind, path):
        if path.startswith(f"/v1/objects/{SID}"):
            m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
            seen.append([r["id"] for r in m["reports"]])
        return base(kind, path)

    code, _ = run_main(tmp_path, monkeypatch, handler)
    assert code == 0 and seen == [[RID]]


# ---------------------------------------------------------------- round four: console and secrets (N6, N11, C1)


def test_a_name_the_console_cannot_encode_never_crashes_the_run(tmp_path, monkeypatch):
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    base = fake_server()

    def handler(kind, path):
        if path.startswith("/v1/objects/"):
            return Resp(body=json.dumps({"data": [{"defaultName": "Omzet \u2192 2024 \u5831\u544a", "ancestors": []}]}))
        return base(kind, path)

    code, m = run_main(tmp_path, monkeypatch, handler, ids=["--report-id", RID])
    console.flush()
    assert code == 0 and m["status"] == "ok"
    assert b"Omzet \\u2192 2024" in raw.getvalue()


def test_a_key_echoed_by_the_server_is_redacted_from_text_outputs(tmp_path, monkeypatch, capsys):
    base = with_viewer(fake_server(), viewer_page("complete"))

    def handler(kind, path):
        if path.startswith("/v1/objects/"):
            return Resp(body=json.dumps({"data": [{"defaultName": f"R {KEY}", "ancestors": []}]}))
        if "fmt=HTMLFragment" in path and "List1" in path:
            body = f"<div><table><tr><td>Name</td></tr><tr><td>{KEY}</td></tr></table></div>"
            return Resp(body=body, ctype="text/xml")
        if "b_action=rc" in path:
            return Resp(body=b"%PDF-1.7 " + KEY.encode(), ctype="application/pdf")
        return base(kind, path)

    sessions = []
    monkeypatch.setattr(
        cco.requests, "Session", lambda: sessions.append(FakeSession(wire_server(handler))) or sessions[-1]
    )
    monkeypatch.setenv("COGNOS_API_KEY", KEY)
    out = tmp_path / "out"
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(out), "--report-id", RID]
    code = cco.main(argv + ["--formats", "pdf"])
    printed = capsys.readouterr()
    assert code == 0 and KEY not in printed.out and "<key>" in printed.out
    for p in out.rglob("*"):
        if p.is_file() and p.suffix != ".pdf":
            assert KEY.encode() not in p.read_bytes(), p.name
    assert "<key>" in (out / RID / "object.json").read_text(encoding="utf-8")
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    legs = {leg["leg"]: leg for leg in m["reports"][0]["legs"]}
    assert legs["object"]["redacted"] == 1 and legs["part:List1:render"]["redacted"] == 1
    # binary outputs are stored as served (documented): rewriting a PDF would corrupt it
    assert KEY.encode() in (out / RID / "report.pdf").read_bytes() and "redacted" not in legs["pdf"]


def test_a_cookie_set_by_a_redirect_hop_is_sent_on_the_next_hop():
    def route(s, method, url, kw):
        if "first" in url:
            s.cookies.set("cam_passport", "NEW-PASSPORT")
            return Wire(302, location="/bi/second")
        return Wire(body="ok")

    client = logged_in(route)
    client.bi("GET", "/first")
    first, second = client.session.sent
    assert PASSPORT in first["headers"]["Cookie"]
    assert "NEW-PASSPORT" in second["headers"]["Cookie"] and PASSPORT not in second["headers"]["Cookie"]


# ---------------------------------------------------------------- round five: an interrupt-safe swap (NEW-1, NEW-2)


def legs_match_disk(out, entry):
    """No ok leg may point at a missing file or at a file holding other content."""
    for rec in entry["legs"]:
        if rec["status"] != "ok":
            continue
        for r in cco.files_of(rec):
            if r.get("path"):
                f = out / r["path"]
                assert f.is_file(), (rec["leg"], r["path"])
                assert hashlib.sha256(f.read_bytes()).hexdigest() == r["sha256"], (rec["leg"], r["path"])


def old_capture(tmp_path):
    final = tmp_path / RID
    final.mkdir()
    (final / "report.pdf").write_bytes(b"old pdf")
    (final / "object.json").write_bytes(b'{"old": 1}')
    return {p.name: p.read_bytes() for p in final.iterdir()}


def rename_steps(monkeypatch, tmp_path, fail, raise_in=None, raise_after=None):
    """Path.rename classified by swap step; ``fail`` counts PermissionErrors per step, ``raise_in`` interrupts one,
    ``raise_after`` interrupts one just after it succeeded."""
    real, final = Path.rename, (tmp_path / RID).resolve()

    def step(src, dst):
        if src.parent.name == ".staging":
            return "new-in" if dst == final else "keep"
        if src == final:
            return "old-aside"
        if src.parent.name == ".trash":
            return "restore" if dst == final else "previous"
        return "other"

    def rename(self, target):
        s = step(Path(self).resolve(), Path(target).resolve())
        if s == raise_in:
            raise KeyboardInterrupt
        if fail.get(s, 0) > 0:
            fail[s] -= 1
            raise PermissionError(13, "in use")
        done = real(self, target)
        if s == raise_after:
            raise KeyboardInterrupt
        return done

    monkeypatch.setattr(Path, "rename", rename)


@pytest.mark.parametrize(
    ("fail", "interrupt_at", "store_cls"),
    [
        ({"old-aside": 99}, 1, "interrupted"),  # Ctrl+C in the old-folder rename backoff
        ({"new-in": 99}, 1, "interrupted"),  # Ctrl+C in the new-folder rename backoff
        ({"new-in": 99, "restore": 1}, 3, cco.STORE_ERROR),  # Ctrl+C in the restore backoff
    ],
)
def test_an_interrupt_in_any_rename_backoff_keeps_the_old_capture_and_a_true_entry(
    tmp_path, monkeypatch, fail, interrupt_at, store_cls
):
    before = old_capture(tmp_path)
    rename_steps(monkeypatch, tmp_path, fail)
    sleeps: list = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == interrupt_at:
            raise KeyboardInterrupt

    cap = cco.Capturer(FakeClient(report_server()), tmp_path, [], "auto", sleep=sleep)
    reports: list = []
    with pytest.raises(KeyboardInterrupt):
        cap.capture_report(RID, "report-id", reports)
    [entry] = reports
    assert {p.name: p.read_bytes() for p in (tmp_path / RID).iterdir()} == before
    legs_match_disk(tmp_path, entry)
    store = entry["legs"][-1]
    assert store["leg"] == "store" and store["class"] == store_cls and "untouched" in store["reason"]
    obj = next(leg for leg in entry["legs"] if leg["leg"] == "object")
    assert obj["status"] == "ok" and obj["path"] == f"{RID}.incoming/object.json"
    assert "previous_capture" not in entry and not cap.kept_trash


def test_main_interrupted_while_the_new_capture_moves_in_keeps_the_old_one_and_says_so(tmp_path, monkeypatch, capsys):
    before = old_capture(tmp_path)
    rename_steps(monkeypatch, tmp_path, {}, raise_in="new-in")
    code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID])
    assert code == 130 and m["status"] == "interrupted"
    assert {p.name: p.read_bytes() for p in (tmp_path / RID).iterdir()} == before
    [entry] = m["reports"]
    legs_match_disk(tmp_path, entry)
    assert entry["legs"][-1]["leg"] == "store" and entry["legs"][-1]["class"] == "interrupted"
    assert f"{RID}: new capture kept as {RID}.incoming; {RID} is as it was before this run" in capsys.readouterr().err
    assert not (tmp_path / ".trash").exists() and not (tmp_path / ".staging").exists()


def test_an_interrupt_just_after_the_new_capture_moved_in_keeps_it_and_drops_the_old(tmp_path, monkeypatch):
    old_capture(tmp_path)
    rename_steps(monkeypatch, tmp_path, {}, raise_after="new-in")
    cap = cco.Capturer(FakeClient(report_server()), tmp_path, [], "auto", sleep=lambda s: None)
    reports: list = []
    with pytest.raises(KeyboardInterrupt):
        cap.capture_report(RID, "report-id", reports)
    [entry] = reports
    legs_match_disk(tmp_path, entry)
    obj = next(leg for leg in entry["legs"] if leg["leg"] == "object")
    assert obj["status"] == "ok" and obj["path"] == f"{RID}/object.json"
    assert (tmp_path / RID / "object.json").read_bytes() != b'{"old": 1}'
    assert not (tmp_path / f"{RID}.incoming").exists() and not (tmp_path / f"{RID}.previous").exists()
    assert "previous_capture" not in entry and not cap.kept_trash and not any((tmp_path / ".trash").iterdir())


@pytest.mark.parametrize("previous_ok", [True, False])
def test_a_relative_out_records_a_kept_old_capture_like_an_absolute_one(tmp_path, monkeypatch, previous_ok):
    base = tmp_path / "rel"
    base.mkdir()
    before = old_capture(base)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    rename_steps(monkeypatch, base, {"new-in": 99, "restore": 99, "previous": 0 if previous_ok else 99})
    cap = cco.Capturer(FakeClient(report_server()), Path("rel"), [], "auto", sleep=lambda s: None)
    reports: list = []
    cap.capture_report(RID, "report-id", reports)
    [entry] = reports
    kept = entry["previous_capture"]["path"]
    assert kept == f"{RID}.previous" if previous_ok else kept.startswith(".trash/")
    assert {p.name: p.read_bytes() for p in (base / kept).iterdir()} == before
    assert entry["legs"][-1]["leg"] == "store" and kept in entry["legs"][-1]["reason"]
    obj = next(leg for leg in entry["legs"] if leg["leg"] == "object")
    assert obj["status"] == "ok" and obj["path"] == f"{RID}.incoming/object.json"
    legs_match_disk(base, entry)


@pytest.mark.parametrize("previous_ok", [True, False])
def test_an_old_capture_that_cannot_be_put_back_is_kept_named_and_never_deleted(tmp_path, monkeypatch, previous_ok):
    before = old_capture(tmp_path)
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    rename_steps(monkeypatch, tmp_path, {"new-in": 99, "restore": 99, "previous": 0 if previous_ok else 99})
    code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID])
    assert code == 1
    [entry] = m["reports"]
    kept = entry["previous_capture"]["path"]
    assert kept == f"{RID}.previous" if previous_ok else kept.startswith(".trash/")
    assert {p.name: p.read_bytes() for p in (tmp_path / kept).iterdir()} == before
    assert kept in entry["legs"][-1]["reason"] and "untouched" not in entry["legs"][-1]["reason"]
    legs_match_disk(tmp_path, entry)


def test_an_interrupt_while_the_old_capture_cannot_be_put_back_names_where_it_is(tmp_path, monkeypatch, capsys):
    before = old_capture(tmp_path)
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    rename_steps(monkeypatch, tmp_path, {"restore": 99}, raise_in="new-in")
    code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID])
    assert code == 130
    kept = m["reports"][0]["previous_capture"]["path"]
    assert kept == f"{RID}.previous" and {p.name: p.read_bytes() for p in (tmp_path / kept).iterdir()} == before
    err = capsys.readouterr().err
    assert kept in err and "old captures are untouched" not in err


def test_a_rerun_removes_a_stale_previous_and_leaves_no_trash(tmp_path, monkeypatch):
    (tmp_path / f"{RID}.previous").mkdir()
    for _ in range(2):
        code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID])
        assert code == 0
    assert not (tmp_path / ".trash").exists() and not (tmp_path / f"{RID}.previous").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [RID, "manifest.json"]


def test_stale_staging_content_never_reaches_the_final_folder(tmp_path, monkeypatch):
    stale = tmp_path / ".staging" / RID / "parts"
    stale.mkdir(parents=True)
    (stale / "Stale9.json").write_text("{}")
    code, m = run_main(tmp_path, monkeypatch, fake_server(), ids=["--report-id", RID])
    assert code == 0 and (tmp_path / RID / "parts").is_dir()
    assert not list((tmp_path / RID).rglob("Stale9.json"))
    # B1/HK1: a killed run's staging may hold the only copy of a capture: kept in .trash and named, never deleted
    [entry] = m["reports"]
    kept = tmp_path / entry["earlier_staging"]["path"]
    assert kept.parent.name == ".trash" and (kept / "parts" / "Stale9.json").exists()


def test_two_consecutive_interrupts_keep_the_latest_partial_capture(tmp_path, monkeypatch):
    def run(n):
        base = cut_short("interrupt")

        def handler(kind, path):
            if path.startswith("/v1/objects/"):
                return Resp(body=json.dumps({"data": [{"defaultName": f"run{n}", "ancestors": []}]}))
            return base(kind, path)

        return run_main(tmp_path, monkeypatch, handler, ids=["--report-id", RID])

    run(1)
    code, m = run(2)
    obj = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "object")
    assert code == 130 and obj["status"] == "ok" and obj["path"] == f"{RID}.incoming/object.json"
    assert "run2" in (tmp_path / obj["path"]).read_text(encoding="utf-8")
    assert f"kept in {RID}.incoming" in m["reports"][0]["legs"][-1]["reason"]


def test_other_captures_names_incoming_and_previous_folders(tmp_path):
    (tmp_path / f"{SID}.incoming").mkdir()
    (tmp_path / f"{XID}.previous").mkdir()
    found = {o["folder"]: o for o in cco.other_captures(tmp_path, set())}
    assert found[f"{SID}.incoming"]["id"] == SID and "interrupted" in found[f"{SID}.incoming"]["note"]
    assert found[f"{XID}.previous"]["id"] == XID and "could not put back" in found[f"{XID}.previous"]["note"]


# ---------------------------------------------------------------- round five: viewer pointers on a lost capture (NEW-3)


@pytest.mark.parametrize("lost", [False, True])
def test_a_lost_capture_names_no_viewer_output_in_any_reason(tmp_path, monkeypatch, lost):
    if lost:
        every_staging_rename_fails(monkeypatch)
    spec = spec_with('<list refQuery="Query1"/><list name="List1" refQuery="Query1"/>')
    handler = with_viewer(fake_server(spec=spec), viewer_page("complete"))
    cap, _ = capturer(tmp_path, handler, formats=["pdf"])
    entry = cap.capture_report(RID, "report-id", [])
    reasons = " ".join(leg.get("reason", "") for leg in entry["legs"])
    # control: an unnamed container points at the captured PDF when the capture is kept
    assert ("report.pdf" in reasons) is not lost
    if lost:
        assert "no output captured on this run holds its values" in reasons


# ---------------------------------------------------------------- round five: redaction (NEW-4, N11c, N11k, M10, N11d, K51)

ODD_KEY = 'Ab/C+D=E"F\\G'  # a key holding every character JSON, HTML and URLs escape


class KeyedClient(FakeClient):
    def __init__(self, handler, key):
        super().__init__(handler)
        self._key = key


def stored(tmp_path, rel, body, ctype, key=ODD_KEY):
    cap = cco.Capturer(KeyedClient(lambda k, p: Resp(404), key), tmp_path, [], "auto", sleep=lambda s: None)
    rec = cap.new_leg("x")
    cap.store(rec, tmp_path / "stage", rel, body, ctype)
    return (tmp_path / "stage" / rel).read_bytes(), rec


@pytest.mark.parametrize("ctype", ["", "application/octet-stream", "application/json"])
def test_a_json_part_is_redacted_whatever_its_content_type(tmp_path, ctype):
    body, rec = stored(tmp_path, "parts/List1.json", json.dumps({"v": ODD_KEY}).encode(), ctype)
    assert json.loads(body) == {"v": "<key>"} and rec["redacted"] == 1


@pytest.mark.parametrize(
    "form",
    [
        'Ab\\/C+D=E\\"F\\\\G',  # JSON with an escaped solidus
        'Ab/C+D\\u003dE\\"F\\\\G',  # JSON with a unicode escape
        "Ab/C\\u002BD=E\\u0022F\\u005cG",  # unicode escapes in upper and lower case hex
        "Ab/C&#43;D=E&quot;F\\G",  # HTML decimal entity
        "Ab/C&#x2B;D=E&#34;F\\G",  # HTML hex entity
        "Ab%2FC%2BD%3DE%22F%5CG",  # %-encoded
    ],
)
def test_every_escaped_form_of_the_key_is_redacted(tmp_path, form):
    body, rec = stored(tmp_path, "parts/List1.html", f"<td>{form}</td>".encode(), "text/xml")
    assert body == b"<td><key></td>" and rec["redacted"] == 1


@pytest.mark.parametrize("bom", [b"\xff\xfe", b""])
def test_a_utf16le_csv_is_redacted_and_stays_valid_utf16(tmp_path, bom):
    text = f"Region\tName\r\nNorth\t{KEY}\r\n"
    body, rec = stored(tmp_path, "report.csv", bom + text.encode("utf-16-le"), "application/csv; charset=UTF-16LE", KEY)
    assert body.startswith(bom) and len(body) % 2 == 0 and rec["redacted"] == 1
    assert body[len(bom) :].decode("utf-16-le") == text.replace(KEY, "<key>")
    assert rec["sha256"] == hashlib.sha256(body).hexdigest()


def test_a_utf16le_csv_with_only_a_bom_to_tell_its_encoding_is_redacted(tmp_path):
    # Non-Latin text has few zero bytes, so neither the content type nor the zero-byte heuristic says UTF-16.
    text = "\u5730\u533a" * 40 + f"\t{KEY}\r\n"
    body, rec = stored(tmp_path, "report.csv", b"\xff\xfe" + text.encode("utf-16-le"), "application/csv", KEY)
    assert body[2:].decode("utf-16-le") == text.replace(KEY, "<key>") and rec["redacted"] == 1


def test_a_binary_output_is_stored_as_served(tmp_path):
    body, rec = stored(tmp_path, "report.pdf", b"%PDF-1.7 " + ODD_KEY.encode(), "text/plain")
    assert ODD_KEY.encode() in body and "redacted" not in rec


def test_write_json_redacts_a_key_holding_quotes_and_backslashes(tmp_path):
    cco.write_json(tmp_path / "m.json", {"a": ODD_KEY, "b": f"x {ODD_KEY} y"}, ODD_KEY)
    text = (tmp_path / "m.json").read_text(encoding="utf-8")
    assert json.loads(text) == {"a": "<key>", "b": "x <key> y"}


def wire_main(tmp_path, monkeypatch, handler, argv, key_text=None):
    sessions: list = []
    monkeypatch.setattr(
        cco.requests, "Session", lambda: sessions.append(FakeSession(wire_server(handler))) or sessions[-1]
    )
    out = tmp_path / "out"
    base = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(out)]
    if key_text is None:
        monkeypatch.setenv("COGNOS_API_KEY", KEY)
    else:
        (tmp_path / "key.txt").write_text(key_text, encoding="utf-8", newline="")
        base += ["--api-key-file", str(tmp_path / "key.txt")]
    code = cco.main(base + argv)
    return code, out, json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def test_a_key_file_with_a_trailing_newline_logs_in_with_the_bare_key(tmp_path, monkeypatch):
    # wire_server asserts the PUT carries exactly KEY
    code, _, m = wire_main(
        tmp_path, monkeypatch, fake_server(), ["--report-id", RID, "--formats", "none"], KEY + "\r\n"
    )
    assert code == 0 and m["status"] == "ok"


def test_a_part_named_like_the_key_is_stored_under_a_hashed_name(tmp_path, monkeypatch):
    spec = spec_with(f'<list name="{KEY}" refQuery="Query1"/><list name="List1" refQuery="Query1"/>')
    _, out, m = wire_main(tmp_path, monkeypatch, fake_server(spec=spec), ["--report-id", RID, "--formats", "none"])
    [entry] = m["reports"]
    paths = [r["path"] for leg in entry["legs"] for r in cco.files_of(leg) if r.get("path")]
    assert any("/parts/part-" in p for p in paths) and any(p.endswith("/parts/List1.json") for p in paths)
    for p in paths:
        assert (out / p).is_file(), p
    assert not [p for p in out.rglob("*") if KEY in p.name]
    assert all(KEY.encode() not in p.read_bytes() for p in out.rglob("*") if p.is_file())


# ---------------------------------------------------------------- round five: pinned boundaries (named mutants)


def test_an_active_report_spec_with_a_real_viewer_output_captures_the_pdf(tmp_path):
    # AR8: the viewer page decides; the spec's application="true" is information only
    cap, _ = capturer(tmp_path, with_viewer(fake_server(spec=AR_SPEC), viewer_page("complete")), formats=["pdf"])
    entry = cap.capture_report(RID, "report-id", [])
    pdf = next(leg for leg in entry["legs"] if leg["leg"] == "pdf")
    assert pdf["status"] == "ok" and (tmp_path / RID / "report.pdf").read_bytes().startswith(b"%PDF")
    assert "active_report" in entry


def test_a_text_only_fragment_is_never_a_render(tmp_path):
    # K47: no table and no image
    cap, _ = capturer(tmp_path, lambda kind, path: Resp(body="<div>No data available</div>", ctype="text/xml"))
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    assert rec["status"] == "refused" and "path" not in rec


def test_a_content_listing_answering_html_is_refused_without_falling_back(tmp_path):
    # G4: only a 404 on the content route falls back to /bi/v1/objects
    def handler(kind, path):
        if kind == "api":
            return Resp(body="<html>login</html>", ctype="text/html")
        return Resp(body=json.dumps({"data": [kid(RID, "report")]}))

    cap, _ = capturer(tmp_path, handler)
    found, legs, _ = cap.discover([FID], [])
    assert found == [] and [leg["status"] for leg in legs] == ["refused"]
    assert not [c for c in cap.client.calls if c[0] == "bi"]


def test_a_still_working_page_after_the_wait_is_incomplete(tmp_path, monkeypatch):
    # K24
    monkeypatch.setattr(cco, "VIEWER_WAIT_S", 0.0)
    _, rec, _ = run_viewer(tmp_path, [viewer_page("stillWorking")])
    assert rec["status"] == "failed" and rec["class"] == cco.INCOMPLETE


def test_a_503_carrying_a_cognos_code_is_transient():
    # C4
    assert cco.classify(503, "XQE-GEN-0002 the server is busy")[0] == cco.TRANSIENT


def test_a_render_pointer_names_only_the_pdf():
    # NP4: report.xlsx holds values, never a render
    assert cco.held_in({"viewer_ok": ["report.pdf", "report.xlsx"]}, "render") == (
        "its render is in report.pdf, captured on this run"
    )
    assert cco.held_in({"viewer_ok": ["report.xlsx"]}, "render") == "no output captured on this run holds its render"


@pytest.mark.parametrize(
    ("page", "marker"),
    [
        ("window.open('../irot/irot.htm?v=1');", True),
        ('var u = "/bi/irot/irot.htm";', True),
        ("window.open('../airot/irot.htm');", False),
        ("window.open('x-irot/irot.htm');", False),
    ],
)
def test_the_active_report_marker_must_open_a_path_segment(page, marker):
    # AR10
    assert cco.is_active_report_page(page) is marker


def test_the_viewer_wait_ends_at_its_wall_clock_deadline(tmp_path, monkeypatch):
    # W8: a clock that advances 100 s per reading passes the 120 s deadline after one poll
    clock = {"t": 0.0}

    def monotonic():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(cco, "time", types.SimpleNamespace(monotonic=monotonic))
    cap, sleeps = capturer(tmp_path, viewer_server([viewer_page("working")]))
    page = cap.wait_viewer(viewer_page("working").text)
    assert cco.viewer_status(page) == "working" and len(cap.client.posts) == 1 == len(sleeps)


def test_a_cookie_reset_on_another_path_replaces_the_old_value():
    # C1: login sets cam_passport on /api; a /bi hop sets it again elsewhere; only the new value is sent on
    def route(s, method, url, kw):
        if "first" in url:
            s.cookies.set("cam_passport", "NEW-PASSPORT", domain=HOST, path="/bi")
            return Wire(302, location="/bi/second")
        return Wire(body="ok")

    client = cco.Client(f"https://{HOST}", "ns", KEY)
    client.session = FakeSession(route)
    client.session.cookies.set("XSRF-TOKEN", XSRF)
    client.session.cookies.set("cam_passport", PASSPORT, domain=HOST, path="/api")
    client.bi("GET", "/first")
    second = client.session.sent[1]["headers"]["Cookie"]
    assert "cam_passport=NEW-PASSPORT" in second and PASSPORT not in second


# ---------------------------------------------------------------- round five: data tables (NEW-5)

LC_SPEC = f"""<report {NS}><queries><query name="qKPI"/><query name="Query1"/></queries>
<layouts><layout><reportDataStores>
  <reportDataStore name="Data store4"><dsSource><dsV5ListQuery refQuery="qKPI"/></dsSource></reportDataStore>
</reportDataStores><reportPages><page name="P"><pageBody><contents>
  <listControl name="Data table2" refDataStore="Data store4" rowHeight="37" showColumnTitles="hide">
    <lcColumns><lcColumn refDsColumn="Admissions" width="140"/></lcColumns>
  </listControl>
  <list name="List1" refQuery="Query1"/>
</contents></pageBody></page></reportPages></layout></layouts></report>"""


def test_discover_parts_finds_a_data_table_through_its_data_store():
    parts = {p["name"]: p for p in cco.discover_parts(LC_SPEC)}
    assert set(parts) == {"Data table2", "List1"}
    assert parts["Data table2"]["tag"] == "listControl" and parts["Data table2"]["refQuery"] == "qKPI"
    assert parts["Data table2"]["chart"] is False


def test_a_data_table_is_named_unsupported_never_requested_and_the_pdf_names_its_placeholder(tmp_path, monkeypatch):
    handler = with_viewer(fake_server(spec=LC_SPEC), viewer_page("complete"))
    calls: list = []

    def spy(kind, path):
        calls.append(path)
        return handler(kind, path)

    code, m = run_main(tmp_path, monkeypatch, spy, formats="pdf", ids=["--report-id", RID])
    assert code == 0 and m["status"] == "ok"
    legs = {leg["leg"]: leg for leg in m["reports"][0]["legs"]}
    for kind in ("data", "render"):
        leg = legs[f"part:Data table2:{kind}"]
        assert leg["status"] == "unsupported" and leg["class"] == cco.UNSUPPORTED
        assert '{"dataSet":null}' in leg["reason"] and "<div></div>" in leg["reason"] and "qKPI" in leg["reason"]
        assert "Data table rendering is not supported" in leg["reason"] and "its values are in" not in leg["reason"]
    assert not [p for p in calls if "Data%20table2" in p or "Data table2" in p]
    assert legs["pdf"]["status"] == "ok" and legs["pdf"]["omits"]["parts"] == ["Data table2"]
    assert {(u["part"], u["leg"]) for u in m["render_unestablished"]} >= {
        ("Data table2", "pdf"),
        ("Data table2", "part:Data table2:render"),
    }
    assert "Data table2" in {d["part"] for d in m["data_unestablished"]}


def test_a_data_table_is_named_on_the_pdf_even_without_parts(tmp_path, monkeypatch):
    handler = with_viewer(fake_server(spec=LC_SPEC), viewer_page("complete"))
    code, m = run_main(tmp_path, monkeypatch, handler, formats="pdf", ids=["--report-id", RID], parts="none")
    assert code == 0
    pdf = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "pdf")
    assert pdf["omits"]["parts"] == ["Data table2"]
    assert ("Data table2", "pdf") in {(u["part"], u["leg"]) for u in m["render_unestablished"]}


def test_a_pdf_without_data_tables_names_no_omission(tmp_path, monkeypatch):
    code, m = run_main(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), formats="pdf")
    pdf = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "pdf")
    assert code == 0 and "omits" not in pdf


# ---------------------------------------------------------------- round six: a partial re-run never loses an output (H1)


def run_rid(tmp_path, monkeypatch, handler, formats="none"):
    return run_main(tmp_path, monkeypatch, handler, formats=formats, ids=["--report-id", RID])


def outputs(folder):
    return {f.relative_to(folder).as_posix(): f.read_bytes() for f in folder.rglob("*") if f.is_file()}


def no_listed_file_is_missing(out, entry):
    legs_match_disk(out, entry)
    for c in entry.get("carried", []):
        f = out / c["path"]
        assert f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == c["sha256"], c["path"]


def test_a_rerun_whose_pdf_is_incomplete_keeps_the_earlier_pdf_where_the_manifest_names_it(tmp_path, monkeypatch):
    # the reviewer's kill test: run 2's viewer is still working at the poll limit, twice
    monkeypatch.setattr(cco, "VIEWER_WAIT_S", 0.0)
    monkeypatch.setattr(cco, "BACKOFF_S", (0.0, 0.0))
    code, _ = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), "pdf")
    first = (tmp_path / RID / "report.pdf").read_bytes()
    stamp = json.loads((tmp_path / RID / "capture.json").read_text(encoding="utf-8"))["captured_at"]
    assert code == 0 and first.startswith(b"%PDF")
    code, m = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("working")), "pdf")
    [entry] = m["reports"]
    pdf = next(leg for leg in entry["legs"] if leg["leg"] == "pdf")
    assert code == 1 and pdf["status"] == "failed" and pdf["class"] == cco.INCOMPLETE and pdf["retries"] == 1
    [kept] = [c for c in entry["carried"] if c["leg"] == "pdf"]
    assert kept["path"] == f"{RID}/report.pdf" and (tmp_path / kept["path"]).read_bytes() == first
    assert kept["captured_at"] == stamp and kept["from"] == RID
    no_listed_file_is_missing(tmp_path, entry)


def test_a_rerun_where_every_call_fails_keeps_every_earlier_output_and_names_each(tmp_path, monkeypatch):
    code, _ = run_rid(tmp_path, monkeypatch, all_viewers(fake_server()), "pdf,csv,xlsx")
    before = {k: v for k, v in outputs(tmp_path / RID).items() if k != "capture.json"}
    assert code == 0 and {"report.pdf", "report.csv", "report.xlsx", "spec.xml", "parts/List1.json"} <= set(before)
    code, m = run_rid(tmp_path, monkeypatch, lambda kind, path: Resp(404), "pdf,csv,xlsx")
    [entry] = m["reports"]
    after = {k: v for k, v in outputs(tmp_path / RID).items() if k != "capture.json"}
    assert code == 1 and after == before
    assert {c["path"] for c in entry["carried"]} == {f"{RID}/{rel}" for rel in before}
    assert "previous_capture" not in entry and entry["stored_as"] == RID
    no_listed_file_is_missing(tmp_path, entry)


def test_an_output_captured_ok_replaces_the_earlier_one_and_is_not_carried(tmp_path, monkeypatch):
    old_capture(tmp_path)
    code, m = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), "pdf")
    [entry] = m["reports"]
    assert code == 0 and (tmp_path / RID / "report.pdf").read_bytes() == b"%PDF-1.7 test"
    assert json.loads((tmp_path / RID / "object.json").read_text(encoding="utf-8")) != {"old": 1}
    assert "carried" not in entry


def stamped_capture(folder, captured_at, pdf):
    folder.mkdir(parents=True)
    (folder / "report.pdf").write_bytes(pdf)
    (folder / "capture.json").write_text(json.dumps({"id": RID, "captured_at": captured_at}), encoding="utf-8")


def test_the_newest_earlier_capture_wins_and_the_one_it_supersedes_is_named(tmp_path, monkeypatch):
    stamped_capture(tmp_path / RID, "2026-01-01T00:00:00+00:00", b"%PDF old")
    stamped_capture(tmp_path / f"{RID}.incoming", "2026-06-01T00:00:00+00:00", b"%PDF newer")
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    [kept] = entry["carried"]
    assert code == 0 and (tmp_path / RID / "report.pdf").read_bytes() == b"%PDF newer"
    assert kept["from"] == f"{RID}.incoming" and kept["captured_at"] == "2026-06-01T00:00:00+00:00"
    assert not (tmp_path / f"{RID}.incoming").exists()
    assert [s["folder"] for s in entry["superseded"]] == [f"{RID}.incoming"]


def test_a_carried_output_keeps_its_own_date_across_runs(tmp_path, monkeypatch):
    ticks = iter(range(1000))
    monkeypatch.setattr(cco, "now", lambda: f"2026-01-01T00:{next(ticks):04d}+00:00")
    monkeypatch.setattr(cco, "VIEWER_WAIT_S", 0.0)
    monkeypatch.setattr(cco, "BACKOFF_S", (0.0, 0.0))
    run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), "pdf")
    first = json.loads((tmp_path / RID / "capture.json").read_text(encoding="utf-8"))["captured_at"]
    for _ in range(2):
        code, m = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("working")), "pdf")
        [kept] = [c for c in m["reports"][0]["carried"] if c["leg"] == "pdf"]
        assert code == 1 and kept["captured_at"] == first


def test_a_carry_that_fails_keeps_the_capture_aside_and_the_old_one_untouched(tmp_path, monkeypatch):
    before = old_capture(tmp_path)

    def copyfile(src, dst):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(cco.shutil, "copyfile", copyfile)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    assert code == 1 and {p.name: p.read_bytes() for p in (tmp_path / RID).iterdir()} == before
    store = entry["legs"][-1]
    assert store["leg"] == "store" and store["class"] == cco.STORE_ERROR and "carried" in store["reason"]
    assert entry["stored_as"] == f"{RID}.incoming" and "carried" not in entry
    no_listed_file_is_missing(tmp_path, entry)


@pytest.mark.parametrize(
    ("rel", "owner"),
    [
        ("report.pdf", "pdf"),
        ("module.json", "lineage"),
        ("parts/List1.json", "part:L1:data"),
        ("parts/List1.html", "part:L1:render"),
        ("parts/List1_2.png", "part:L1:render"),
        ("parts/Q_2.html", "part:Q2:render"),
        ("parts/Q_2_1.svg", "part:Q2:render"),
        ("parts/Gone.json", "part-file:Gone:data"),
        ("notes.txt", "file:notes.txt"),
    ],
)
def test_each_capture_file_is_owned_by_the_leg_that_writes_it(rel, owner):
    assert cco.owner_of(rel, {"List1": "L1", "Q_2": "Q2"}) == owner


def test_a_file_this_script_does_not_write_is_not_carried(tmp_path, monkeypatch):
    (tmp_path / RID).mkdir()
    (tmp_path / RID / "notes.txt").write_text("not ours")
    (tmp_path / RID / "report.pdf").write_bytes(b"%PDF old")
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 0 and [c["leg"] for c in m["reports"][0]["carried"]] == ["pdf"]
    assert not (tmp_path / RID / "notes.txt").exists()


# ---------------------------------------------------------------- round six: a viewer still working gets one more try


def test_a_viewer_still_working_then_complete_is_ok_after_one_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(cco, "VIEWER_WAIT_S", 0.0)
    cap, rec, sleeps = run_viewer(tmp_path, [viewer_page("working"), viewer_page("complete")])
    assert rec["status"] == "ok" and rec["retries"] == 1 and sleeps == [cco.BACKOFF_S[0]]
    assert rec["retry_reasons"][0].startswith(cco.INCOMPLETE)


def test_the_viewer_retry_shares_the_attempt_budget_so_the_deadline_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(cco, "VIEWER_WAIT_S", 0.0)
    busy = Resp(status=503, body="busy", ctype="text/html")
    cap, rec, _ = run_viewer(tmp_path, [busy, busy, viewer_page("working"), viewer_page("complete")])
    gets = [p for _, p in cap.client.calls if "b_action=cognosViewer" in p]
    assert rec["status"] == "failed" and rec["class"] == cco.INCOMPLETE and len(gets) == cco.MAX_ATTEMPTS


# ---------------------------------------------------------------- round six: the store keeps and names (A2, B, HK1, S*)


def stale_incoming(tmp_path, finished=None):
    folder = tmp_path / f"{RID}.incoming"
    folder.mkdir()
    (folder / "object.json").write_bytes(b'{"stale": 1}')
    stamp = {"id": RID, "captured_at": "2026-01-01T00:00:00+00:00"}
    if finished is not None:
        stamp["kept_aside"] = {"finished": finished, "class": "store_error"}
    (folder / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    return folder


def test_a_second_interrupt_while_an_older_incoming_moves_aside_keeps_every_capture_and_a_true_entry(
    tmp_path, monkeypatch
):
    # A2: Ctrl+C in the rename moving the new capture in, then again while the stale .incoming moves aside
    before = old_capture(tmp_path)
    stale_incoming(tmp_path)
    real, final, hits = Path.rename, (tmp_path / RID).resolve(), []

    def rename(self, target):
        src, dst = Path(self).resolve(), Path(target).resolve()
        if src.parent.name == ".staging" and dst == final:
            raise KeyboardInterrupt
        if src.name == f"{RID}.incoming" and dst.parent.name == ".trash" and not hits:
            hits.append(1)
            raise KeyboardInterrupt
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    assert code == 130 and hits and {p.name: p.read_bytes() for p in (tmp_path / RID).iterdir()} == before
    no_listed_file_is_missing(tmp_path, entry)
    assert entry["legs"][-1]["leg"] == "store" and entry["stored_as"] == f"{RID}.incoming"
    for name in ("spec.xml", "lineage.json", "parts/List1.json"):
        assert (tmp_path / f"{RID}.incoming" / name).is_file(), name
    earlier = tmp_path / entry["earlier_incoming"]["path"]
    assert (earlier / "object.json").read_bytes() == b'{"stale": 1}'


def test_an_interrupt_in_the_keep_backoff_on_the_store_error_path_is_raised(tmp_path, monkeypatch):
    # S5: the new capture cannot move in (store error) and Ctrl+C lands while it is kept aside
    before = old_capture(tmp_path)
    fail = {"new-in": 99, "keep": 1}
    rename_steps(monkeypatch, tmp_path, fail)

    def sleep(s):
        if fail["keep"] == 0:
            raise KeyboardInterrupt

    cap = cco.Capturer(FakeClient(report_server()), tmp_path, [], "auto", sleep=sleep)
    reports: list = []
    with pytest.raises(KeyboardInterrupt):
        cap.capture_report(RID, "report-id", reports)
    [entry] = reports
    assert {p.name: p.read_bytes() for p in (tmp_path / RID).iterdir()} == before
    assert entry["legs"][-1]["class"] == cco.STORE_ERROR and entry["stored_as"] == f"{RID}.incoming"
    no_listed_file_is_missing(tmp_path, entry)


def test_an_older_incoming_that_cannot_move_aside_is_never_credited_as_this_run(tmp_path, monkeypatch):
    # S10, S15: the new capture cannot move in, and the stale .incoming blocks keeping it aside
    before = old_capture(tmp_path)
    stale = stale_incoming(tmp_path)
    real, final = Path.rename, (tmp_path / RID).resolve()

    def rename(self, target):
        src, dst = Path(self).resolve(), Path(target).resolve()
        if src == stale.resolve() or (src.parent.name == ".staging" and dst == final):
            raise PermissionError(13, "in use")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    cap = cco.Capturer(FakeClient(report_server()), tmp_path, [], "auto", sleep=lambda s: None)
    entry = cap.capture_report(RID, "report-id", [])
    assert {p.name: p.read_bytes() for p in (tmp_path / RID).iterdir()} == before
    assert (stale / "object.json").read_bytes() == b'{"stale": 1}'
    assert entry["stored_as"] is None and "could not be moved aside" in entry["stale_incoming"]
    assert all(r.get("path") is None for leg in entry["legs"] for r in cco.files_of(leg))
    no_listed_file_is_missing(tmp_path, entry)


def test_an_interrupt_just_after_the_old_capture_was_put_back_names_no_missing_folder(tmp_path, monkeypatch):
    # S11
    before = old_capture(tmp_path)
    rename_steps(monkeypatch, tmp_path, {"new-in": 99}, raise_after="restore")
    cap = cco.Capturer(FakeClient(report_server()), tmp_path, [], "auto", sleep=lambda s: None)
    reports: list = []
    with pytest.raises(KeyboardInterrupt):
        cap.capture_report(RID, "report-id", reports)
    [entry] = reports
    assert "previous_capture" not in entry and not cap.kept_trash
    assert {p.name: p.read_bytes() for p in (tmp_path / RID).iterdir()} == before
    no_listed_file_is_missing(tmp_path, entry)


def test_a_crash_in_the_keep_rename_leaves_no_stale_path(tmp_path, monkeypatch):
    # S27
    old_capture(tmp_path)
    real = Path.rename

    def rename(self, target):
        if Path(self).parent.name == ".staging":
            raise RuntimeError("disk gone")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    cap = cco.Capturer(FakeClient(report_server()), tmp_path, [], "auto", sleep=lambda s: None)
    reports: list = []
    with pytest.raises(RuntimeError):
        cap.capture_report(RID, "report-id", reports)
    [entry] = reports
    assert entry["stored_as"] is None and entry["legs"][-1]["leg"] == "store"
    assert all(r.get("path") is None for leg in entry["legs"] for r in cco.files_of(leg))
    no_listed_file_is_missing(tmp_path, entry)


def leftovers(tmp_path):
    """An old capture set aside by a run killed between the swap renames, its staging, and an older .incoming."""
    aside, stage, earlier = (
        tmp_path / ".trash" / f"{RID}-deadbeef",
        tmp_path / ".staging" / RID,
        tmp_path / ".trash" / f"{RID}.incoming-cafebabe",
    )
    for folder, at, pdf in ((aside, "t1", b"%PDF a"), (stage, "t2", b"%PDF b"), (earlier, "t3", b"%PDF c")):
        stamped_capture(folder, at, pdf)
    stamp = json.loads((earlier / "capture.json").read_text(encoding="utf-8"))
    stamp["kept_aside"] = {"finished": True, "class": "store_error"}
    (earlier / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    return {p: outputs(p) for p in (aside, stage, earlier)}


def assert_leftovers_kept_and_named(tmp_path, m, kept):
    for folder, files in kept.items():
        assert outputs(folder) == files, folder
    others = {o["folder"]: o["note"] for o in m["other_captures"]}
    assert "missing, so this may be the only copy" in others[f".trash/{RID}-deadbeef"]
    assert "set aside" in others[f".trash/{RID}-deadbeef"]
    assert "killed" in others[f".staging/{RID}"]
    assert "finished" in others[f".trash/{RID}.incoming-cafebabe"]


def test_a_run_of_another_report_keeps_and_names_earlier_leftovers(tmp_path, monkeypatch):
    # B1, HK1
    kept = leftovers(tmp_path)
    code, m = run_main(tmp_path, monkeypatch, two_report_server([SID]))
    assert code == 0 and m["status"] == "ok" and (tmp_path / SID).is_dir()
    assert_leftovers_kept_and_named(tmp_path, m, kept)


def test_a_run_stopped_at_login_keeps_and_names_earlier_leftovers(tmp_path, monkeypatch):
    # B1
    kept = leftovers(tmp_path)

    class Refused(FakeClient):
        def login(self):
            raise cco.Stop("login refused")

    monkeypatch.setattr(cco, "Client", lambda *a, **k: Refused(fake_server()))
    monkeypatch.setenv("COGNOS_API_KEY", "not-a-real-key")
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(tmp_path), "--report-id", RID]
    code = cco.main(argv)
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert code == 2 and m["status"] == "stopped"
    assert_leftovers_kept_and_named(tmp_path, m, kept)


@pytest.mark.parametrize(("finished", "word"), [(True, "finished"), (False, "interrupted"), (None, "interrupted")])
def test_an_incoming_of_another_report_is_named_for_what_it_is(tmp_path, monkeypatch, finished, word):
    # B2: a complete capture not swapped in is not "an interrupted capture"
    stale_incoming(tmp_path, finished)
    code, m = run_main(tmp_path, monkeypatch, two_report_server([SID]))
    [other] = [o for o in m["other_captures"] if o["folder"] == f"{RID}.incoming"]
    assert code == 0 and word in other["note"] and (tmp_path / f"{RID}.incoming").is_dir()
    if finished:
        assert "interrupted" not in other["note"]


def test_a_capture_of_the_same_id_moves_its_killed_staging_aside_and_names_it(tmp_path, monkeypatch, capsys):
    kept = leftovers(tmp_path)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    moved = tmp_path / entry["earlier_staging"]["path"]
    assert code == 0 and outputs(moved) == kept[tmp_path / ".staging" / RID]
    assert f"KEPT: {RID} earlier_staging in {entry['earlier_staging']['path']}" in capsys.readouterr().err
    assert entry["earlier_staging"]["path"] not in {o["folder"] for o in m["other_captures"]}
    assert outputs(tmp_path / ".trash" / f"{RID}-deadbeef") == kept[tmp_path / ".trash" / f"{RID}-deadbeef"]


def test_an_interrupt_after_the_swap_says_the_old_capture_was_replaced(tmp_path, monkeypatch, capsys):
    # A1
    old_capture(tmp_path)
    real, hits = cco.shutil.rmtree, []

    def rmtree(path, *a, **k):
        if Path(path).name.startswith(f"{RID}-") and not hits:
            hits.append(1)
            raise KeyboardInterrupt
        return real(path, *a, **k)

    monkeypatch.setattr(cco.shutil, "rmtree", rmtree)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    err = capsys.readouterr().err
    assert code == 130 and hits and f"INTERRUPTED: {RID} swapped in, replacing the old capture" in err
    assert "untouched" not in err
    no_listed_file_is_missing(tmp_path, m["reports"][0])


def test_an_old_capture_not_put_back_is_named_on_stderr_on_the_store_error_path(tmp_path, monkeypatch, capsys):
    # G1
    old_capture(tmp_path)
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    rename_steps(monkeypatch, tmp_path, {"new-in": 99, "restore": 99, "previous": 99})
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    path = entry["previous_capture"]["path"]
    assert code == 1 and (tmp_path / path / "report.pdf").read_bytes() == b"old pdf"
    assert f"KEPT: {RID} previous_capture in {path}" in capsys.readouterr().err


# ---------------------------------------------------------------- round six: data tables (E1, L9, L12)


UNNAMED_LC_SPEC = LC_SPEC.replace('name="Data table2" ', "")


def test_an_unnamed_data_table_is_a_data_table_and_never_points_at_the_pdf(tmp_path, monkeypatch):
    handler = with_viewer(fake_server(spec=UNNAMED_LC_SPEC), viewer_page("complete"))
    code, m = run_rid(tmp_path, monkeypatch, handler, "pdf")
    [entry] = m["reports"]
    [label] = [p["label"] for p in entry["parts"] if p["tag"] == "listControl"]
    legs = [leg for leg in entry["legs"] if leg.get("part") == label]
    assert code == 0 and label and len(legs) == 2
    for leg in legs:
        assert "Data table rendering is not supported" in leg["reason"] and "report.pdf, captured" not in leg["reason"]
    pdf = next(leg for leg in entry["legs"] if leg["leg"] == "pdf")
    assert pdf["omits"]["parts"] == [label]


def test_a_failed_pdf_names_no_omission(tmp_path, monkeypatch):
    handler = with_viewer(fake_server(spec=LC_SPEC), Resp(status=404))
    code, m = run_rid(tmp_path, monkeypatch, handler, "pdf")
    pdf = next(leg for leg in m["reports"][0]["legs"] if leg["leg"] == "pdf")
    assert code == 1 and pdf["status"] == "failed" and "omits" not in pdf


def test_the_data_table_reason_is_consistent_with_what_was_measured():
    reason = cco.list_control_reason({"tag": "listControl", "refQuery": "qKPI"})
    assert "no headless route" not in reason and "unmeasured" in reason and "qKPI" in reason


# ---------------------------------------------------------------- round six: redaction residuals (D, K)


AMP_KEY = "Ab&C'D-0d4e"


@pytest.mark.parametrize("form", ["Ab&amp;C'D-0d4e", "Ab&C\\'D-0d4e"])
def test_the_amp_and_repr_forms_of_the_key_are_redacted(tmp_path, form):
    # K8, K9
    body, rec = stored(tmp_path, "parts/List1.html", f"<td>{form}</td>".encode(), "text/html", AMP_KEY)
    assert body == b"<td><key></td>" and rec["redacted"] == 1


def test_a_csv_served_as_octet_stream_is_redacted(tmp_path):
    # K14b
    body, rec = stored(tmp_path, "report.csv", f"a,{ODD_KEY}\n".encode(), "application/octet-stream")
    assert body == b"a,<key>\n" and rec["redacted"] == 1


@pytest.mark.parametrize(
    ("ctype", "redacted"),
    [("application/json", True), ("image/svg", True), ("application/octet-stream", False)],
)
def test_an_unknown_extension_is_text_only_by_its_content_type(tmp_path, ctype, redacted):
    # K22, D4: any svg content type is text, "+xml" or not
    raw = json.dumps({"v": ODD_KEY}).encode()
    body, _ = stored(tmp_path, "parts/x.dat", raw, ctype)
    assert (body != raw) is redacted


@pytest.mark.parametrize("enc", ["utf-16-le", "utf-16-be"])
def test_a_bomless_utf16_cjk_csv_without_a_charset_is_redacted(tmp_path, enc):
    # D1, K11, K12: no BOM, no charset, few zero bytes; every view is searched for the key
    text = "\u5730\u533a" * 40 + f"\t{KEY}\r\n"
    body, rec = stored(tmp_path, "report.csv", text.encode(enc), "", KEY)
    assert body.decode(enc) == text.replace(KEY, "<key>") and rec["redacted"] == 1


def test_an_undecodable_text_body_is_redacted_through_latin1(tmp_path):
    # K13: invalid UTF-8 at an odd length, so neither UTF-8 nor UTF-16 decodes it
    raw = b"\xe9 v=" + KEY.encode() + b" "
    raw += b"" if len(raw) % 2 else b" "
    body, rec = stored(tmp_path, "parts/List1.html", raw, "text/html", KEY)
    assert body == raw.replace(KEY.encode(), b"<key>") and rec["redacted"] == 1


def test_an_svg_image_is_stored_as_svg_and_redacted(tmp_path):
    # D4: image/svg without +xml
    def handler(kind, path):
        if "fmt=HTMLFragment" in path:
            return Resp(body=f'<div><img src="https://{HOST}/bi/v1/disp?SM=query&amp;n=1"/></div>', ctype="text/xml")
        return Resp(body=f"<svg><text>{KEY}</text></svg>", ctype="image/svg")

    cap = cco.Capturer(KeyedClient(handler, KEY), tmp_path, [], "auto", sleep=lambda s: None)
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    [img] = rec["images"]
    assert rec["status"] == "ok" and img["path"].endswith(".svg")
    assert (tmp_path / "parts" / Path(img["path"]).name).read_bytes() == b"<svg><text><key></text></svg>"


def test_a_key_echoed_across_the_reason_cut_leaves_no_prefix(tmp_path):
    # D2: the snippet is cut at 200 characters; the key straddles the cut
    cco.Capturer(KeyedClient(lambda k, p: Resp(404), KEY), tmp_path, [], "auto", sleep=lambda s: None)
    _, reason = cco.classify(400, "RDS-ERR-1000 " + "y" * 180 + KEY)
    assert reason.startswith("RDS-ERR-1000 y") and "SECRET" not in reason


def test_a_crash_message_is_redacted_before_it_is_cut(tmp_path, monkeypatch):
    # K28: "RuntimeError: " + 276 characters puts the key across the 300-character cut
    base = fake_server()

    def handler(kind, path):
        if "fields=specification" in path:
            raise RuntimeError("x" * 276 + "not-a-real-key")
        return base(kind, path)

    code, m = run_rid(tmp_path, monkeypatch, handler)
    assert code == 3 and m["crash"].endswith("<key>") and "not-a" not in m["crash"]


def test_a_502_with_a_cognos_code_is_retried():
    # C1
    assert cco.classify(502, "XQE-GEN-0002 the gateway failed")[0] == cco.TRANSIENT


def test_a_reason_naming_two_lost_outputs_is_rewritten():
    # NP5
    reason = "unnamed list: its values are in report.pdf and report.xlsx, captured on this run"
    assert cco.drop_viewer_pointers(reason) == "unnamed list: no output captured on this run holds its values"


def test_the_viz_csv_note_is_dropped_once_the_csv_is_lost():
    # NP6
    assert cco.drop_viewer_pointers(f"chart: {cco.VIZ_CSV_NOTE}query Query1") == "chart: query Query1"


# ---------------------------------------------------------------- round seven: provenance of ok outputs (F1, F2)


def ticking(monkeypatch):
    ticks = iter(range(10000))
    monkeypatch.setattr(cco, "now", lambda: f"2026-01-01T00:{next(ticks):04d}+00:00")


def lineage_files(folder):
    return {n: (folder / n).read_bytes() for n in ("lineage.json", "module.json")}


def no_modules(base, answer=None):
    def handler(kind, path):
        if path == "/modules":
            return answer or Resp(body=json.dumps({"modules": []}))
        return base(kind, path)

    return handler


def test_a_rerun_with_an_empty_module_listing_keeps_both_lineage_files_and_names_them(tmp_path, monkeypatch):
    # F1, CF2: a failed lineage leg writes nothing, so the earlier lineage.json and module.json are carried
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    assert code == 0
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    [entry] = m["reports"]
    lineage = next(leg for leg in entry["legs"] if leg["leg"] == "lineage")
    assert code == 1 and lineage["status"] == "failed" and lineage["class"] == cco.NOT_FOUND
    assert not lineage.get("path") and lineage_files(tmp_path / RID) == before
    carried = {c["path"]: c for c in entry["carried"]}
    assert set(carried) == {f"{RID}/lineage.json", f"{RID}/module.json"}
    assert {c["leg"] for c in carried.values()} == {"lineage"}
    no_listed_file_is_missing(tmp_path, entry)


def test_a_newer_legacy_incoming_holding_lineage_without_its_module_is_not_carried(tmp_path, monkeypatch):
    # F2a: an .incoming written without an ok_files record (older script) whose lineage names a missing module.json
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    incoming = tmp_path / f"{RID}.incoming"
    incoming.mkdir()
    (incoming / "lineage.json").write_text(json.dumps({"model": {"metadata": "module.json"}}), encoding="utf-8")
    stamp = {"id": RID, "captured_at": "2026-01-01T00:9000+00:00"}
    (incoming / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server(), Resp(404)))
    [entry] = m["reports"]
    assert code == 1 and lineage_files(tmp_path / RID) == before
    assert {c["from"] for c in entry["carried"] if c["leg"] == "lineage"} == {RID}
    no_listed_file_is_missing(tmp_path, entry)


def test_a_render_cut_between_its_html_and_its_image_is_never_carried_over_a_complete_one(tmp_path, monkeypatch):
    # F2b: Ctrl+C after List1.html was stored and before List1_0.png, then a run whose fragment fails
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = {k: v for k, v in outputs(tmp_path / RID).items() if k.startswith("parts/List1")}
    assert set(before) == {"parts/List1.json", "parts/List1.html", "parts/List1_0.png"}
    real = cco.Capturer.store

    def store(self, rec, stage, rel, body, ct):
        if rel == "parts/List1_0.png":
            raise KeyboardInterrupt
        return real(self, rec, stage, rel, body, ct)

    monkeypatch.setattr(cco.Capturer, "store", store)
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    monkeypatch.setattr(cco.Capturer, "store", real)
    incoming = tmp_path / f"{RID}.incoming"
    assert code == 130 and (incoming / "parts" / "List1.html").is_file()
    assert not (incoming / "parts" / "List1_0.png").exists()
    base = fake_server()

    def handler(kind, path):
        return Resp(404) if "fmt=HTMLFragment" in path and "List1" in path else base(kind, path)

    code, m = run_rid(tmp_path, monkeypatch, handler)
    [entry] = m["reports"]
    after = {k: v for k, v in outputs(tmp_path / RID).items() if k.startswith("parts/List1")}
    assert code == 1 and after == before
    froms = {c["path"]: c["from"] for c in entry["carried"]}
    assert froms == {f"{RID}/parts/List1.html": RID, f"{RID}/parts/List1_0.png": RID}
    no_listed_file_is_missing(tmp_path, entry)


def test_a_group_whose_carry_copy_failed_halfway_is_never_carried_later(tmp_path, monkeypatch):
    # F2c: run 2 keeps an .incoming holding lineage.json only (its copy of module.json failed); run 3 fails all
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    real, calls = cco.shutil.copyfile, []

    def copyfile(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError(13, "in use")
        return real(src, dst)

    monkeypatch.setattr(cco.shutil, "copyfile", copyfile)
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    monkeypatch.setattr(cco.shutil, "copyfile", real)
    incoming = tmp_path / f"{RID}.incoming"
    assert code == 1 and (incoming / "lineage.json").is_file() and not (incoming / "module.json").exists()
    code, m = run_rid(tmp_path, monkeypatch, lambda kind, path: Resp(404))
    [entry] = m["reports"]
    assert code == 1 and lineage_files(tmp_path / RID) == before
    assert {c["from"] for c in entry["carried"] if c["leg"] == "lineage"} == {RID}
    no_listed_file_is_missing(tmp_path, entry)


def test_a_recorded_group_whose_file_changed_is_not_carried(tmp_path, monkeypatch):
    # the ok_files record is checked by hash: a file edited after the capture is not credited as that output
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), "pdf")
    (tmp_path / RID / "report.pdf").write_bytes(b"%PDF edited")
    code, m = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), Resp(404)), "pdf")
    assert code == 1 and not [c for c in m["reports"][0].get("carried", []) if c["leg"] == "pdf"]


def test_the_only_earlier_pdf_in_a_previous_folder_is_carried(tmp_path, monkeypatch):
    # CF11
    stamped_capture(tmp_path / f"{RID}.previous", "2026-01-01T00:00:00+00:00", b"%PDF kept")
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    [kept] = entry["carried"]
    assert code == 0 and (tmp_path / RID / "report.pdf").read_bytes() == b"%PDF kept"
    assert kept["from"] == f"{RID}.previous" and kept["path"] == f"{RID}/report.pdf"
    assert [s["folder"] for s in entry["superseded"]] == [f"{RID}.previous"]
    assert "carried" in entry["superseded"][0]["note"]


def test_a_lost_capture_names_no_carried_file(tmp_path, monkeypatch):
    # KI2: the carried copies were in the staging folder that could not be kept
    old_capture(tmp_path)
    every_staging_rename_fails(monkeypatch)
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    assert code == 1 and entry["stored_as"] is None and "carried" not in entry


def test_a_capture_kept_aside_names_its_carried_copies_where_they_are(tmp_path, monkeypatch, capsys):
    # KI4, KI5: the newer pdf is carried from an .incoming, then the swap fails; the carried path follows the copy
    stamped_capture(tmp_path / RID, "2026-01-01T00:00:00+00:00", b"%PDF old")
    stamped_capture(tmp_path / f"{RID}.incoming", "2026-06-01T00:00:00+00:00", b"%PDF newer")
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    rename_steps(monkeypatch, tmp_path, {"new-in": 99})
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    [kept] = entry["carried"]
    assert code == 1 and entry["stored_as"] == f"{RID}.incoming"
    assert kept["path"] == f"{RID}.incoming/report.pdf" and kept["from"] == f"{RID}.incoming"
    assert (tmp_path / RID / "report.pdf").read_bytes() == b"%PDF old"
    no_listed_file_is_missing(tmp_path, entry)
    monkeypatch.undo()
    code, m = run_main(tmp_path, monkeypatch, two_report_server([SID]))
    [other] = [o for o in m["other_captures"] if o["folder"] == f"{RID}.incoming"]
    assert code == 0 and other["note"].startswith("a finished capture not swapped in")


# ---------------------------------------------------------------- round seven: leftovers named, cleanup, wording


def test_a_previous_folder_of_a_run_that_stops_without_a_swap_is_named(tmp_path, monkeypatch, capsys):
    # F3
    stamped_capture(tmp_path / f"{RID}.previous", "2026-01-01T00:00:00+00:00", b"%PDF kept")
    code, m = run_main(tmp_path, monkeypatch, cut_short("stop"), formats="pdf", ids=["--report-id", RID])
    err = capsys.readouterr().err
    assert code == 2 and (tmp_path / f"{RID}.previous" / "report.pdf").read_bytes() == b"%PDF kept"
    [other] = [o for o in m["other_captures"] if o["folder"] == f"{RID}.previous"]
    assert other["id"] == RID and "could not put back" in other["note"]
    assert f"KEPT: {RID}.previous" in err
    assert f"{RID}.incoming" not in {o["folder"] for o in m["other_captures"]}  # this run's own capture


def test_a_ctrl_c_during_the_final_cleanup_still_writes_a_finished_manifest(tmp_path, monkeypatch, capsys):
    # F4: the staged capture could not be moved, and Ctrl+C lands in the cleanup that removes it
    every_staging_rename_fails(monkeypatch)
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    real, hits = cco.shutil.rmtree, []

    def rmtree(path, *a, **k):
        if Path(path).name == RID and Path(path).parent.name == ".staging" and not hits:
            hits.append(1)
            raise KeyboardInterrupt
        return real(path, *a, **k)

    monkeypatch.setattr(cco.shutil, "rmtree", rmtree)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 130 and hits and m["status"] == "interrupted" and m["finished_at"]
    assert not (tmp_path / ".staging").exists()
    assert "INTERRUPTED" in capsys.readouterr().err


@pytest.mark.parametrize("old", [True, False])
def test_an_interrupt_just_after_the_swap_rename_names_what_happened(tmp_path, monkeypatch, capsys, old):
    # CS1, CS2
    if old:
        old_capture(tmp_path)
    rename_steps(monkeypatch, tmp_path, {}, raise_after="new-in")
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    err = capsys.readouterr().err
    want = f"INTERRUPTED: {RID} swapped in, replacing the old capture" if old else f"INTERRUPTED: {RID} written"
    assert code == 130 and want in err and m["reports"][0]["stored_as"] == RID


@pytest.mark.parametrize(
    ("entry", "want"),
    [
        ({"stored_as": RID, "replaced_old": True}, f"{RID} swapped in, replacing the old capture"),
        ({"stored_as": RID, "replaced_old": False}, f"{RID} written"),
        ({"stored_as": f"{RID}.incoming"}, f"{RID}: new capture kept as {RID}.incoming; {RID} is as it was"),
        ({"stored_as": None, "previous_capture": {"path": f"{RID}.previous"}}, f"{RID}: new capture lost; its old"),
        ({}, None),
    ],
)
def test_capture_state_names_each_outcome(entry, want):
    got = cco.capture_state({"id": RID, **entry})
    assert got == want if want is None else got.startswith(want)
    if "previous_capture" in entry:
        assert got.endswith(f"its old capture is kept as {RID}.previous")


def test_an_aside_old_capture_does_not_claim_its_report_folder_holds_its_outputs(tmp_path, monkeypatch):
    # F6
    (tmp_path / RID).mkdir()
    stamped_capture(tmp_path / ".trash" / f"{RID}-deadbeef", "t1", b"%PDF a")
    earlier = tmp_path / ".trash" / f"{RID}.incoming-cafebabe"
    stamped_capture(earlier, "t3", b"%PDF c")
    stamp = json.loads((earlier / "capture.json").read_text(encoding="utf-8"))
    stamp["kept_aside"] = {"finished": True, "class": "store_error"}
    (earlier / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    found = {o["folder"]: o["note"] for o in cco.other_captures(tmp_path, set())}
    aside = found[f".trash/{RID}-deadbeef"]
    assert f"{RID} exists" not in aside and "only this folder holds" in aside
    assert "an earlier a" not in found[f".trash/{RID}.incoming-cafebabe"]
    assert found[f".trash/{RID}.incoming-cafebabe"].startswith("a finished capture not swapped in")


# ---------------------------------------------------------------- round seven: redaction (F5, F8)


@pytest.mark.parametrize("enc", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("bom", [True, False])
def test_an_odd_length_utf16_body_is_redacted(tmp_path, enc, bom):
    # F5: a truncated UTF-16 body ends in half a code unit; the rest is still searched and the tail kept
    mark = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}[enc] if bom else b""
    text = f"Region\tName\r\nNorth\t{KEY}\r\n"
    raw = mark + text.encode(enc) + b"\x41"
    body, rec = stored(tmp_path, "report.csv", raw, "", KEY)
    assert rec["redacted"] == 1 and body.startswith(mark) and body.endswith(b"\x41")
    assert body[len(mark) : -1].decode(enc) == text.replace(KEY, "<key>")


def test_a_fragment_error_echoing_the_key_across_its_cut_leaves_no_prefix(tmp_path):
    # F8, D1b: fragment_error cuts the text at 120 characters; the key straddles the cut
    text = "RSV-SRV-0042 " + "y" * 97 + KEY
    assert text.index(KEY) < 120 < text.index(KEY) + len(KEY)

    def handler(kind, path):
        return Resp(body=f"<div><table><tr><td>{text}</td></tr></table></div>", ctype="text/xml")

    cap = cco.Capturer(KeyedClient(handler, KEY), tmp_path, [], "auto", sleep=lambda s: None)
    rec = run_part(cap, tmp_path, cap.get_part_render, part("List1"))
    assert rec["status"] == "refused" and "RSV-SRV-0042" in rec["reason"]
    assert "SECRET" not in rec["reason"] and "<key>" in rec["reason"]


def strip_record(folder):
    stamp = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    stamp.pop("ok_files")
    (folder / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")


def fragment_404(base):
    def handler(kind, path):
        return Resp(404) if "fmt=HTMLFragment" in path else base(kind, path)

    return handler


def test_a_carried_group_is_recorded_so_it_survives_a_second_failing_rerun(tmp_path, monkeypatch):
    # carry_forward records the groups it carried whole in ok_files, so the next run may carry them again
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    for _ in range(2):
        code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
        [entry] = m["reports"]
        assert code == 1 and lineage_files(tmp_path / RID) == before
        assert {c["path"] for c in entry["carried"]} == {f"{RID}/lineage.json", f"{RID}/module.json"}


def test_a_capture_kept_aside_records_its_carried_groups_for_the_next_run(tmp_path, monkeypatch):
    # keep_incoming merges this run's ok legs into the record carry_forward wrote; the newest copy is then used
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    real = Path.rename
    monkeypatch.setattr(cco, "RENAME_BACKOFF_S", 0.0)
    rename_steps(monkeypatch, tmp_path, {"new-in": 99})
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    assert code == 1 and m["reports"][0]["stored_as"] == f"{RID}.incoming"
    monkeypatch.setattr(Path, "rename", real)
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    [entry] = m["reports"]
    assert code == 1 and lineage_files(tmp_path / RID) == before
    assert {c["from"] for c in entry["carried"] if c["leg"] == "lineage"} == {f"{RID}.incoming"}


def test_an_ok_output_of_a_capture_cut_short_is_recorded_and_checked_by_hash(tmp_path, monkeypatch):
    # keep_incoming records the cut capture's ok legs: an .incoming file changed later is not credited, the older
    # complete one is carried instead
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    real = cco.Capturer.carry_forward

    def cut(self, *a):
        raise KeyboardInterrupt

    monkeypatch.setattr(cco.Capturer, "carry_forward", cut)
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    monkeypatch.setattr(cco.Capturer, "carry_forward", real)
    incoming = tmp_path / f"{RID}.incoming"
    assert code == 130 and (incoming / "lineage.json").is_file()
    (incoming / "lineage.json").write_text("{}", encoding="utf-8")
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    [entry] = m["reports"]
    assert code == 1 and lineage_files(tmp_path / RID) == before
    assert {c["from"] for c in entry["carried"] if c["leg"] == "lineage"} == {RID}


def test_a_complete_render_in_a_folder_without_a_record_is_carried(tmp_path, monkeypatch):
    # an older version wrote no ok_files: a render with its html and every image it links counts as complete
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    strip_record(tmp_path / RID)
    before = {k: v for k, v in outputs(tmp_path / RID).items() if k.startswith("parts/List1.") or "_0." in k}
    code, m = run_rid(tmp_path, monkeypatch, fragment_404(fake_server()))
    [entry] = m["reports"]
    after = {k: v for k, v in outputs(tmp_path / RID).items() if k in before}
    assert code == 1 and after == before and f"{RID}/parts/List1_0.png" in {c["path"] for c in entry["carried"]}
    no_listed_file_is_missing(tmp_path, entry)


@pytest.mark.parametrize("cut", ["image", "html"])
def test_a_render_missing_a_file_in_a_folder_without_a_record_is_not_carried(tmp_path, monkeypatch, cut):
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    strip_record(tmp_path / RID)
    (tmp_path / RID / "parts" / ("List1_0.png" if cut == "image" else "List1.html")).unlink()
    code, m = run_rid(tmp_path, monkeypatch, fragment_404(fake_server()))
    [entry] = m["reports"]
    assert code == 1 and not [c for c in entry.get("carried", []) if c["leg"] == "part:List1:render"]
    assert not list((tmp_path / RID / "parts").glob("List1*.html"))


def test_a_complete_lineage_in_a_folder_without_a_record_is_carried(tmp_path, monkeypatch):
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    strip_record(tmp_path / RID)
    before = lineage_files(tmp_path / RID)
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    assert code == 1 and lineage_files(tmp_path / RID) == before
    assert {c["leg"] for c in m["reports"][0]["carried"]} == {"lineage"}


def test_a_failed_leg_that_left_files_is_replaced_by_the_earlier_complete_group(tmp_path, monkeypatch):
    # no leg today fails after it stored a file; should one, its files neither block the carry nor stay behind
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = {k: v for k, v in outputs(tmp_path / RID).items() if k.startswith("parts/List1")}
    real = cco.Capturer.get_part_render

    def render(self, rid, part, stage, rec):
        if part["name"] != "List1":
            return real(self, rid, part, stage, rec)
        self.store(rec, stage, "parts/List1.html", b"<div>half</div>", "text/xml")
        sub: dict = {}
        self.store(sub, stage, "parts/List1_1.png", b"\x89PNG half", "image/png")
        rec["images"] = [sub]
        raise cco.Failure(cco.REFUSED, "cut after storing")

    monkeypatch.setattr(cco.Capturer, "get_part_render", render)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [entry] = m["reports"]
    after = {k: v for k, v in outputs(tmp_path / RID).items() if k.startswith("parts/List1")}
    assert code == 1 and after == before
    assert {c["path"] for c in entry["carried"]} == {f"{RID}/parts/List1.html", f"{RID}/parts/List1_0.png"}
    for rec in entry["legs"]:
        for r in cco.files_of(rec):
            if r.get("path"):
                f = tmp_path / r["path"]
                assert f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest() == r["sha256"], r["path"]


# ---------------------------------------------------------------- round eight: a self-describing --out


def two_reports_second_without_renders(ids):
    base = two_report_server(ids)

    def handler(kind, path):
        if SID in path and "fmt=HTMLFragment" in path:
            return Resp(404)
        return base(kind, path)

    return handler


def test_every_report_in_out_stays_described_after_a_run_of_one_report(tmp_path, monkeypatch):
    # scope gap: B's failed legs and reasons are recorded in B's own capture.json and listed in earlier_reports
    code, m = run_main(tmp_path, monkeypatch, two_reports_second_without_renders([RID, SID]))
    assert code == 1
    record = json.loads((tmp_path / SID / "capture.json").read_text(encoding="utf-8"))["report"]
    failed = {r["leg"]: r["reason"] for r in record["legs"] if r["status"] == "failed"}
    assert failed and all(leg.endswith(":render") for leg in failed) and all(failed.values())
    old = tmp_path / XID
    old.mkdir()
    (old / "capture.json").write_text(json.dumps({"id": XID, "captured_at": "t0"}), encoding="utf-8")
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    assert code == 0 and [r["id"] for r in m["reports"]] == [RID]
    earlier = {e["folder"]: e for e in m["earlier_reports"]}
    assert set(earlier) == {SID, XID}
    sid = earlier[SID]
    assert sid["recorded"] and sid["captured_at"] and sid["report"]["id"] == SID
    assert {r["leg"]: r.get("reason") for r in sid["report"]["legs"] if r["status"] == "failed"} == failed
    assert {u["leg"] for u in sid["render_unestablished"]} == set(failed)
    for r in sid["report"]["legs"]:
        for f in cco.files_of(r):
            if f.get("path"):
                assert f["path"].startswith(f"{SID}/") and (tmp_path / f["path"]).is_file()
    assert earlier[XID]["recorded"] is False and "not known to be complete" in earlier[XID]["note"]


def test_an_earlier_report_whose_ok_file_changed_is_listed_as_failed(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    (tmp_path / SID / "lineage.json").write_text("{}", encoding="utf-8")
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    lineage = next(r for r in sid["report"]["legs"] if r["leg"] == "lineage")
    assert code == 0 and lineage["status"] == "failed" and lineage["class"] == cco.STORE_ERROR
    assert lineage["path"] is None and sid["lineage_unestablished"]
    others = [r for r in sid["report"]["legs"] if r["leg"] != "lineage" and r["status"] == "ok"]
    assert others and all(r["status"] == "ok" for r in others)


def test_a_legacy_failed_lineage_without_metadata_is_not_carried(tmp_path, monkeypatch):
    # N1: an older version's .incoming whose lineage.json names no module metadata is no complete module lineage
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    incoming = tmp_path / f"{RID}.incoming"
    incoming.mkdir()
    bad = {"model": {"kind": "module", "id": None}}
    (incoming / "lineage.json").write_text(json.dumps(bad), encoding="utf-8")
    stamp = {"id": RID, "captured_at": "2026-01-01T00:9000+00:00"}
    (incoming / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    [entry] = m["reports"]
    assert code == 1 and lineage_files(tmp_path / RID) == before
    assert {c["from"] for c in entry["carried"] if c["leg"] == "lineage"} == {RID}


@pytest.mark.parametrize("record", ["partial", "other-leg"])
def test_a_corrupt_record_never_carries_a_partial_or_misfiled_lineage(tmp_path, monkeypatch, record):
    # N2: the record's leg key and the group's completeness are both checked
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    before = lineage_files(tmp_path / RID)
    incoming = tmp_path / f"{RID}.incoming"
    incoming.mkdir()
    lineage = json.dumps({"model": {"kind": "module", "id": MID, "metadata": "module.json", "x": 1}}).encode()
    (incoming / "lineage.json").write_bytes(lineage)
    files = {"lineage.json": hashlib.sha256(lineage).hexdigest()}
    if record == "other-leg":
        (incoming / "module.json").write_bytes(b"{}")
        files["module.json"] = hashlib.sha256(b"{}").hexdigest()
    leg = "lineage" if record == "partial" else "pdf"
    stamp = {"id": RID, "captured_at": "2026-01-01T00:9000+00:00", "ok_files": {leg: files}}
    (incoming / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    [entry] = m["reports"]
    assert code == 1 and lineage_files(tmp_path / RID) == before
    assert {c["from"] for c in entry["carried"]} == {RID}


def test_a_swap_names_every_file_it_deletes_without_a_replacement(tmp_path, monkeypatch):
    # N3: a file this script does not write and an ok output edited after its capture are named in dropped
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), "pdf")
    (tmp_path / RID / "notes.txt").write_text("mine", encoding="utf-8")
    (tmp_path / RID / "report.pdf").write_bytes(b"%PDF edited")
    code, m = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), Resp(404)), "pdf")
    [entry] = m["reports"]
    dropped = {d["path"]: d for d in entry["dropped"]}
    assert code == 1 and set(dropped) == {"notes.txt", "report.pdf"}
    assert dropped["notes.txt"]["why"] == "not a file this script writes" and dropped["notes.txt"]["folder"] == RID
    assert dropped["report.pdf"]["sha256"] == hashlib.sha256(b"%PDF edited").hexdigest()
    assert "changed since" in dropped["report.pdf"]["why"]
    record = json.loads((tmp_path / RID / "capture.json").read_text(encoding="utf-8"))["report"]
    assert record["dropped"] == entry["dropped"]


def test_a_renamed_container_keeps_its_old_outputs_marked_outside_the_run(tmp_path, monkeypatch):
    # N8: kept (never lose a good output), but never mistaken for an output of this run
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    code, m = run_rid(tmp_path, monkeypatch, fake_server(spec=SPEC.replace("List1", "ListX")))
    [entry] = m["reports"]
    carried = {c["path"]: c for c in entry["carried"]}
    old = {f"{RID}/parts/List1.json", f"{RID}/parts/List1.html", f"{RID}/parts/List1_0.png"}
    assert old <= set(carried) and all((tmp_path / p).is_file() for p in old)
    assert all("renamed or removed" in carried[p]["outside_run"] for p in old)


def test_a_format_not_requested_is_carried_marked_outside_the_run(tmp_path, monkeypatch):
    # N8
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, all_viewers(fake_server()), "pdf,csv")
    code, m = run_rid(tmp_path, monkeypatch, all_viewers(fake_server()), "pdf")
    [entry] = m["reports"]
    [csv] = [c for c in entry["carried"] if c["leg"] == "csv"]
    assert code == 0 and csv["outside_run"] == "outside this run's scope: this format was not requested on this run"
    assert not [c for c in entry["carried"] if c["leg"] != "csv" and "outside_run" not in c]


def test_a_failed_leg_of_this_run_is_carried_without_an_outside_run_mark(tmp_path, monkeypatch):
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    assert code == 1 and m["reports"][0]["carried"]
    assert not [c for c in m["reports"][0]["carried"] if "outside_run" in c]


# ---------------------------------------------------------------- round eight: nothing escapes the cleanup


class RaisingStderr(io.StringIO):
    def __init__(self, marker, exc=KeyboardInterrupt):
        super().__init__()
        self.marker, self.exc, self.hits = marker, exc, 0

    def write(self, s):
        if self.marker in s:
            self.hits += 1
            raise self.exc
        return super().write(s)


def test_a_ctrl_c_during_a_kept_line_exits_130_with_a_finished_manifest(tmp_path, monkeypatch):
    # N4
    stamped_capture(tmp_path / f"{SID}.previous", "2026-01-01T00:00:00+00:00", b"%PDF kept")
    err = RaisingStderr("KEPT:")
    monkeypatch.setattr(sys, "stderr", err)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    assert err.hits and code == 130 and m["status"] == "interrupted" and m["finished_at"]
    assert "during the final cleanup" in err.getvalue()


def test_a_manifest_that_cannot_be_written_at_the_end_is_a_crash(tmp_path, monkeypatch, capsys):
    # N5
    real = cco.write_json

    def write_json(path, data, key):
        if path.name == "manifest.json" and data.get("finished_at"):
            raise PermissionError(13, "in use")
        return real(path, data, key)

    monkeypatch.setattr(cco, "write_json", write_json)
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    err = capsys.readouterr().err
    assert code == 3 and "CRASHED: manifest.json not written" in err
    assert "CRASHED: manifest.json may not be final" in err


def test_a_ctrl_c_during_the_crash_traceback_leaks_no_key_and_escapes_nothing(tmp_path, monkeypatch, capsys):
    # N6: the crash message holds the key; the traceback print is cut short
    base = fake_server()

    def handler(kind, path):
        if "fields=specification" in path:
            raise RuntimeError("boom not-a-real-key")
        return base(kind, path)

    err = RaisingStderr("Traceback")
    monkeypatch.setattr(sys, "stderr", err)
    code, m = run_rid(tmp_path, monkeypatch, handler)
    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    assert err.hits and code == 130 and m["finished_at"] and m["crash"].startswith("RuntimeError: boom")
    assert "not-a-real-key" not in text + err.getvalue() + capsys.readouterr().out


@pytest.mark.parametrize("times", [1, 3])
def test_a_ctrl_c_in_the_final_manifest_write_is_never_lost(tmp_path, monkeypatch, capsys, times):
    # N7, SH4: an interrupt in the final write exits 130; once, the manifest written again is final and says so
    real, hits = cco.write_json, []

    def write_json(path, data, key):
        if path.name == "manifest.json" and data.get("finished_at") and len(hits) < times:
            hits.append(1)
            raise KeyboardInterrupt
        return real(path, data, key)

    monkeypatch.setattr(cco, "write_json", write_json)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    out = capsys.readouterr()
    assert code == 130 and len(hits) == times and not out.out.startswith("ok:")
    if times == 1:
        assert m["finished_at"] and m["status"] == "interrupted"
    else:
        assert "manifest.json may not be final" in out.err


def test_a_ctrl_c_in_logout_exits_130(tmp_path, monkeypatch):
    # SH3
    def logout(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeClient, "logout", logout)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 130 and m["status"] == "interrupted" and m["finished_at"]


# ---------------------------------------------------------------- round eight: survivors of round seven


def cut_carry(monkeypatch):
    real = cco.Capturer.carry_forward

    def cut(self, *a):
        raise KeyboardInterrupt

    monkeypatch.setattr(cco.Capturer, "carry_forward", cut)
    return lambda: monkeypatch.setattr(cco.Capturer, "carry_forward", real)


def test_the_only_ok_pdf_of_a_first_capture_cut_short_survives_a_failing_rerun(tmp_path, monkeypatch):
    # MK3
    ticking(monkeypatch)
    restore = cut_carry(monkeypatch)
    code, _ = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), "pdf")
    restore()
    assert code == 130 and (tmp_path / f"{RID}.incoming" / "report.pdf").is_file() and not (tmp_path / RID).exists()
    code, m = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), Resp(404)), "pdf")
    [pdf] = [c for c in m["reports"][0]["carried"] if c["leg"] == "pdf"]
    assert code == 1 and (tmp_path / RID / "report.pdf").read_bytes() == b"%PDF-1.7 test"
    assert pdf["from"] == f"{RID}.incoming"


def test_a_lineage_whose_module_is_not_found_stores_nothing(tmp_path, monkeypatch):
    # GL1
    code, m = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    lineage = next(r for r in m["reports"][0]["legs"] if r["leg"] == "lineage")
    assert code == 1 and lineage["status"] != "ok" and not (tmp_path / RID / "lineage.json").exists()


def test_a_capture_cut_short_is_named_as_interrupted(tmp_path, monkeypatch):
    # MK4, OC5
    cut_carry(monkeypatch)
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 130
    assert cco.incoming_note(tmp_path / f"{RID}.incoming").startswith("an interrupted capture (interrupted)")
    record = json.loads((tmp_path / f"{RID}.incoming" / "capture.json").read_text(encoding="utf-8"))["report"]
    assert record["legs"][-1]["leg"] == "store" and f"kept in {RID}.incoming" in record["legs"][-1]["reason"]


def test_a_record_spanning_two_legs_carries_nothing(tmp_path, monkeypatch):
    # CG3
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, all_viewers(fake_server()), "pdf,csv")
    path = tmp_path / RID / "capture.json"
    stamp = json.loads(path.read_text(encoding="utf-8"))
    stamp["ok_files"]["pdf"].update(stamp["ok_files"].pop("csv"))
    path.write_text(json.dumps(stamp), encoding="utf-8")
    code, m = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), Resp(404)), "pdf,csv")
    assert code == 1 and not [c for c in m["reports"][0].get("carried", []) if c["leg"] in ("pdf", "csv")]


def test_a_truncated_report_record_is_listed_as_unrecorded(tmp_path, monkeypatch):
    old = tmp_path / XID
    old.mkdir()
    stamp = {"id": XID, "captured_at": "t0", "report": {"id": XID}}
    (old / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    [xid] = m["earlier_reports"]
    assert code == 0 and xid["recorded"] is False and "cleanup_error" not in m


def test_a_capture_cut_mid_leg_records_that_leg_as_failed_in_its_folder(tmp_path, monkeypatch):
    real = cco.Capturer.store

    def store(self, rec, stage, rel, body, ct):
        if rel == "parts/List1_0.png":
            raise KeyboardInterrupt
        return real(self, rec, stage, rel, body, ct)

    monkeypatch.setattr(cco.Capturer, "store", store)
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    record = json.loads((tmp_path / f"{RID}.incoming" / "capture.json").read_text(encoding="utf-8"))["report"]
    render = next(r for r in record["legs"] if r["leg"] == "part:List1:render")
    assert code == 130 and not [r for r in record["legs"] if r["status"] == "pending"]
    assert render["status"] == "failed" and render["class"] == "interrupted"


def test_a_file_an_ok_leg_no_longer_writes_is_named_when_dropped(tmp_path, monkeypatch):
    ticking(monkeypatch)
    run_rid(tmp_path, monkeypatch, fake_server())
    (tmp_path / RID / "parts" / "List1_1.png").write_bytes(b"\x89PNG old")
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    dropped = {d["path"]: d["why"] for d in m["reports"][0]["dropped"]}
    assert code == 0 and dropped == {
        "parts/List1_1.png": "its leg was captured ok again on this run, without this file"
    }


def test_a_ctrl_c_during_an_early_kept_line_still_finishes_the_cleanup(tmp_path, monkeypatch):
    # N4: the cleanup after the cut line still runs (the lists are written, staging is removed)
    stamped_capture(tmp_path / f"{RID}.incoming", "2026-01-01T00:00:00+00:00", b"%PDF older")
    cut_carry(monkeypatch)
    err = RaisingStderr("KEPT: ")
    monkeypatch.setattr(sys, "stderr", err)
    code, m = run_rid(tmp_path, monkeypatch, fake_server())
    assert err.hits and code == 130 and m["finished_at"] and m["reports"][0]["earlier_incoming"]
    assert {"other_captures", "earlier_reports", "render_unestablished"} <= set(m)
    assert not (tmp_path / ".staging").exists()


# ---------------------------------------------------------------- round nine: the previous manifest.json is adopted

OLD_KEYS = ("schema", "id", "origin", "captured_at", "server")


def as_older_version(out):
    """--out as the previous script version left it: bare capture.json stamps, the old manifest entry keys."""
    for stamp_file in out.glob("i*/capture.json"):
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
        stamp_file.write_text(json.dumps({k: stamp[k] for k in OLD_KEYS if k in stamp}), encoding="utf-8")
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    m["reports"] = [
        {k: e[k] for k in ("id", "origin", "name", "path", "parts", "legs") if k in e} for e in m["reports"]
    ]
    for k in ("earlier_reports", "inherited"):
        m.pop(k, None)
    (out / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    return m


def failed_legs(report):
    return {r["leg"]: r.get("reason") for r in report["legs"] if r["status"] == "failed"}


def test_a_report_recorded_only_in_the_previous_manifest_is_adopted_and_saved(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_reports_second_without_renders([RID, SID]))
    old = as_older_version(tmp_path)
    entry = next(e for e in old["reports"] if e["id"] == SID)
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    assert code == 0 and sid["recorded"] is True and sid["record_saved"] is True and "inherited" not in m
    assert sid["recorded_in"] == {"file": "manifest.json", "started_at": old["started_at"], "status": "partial"}
    assert failed_legs(sid["report"]) == failed_legs(entry) and failed_legs(entry)
    assert m["previous_manifest"] == {"started_at": old["started_at"], "status": "partial", "records": 2}
    stamp = json.loads((tmp_path / SID / "capture.json").read_text(encoding="utf-8"))
    assert stamp["recorded_in"] == sid["recorded_in"] and stamp["id"] == SID
    # the folder now describes itself: a later run finds it without any manifest.json
    (tmp_path / "manifest.json").unlink()
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    assert code == 0 and sid["recorded"] is True and failed_legs(sid["report"]) == failed_legs(entry)
    assert sid["recorded_in"]["started_at"] == old["started_at"] and "record_saved" not in sid


def test_an_adopted_record_whose_file_changed_is_never_ok_nor_saved(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    old = as_older_version(tmp_path)
    (tmp_path / SID / "lineage.json").write_text("{}", encoding="utf-8")
    bare = (tmp_path / SID / "capture.json").read_bytes()
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    lineage = next(r for r in sid["report"]["legs"] if r["leg"] == "lineage")
    assert code == 0 and lineage["status"] == "failed" and lineage["path"] is None
    assert sid["record_saved"] is False and (tmp_path / SID / "capture.json").read_bytes() == bare
    assert [r for r in sid["report"]["legs"] if r["status"] == "ok"]
    # not saved, so the next run takes it from this run's manifest, still naming the run that recorded it
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    lineage = next(r for r in sid["report"]["legs"] if r["leg"] == "lineage")
    assert sid["recorded"] is True and sid["recorded_in"]["started_at"] == old["started_at"]
    assert lineage["status"] == "failed"


def test_a_capture_not_swapped_in_keeps_the_record_of_its_old_folder(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    old = as_older_version(tmp_path)
    restore = cut_carry(monkeypatch)
    code, m = run_main(tmp_path, monkeypatch, two_report_server([SID]), ids=["--report-id", SID])
    assert code == 130 and [e["id"] for e in m["inherited"]] == [SID]
    restore()
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    sid = next(e for e in m["earlier_reports"] if e["folder"] == SID)
    assert sid["recorded"] is True and sid["recorded_in"]["started_at"] == old["started_at"]
    assert not failed_legs(sid["report"]) and sid["record_saved"] is True


def test_a_killed_runs_manifest_still_hands_on_what_it_inherited(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    old = as_older_version(tmp_path)
    written = []
    real = cco.write_json

    def keep(path, data, secret=None):
        if path.name == "manifest.json" and not written:
            written.append(json.loads(json.dumps(data)))
        return real(path, data, secret)

    monkeypatch.setattr(cco, "write_json", keep)
    run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [running] = written
    assert running["status"] == "running" and SID in {e["id"] for e in running["inherited"]}
    # the kill: that running manifest is what is left, with an entry of SID that describes no folder content
    running["reports"].append({**running["inherited"][0], "id": SID, "legs": [{"leg": "lineage", "status": "pending"}]})
    (tmp_path / SID / "capture.json").write_text(json.dumps({"id": SID}), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps(running), encoding="utf-8")
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    assert sid["recorded"] is True and sid["recorded_in"]["started_at"] == old["started_at"]
    assert len(sid["report"]["legs"]) > 1 and not failed_legs(sid["report"])


def test_a_leg_left_pending_in_the_previous_manifest_is_adopted_as_failed(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    old = as_older_version(tmp_path)
    old["status"] = "running"
    entry = next(e for e in old["reports"] if e["id"] == SID)
    lineage = next(r for r in entry["legs"] if r["leg"] == "lineage")
    lineage["status"] = "pending"
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    lineage = next(r for r in sid["report"]["legs"] if r["leg"] == "lineage")
    assert code == 0 and lineage["status"] == "failed" and "pending" in lineage["reason"]
    stamp = json.loads((tmp_path / SID / "capture.json").read_text(encoding="utf-8"))
    assert next(r for r in stamp["report"]["legs"] if r["leg"] == "lineage")["status"] == "failed"


@pytest.mark.parametrize(
    "text",
    [
        "not json {",
        "[]",
        '{"status": "ok", "reports": 5, "earlier_reports": {"a": 1}, "inherited": [7]}',
        json.dumps({"reports": [{"id": SID, "legs": [{"leg": "lineage", "status": "ok", "path": 5}]}]}),
        json.dumps({"reports": [{"id": SID, "legs": [{"leg": "lineage", "status": "ok", "images": 3}]}]}),
        json.dumps({"reports": [{"id": SID, "legs": [{"leg": "lineage", "status": "ok"}], "carried": 4}]}),
        json.dumps({"reports": [{"id": SID, "legs": [{"leg": "lineage", "status": "ok", "path": "../x"}]}]}),
        json.dumps({"reports": [{"id": SID, "legs": [{"leg": "pdf", "status": "ok", "omits": {}}]}]}),
    ],
    ids=[
        "not-json",
        "a-list",
        "wrong-types",
        "path-not-a-string",
        "images-not-a-list",
        "carried-not-a-list",
        "escape",
        "omits-malformed",
    ],
)
def test_a_malformed_previous_manifest_never_crashes_the_run_nor_reads_as_ok(tmp_path, monkeypatch, text):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    as_older_version(tmp_path)
    (tmp_path / "manifest.json").write_text(text, encoding="utf-8")
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    assert code == 0 and "cleanup_error" not in m and m["status"] == "ok"
    assert "images" not in text or sid["recorded"] is True  # an odd shape of one leg drops no record
    if sid["recorded"]:
        assert not [r for r in sid["report"]["legs"] if r["status"] == "ok" and r.get("path") is not None]
        assert '"path"' not in text or sid["record_saved"] is False


def test_no_key_in_the_previous_manifest_reaches_any_output(tmp_path, monkeypatch, capsys):
    run_main(tmp_path, monkeypatch, two_reports_second_without_renders([RID, SID]))
    old = as_older_version(tmp_path)
    entry = next(e for e in old["reports"] if e["id"] == SID)
    entry["name"] = "report not-a-real-key"
    next(r for r in entry["legs"] if r["status"] == "failed")["reason"] = 'said "not\\u002da-real-key"'
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    capsys.readouterr()
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    assert code == 0 and sid["record_saved"] is True
    out, err = capsys.readouterr()
    texts = [out, err, *(f.read_text(encoding="utf-8", errors="replace") for f in tmp_path.rglob("*.json"))]
    assert not [t for t in texts if "not-a-real-key" in t or "not\\u002da-real-key" in t]
    assert "<key>" in (tmp_path / SID / "capture.json").read_text(encoding="utf-8")


def test_an_adopted_record_never_creates_a_capture_json(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    as_older_version(tmp_path)
    (tmp_path / SID / "capture.json").unlink()
    code, m = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])
    [sid] = m["earlier_reports"]
    assert code == 0 and sid["recorded"] is True and sid["record_saved"] is False
    assert not (tmp_path / SID / "capture.json").exists()


def test_a_partial_manifest_entry_cannot_replace_a_full_inherited_record(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    old = as_older_version(tmp_path)
    [full] = [e for e in old["reports"] if e["id"] == SID]
    source = {"file": "manifest.json", "started_at": old["started_at"], "status": old["status"]}
    old["inherited"] = [{**full, "recorded_in": source}]
    old["reports"] = [{"id": SID, "legs": [{"leg": "lineage", "status": "failed", "class": cco.STORE_ERROR}]}]
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")

    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])

    [sid] = manifest["earlier_reports"]
    assert code == 0 and sid["recorded"] is True and sid["recorded_in"] == source
    assert len(sid["report"]["legs"]) == len(full["legs"])
    assert not failed_legs(sid["report"])


@pytest.mark.parametrize("retained_as", ["trash", "previous"])
def test_a_swap_leftover_keeps_the_old_manifest_record(tmp_path, monkeypatch, retained_as):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    old = as_older_version(tmp_path)
    if retained_as == "trash":
        trash = tmp_path / ".trash"
        trash.mkdir()
        (tmp_path / SID).rename(trash / f"{SID}-1234abcd")
        folder = f".trash/{SID}-1234abcd"
    else:
        (tmp_path / SID).rename(tmp_path / f"{SID}.previous")
        folder = f"{SID}.previous"
    previous_stamp = (tmp_path / f"{SID}.previous" / "capture.json").read_bytes() if retained_as == "previous" else None

    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])

    sid = next(e for e in manifest["earlier_reports"] if e["folder"] == folder)
    expected_saved = retained_as == "trash"
    assert code == 0 and sid["recorded"] is True and sid["record_saved"] is expected_saved
    assert sid["recorded_in"]["started_at"] == old["started_at"]
    assert not failed_legs(sid["report"])
    assert all(r.get("path") is None or r["path"].startswith(folder + "/") for r in sid["report"]["legs"])
    if previous_stamp is not None:
        assert (tmp_path / f"{SID}.previous" / "capture.json").read_bytes() == previous_stamp


def test_an_ok_adopted_lineage_or_pdf_without_a_path_is_failed(tmp_path, monkeypatch):
    folder = tmp_path / SID
    folder.mkdir()
    stamp = {"id": SID, "captured_at": "2026-01-01T00:00:00+00:00"}
    (folder / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")
    (folder / "lineage.json").write_text("{}", encoding="utf-8")
    (folder / "report.pdf").write_bytes(b"%PDF-1.7 old")
    old = {
        "status": "partial",
        "reports": [
            {
                "id": SID,
                "legs": [
                    {"leg": "lineage", "status": "ok", "sha256": hashlib.sha256(b"{}").hexdigest()},
                    {
                        "leg": "pdf",
                        "status": "ok",
                        "path": None,
                        "sha256": hashlib.sha256(b"%PDF-1.7 old").hexdigest(),
                    },
                ],
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")

    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])

    [sid] = manifest["earlier_reports"]
    legs = {r["leg"]: r for r in sid["report"]["legs"]}
    assert code == 0 and legs["lineage"]["status"] == legs["pdf"]["status"] == "failed"
    assert legs["lineage"].get("path") is None and legs["pdf"].get("path") is None
    assert sid["record_saved"] is False


def test_an_adopted_capture_metadata_file_is_never_an_ok_leg(tmp_path, monkeypatch):
    folder = tmp_path / SID
    folder.mkdir()
    (folder / "capture.json").write_text(json.dumps({"id": SID}), encoding="utf-8")
    digest = hashlib.sha256((folder / "capture.json").read_bytes()).hexdigest()
    old = {
        "status": "partial",
        "reports": [
            {"id": SID, "legs": [{"leg": "data", "status": "ok", "path": f"{SID}/capture.json", "sha256": digest}]}
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")

    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])

    [sid] = manifest["earlier_reports"]
    assert code == 0 and sid["report"]["legs"][0]["status"] == "failed"
    assert sid["report"]["legs"][0]["path"] is None and sid["record_saved"] is False


def test_swapped_in_ids_are_removed_from_inherited_even_when_listing_fails(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID]))
    old = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    record = old["reports"][0]
    old["reports"] = []
    old["inherited"] = [
        {
            **record,
            "recorded_in": {"file": "manifest.json", "started_at": old["started_at"], "status": "partial"},
        }
    ]
    obj = tmp_path / RID / "object.json"
    stale = b"stale object bytes"
    obj.write_bytes(stale)
    obj_leg = next(r for r in old["inherited"][0]["legs"] if r["leg"] == "object")
    obj_leg["sha256"] = hashlib.sha256(stale).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")

    def fail_listing(*args, **kwargs):
        raise OSError("listing unavailable")

    monkeypatch.setattr(cco, "earlier_reports", fail_listing)
    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])

    assert code == 0 and manifest["earlier_reports"] == [{"error": "out folder not listed (OSError)"}]
    assert not [e for e in manifest.get("inherited", []) if e["id"] == RID]
    assert hashlib.sha256(obj.read_bytes()).hexdigest() != obj_leg["sha256"]


def test_reparse_point_report_folders_are_not_adopted_or_written(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    as_older_version(tmp_path)
    stamp = (tmp_path / SID / "capture.json").read_bytes()
    monkeypatch.setattr(cco, "is_reparse_point", lambda path: Path(path) == tmp_path / SID)

    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])

    [sid] = manifest["earlier_reports"]
    assert code == 0 and sid["recorded"] is False and "reparse point" in sid["note"]
    assert (tmp_path / SID / "capture.json").read_bytes() == stamp


def test_is_reparse_point_checks_the_windows_reparse_attribute(monkeypatch, tmp_path):
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: types.SimpleNamespace(st_mode=0, st_file_attributes=0x400),
    )
    assert cco.is_reparse_point(tmp_path / "junction")


def test_capture_files_includes_unfinished_suffixes_so_deletions_can_be_named(tmp_path):
    folder = tmp_path / SID
    folder.mkdir()
    for name in ("budget.tmp", "draft.part", "capture.json"):
        (folder / name).write_text("x", encoding="utf-8")
    assert cco.capture_files(folder) == ["budget.tmp", "draft.part"]


def test_a_crosstab_data_group_cannot_claim_a_list_output(tmp_path):
    folder = tmp_path / SID
    (folder / "parts").mkdir(parents=True)
    output = folder / "parts" / "List1.json"
    output.write_text("{}", encoding="utf-8")
    stamp = {
        "ok_files": {
            "part:Crosstab1:data": {
                "parts/List1.json": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        }
    }
    labels = {"List1": "List1", "Crosstab1": "Crosstab1"}
    assert cco.complete_groups(folder, stamp, labels, HOST) == {}


def test_a_deeply_nested_previous_manifest_is_malformed_data_not_a_crash(tmp_path, monkeypatch, capsys):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    nested = "[" * 1100 + "0" + "]" * 1100
    deep = (
        '{"reports":[{"id":"'
        + SID
        + '","legs":[{"leg":"lineage","status":"ok"}],"parts":'
        + nested
        + '}],"key":"not-a-real-key"}'
    )
    (tmp_path / "manifest.json").write_text(deep, encoding="utf-8")

    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([RID]), ids=["--report-id", RID])

    out, err = capsys.readouterr()
    assert code == 0 and manifest["status"] == "ok"
    assert manifest["previous_manifest"]["error"].startswith("manifest.json not read")
    assert "Traceback" not in out + err and "not-a-real-key" not in out + err


RID_ONLY = ["--report-id", RID]
SID_ONLY = ["--report-id", SID]
BROKEN_CHART = {"dataSet": {"dataTable": [{"row": []}]}}


def rerun_rid(out, monkeypatch):
    return run_main(out, monkeypatch, two_report_server([RID]), ids=RID_ONLY)


def two_reports(out, monkeypatch):
    code, _ = run_main(out, monkeypatch, two_report_server([RID, SID]))
    assert code == 0


def report_item(manifest, folder):
    return next(e for e in manifest["earlier_reports"] if e.get("folder") == folder)


def edit_stamp(folder: Path, change) -> None:
    stamp = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
    change(stamp)
    (folder / "capture.json").write_text(json.dumps(stamp), encoding="utf-8")


def manifest_path_problems(out: Path, manifest: dict) -> list[str]:
    records = []
    for entry in manifest.get("reports", []):
        for rec in entry.get("legs", []):
            records.extend((f"reports[{entry['id']}].{rec.get('leg')}", f) for f in cco.files_of(rec))
        records.extend((f"reports[{entry['id']}].carried", f) for f in entry.get("carried", []))
    for entry in manifest.get("earlier_reports", []):
        report = entry.get("report") if entry.get("recorded") else None
        if isinstance(report, dict):
            for rec in report.get("legs", []):
                records.extend((f"earlier_reports[{entry['folder']}].{rec.get('leg')}", f) for f in cco.files_of(rec))
            records.extend((f"earlier_reports[{entry['folder']}].carried", f) for f in report.get("carried", []))
    problems = []
    for label, record in records:
        path = record.get("path") if isinstance(record, dict) else None
        if path is not None and not (out / str(path)).is_file():
            problems.append(f"{label}: {path} is missing under --out")
            continue
        expected = record.get("sha256") if isinstance(record, dict) else None
        if path is not None and isinstance(expected, str):
            actual = hashlib.sha256((out / str(path)).read_bytes()).hexdigest()
            if actual != expected:
                problems.append(f"{label}: {path} does not match its sha256")
    return problems


@pytest.fixture
def junctions():
    made: list[Path] = []

    def make(link: Path, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if result.returncode:
            pytest.skip(f"mklink /J is unavailable: {result.stdout}{result.stderr}")
        made.append(link)

    yield make
    for link in made:
        if os.path.lexists(link):
            subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)


def folder_snapshot(folder: Path) -> dict[str, bytes]:
    return {f.relative_to(folder).as_posix(): f.read_bytes() for f in folder.rglob("*") if f.is_file()}


@pytest.mark.parametrize("variant", ["own-record", "adopted-record"])
def test_n2_failed_embedded_file_check_still_normalizes_every_path(tmp_path, monkeypatch, variant):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    if variant == "own-record":
        (tmp_path / SID / "parts" / "List1.html").write_text("<div>edited</div>", encoding="utf-8")
    else:
        as_older_version(tmp_path)
        (tmp_path / SID / "lineage.json").write_text("{}", encoding="utf-8")
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    assert code == 0
    assert not (problems := manifest_path_problems(tmp_path, manifest)), problems


def test_n1_store_only_failure_does_not_describe_the_old_capture(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    old = as_older_version(tmp_path)
    old["reports"] = [e for e in old["reports"] if e["id"] != SID]
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    (tmp_path / ".staging" / SID).mkdir(parents=True)
    real = cco.Capturer.rename

    def rename(self, src, dst):
        if src.parent.name == ".staging" and dst.parent.name == ".trash":
            raise PermissionError(13, "in use")
        return real(self, src, dst)

    monkeypatch.setattr(cco.Capturer, "rename", rename)
    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([SID]), ids=SID_ONLY)
    [entry] = manifest["reports"]
    assert code == 1 and [r["leg"] for r in entry["legs"]] == ["store"] and "stored_as" not in entry
    monkeypatch.setattr(cco.Capturer, "rename", real)
    _, manifest = rerun_rid(tmp_path, monkeypatch)
    sid = report_item(manifest, SID)
    saved = json.loads((tmp_path / SID / "capture.json").read_text(encoding="utf-8")).get("report")
    assert sid.get("record_saved") is not True
    assert not isinstance(saved, dict) or [r.get("leg") for r in saved.get("legs", [])] != ["store"]


def test_j78b_trash_junction_keeps_truthful_capture_paths(tmp_path, monkeypatch, junctions):
    out, outside = tmp_path / "out", tmp_path / "outside"
    out.mkdir()
    code, _ = run_main(out, monkeypatch, two_report_server([SID]), ids=SID_ONLY)
    assert code == 0 and not (out / ".trash").exists()
    target = outside / "held"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("outside --out", encoding="utf-8")
    junctions(out / ".trash", target)
    before = folder_snapshot(outside)
    base = two_report_server([SID])

    def renamed(kind, path):
        if path.startswith("/v1/objects/"):
            return Resp(body=json.dumps({"data": [{"defaultName": "R renamed", "ancestors": [], "module": None}]}))
        return base(kind, path)

    code, manifest = run_main(out, monkeypatch, renamed, ids=SID_ONLY)
    assert not (problems := manifest_path_problems(out, manifest)), (code, manifest["status"], problems)
    assert folder_snapshot(outside) == before


def short_name(path: Path) -> str | None:
    buffer = ctypes.create_unicode_buffer(1024)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return Path(buffer.value).name if length else None


@pytest.mark.parametrize("alias", ["trailing-dot", "trailing-space", "data-stream", "short-name"])
def test_f2_metadata_alias_is_never_an_ok_adopted_output(tmp_path, monkeypatch, alias):
    folder = tmp_path / SID
    folder.mkdir()
    capture = folder / "capture.json"
    capture.write_text(json.dumps({"id": SID}), encoding="utf-8")
    digest = hashlib.sha256(capture.read_bytes()).hexdigest()
    if alias == "short-name":
        name = short_name(capture)
        if not name or name.lower() == "capture.json":
            pytest.skip("volume has no 8.3 short name")
    else:
        name = {
            "trailing-dot": "capture.json.",
            "trailing-space": "capture.json ",
            "data-stream": "capture.json::$DATA",
        }[alias]
    old = {
        "status": "partial",
        "reports": [{"id": SID, "legs": [{"leg": "data", "status": "ok", "path": f"{SID}/{name}", "sha256": digest}]}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(old), encoding="utf-8")
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    [sid] = manifest["earlier_reports"]
    leg = sid["report"]["legs"][0]
    assert code == 0 and leg["status"] == "failed" and sid["record_saved"] is False


@pytest.mark.parametrize("whose", ["untouched-report", "captured-report"])
def test_n6_deep_capture_metadata_is_unreadable_not_a_crash(tmp_path, monkeypatch, whose):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    (tmp_path / (SID if whose == "untouched-report" else RID) / "capture.json").write_text("[" * 5000, encoding="utf-8")
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    assert code != 3 and "cleanup_error" not in manifest
    assert not (tmp_path / f"{RID}.incoming").exists()
    if whose == "untouched-report":
        assert code == 0


def test_f3c_previous_beside_canonical_folder_is_not_adopted_or_written(tmp_path, monkeypatch):
    run_main(tmp_path, monkeypatch, two_report_server([RID, SID]))
    as_older_version(tmp_path)
    previous = tmp_path / f"{SID}.previous"
    shutil.copytree(tmp_path / SID, previous)
    before = (previous / "capture.json").read_bytes()
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    item = report_item(manifest, f"{SID}.previous")
    assert code == 0 and not item.get("recorded") and item.get("record_saved") is not True
    assert (previous / "capture.json").read_bytes() == before


def test_n5b_failed_final_manifest_write_preserves_stopped_exit(tmp_path, monkeypatch, capsys):
    base = fake_server()

    def handler(kind, path):
        if "b_action=cognosViewer" in path:
            return Resp(body="<html>CV-0015 no capability</html>", ctype="text/html")
        return base(kind, path)

    real = cco.write_json

    def write_json(path, data, secret=None):
        if Path(path).name == "manifest.json" and isinstance(data, dict) and data.get("finished_at"):
            raise PermissionError(13, "manifest.json is locked")
        return real(path, data, secret)

    monkeypatch.setattr(cco, "write_json", write_json)
    monkeypatch.setattr(cco, "Client", lambda *a, **k: FakeClient(handler))
    monkeypatch.setenv("COGNOS_API_KEY", "not-a-real-key")
    argv = ["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(tmp_path), "--formats", "pdf"]
    code = cco.main(argv + RID_ONLY)
    err = capsys.readouterr().err
    assert "STOPPED:" in err and "manifest.json not written" in err
    assert code == 2


@pytest.mark.parametrize("leftover", [".staging", ".trash"])
def test_j78_cleanup_never_removes_an_existing_junction(tmp_path, monkeypatch, junctions, leftover):
    out, target = tmp_path / "out", tmp_path / "outside" / "held"
    out.mkdir()
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("preserve", encoding="utf-8")
    junctions(out / leftover, target)
    monkeypatch.setattr(cco, "Client", lambda *a, **k: FakeClient(fake_server()))
    monkeypatch.setattr(cco.Capturer, "capture_report", lambda *a, **k: (_ for _ in ()).throw(cco.Stop("test")))
    monkeypatch.setenv("COGNOS_API_KEY", "not-a-real-key")
    code = cco.main(["--server", f"https://{HOST}", "--namespace", "ns", "--out", str(out), *RID_ONLY])
    assert code == 2
    assert (out / leftover).exists() and (out / leftover).is_dir()
    assert (target / "keep.txt").read_text(encoding="utf-8") == "preserve"


def test_sh3_logout_interrupt_does_not_cut_off_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeClient, "logout", lambda self: (_ for _ in ()).throw(KeyboardInterrupt))
    code, manifest = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 130
    assert all(isinstance(manifest.get(k), list) for k in ("render_unestablished", "other_captures", "earlier_reports"))
    assert not (tmp_path / ".staging").exists()


def test_cg3_record_spanning_distinct_parts_has_no_single_owner():
    assert not cco.owned_by(["parts/Crosstab1.json", "parts/List1.json"], "part:Crosstab1:data", {})


def test_r4_temporary_file_is_not_an_adopted_ok_leg(tmp_path, monkeypatch):
    folder = tmp_path / SID
    folder.mkdir()
    (folder / "capture.json").write_text(json.dumps({"id": SID}), encoding="utf-8")
    (folder / "notes.tmp").write_bytes(b"scratch")
    leg = {
        "leg": "data",
        "status": "ok",
        "path": f"{SID}/notes.tmp",
        "sha256": hashlib.sha256(b"scratch").hexdigest(),
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps({"status": "partial", "reports": [{"id": SID, "legs": [leg]}]}), encoding="utf-8"
    )
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    [sid] = manifest["earlier_reports"]
    assert code == 0 and sid["report"]["legs"][0]["status"] == "failed" and sid["record_saved"] is False


def test_r8_deep_old_manifest_is_tolerated(tmp_path, monkeypatch):
    deep = "[" * 5000 + "]" * 5000
    with pytest.raises(RecursionError):
        json.loads(deep)
    (tmp_path / "manifest.json").write_text(deep, encoding="utf-8")
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    assert code == 0 and manifest["previous_manifest"] == {"error": "manifest.json not read (RecursionError)"}


def test_r11_trash_copy_is_not_described_when_canonical_exists(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    shutil.copytree(tmp_path / SID, tmp_path / ".trash" / f"{SID}-0123abcd")
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    assert code == 0 and [e["folder"] for e in manifest["earlier_reports"]] == [SID]


def test_r25_unrecognized_trash_name_is_not_described(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    (tmp_path / ".trash").mkdir()
    (tmp_path / SID).rename(tmp_path / ".trash" / f"{SID}.staging-1234abcd")
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    trash = [e["folder"] for e in manifest["earlier_reports"] if str(e.get("folder", "")).startswith(".trash/")]
    assert code == 0 and not trash


def test_g3_cleanup_interrupt_is_retried(tmp_path, monkeypatch):
    real, calls = cco.render_unestablished, []

    def cut_once(reports):
        calls.append(1)
        if len(calls) == 1:
            raise KeyboardInterrupt
        return real(reports)

    monkeypatch.setattr(cco, "render_unestablished", cut_once)
    code, manifest = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 130 and manifest["status"] == "interrupted" and isinstance(manifest["render_unestablished"], list)


def test_g7_interrupt_during_final_manifest_write_is_recorded(tmp_path, monkeypatch):
    import signal

    real, fired = cco.write_json, []

    def write_json(path, data, secret=None):
        if Path(path).name == "manifest.json" and isinstance(data, dict) and data.get("finished_at") and not fired:
            fired.append(1)
            signal.raise_signal(signal.SIGINT)
        return real(path, data, secret)

    monkeypatch.setattr(cco, "write_json", write_json)
    code, manifest = run_rid(tmp_path, monkeypatch, fake_server())
    assert fired and code == 130 and manifest["status"] == "interrupted"


def test_g10_cleanup_error_after_partial_capture_is_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(cco, "lineage_unestablished", lambda reports: (_ for _ in ()).throw(ValueError("failed")))
    code, manifest = run_rid(tmp_path, monkeypatch, fake_server(chart_body=BROKEN_CHART))
    assert manifest.get("cleanup_error") and code == 3 and manifest["status"] == "crashed"


def test_g12_interrupt_in_final_summary_does_not_escape_main(tmp_path, monkeypatch):
    stdout = RaisingStderr("reports, ")
    monkeypatch.setattr(sys, "stdout", stdout)
    code, manifest = run_rid(tmp_path, monkeypatch, fake_server())
    assert stdout.hits == 1 and manifest["status"] == "ok" and code == 0


def test_wu3_inherited_record_survives_unlisted_output_folder(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    as_older_version(tmp_path)
    monkeypatch.setattr(
        cco,
        "earlier_reports",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(13, "out folder not listed")),
    )
    _, manifest = rerun_rid(tmp_path, monkeypatch)
    assert manifest["earlier_reports"] == [{"error": "out folder not listed (PermissionError)"}]
    assert [entry["id"] for entry in manifest.get("inherited", [])] == [SID]


def test_pr6_confirmed_entry_precedes_inherited_record():
    record = cco.previous_reports(
        {
            "status": "ok",
            "reports": [{"id": SID, "stored_as": SID, "legs": [{"leg": "object", "status": "ok"}]}],
            "inherited": [{"id": SID, "legs": [{"leg": "object", "status": "failed"}]}],
        }
    )[SID.lower()]
    assert [r["status"] for r in record["legs"]] == ["ok"]


def test_pr10_recorded_source_without_file_uses_fallback():
    fallback = {"file": "manifest.json"}
    assert cco.recorded_source({"started_at": "t", "status": "ok"}, fallback) == fallback


def test_pr11_recorded_source_keeps_only_string_fields():
    assert cco.recorded_source({"file": "manifest.json", "started_at": 7}, {"file": "fallback.json"}) == {
        "file": "manifest.json"
    }


def test_r33_unswapped_entry_never_describes_canonical_folder():
    leg = {"leg": "object", "status": "ok", "path": f"{SID}.incoming/object.json"}
    data = {"status": "interrupted", "reports": [{"id": SID, "stored_as": f"{SID}.incoming", "legs": [leg]}]}
    assert cco.previous_reports(data) == {}


def test_lc1_module_lineage_missing_module_file_is_not_carried(tmp_path, monkeypatch):
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 0
    strip_record(tmp_path / RID)
    (tmp_path / RID / "module.json").unlink()
    code, manifest = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    carried = [c["path"] for c in manifest["reports"][0].get("carried", []) if c["leg"] == "lineage"]
    assert code == 1 and not carried


def test_lc2_package_lineage_with_module_file_is_not_carried(tmp_path, monkeypatch):
    code, _ = run_rid(tmp_path, monkeypatch, fake_server())
    assert code == 0
    strip_record(tmp_path / RID)
    lineage = json.loads((tmp_path / RID / "lineage.json").read_text(encoding="utf-8"))
    lineage["model"].update(kind="package", metadata=None)
    (tmp_path / RID / "lineage.json").write_text(json.dumps(lineage), encoding="utf-8")
    code, manifest = run_rid(tmp_path, monkeypatch, no_modules(fake_server()))
    carried = [c["path"] for c in manifest["reports"][0].get("carried", []) if c["leg"] == "lineage"]
    assert code == 1 and not carried


def test_nd2_replaced_older_output_is_named_dropped(tmp_path, monkeypatch):
    ticking(monkeypatch)
    pdf_ok = with_viewer(fake_server(), viewer_page("complete"))
    code, _ = run_rid(tmp_path, monkeypatch, pdf_ok, "pdf")
    assert code == 0
    restore = cut_carry(monkeypatch)
    code, _ = run_rid(tmp_path, monkeypatch, pdf_ok, "pdf")
    restore()
    assert code == 130 and (tmp_path / f"{RID}.incoming" / "report.pdf").is_file()
    code, manifest = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), Resp(404)), "pdf")
    [entry] = manifest["reports"]
    [pdf] = [c for c in entry["carried"] if c["leg"] == "pdf"]
    dropped = [(d["folder"], d["path"]) for d in entry.get("dropped", [])]
    assert code == 1 and pdf["from"] == f"{RID}.incoming" and (RID, "report.pdf") in dropped


def test_er6_untouched_report_carried_paths_are_relative_to_output(tmp_path, monkeypatch):
    ticking(monkeypatch)
    code, _ = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), viewer_page("complete")), "pdf")
    assert code == 0
    code, manifest = run_rid(tmp_path, monkeypatch, with_viewer(fake_server(), Resp(404)), "pdf")
    assert code == 1 and [c["leg"] for c in manifest["reports"][0]["carried"]] == ["pdf"]
    code, manifest = run_main(tmp_path, monkeypatch, two_report_server([SID]), ids=SID_ONLY)
    carried = report_item(manifest, RID)["report"]["carried"]
    assert code == 0 and carried
    assert not (problems := manifest_path_problems(tmp_path, manifest)), problems


def test_er9_leg_without_status_is_dropped_without_failing_capture(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    edit_stamp(tmp_path / SID, lambda stamp: stamp["report"]["legs"].append({"leg": "note"}))
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    sid = report_item(manifest, SID)
    assert code == 0 and sid["recorded"] and "note" not in [r["leg"] for r in sid["report"]["legs"]]


def test_er11_non_string_leg_name_is_malformed_not_a_crash(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    edit_stamp(tmp_path / SID, lambda stamp: stamp["report"]["legs"].append({"leg": 5, "status": "failed"}))
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    sid = next((entry for entry in manifest["earlier_reports"] if entry.get("folder") == SID), None)
    assert sid is not None and code == 0 and "cleanup_error" not in manifest
    assert not sid["recorded"] and "malformed" in sid["note"]


def test_er12_failed_atomic_stamp_write_leaves_no_capture_tmp(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    as_older_version(tmp_path)
    real = Path.replace

    def replace(self, target):
        if self.name == "capture.tmp":
            raise PermissionError(13, "capture.json is locked")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    _, manifest = rerun_rid(tmp_path, monkeypatch)
    sid = report_item(manifest, SID)
    assert sid["recorded"] and sid["record_saved"] is False and "PermissionError" in sid["note"]
    assert not (tmp_path / SID / "capture.tmp").exists()


def test_er19_wrong_id_stamp_is_not_rewritten(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    as_older_version(tmp_path)
    edit_stamp(tmp_path / SID, lambda stamp: stamp.update(id=XID))
    before = (tmp_path / SID / "capture.json").read_bytes()
    _, manifest = rerun_rid(tmp_path, monkeypatch)
    sid = report_item(manifest, SID)
    assert sid["recorded"] and sid["record_saved"] is False
    assert (tmp_path / SID / "capture.json").read_bytes() == before


def test_er20_capture_stamp_without_legs_adopts_earlier_record(tmp_path, monkeypatch):
    two_reports(tmp_path, monkeypatch)
    as_older_version(tmp_path)
    edit_stamp(tmp_path / SID, lambda stamp: stamp.update(report={"id": SID}))
    code, manifest = rerun_rid(tmp_path, monkeypatch)
    sid = report_item(manifest, SID)
    assert code == 0 and sid["recorded"] and sid["report"]["legs"]
