"""Regression tests for the second community-id review (2026-09-28).

  1. A one-column sheet is sent only under an exact person-name header, and
     every person on it goes to review - never an automatic new id.
  2. A kept name that shares a couple or list cell is never deleted from
     the output: that cell keeps the normal per-value treatment.
  3. A frozen commit is never unlocked by a resend that can't prove nothing
     was written (settings changed, or stale decisions in the refusal).
  4. A commit that never left this machine (not configured, connection
     refused) is a clean failure, not a frozen "may be written".

Names here are fixtures, never real people.
"""

from __future__ import annotations

import copy
import io
import socket

import pytest

import tests.test_community_flow_fixes as flow
from tests.test_community import API_KEY, DECISION, _client, _detect
from tests.test_community_core_fixes import _grid, _person, _session
from tests.test_community_flow_fixes import ATTACH, _decided, ifg  # noqa: F401 (fixture)

JOHN, MARY = "I1111222233334444", "I5555666677778888"
SMITHS = "F3D6B57AFBDADAEBD"


# ---------------------------------------------------------------------------
# 1. One-column sheets
# ---------------------------------------------------------------------------

def test_a_one_column_list_needs_an_exact_person_name_header():
    from app import community
    # A header word is not enough: these would mint a person per line.
    for header, rows in (("Ministry Name", ["Altar Servers", "Youth Choir", "Room 12"]),
                         ("Room Name", ["Room 12", "Gym"]),
                         ("Family notes", ["Call the Smith family about tuition"]),
                         ("Household", ["Smith Family", "Jones Family"])):
        assert community.build_sheets([[[header]] + [[r] for r in rows]]) == [], header
    # A data row holding a header word is never taken for the header.
    [s] = community.build_sheets([[["Name"], ["Emma Child"], ["Liam Roe"]]])
    assert s["header_row"] == 0 and s["one_column"] and len(s["rows"]) == 2
    [s] = community.build_sheets([[["Student Roster"], [""], ["Student Name"], ["Jane Doe"]]])
    assert s["header_row"] == 2
    # Wide sheets are unchanged and not flagged.
    [s] = community.build_sheets([[["First", "Last"], ["Jane", "Doe"]]])
    assert s["header_row"] == 0 and not s["one_column"]


def _one_column_state():
    from app import community
    st = community.CommunityState(status="ready")
    st.sheets = community.build_sheets([[["Name"], ["Jane Doe"], ["John Roe"], ["Ann Lee"]]])
    known = {"community_id": JOHN, "sheet_ref": None, "given_name": "John", "family_name": "Roe"}
    st.plan = {"summary": {}, "stale_decisions": [], "sheets": [{"rows": [
        {"index": 0, "persons": [_person("0:0:0", "Jane", "Doe", [{"col": 0, "part": "full"}], None)],
         "family": {"key": "0:0:family", "display_name": "Doe", "action": "new", "community_id": None}},
        {"index": 1, "persons": [{**_person("0:1:0", "John", "Roe", [{"col": 0, "part": "full"}], JOHN,
                                            action="matched"), "matched": known}]},
        {"index": 2, "persons": [{**_person("0:2:0", "Ann", "Lee", [{"col": 0, "part": "full"}], None),
                                  "action": "skip"}]},
    ]}]}
    return st


def test_every_person_on_a_one_column_list_goes_to_review():
    from app import community
    st = _one_column_state()
    items = {i["key"]: i for i in community._items(st)}
    assert items["0:0:0"]["action"] == "review" and "one_column_list" in items["0:0:0"]["review_reasons"]
    # A match is offered as the candidate, never applied on its own.
    assert items["0:1:0"]["action"] == "review"
    assert [c["community_id"] for c in items["0:1:0"]["candidates"]] == [JOHN]
    assert items["0:2:0"]["action"] == "skip", "an operator's skip stays a skip"
    assert set(community.pending(st)) == {"0:0:0", "0:1:0"}
    community.decide(st, "0:0:0", "create")
    community.decide(st, "0:1:0", "attach", JOHN)
    assert community.pending(st) == []
    # The plan Family Graph sent is not changed underneath.
    assert st.plan["sheets"][0]["rows"][0]["persons"][0]["action"] == "new"
    # A wide sheet's new people stay automatic.
    st.sheets[0]["one_column"] = False
    assert community.pending(st) == []
    assert {i["action"] for i in community._items(st)} == {"new", "matched", "skip"}


def test_the_one_column_reason_has_a_plain_label():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "one_column_list:" in js


# ---------------------------------------------------------------------------
# 2. A kept name in a shared cell is not deleted
# ---------------------------------------------------------------------------

