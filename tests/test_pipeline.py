"""サンプルデータ＋偽 LLM で、起動 → 承認① → 採点 → 承認② → 要約 → レポートまで通す（API を呼ばず、費用ゼロ）。

デモの流れ（起動 → 承認① → 採点 → 承認② → 要約 → レポート）と、設計時の確認項目を pytest にしたもの。
"""

import contextlib
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import threading
from collections import Counter

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import tsundoku.llm as llm
from tsundoku.config import CONFIG, log_start, settings, setup
from tsundoku.graph import build_graph
from tsundoku.guardrails import check_report
from tsundoku.llm import FAKE_TOKENS, cost_jpy, reset_tokens, structured_call, token_usage
from tsundoku.models import Score
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
    # 偽 LLM の実際の呼び出し回数を数える（トークンの数え方を、回数から逆算せずに突き合わせるため）
    calls = {"n": 0}
    lock = threading.Lock()
    real_fake = llm._fake

    def counting_fake(schema, user):
        with lock:  # score_one・summarize は並列に呼ばれる
            calls["n"] += 1
        return real_fake(schema, user)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm, "_fake", counting_fake)
        result = _run_demo(tmp_path_factory)
    result["llm_calls"] = calls["n"]
    return result


def _run_demo(tmp_path_factory):
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
    calls = demo["llm_calls"]  # fixture で数えた、偽 LLM の実際の呼び出し回数
    assert calls > 0 and m["tokens"] > 0 and m["tokens"] == m["tokens_input"] + m["tokens_output"]
    # 1回の呼び出しにつき固定のトークン数を、ちょうど1回ずつ数えている（二重に数えれば合わない）
    assert (m["tokens_input"], m["tokens_output"]) == (calls * FAKE_TOKENS[0], calls * FAKE_TOKENS[1])
    assert m["cost_jpy"] == round(cost_jpy({"input": m["tokens_input"], "output": m["tokens_output"]}), 1)
    lines = [ln for ln in demo["final"]["report_md"].splitlines() if "LLM トークン" in ln]
    assert len(lines) == 1
    hit = re.fullmatch(r"- LLM トークン: 入力 ([\d,]+) ・ 出力 ([\d,]+)（約 ([\d.]+) 円）", lines[0])
    assert hit, lines[0]
    n, out, yen = hit.groups()
    assert int(n.replace(",", "")) == m["tokens_input"] and int(out.replace(",", "")) == m["tokens_output"]
    assert float(yen) == m["cost_jpy"]


# --- F-18：本物の LLM 経路のトークン計数（init_chat_model を偽物に差し替え、API は呼ばない）------------------
class _FakeChat:
    def __init__(self, out):
        self.out = out
        self.schema = None

    def with_structured_output(self, schema, include_raw=False):
        assert include_raw  # usage_metadata を受け取るために必須
        self.schema = schema
        return self

    def invoke(self, messages):
        return self.out


@pytest.fixture
def real_llm(monkeypatch):
    """settings.fake_llm=False にして、init_chat_model が返す応答を差し替えられるようにする。"""
    import langchain.chat_models

    monkeypatch.setattr(settings, "fake_llm", False)
    monkeypatch.setitem(CONFIG, "model", "openai:dummy")
    holder = {}

    def fake_init(model, **kw):
        return _FakeChat(holder["out"])

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init)
    reset_tokens()
    yield holder
    reset_tokens()


def _raw(usage=True):
    meta = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    return AIMessage(content="", usage_metadata=meta) if usage else AIMessage(content="")


def test_real_call_counts_tokens(real_llm):
    from tsundoku.models import Summary

    parsed = Summary(text="t", sources_used=1)
    real_llm["out"] = {"raw": _raw(), "parsed": parsed, "parsing_error": None}
    assert structured_call(Summary, "s", "u") is parsed
    assert token_usage() == {"input": 7, "output": 3}


