"""承認②で人がまとめる処理（apply_merges）を、偽の State と固定の採点で単体で確かめる（LLM を呼ばない）。"""

import pytest

import tsundoku.nodes.human_merge as hm


def _cand(i):
    return {
        "id": i,
        "title": f"T {i}",
        "url": f"https://x.example.com/{i}",
        "feed": "f",
        "abstract": "abc",
        "entities": [],
        "keywords": [],
    }


def _score(i, relevance):
    return {"id": i, "relevance": relevance, "novelty": 1, "impact": 1, "reason": "r"}


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(hm, "score_candidate", lambda m, theme, plan: {**_score(m["id"], 1), "reason": "merged"})
    return {
        "pool": [_cand(i) for i in "abcd"],
        "scored": [_score("a", 3), _score("b", 1), _score("c", 5), _score("d", 1)],
        "shortlist": list("abcd"),
        "shown": list("abcd"),
        "selected": ["a"],
        "summaries": {},
        "theme": "t",
        "approved_plan": {"focus_keywords": ["x"]},
        "feedback": {"proposed": list("abcd"), "approved": [], "rejected": {"a": "古い"}, "added": []},
    }


def test_chained_merges_resolve_to_final_id(state):
    """a←b の後に c←a とまとめ先が移っても、消えた a・b はどこにも残らず、b を含む3組目も c にまとまる。"""
    extra, alias = hm.apply_merges(state, [["a", "b"], ["a", "c"], ["b", "d"]])
    assert [c["id"] for c in extra["pool"]] == ["c"]
    assert alias == {"a": "c", "b": "c", "d": "c"}
    assert extra["shortlist"] == ["c"] and extra["shown"] == ["c"] and extra["selected"] == ["c"]
    assert extra["feedback"]["proposed"] == ["c"] and extra["feedback"]["rejected"] == {"c": "古い"}
    assert [m["into"] for m in extra["feedback"]["merges"]] == ["a", "c", "c"]
    assert {r["url"] for r in extra["pool"][0]["related"]} == {f"https://x.example.com/{i}" for i in "abd"}
