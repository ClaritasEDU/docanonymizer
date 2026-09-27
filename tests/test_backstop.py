"""Deterministic recall on top of the LLM (backstop.py), reproducing what real
llama3.2 missed on a 25-row pledge sheet."""

from __future__ import annotations

import io
import json
import time
from unittest.mock import patch


def test_patterns_catch_the_shapes_the_model_missed():
    from app.backstop import pattern_hits
    text = ("Spoke with John; spouse Ann prefers texts at 512-555-2202. "
            "Lives at 177 Elm Dr, Austin TX 78701. SSN 123-45-6789, card 4111 1111 1111 1111, "
            "bad card 4111 1111 1111 1112, host 192.168.1.20, version 1.2.3.4, a@b.org")
    hits = dict(pattern_hits(text, {"PHONE", "ADDRESS", "ID", "FINANCIAL", "IP", "EMAIL"}))
    assert hits == {
        "512-555-2202": "PHONE", "177 Elm Dr, Austin TX 78701": "ADDRESS", "123-45-6789": "ID",
        "4111 1111 1111 1111": "FINANCIAL", "192.168.1.20": "IP", "a@b.org": "EMAIL",
    }


def test_patterns_respect_the_selected_types():
    from app.backstop import pattern_hits
    assert pattern_hits("a@b.org 512-555-2202", {"EMAIL"}) == [("a@b.org", "EMAIL")]


def test_column_consensus_fills_a_mostly_detected_column():
    from app.backstop import column_consensus
    from app.detector import is_plain_amount
    rows = [["Donor", "Pledge", "Notes"],
            ["Maria Gonzalez", "250", "Called Maria"],
            ["Ana Delgado", "500", "No contact"],          # model missed this name
            ["John O'Brien", "100", "Spoke after Mass"],
            ["Grace Park", "1000", "-"]]
    detected = {"Maria Gonzalez": "PERSON", "John O'Brien": "PERSON", "Grace Park": "PERSON",
                "250": "PERSON"}                           # the model's junk amount tag
    added = column_consensus([rows], detected, is_plain_amount)
    assert added == [("Ana Delgado", "PERSON")]            # never the header, never amounts


def test_column_consensus_numeric_pii_columns():
    from app.backstop import column_consensus
    from app.detector import is_plain_amount
    rows = [["Student ID", "Score"], ["40021", "94"], ["40022", "87"], ["40023", "91"]]
    added = column_consensus([rows], {"40021": "SID", "40022": "SID", "94": "GRADE", "87": "GRADE"},
                             is_plain_amount)
    assert sorted(added) == [("40023", "SID"), ("91", "GRADE")]


def test_no_consensus_below_half():
    from app.backstop import column_consensus
    from app.detector import is_plain_amount
    rows = [["City"], ["Austin"], ["Dallas"], ["Houston"], ["Waco"], ["Tyler"]]
    assert column_consensus([rows], {"Austin": "ADDRESS"}, is_plain_amount) == []


def test_pledge_sheet_through_the_api_with_a_model_that_misses_things():
    """The real llama3.2 failure modes, stubbed: it misses a donor, phones in
    notes, and addresses, and tags pledges as PERSON. Nothing may ship."""
    from openpyxl import Workbook
    from app import detector
    from app.server import create_app
    people = [("Maria Gonzalez", "maria@gmail.com", "(512) 555-1100", "100 Oak St, Austin TX 78700"),
              ("Ana Delgado", "ana@gmail.com", "(512) 555-1101", "107 Maple Ave, Austin TX 78701"),
              ("John O'Brien", "john@gmail.com", "(512) 555-1102", "114 Cedar Ln, Austin TX 78702"),
              ("Grace Park", "grace@gmail.com", "(512) 555-1103", "121 Elm Dr, Austin TX 78703")]
    wb = Workbook()
    ws = wb.active
    ws.append(["Donor", "Email", "Phone", "Address", "Pledge", "Notes"])
    for i, p in enumerate(people):
        ws.append(list(p) + [(i + 1) * 250, f"Spouse prefers texts at 512-555-22{i:02d}."])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    model_answer = [{"text": p[0], "type": "PERSON"} for p in people if p[0] != "Ana Delgado"]
    model_answer += [{"text": p[1], "type": "EMAIL"} for p in people]
    model_answer += [{"text": "250", "type": "PERSON"}, {"text": "500", "type": "FINANCIAL"}]
    c = create_app().test_client()
    with patch.object(detector.llm, "llm_call", return_value=json.dumps(model_answer)):
        sid = c.post("/api/anonymize/upload", data={"file": (buf, "pledges.xlsx"), "tags": "ALL"},
                     content_type="multipart/form-data").get_json()["session_id"]
        for _ in range(100):
            s = c.get(f"/api/anonymize/{sid}/status").get_json()
            if s.get("detection_complete"):
                break
            time.sleep(0.05)
        c.post(f"/api/anonymize/{sid}/confirm", json={"deselected": []})
        for _ in range(100):
            res = c.get(f"/api/anonymize/{sid}/results").get_json()
            if res.get("verify_result"):
                break
            time.sleep(0.05)
    assert res["verify_result"]["passed"]
    text = c.get(f"/api/anonymize/{sid}/text").get_json()["text"]
    for p in people:
        for v in p:
            assert v not in text, v
    for i in range(4):
        assert f"512-555-22{i:02d}" not in text
    pledges = [line.split("\t")[4] for line in text.splitlines()[1:]]
    assert pledges == ["250", "500", "750", "1000"]
