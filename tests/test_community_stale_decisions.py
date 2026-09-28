"""Family Graph refuses a commit whose decisions the data has overtaken
(stale_decisions). Those answers must be dropped, not resent forever, and
the operator must be told why when nothing is left to decide. A roster key
Family Graph refuses for its source is explained as that, not as a missing
scope.
"""

from __future__ import annotations

import copy

import pytest

from tests.test_community import TWIN_DECISIONS, _twin_result, _twin_state


class _Sess:
    id = "t"
    fg_rows = 0
    fg_busy = ""


def _matched_plan():
    """The same plan after someone else committed Marie meanwhile: her item
    is now an automatic match and her 'attach' is listed as stale."""
    plan = _twin_result(False)
    marie = plan["sheets"][0]["rows"][2]["persons"][0]
    marie["action"] = "matched"
    marie["stale_decision"] = "decision_no_longer_applies"
    plan["pending"] = ["0:0:1", "0:1:1", "0:1:family"]
    plan["stale_decisions"] = ["0:2:0"]
    return plan


def test_stale_decisions_are_dropped_on_plan():
    from app import community
    st = _twin_state()
    for k, d in TWIN_DECISIONS.items():
        community.decide(st, k, d["action"], d["target"])
    st.plan = _matched_plan()
    community._prune_decisions(_Sess(), st)
    assert "0:2:0" not in st.decisions
    assert set(st.decisions) == set(TWIN_DECISIONS) - {"0:2:0"}


def test_refused_commit_with_only_stale_decisions_drops_them_and_explains(monkeypatch):
    from app import community, familygraph
    st = _twin_state()
    for k, d in TWIN_DECISIONS.items():
        community.decide(st, k, d["action"], d["target"])
    # Everything else decided; Marie's answer went stale.
    refused = _matched_plan()
    for row in refused["sheets"][0]["rows"]:
        for p in row["persons"]:
            if p["key"] != "0:2:0":
                p.setdefault("decided", True)
    refused["pending"] = []
    sent = []
    monkeypatch.setattr(community, "commit_body", lambda sess, state: {"decisions": dict(state.decisions)})
    monkeypatch.setattr(familygraph, "commit", lambda body: (sent.append(copy.deepcopy(body)), (False, refused))[1])
    sess = _Sess()
    sess.community = st
    assert community.commit_for_session(sess) is False
    assert "0:2:0" not in st.decisions, "the stale answer is not resent"
    assert "changed since you decided" in st.commit_error
    # The next confirm sends the remaining answers only.
    community.commit_for_session(sess)
    assert "0:2:0" not in sent[-1]["decisions"]


def test_refusal_with_real_pending_items_keeps_the_old_message(monkeypatch):
    from app import community, familygraph
    st = _twin_state()
    refused = _twin_result(False)
    refused["stale_decisions"] = []
    monkeypatch.setattr(community, "commit_body", lambda sess, state: {})
    monkeypatch.setattr(familygraph, "commit", lambda body: (False, refused))
    sess = _Sess()
    sess.community = st
    assert community.commit_for_session(sess) is False
    assert st.commit_error == "Family Graph found new items that need a decision"


def test_roster_forbidden_is_not_reported_as_a_missing_scope():
    from app import familygraph
    with pytest.raises(familygraph.FamilyGraphError) as exc:
        familygraph._raise_for(403, {"error": "roster_forbidden",
                                     "detail": 'this key may not use source "missioniq"'}, "roster plan")
    assert "roster scope" not in str(exc.value)
    assert "missioniq" in str(exc.value)
    with pytest.raises(familygraph.FamilyGraphError, match="roster scope"):
        familygraph._raise_for(403, {"error": "forbidden"}, "roster plan")