def test_real_call_parsing_error_still_counted(real_llm):
    from tsundoku.models import Summary

    err = RuntimeError("bad json")
    real_llm["out"] = {"raw": _raw(), "parsed": None, "parsing_error": err}
    with pytest.raises(RuntimeError) as e:
        structured_call(Summary, "s", "u")
    assert e.value is err
    assert token_usage() == {"input": 7, "output": 3}  # 形式が崩れた応答も費用はかかる


def test_real_call_empty_parsed_raises(real_llm):
    from tsundoku.models import Summary

    real_llm["out"] = {"raw": _raw(), "parsed": None, "parsing_error": None}
    with pytest.raises(ValueError):
        structured_call(Summary, "s", "u")
    assert token_usage() == {"input": 7, "output": 3}


def test_real_call_without_usage_metadata(real_llm):
    from tsundoku.models import Summary

    parsed = Summary(text="t", sources_used=1)
    real_llm["out"] = {"raw": _raw(usage=False), "parsed": parsed, "parsing_error": None}
    assert structured_call(Summary, "s", "u") is parsed
    assert token_usage() == {"input": 0, "output": 0}


def test_cost_jpy(monkeypatch):
    monkeypatch.setitem(CONFIG, "price_per_1m_input", 60)
    monkeypatch.setitem(CONFIG, "price_per_1m_output", 240)
    # 入力 50万 × 60円/100万 ＋ 出力 10万 × 240円/100万 ＝ 30 ＋ 24 ＝ 54円
    assert cost_jpy({"input": 500_000, "output": 100_000}) == pytest.approx(54.0)


def test_token_cost_in_log(demo):
    """実行ログの [report] の行に「LLM 約 X 円」が出る（F-18 の出力）。"""
    log_text = open(os.path.join(demo["work"], "log-demo-weekly-2026-W40.txt"), encoding="utf-8").read()
    # 後のテスト（期間0件の実行や report の呼び直し）も同じログに追記するので、fixture の実行が書いた最初の行を見る
    lines = [ln for ln in log_text.splitlines() if "[report] 取得" in ln and "report-weekly-2026-W40.md" in ln]
    assert lines and f"／LLM 約 {demo['final']['metrics']['cost_jpy']} 円 → " in lines[0]


@contextlib.contextmanager
def _keep_config():
    """CONFIG・settings・実行ログの行き先・トークン数を、抜けるときに元に戻す（setup を呼ぶテスト用）。"""
    import copy
    import dataclasses

    import tsundoku.config as config

    saved_config, saved_settings, saved_log = copy.deepcopy(CONFIG), dataclasses.replace(settings), dict(config._LOG)
    try:
        yield
    finally:
        CONFIG.clear()
        CONFIG.update(saved_config)
        for f in dataclasses.fields(settings):
            setattr(settings, f.name, getattr(saved_settings, f.name))
        config._LOG.clear()
        config._LOG.update(saved_log)
        reset_tokens()


def test_default_config_prices():
    """default_config.yaml に料金（円／100万トークン）があり、cost_jpy が読むキー名と合っている（F-18 の設定）。"""
    import yaml

    from tsundoku.config import DEFAULT_CONFIG_PATH

    data = yaml.safe_load(open(DEFAULT_CONFIG_PATH, encoding="utf-8"))
    assert (data["price_per_1m_input"], data["price_per_1m_output"]) == (60, 240)  # 第14章の初期値
    with _keep_config():
        setup(fake_llm=True, sample_data=True, config_path=DEFAULT_CONFIG_PATH, quiet=True)
        assert (CONFIG["price_per_1m_input"], CONFIG["price_per_1m_output"]) == (60, 240)
        # 入力だけ・出力だけで、それぞれのキーの値が効いている（キー名が食い違えば同梱の値にも CONFIG にも届かない）
        assert cost_jpy({"input": 1_000_000, "output": 0}) == 60
        assert cost_jpy({"input": 0, "output": 1_000_000}) == 240


