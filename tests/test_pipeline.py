"""サンプルデータ＋偽 LLM で、起動 → 承認① → 採点 → 承認② → 要約 → レポートまで通す（API を呼ばず、費用ゼロ）。

デモの流れ（起動 → 承認① → 採点 → 承認② → 要約 → レポート）と、設計時の確認項目を pytest にしたもの。
"""

import csv
import datetime as dt
import json
import os
import sqlite3
from collections import Counter

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from tsundoku.config import CONFIG, log_start, settings, setup
from tsundoku.graph import build_graph
from tsundoku.guardrails import check_report
from tsundoku.llm import FAKE_TOKENS, cost_jpy, reset_tokens
from tsundoku.nodes.cluster import SAME_TOPIC, title_part, topic_score
from tsundoku.nodes.fetch import allocate
from tsundoku.nodes.report import report
from tsundoku.nodes.scoring import buzz, ja_title, total_score
from tsundoku.periods import JST, period_label, period_range
from tsundoku.sources import normalize_url, register

DEMO_SOURCES = [
    {"name": "inoreader-saved", "type": "inoreader_api", "limit": 1000, "quota": 100},
    {"name": "broken-feed (demo)", "type": "always_fail"},  # わざと失敗する取得元（registry で差し込む）
]


@register("always_fail")
def _always_fail(source, *, mode, period, limit=100):
    raise ConnectionError("simulated outage")


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """デモの流れを最後まで動かし、最終状態と途中の値を返す。"""
    work = str(tmp_path_factory.mktemp("work"))
    setup(fake_llm=True, sample_data=True, workdir=work, quiet=True)
    reset_tokens()  # 他のテストで呼んだ偽 LLM の分を数えない
    # サンプル記事に合わせた設定：目安10件・しきい値は自動・次点埋めなし・付録まで・キーワード3語
    CONFIG.update(
        shortlist_size=10,
        shortlist_min_score=None,
        backfill=False,
        summary_scope="all",
        focus_keywords=["VLA", "world model", "agent benchmark"],
    )
    db = os.path.join(work, "checkpoints.demo.sqlite")
    cfg = {"configurable": {"thread_id": "weekly-2026-W40"}}
    log_start(os.path.join(work, "log-demo-weekly-2026-W40.txt"))

    # 1) 起動：取得 → まとめ → 集計 → 計画 → 承認①で止まる
    app = build_graph(SqliteSaver(sqlite3.connect(db, check_same_thread=False)))
    app.invoke({"mode": "weekly", "period": "2026-W40", "theme": CONFIG["theme"], "sources": DEMO_SOURCES}, cfg)
    assert app.get_state(cfg).next == ("approve_plan",)
    assert app.get_state(cfg).interrupts[0].value["proposal"]["suggestions"]

    # 2) 承認①：採点件数を 100 → 20 に直して再開 → 枠 → 採点 → 承認②で止まる
    app.invoke(Command(resume={"approved": True, "max_items": 20}), cfg)
    snap = app.get_state(cfg)
    assert snap.next == ("review",)

    # 3) プロセスを落とした想定：新しい接続・新しいグラフで同じ thread_id から再開
    app2 = build_graph(SqliteSaver(sqlite3.connect(db, check_same_thread=False)))
    v = app2.get_state(cfg).values
    sl = v["shortlist"]
    hints = app2.get_state(cfg).interrupts[0].value["merge_hints"]  # 「同じ話題かも」の推薦
    acme = next(c["id"] for c in v["pool"] if "RoboMind-2" in c["title"])  # 同じ話題を4本まとめた記事
    approve = [i for i in sl if i != acme][:2] + ([acme] if acme in sl else [])
    reject = next(i for i in sl if i not in approve and i != acme)
    add = [acme] if acme not in sl else [next(c["id"] for c in v["pool"] if c["id"] not in sl)]
    if len(approve) < 3:
        approve.append(next(i for i in sl if i not in approve + [reject]))
    # 人がまとめる：提示の末尾2件（承認・除外・追加に使っていないもの）を1つの話題に
    merge = [i for i in sl if i not in approve + [reject] + add and i != acme][-2:]
    # 承認②：3本を承認、1本を「既読」で除外、1本を追加、2話題を1つにまとめる
    app2.invoke(
        Command(resume={"approve": approve, "reject": {reject: "既読"}, "add": add, "merge": [merge]}),
        cfg,
    )
    final = app2.get_state(cfg).values
    assert not app2.get_state(cfg).next
    return {
        "work": work,
        "db": db,
        "cfg": cfg,
        "before": v,  # 承認②の直前（人が手を入れる前）の State
        "final": final,
        "approve": approve,
        "add": add,
        "merge": merge,
        "hints": hints,
    }


