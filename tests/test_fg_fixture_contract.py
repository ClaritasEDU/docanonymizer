"""The captured Family Graph fixtures carry the fields Doc Anonymizer reads.

tests/fixtures/fg_*.json are re-captured from a real Family Graph build by
tests/fixtures/capture_fg_fixtures.js. If a recapture renames or retypes a
field app/community.py depends on, this fails instead of the defensive
defaults in community.py quietly hiding it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"
PLAN = json.loads((FIX / "fg_plan.json").read_text())
COMMIT = json.loads((FIX / "fg_commit.json").read_text())
REFUSED = json.loads((FIX / "fg_commit_refused.json").read_text())
DECISION = json.loads((FIX / "fg_decision.json").read_text())


@pytest.mark.parametrize("name,body", [("plan", PLAN), ("commit", COMMIT), ("refused", REFUSED["plan"])])
def test_summary_carries_told_apart_and_crosswalk(name, body):
    s = body["summary"]
    assert isinstance(s["told_apart"], int), name
    assert set(s["crosswalk"]) == {"created", "unchanged", "relinked"}, name
    assert all(isinstance(v, int) for v in s["crosswalk"].values()), name
    for group in ("persons", "families"):
        assert all(isinstance(v, int) for v in s[group].values()), (name, group)


def test_refusal_is_the_real_409_shape():
    assert REFUSED["error"] == "review_incomplete"
    assert REFUSED["plan"]["committed"] is False
    assert REFUSED["plan"]["pending"] == [DECISION["key"]]
    assert isinstance(REFUSED["plan"]["stale_decisions"], list)


def test_matched_household_says_how_it_matched():
    fam = PLAN["sheets"][0]["rows"][1]["family"]
    assert fam["action"] == "matched" and isinstance(fam["via"], str)


def test_the_decision_targets_the_review_candidate():
    marie = PLAN["sheets"][0]["rows"][2]["persons"][0]
    assert marie["key"] == DECISION["key"] and marie["action"] == "review"
    assert [c["community_id"] for c in marie["candidates"]] == [DECISION["target"]]
    assert PLAN["pending"] == [DECISION["key"]]


def test_commit_issues_every_id_and_honours_the_decision():
    rows = COMMIT["sheets"][0]["rows"]
    assert COMMIT["committed"] is True and COMMIT["pending"] == []
    for row in rows:
        for p in row["persons"]:
            assert p["community_id"] and p["community_id"].startswith("I")
        assert row["family"]["community_id"].startswith("F")
    assert rows[2]["persons"][0]["community_id"] == DECISION["target"]
    assert COMMIT["import_runs"] == [s["import_run"] for s in COMMIT["sheets"]]
