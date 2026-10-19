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


def test_chained_merges_record_final_id(state):
    """同じ回答の中で連鎖してまとめると、into は記録した時点の行き先のまま、final は今残っている id になる。"""
    extra, _ = hm.apply_merges(state, [["a", "b"], ["a", "c"]])
    merges = extra["feedback"]["merges"]
    assert [m["into"] for m in merges] == ["a", "c"]
    assert [m["final"] for m in merges] == ["c", "c"]
    assert [m["merged"] for m in merges] == [["b"], ["a"]]
    assert {m["final"] for m in merges} <= {c["id"] for c in extra["pool"]}


def test_previous_merge_records_follow_new_aliases(state):
    """前の回の記録（final あり・final なしの古い形）も、今回さらに吸収された先まで final をたどり直す。"""
    state["feedback"]["merges"] = [
        {"into": "x", "merged": ["y"], "final": "a"},  # 前の回で x → a まで吸収済み
        {"into": "b", "merged": ["z"]},  # final がない古い記録は into から始める
        {"into": "d", "merged": ["w"], "final": "d"},  # 今回まとめていない先はそのまま
    ]
    extra, _ = hm.apply_merges(state, [["a", "b", "c"]])  # 採点の高い c が残り、a・b を吸収する
    merges = extra["feedback"]["merges"]
    assert [m["into"] for m in merges] == ["x", "b", "d", "c"]  # 履歴の into は書き換えない
    assert [m["final"] for m in merges] == ["c", "c", "d", "c"]
    assert {m["final"] for m in merges} <= {c["id"] for c in extra["pool"]}


def _report_state(state, **kw):
    """report() が読むキーを足した State（記事の出どころ・保存日・期間など）。"""
    pool = [{**c, "outlet": "o", "saved_at": "2026-10-01"} for c in kw.pop("pool", state["pool"])]
    return {**state, "pool": pool, "mode": "weekly", "period": "2026-W40", "candidates": [], "topics": [], **kw}


def test_report_counts_chained_merges_by_final(state, tmp_path, monkeypatch):
    """[[a,b],[a,c]] のレポート表示は「2 組（T c ← 2）」で、組の数と中身が合う。"""
    from tsundoku.config import settings
    from tsundoku.nodes.report import report

    monkeypatch.setattr(settings, "workdir", str(tmp_path))
    extra, _ = hm.apply_merges(state, [["a", "b"], ["a", "c"]])
    merged = {**state, **extra, "scored": state["scored"] + extra["scored"]}  # scored は reducer で連結される
    md = report(_report_state(merged))["report_md"]
    assert "- 承認②で人がまとめた話題: 2 組（T c ← 2）" in md


def test_report_merges_old_records_without_final(state, tmp_path, monkeypatch):
    """final がない古い記録だけでも、into ごとに数えて表示できる。"""
    from tsundoku.config import settings
    from tsundoku.nodes.report import report

    monkeypatch.setattr(settings, "workdir", str(tmp_path))
    state["feedback"]["merges"] = [{"into": "a", "merged": ["x"]}, {"into": "a", "merged": ["y", "z"]}]
    md = report(_report_state(state))["report_md"]
    assert "- 承認②で人がまとめた話題: 2 組（T a ← 3）" in md


def test_resume_from_empty_question_does_not_rescore(state, monkeypatch):
    # 全部除外して「次の候補を出すか」で止まり、そこから再開しても、まとめた話題の採点（LLM）は1回だけ
    import operator
    from typing import Annotated, TypedDict

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command

    import tsundoku.nodes.review as rv
    from tsundoku.config import CONFIG

    calls = []
    monkeypatch.setattr(hm, "score_candidate", lambda m, theme, plan: calls.append(m["id"]) or _score(m["id"], 1))
    monkeypatch.setitem(CONFIG, "backfill", False)

    class S(TypedDict, total=False):
        pool: list
        scored: Annotated[list, operator.add]
        shortlist: list
        shown: list
        selected: list
        summaries: dict
        theme: str
        approved_plan: dict
        feedback: dict
        review_action: str

    g = StateGraph(S)
    g.add_node("review", rv.review)
    g.add_node("confirm_empty", rv.confirm_empty)
    g.add_edge(START, "review")
    route = {"shortlist": END, "confirm_empty": "confirm_empty", "summarize": END}
    g.add_conditional_edges("review", rv.route_after_review, route)
    g.add_conditional_edges("confirm_empty", rv.route_after_review, route)
    app = g.compile(checkpointer=MemorySaver())
    cfg = {"configurable": {"thread_id": "t"}}

    app.invoke({**state, "selected": [], "feedback": None}, cfg)
    app.invoke(Command(resume={"approve_all": True, "merge": [["a", "b"]], "reject": {i: "x" for i in "abcd"}}), cfg)
    assert app.get_state(cfg).next == ("confirm_empty",)
    out = app.invoke(Command(resume={"action": "end"}), cfg)
    assert calls == ["a"]  # まとめた話題を1回だけ採点し直す（2回目の再開で走らない）
    assert out["review_action"] == "done"


def test_old_chained_records_resolve_through_history(state, tmp_path, monkeypatch):
    """final がない古い記録で同じ回に連鎖していても（a←b の後に c←a）、履歴をたどって今残っている c にまとまる。"""
    from tsundoku.config import settings
    from tsundoku.nodes.report import report

    monkeypatch.setattr(settings, "workdir", str(tmp_path))
    state["pool"] = [c for c in state["pool"] if c["id"] in "cd"]  # a・b は前の回で吸収済み
    state["feedback"]["merges"] = [{"into": "a", "merged": ["b"]}, {"into": "c", "merged": ["a"]}]
    assert hm.merge_finals(state["feedback"]["merges"]) == ["c", "c"]
    md = report(_report_state(state))["report_md"]
    assert "- 承認②で人がまとめた話題: 2 組（T c ← 2）" in md
    state["feedback"]["merges"][0]["final"] = "a"  # 前の版が吸収済みの id を final に書いた記録も、たどり直す
    state["shortlist"] = ["c", "d"]
    extra, _ = hm.apply_merges(state, [["c", "d"]])
    assert [m["final"] for m in extra["feedback"]["merges"]] == ["c", "c", "c"]


def test_chained_merges_rescore_only_surviving_topic(state, monkeypatch):
    """連鎖してまとめても（a←b の後に c←a）、採点し直す（LLM）のは残った c の1回だけで、中身は a・b を含む。"""
    calls = []
    monkeypatch.setattr(
        hm, "score_candidate", lambda m, theme, plan: calls.append((m["id"], len(m["related"]))) or _score(m["id"], 1)
    )
    extra, _ = hm.apply_merges(state, [["a", "b"], ["a", "c"]])
    assert calls == [("c", 2)]
    assert [s["id"] for s in extra["scored"]] == ["c"]