def test_prices_fallback_when_config_lacks_them(tmp_path):
    """料金を書いていない config.yaml でも 0 円にならず、同梱の既定値で計算する。書けばその値が優先される（F-18）。"""
    from pathlib import Path

    import yaml

    from tsundoku.config import DEFAULT_CONFIG_PATH

    data = yaml.safe_load(open(DEFAULT_CONFIG_PATH, encoding="utf-8"))
    old = {k: v for k, v in data.items() if not k.startswith("price_per_1m_")}  # 料金の項目より前に作った config.yaml
    assert len(old) == len(data) - 2
    both = {"input": 1_000_000, "output": 1_000_000}
    with _keep_config():
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(old, allow_unicode=True), encoding="utf-8")
        setup(fake_llm=True, sample_data=True, workdir=str(tmp_path / "w1"), config_path=str(path), quiet=True)
        assert "price_per_1m_input" not in CONFIG and "price_per_1m_output" not in CONFIG
        assert cost_jpy(both) == 60 + 240  # 0 円ではない

        path.write_text(
            yaml.safe_dump({**old, "price_per_1m_input": 1, "price_per_1m_output": 2}, allow_unicode=True),
            encoding="utf-8",
        )
        setup(fake_llm=True, sample_data=True, workdir=str(tmp_path / "w2"), config_path=str(path), quiet=True)
        assert cost_jpy(both) == 1 + 2  # config.yaml に書いた料金が優先

        example = Path(__file__).resolve().parents[1] / "config.example.yaml"
        ex = yaml.safe_load(open(example, encoding="utf-8"))
        assert (ex["price_per_1m_input"], ex["price_per_1m_output"]) == (60, 240)
        setup(fake_llm=True, sample_data=True, workdir=str(tmp_path / "w3"), config_path=str(example), quiet=True)
        assert cost_jpy(both) == 300
    # 元に戻っている（他のテストに影響させない）
    assert settings.workdir != str(tmp_path / "w3") and CONFIG.get("price_per_1m_input") != 1


# --- F-18：tsundoku run の経路（run_interactive）で、実行ごとの数え直しと再開をまたいだ扱い ---------------
# ノードの途中で落とす実行の期間。thread_id が別になるよう W40 の実行と別の期間にする（サンプルの 10/1〜10/4 の96話題）
CRASH_MODE, CRASH_PERIOD = "monthly", "2026-10"
CRASH_AT = 4  # この回数目の score_one（Score）の呼び出しで落とす