def _couple_roster() -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Parents", "Notes"])
    ws.append(["John & Mary Smith", "John Smith coaches soccer"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _couple_result():
    cells = [{"col": 0, "part": "list"}]
    john = {**_person("0:0:0", "John", "Smith", cells, None), "action": "skip", "decided": True}
    mary = _person("0:0:1", "Mary", "Smith", cells, MARY)
    return {"committed": True, "import_runs": ["imp_couple000001"], "summary": {},
            "sheets": [{"rows": [{"index": 0, "persons": [john, mary],
                                  "family": {"key": "0:0:family", "display_name": "Smith", "cell": None,
                                             "action": "new", "community_id": SMITHS}}]}]}


def test_a_kept_name_in_a_couple_cell_is_not_deleted():
    from app import community
    sess = _session("couple.xlsx", _couple_roster(), _couple_result(), flagged=("John Smith", "Mary Smith"))
    ov = community.overrides_for(sess)
    assert (0, 1, 0) not in ov.cells, "the shared cell is not rewritten to Mary's token alone"
    assert MARY in ov.registry and ov.left_alone == 1

    from app import pipeline
    pipeline.confirm_and_scrub(sess, deselected=["John Smith"])
    assert sess.error is None, sess.error
    assert sess.verify_result and sess.verify_result["passed"], sess.verify_result
    got = _grid(sess.output_path)
    assert "John" in got[1][0], "the operator kept John: his text stays"
    assert "Mary" not in got[1][0] and "[PERSON_" in got[1][0], "Mary still gets the normal token"
    assert f"[{MARY}]" not in got[1][0]


# ---------------------------------------------------------------------------
# 3. A frozen commit stays frozen unless the resend proves nothing was written
# ---------------------------------------------------------------------------

def _freeze(c, ifg):
    sid = _decided(c, ifg)
    ifg.delay["commit"] = 2.5                    # written, then answered too late
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 504
    ifg.delay.clear()
    assert ifg.writes == 1
    return sid


def test_a_resend_under_changed_settings_is_not_sent_and_stays_frozen(ifg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "1")       # before app.config is imported
    c = _client()
    sid = _freeze(c, ifg)
    r = c.post("/api/familygraph", json={"base_url": ifg.url, "api_key": "sk_rotated_key_0123456789abcdef"})
    assert r.status_code == 200
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 502
    assert "changed" in r.get_json()["error"] and "upload it again" in r.get_json()["error"]
    assert r.get_json()["community"]["status"] == "commit_unknown"
    assert len(ifg.commits()) == 1, "nothing went out under the new key"
    # Put the original key back: the same request replays.
    assert c.post("/api/familygraph", json={"base_url": ifg.url, "api_key": API_KEY}).status_code == 200
    assert c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []}).status_code == 200
    assert ifg.writes == 1


def test_a_resend_refused_with_stale_decisions_stays_frozen(ifg, monkeypatch):
    monkeypatch.setenv("FAMILYGRAPH_ROSTER_TIMEOUT_S", "1")       # before app.config is imported
    c = _client()
    sid = _freeze(c, ifg)
    # Family Graph no longer finds the replay (another caller scope) and its
    # prior-run check marks the decision stale.
    ifg.keys.clear()
    ifg.expect = {DECISION["key"]: {"action": "create"}}
    refused = copy.deepcopy(flow.REFUSED)
    refused["plan"]["stale_decisions"] = [DECISION["key"]]
    monkeypatch.setattr(flow, "REFUSED", refused)
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 502
    assert "already written" in r.get_json()["error"]
    assert r.get_json()["community"]["status"] == "commit_unknown"
    assert c.post(f"/api/anonymize/{sid}/community/decide",
                  json={"key": DECISION["key"], "action": "create"}).status_code == 409
    assert ifg.writes == 1


# ---------------------------------------------------------------------------
# 4. A request that never left this machine does not freeze the file
# ---------------------------------------------------------------------------

def test_outcome_unknown_ignores_requests_never_sent():
    from app import community, familygraph
    assert not community.outcome_unknown(familygraph.FamilyGraphError("x", 0, not_sent=True))
    assert community.outcome_unknown(familygraph.FamilyGraphError("x", 0))
    assert community.outcome_unknown(familygraph.FamilyGraphError("x", 0, timed_out=True))
    assert community.outcome_unknown(familygraph.FamilyGraphError("x", 503))
    assert not community.outcome_unknown(familygraph.FamilyGraphError("x", 403))


def test_family_graph_cleared_before_confirm_does_not_freeze(ifg):
    c = _client()
    sid = _decided(c, ifg)
    assert c.delete("/api/familygraph").status_code == 204
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 502
    assert r.get_json()["community"]["status"] == "ready"
    assert "may have written" not in r.get_json()["error"]
    assert c.post(f"/api/anonymize/{sid}/community/skip", json={"skip": True}).status_code == 200


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_connection_refused_is_not_sent(ifg, monkeypatch):
    from app import familygraph
    c = _client()
    sid = _decided(c, ifg)
    stored = familygraph.settings()
    monkeypatch.setattr(familygraph, "_read",
                        lambda: {**stored, "base_url": f"http://127.0.0.1:{_closed_port()}"})
    r = c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
    assert r.status_code == 502
    assert r.get_json()["community"]["status"] == "ready"
    with pytest.raises(familygraph.FamilyGraphError) as ei:
        familygraph.commit({})
    assert ei.value.not_sent and not ei.value.timed_out