def test_human_in_the_loop_and_resume(demo):
    final = demo["final"]
    assert final["approved_plan"]["max_items"] == 20  # 人の修正が反映された
    assert final["selected"] == demo["approve"] + demo["add"]  # 承認3本＋追加1本が確定した
    assert final["metrics"]["adoption_rate"] == round(3 / len(final["shortlist"]), 2)


def test_fetch_cluster_aggregate(demo):
    final, work = demo["final"], demo["work"]
    assert any("broken-feed" in f for f in final["failed_sources"])  # 失敗した取得元が記録された
    assert final["funnel"]["inoreader-saved"] == {"read": 150, "topics": 147, "scored": 20}
    raw = os.path.join(work, "inoreader-inoreader-saved-weekly-2026-W40.json")
    assert len(json.load(open(raw))) == 160  # 取ったままの JSON（前の週の10本も含む）
    titles = [c["title"] for c in final["pool"]]
    assert titles[:2] == [  # キーワードに合う記事が先に並び、同じ話題は1枠だけ
        "[Sample blog] Acme Robotics releases RoboMind-2, an agent reasoning benchmark for robots",
        "[Sample blog] Zentra releases VeloBench-3, an agent reasoning benchmark for robots",
    ]
    acme = next(c for c in final["pool"] if "RoboMind-2" in c["title"])
    assert {r["url"].split("/")[2] for r in acme["related"]} == {
        "roboweekly.example.com",
        "labnotes.example.com",
        "technews.example.com",
    }
    assert acme["category"] == "Physical AI" and "robot" in acme["keywords"]
    st = final["stats"]
    assert st["saved"] == 150 and st["topics"] == 147
    assert len(st["by_time"]) == 7 and sum(n for _, n in st["by_time"]) == 150
    assert (st["top_topics"][0]["articles"], len(st["top_topics"][0]["outlets"])) == (4, 3)
    assert dict(st["by_tag"]) == {"business": 47, "robotics": 5, "agents": 2}
    assert sum(v["topics"] for _, v in st["by_category"]) == 147
    assert os.path.exists(os.path.join(work, "weekly-2026-W40-articles.csv"))


def test_threshold_shortlist(demo):
    v = demo["before"]  # shortlist の出力（人がまとめる前）
    pool = {c["id"]: c for c in v["pool"]}
    tot = sorted((total_score(s, pool[s["id"]]) for s in v["scored"]), reverse=True)
    thr = tot[9]  # 目安10件 → 上位10番目の合計点がしきい値
    scored = {s["id"]: s for s in v["scored"]}
    assert len(v["shortlist"]) >= 10
    assert all(total_score(scored[i], pool[i]) >= thr for i in v["shortlist"])
    assert len(v["shortlist"]) == sum(t >= thr for t in tot)  # しきい値以上は全部提示（件数で切らない）


def test_human_merge(demo):
    """承認②で人が2話題を1つにまとめると、pool・提示・採点・要約が1つになり、記録に残る。"""
    final, before, (a, b) = demo["final"], demo["before"], demo["merge"]
    pool = {c["id"]: c for c in final["pool"]}
    keep = next(m["into"] for m in final["feedback"]["merges"])
    gone = next(m["merged"] for m in final["feedback"]["merges"])
    assert {keep, *gone} == {a, b} and len(gone) == 1
    assert gone[0] not in pool and keep in pool  # 吸収した方は pool から消え、残した方の related に入る
    rel = pool[keep]["related"]
    assert rel[0]["why"].startswith("人がまとめた")
    assert rel[0]["url"] == next(c["url"] for c in before["pool"] if c["id"] == gone[0])
    assert len(final["pool"]) == len(before["pool"]) - 1 and len(final["shortlist"]) == len(before["shortlist"]) - 1
    assert gone[0] not in final["shortlist"] and gone[0] not in final["feedback"]["proposed"]
    scores = {s["id"]: s for s in final["scored"]}  # 残した話題は、まとめた後の話題として採点し直されている
    assert sum(1 for s in final["scored"] if s["id"] == keep) == 2 and "reason" in scores[keep]
    assert final["summaries"][keep]["articles"] >= 2  # 要約は両方の記事を横断して1つ
    assert gone[0] not in final["summaries"]
    assert "承認②で人がまとめた話題: 1 組" in final["report_md"]
    assert demo["hints"] is not None and all({"a", "b", "score", "why"} <= set(h) for h in demo["hints"])