@pytest.fixture(scope="module")
def cli_runs(tmp_path_factory):
    """偽 LLM・サンプル・--yes で run_interactive を動かす。

    W40：1回目は承認②で止めたところで落とし（Ctrl+C の想定）、2回目で同じ期間を再開して完了させる。
    3回目は fresh=True で止めずに最後まで通す。
    CRASH_PERIOD：4回目は CRASH_AT 回目の score_one で例外を投げて落とし（ノードの途中で落ちた想定）、5回目で再開して完了させる。
    6回目は fresh=True で止めずに最後まで通す（呼び出し回数の基準）。
    各回の偽 LLM の呼び出し回数と画面の出力を返す。
    """
    import io

    import tsundoku.cli as cli
    from tsundoku.config import DEFAULT_CONFIG_PATH

    calls = {"n": 0, "score": 0}
    lock = threading.Lock()
    real_fake, real_ask = llm._fake, cli.ask
    crash = {"armed": False, "at": 0}  # at：落とす Score の呼び出しの通し番号

    def counting_fake(schema, user):
        with lock:
            if schema is Score and crash["armed"] and calls["score"] + 1 == crash["at"]:
                crash["armed"] = False
                # RetryPolicy が再試行しない例外（default_retry_on）。LLM の応答を受け取る前に落ちたので回数に数えない
                raise RuntimeError("simulated crash in score_one")
            calls["n"] += 1
            calls["score"] += schema is Score
        return real_fake(schema, user)

    stop = {"armed": True}

    def ask_then_crash(value):
        if value["type"] == "review" and stop["armed"]:
            stop["armed"] = False
            raise KeyboardInterrupt  # 承認②の入力中にプロセスが落ちた想定
        return real_ask(value)

    def run(period, mode="weekly", **kw):
        start, score_start = calls["n"], calls["score"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.run_interactive(mode, period, **kw)
        return calls["n"] - start, calls["score"] - score_start, buf.getvalue()

    work = str(tmp_path_factory.mktemp("cli"))
    out = {"work": work}
    with _keep_config(), pytest.MonkeyPatch.context() as mp:
        mp.setattr(llm, "_fake", counting_fake)
        mp.setattr(cli, "ask", ask_then_crash)
        mp.setattr(cli, "AUTO_APPROVE", True)
        setup(fake_llm=True, sample_data=True, workdir=work, config_path=DEFAULT_CONFIG_PATH, quiet=True)

        # 1回目：前の実行の分が残っている想定で、わざと数を入れておく（頭で 0 に戻るはず）
        llm.count_tokens(123_456, 7_890)
        with contextlib.redirect_stdout(io.StringIO()), pytest.raises(KeyboardInterrupt):
            cli.run_interactive("weekly", "2026-W40")
        out["calls_before"] = calls["n"]
        out["usage_before"] = token_usage()

        # 2回目：同じ期間を指定して再開（同じプロセスだが、run_interactive の頭で数え直す）
        out["calls_after"], _, out["stdout_resumed"] = run("2026-W40")

        # 3回目：fresh=True で最初から止めずに通す
        out["calls_fresh"], _, out["stdout_fresh"] = run("2026-W40", fresh=True)

        # 4回目：別の期間で、score_one の途中で落とす
        start, score_start = calls["n"], calls["score"]
        crash["armed"], crash["at"] = True, calls["score"] + CRASH_AT
        with contextlib.redirect_stdout(io.StringIO()), pytest.raises(RuntimeError, match="simulated crash"):
            cli.run_interactive(CRASH_MODE, CRASH_PERIOD)
        assert not crash["armed"]  # 確かに score_one で落ちた
        out["crash_calls_before"], out["crash_score_before"] = calls["n"] - start, calls["score"] - score_start

        # 5回目：同じ期間を再開（承認の待ちではなく、ノードの途中からの再開。cli の app.invoke(None, cfg) の分岐）
        out["crash_calls_after"], out["crash_score_after"], out["crash_stdout_resumed"] = run(CRASH_PERIOD, CRASH_MODE)
        path = os.path.join(work, f"report-{CRASH_MODE}-{CRASH_PERIOD}.md")  # 6回目で上書きされるので今読む
        out["crash_report_resumed"] = open(path, encoding="utf-8").read()

        # 6回目：同じ期間を fresh=True で最初から（呼び出し回数の基準）
        fresh = run(CRASH_PERIOD, CRASH_MODE, fresh=True)
        out["crash_calls_fresh"], out["crash_score_fresh"], out["crash_stdout_fresh"] = fresh
    return out


def _printed_metrics(stdout: str) -> dict:
    lines = [ln for ln in stdout.splitlines() if ln.startswith("metrics: ")]
    assert len(lines) == 1, lines
    return json.loads(lines[0][len("metrics: ") :])


def _yen(calls: int) -> float:
    """偽 LLM を calls 回呼んだときの費用の目安。同梱の料金（入力 60 円・出力 240 円／100万トークン）から具体的に作る。"""
    return round(calls * (1000 * 60 + 200 * 240) / 1e6, 1)


def test_run_interactive_resets_tokens(cli_runs):
    """run_interactive の頭で reset_tokens する：前に残っていた数を持ち越さない。"""
    n = cli_runs["calls_before"]
    assert n > 0
    assert cli_runs["usage_before"] == {"input": n * FAKE_TOKENS[0], "output": n * FAKE_TOKENS[1]}


def test_resume_counts_only_after_resume(cli_runs):
    """中断して再開すると、レポートには再開後の分だけが出る（F-18 の制約）。"""
    m = _printed_metrics(cli_runs["stdout_resumed"])
    n = cli_runs["calls_after"]
    assert n > 0  # 再開後も要約などで LLM を呼んでいる
    assert (m["tokens_input"], m["tokens_output"]) == (n * FAKE_TOKENS[0], n * FAKE_TOKENS[1])
    assert m["tokens"] == m["tokens_input"] + m["tokens_output"]
    assert FAKE_TOKENS == (1000, 200) and m["cost_jpy"] == _yen(n) > 0  # 実装の関数ではなく、具体的な数から


def test_printed_metrics_json_and_report_file(cli_runs):
    """画面の metrics（JSON）にトークン数・費用の目安が入り、保存したレポートに3桁区切りの行が出る。"""
    m = _printed_metrics(cli_runs["stdout_fresh"])
    assert {"tokens", "tokens_input", "tokens_output", "cost_jpy"} <= m.keys()
    assert m["tokens"] > 0 and m["tokens"] == m["tokens_input"] + m["tokens_output"]
    assert m["cost_jpy"] == _yen(cli_runs["calls_fresh"]) > 0
    md = open(os.path.join(cli_runs["work"], "report-weekly-2026-W40.md"), encoding="utf-8").read()
    want = f"- LLM トークン: 入力 {m['tokens_input']:,} ・ 出力 {m['tokens_output']:,}（約 {m['cost_jpy']} 円）"
    assert want in md.splitlines()


def test_fake_llm_tokens_are_deterministic(cli_runs):
    """偽 LLM では、同じ期間を最初から通したトークン数が呼び出し回数どおりで、中断前後の合計と一致する。"""
    m = _printed_metrics(cli_runs["stdout_fresh"])
    n = cli_runs["calls_fresh"]
    assert (m["tokens_input"], m["tokens_output"]) == (n * FAKE_TOKENS[0], n * FAKE_TOKENS[1])
    # 中断しても呼び出しの総数は変わらない（再開前の分は数えないだけ）
    assert n == cli_runs["calls_before"] + cli_runs["calls_after"]


def test_resume_after_crash_in_node(cli_runs):
    """score_one の途中で落ちた後、同じ期間で再開すると、終わっていた採点を呼び直さずに完了する（F-18 の制約）。

    承認の待ちではなく、ノードの途中（snap.next があり snap.interrupts がない）からの再開。
    レポートのトークン数は再開後に実際に呼んだ分だけで、中断前と再開後の呼び出し回数の合計は最初から通した回と同じ。
    """
    before, after, fresh = (cli_runs[f"crash_calls_{k}"] for k in ("before", "after", "fresh"))
    s_before, s_after, s_fresh = (cli_runs[f"crash_score_{k}"] for k in ("before", "after", "fresh"))
    out = cli_runs["crash_stdout_resumed"]
    # 再開の経路：承認で止まっていたのではなく、score_one を残して落ちていた
    resumed = [ln for ln in out.splitlines() if ln.startswith(f"▶ 再開: {CRASH_MODE}-{CRASH_PERIOD}")]
    assert len(resumed) == 1 and "score_one" in resumed[0], out[:500]
    # 落ちる前に終わった採点がある（並列に走るので、落ちた呼び出しの後に終わる分もある）・残りを再開後に採点
    assert s_before >= CRASH_AT - 1 and 0 < s_after < s_fresh
    # 終わっていた score_one を二重に呼んでいない（採点も LLM 呼び出し全体も、合計が最初から通した回と同じ）
    assert s_before + s_after == s_fresh
    assert before + after == fresh
    # レポートの metrics は再開後に実際に呼んだ分だけ
    m = _printed_metrics(out)
    assert (m["tokens_input"], m["tokens_output"]) == (after * FAKE_TOKENS[0], after * FAKE_TOKENS[1])
    assert m["tokens"] == m["tokens_input"] + m["tokens_output"]
    assert m["cost_jpy"] == _yen(after) > 0
    md = cli_runs["crash_report_resumed"]  # 再開後に保存したレポート
    assert f"- LLM トークン: 入力 {after * 1000:,} ・ 出力 {after * 200:,}（約 {_yen(after)} 円）" in md.splitlines()
    # 最初から通した回は、全部の呼び出しを数えている
    mf = _printed_metrics(cli_runs["crash_stdout_fresh"])
    assert (mf["tokens_input"], mf["tokens_output"]) == (fresh * FAKE_TOKENS[0], fresh * FAKE_TOKENS[1])


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