def test_report_and_trace(demo):
    final, work = demo["final"], demo["work"]
    md = final["report_md"]
    for needle in [
        "## 集計",
        "## 今回読むもの",
        "## 全話題",
        "## 付録",
        "[Physical AI]",
        "同じ話題: 3本",
        "（要約の元: ",
        "| 基準 | 評価点 | 文章 |",
        "| 総評 |",
        "関連度 ",
        "新規性 ",
        "注目度 ",
        "合計",
    ]:
        assert needle in md, needle
    assert all(final["summaries"][pid]["text"] for pid in final["selected"])  # 読むものには要約が付く
    assert len(final["summaries"]) == len(final["pool"])  # summary_scope = all：付録まで要約
    assert os.path.exists(os.path.join(work, "report-weekly-2026-W40.md"))
    assert os.path.exists(os.path.join(work, "report-weekly-2026-W40.docx"))
    assert os.path.exists(os.path.join(work, "log-demo-weekly-2026-W40.txt"))
    trace = list(csv.DictReader(open(os.path.join(work, "weekly-2026-W40-trace.csv"), encoding="utf-8-sig")))
    stages = Counter(r["stage"] for r in trace)
    assert len(trace) == 150 and stages["確定"] == 4 and stages["まとめ"] == 4  # 自動3本＋人がまとめた1本
    assert all(k in trace[0] for k in ("total_score", "relevance_reason", "novelty_reason", "impact_reason"))


def test_guardrails(demo):
    final = demo["final"]
    assert "input:suspicious_text:https://bizwire.example.com/2" in final["guardrail_flags"]  # 仕込んだ指示文を検出
    assert check_report(["zzz"], final["pool"]) == ["output:unknown_id:zzz"]  # 出力側の実在照合


def test_empty_period_skips_review(demo):
    """期間内0件のときは承認②を飛ばして終わる（期間を間違えても落ちない）。"""
    app0 = build_graph(SqliteSaver(sqlite3.connect(demo["db"], check_same_thread=False)))
    cfg0 = {"configurable": {"thread_id": "weekly-2026-W30"}}
    app0.invoke({"mode": "weekly", "period": "2026-W30", "theme": CONFIG["theme"], "sources": DEMO_SOURCES[:1]}, cfg0)
    app0.invoke(Command(resume={"approved": True}), cfg0)
    assert not app0.get_state(cfg0).next and app0.get_state(cfg0).values["metrics"]["fetched"] == 0


def test_report_tolerates_old_scores(demo):
    """古い版の採点（summary_ja なし）が残っていてもレポートが落ちない。"""
    final = demo["final"]
    old = {
        **final,
        "scored": [{k: v for k, v in s.items() if k not in ("summary_ja", "title_ja")} for s in final["scored"]],
    }
    assert "## 全話題" in report(old)["report_md"]


def test_token_cost(demo):
    """偽 LLM でもトークン数を数え、レポートの「選定の記録」に費用の目安が出る（F-18）。"""
    m = demo["final"]["metrics"]
    assert m["tokens"] > 0 and m["tokens"] == m["tokens_input"] + m["tokens_output"]
    calls = m["tokens_input"] // FAKE_TOKENS[0]  # 偽 LLM は1回あたり固定のトークン数
    assert (m["tokens_input"], m["tokens_output"]) == (calls * FAKE_TOKENS[0], calls * FAKE_TOKENS[1])
    assert m["cost_jpy"] == round(cost_jpy({"input": m["tokens_input"], "output": m["tokens_output"]}), 1)
    assert any(line.startswith("- LLM トークン: 入力 ") for line in demo["final"]["report_md"].splitlines())


# --- 純粋関数の確認（グラフを動かさない）--------------------------------------------------------
def test_periods():
    assert period_range("weekly", "2026-W40") == (
        dt.datetime(2026, 9, 28, tzinfo=JST).timestamp(),
        dt.datetime(2026, 10, 5, tzinfo=JST).timestamp(),
    )
    assert period_range("monthly", "2026-12")[1] == dt.datetime(2027, 1, 1, tzinfo=JST).timestamp()
    assert period_label("weekly", "2026-W41") == "2026-W41（10/5〜10/11）"


def test_normalize_period():
    from tsundoku.periods import normalize_period

    t = dt.date(2026, 10, 14)
    assert normalize_period("weekly", "2026-10-06", t) == "2026-W41"
    assert normalize_period("weekly", "2026/10/11", t) == "2026-W41"
    assert normalize_period("weekly", None, t) == "2026-W41" and normalize_period("weekly", -2, t) == "2026-W40"
    assert normalize_period("monthly", "2026-9", t) == "2026-09"
    assert normalize_period("monthly", "last", t) == "2026-09" and normalize_period("monthly", -10, t) == "2025-12"


def test_clustering_rules():
    assert normalize_url("http://www.a.example.com/x/?utm_source=rss#top") == "https://a.example.com/x"
    rss = {"source": "rss"}
    # 製品名の一致で同じ話題（日英）
    assert (
        topic_score(rss, rss, 0.3, {"meta", "muse wonder"}, {"meta", "muse wonder"}, {"muse wonder"}, {"muse wonder"})[
            0
        ]
        >= SAME_TOPIC
    )
    assert title_part({"title": "Meta、『Muse Wonder』発表"}, {"meta", "muse wonder", "wonder"}) == {
        "meta",
        "muse wonder",
        "wonder",
    }
    # 本文の地名だけが共通：見出しの固有名詞が食い違うのでまとめない
    e1, e2 = {"openai", "gpt-6", "ohio"}, {"google", "gemini-4", "ohio"}
    assert topic_score(rss, rss, 0.85, e1, e2, {"openai", "gpt-6"}, {"google", "gemini-4"})[0] == 0


def test_allocate_and_titles():
    assert allocate({"a": [1] * 5, "b": [2] * 50}, {"a": 70, "b": 30}, 20) == {"a": [1] * 5, "b": [2] * 15}
    assert (
        ja_title({"title": "T"}, {"title_ja": ""}) == "T"
        and ja_title({"title": "T"}, {"title_ja": "日本語"}) == "日本語"
    )


def test_buzz_and_total_score():
    def cand(*outlets, same_id=()):
        rel = [{"outlet": o, "why": "似た話題"} for o in outlets[1:]] + [
            {"outlet": o, "why": "同じ ID"} for o in same_id
        ]
        return {"outlet": outlets[0], "related": rel}

    # 媒体1つで0、2つで1、5つでも3で頭打ち（同じ媒体の重複は1つに数える）
    assert buzz(cand("A")) == 0 and buzz(cand("A", "A")) == 0
    assert buzz(cand("A", "B")) == 1
    assert buzz(cand("A", "B", "C", "D", "E")) == 3
    # 「同じ ID」の記事（同じ論文の別経路など）は別媒体でも数えない
    assert buzz(cand("A", same_id=("B",))) == 0 and buzz(cand("A", "B", same_id=("C",))) == 1
    # 合計 ＝ 関連度 ＋ 新しさ ＋ impact ＋ buzz
    assert total_score({"relevance": 4, "novelty": 3, "impact": 2}, cand("A", "B", "C")) == 4 + 3 + 2 + 2


def test_settings_restored():
    """テストが設定を汚しても、本番の既定値は default_config.yaml から読み直せる。"""
    setup(fake_llm=True, sample_data=True, workdir=settings.workdir, quiet=True)
    assert CONFIG["shortlist_size"] == 20 and CONFIG["summary_scope"] == "all"
