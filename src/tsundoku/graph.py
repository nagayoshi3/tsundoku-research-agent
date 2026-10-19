"""LangGraph の配線。"""

import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from tsundoku.config import log
from tsundoku.models import ResearchState
from tsundoku.nodes.aggregate import aggregate
from tsundoku.nodes.cluster import cluster_topics
from tsundoku.nodes.fetch import fetch_source, merge, route_sources
from tsundoku.nodes.planning import approve_plan, plan
from tsundoku.nodes.report import report
from tsundoku.nodes.review import confirm_empty, review, route_after_review
from tsundoku.nodes.scoring import route_scoring, score_one, shortlist
from tsundoku.nodes.summarize import summarize


def timed(name, fn):
    # ノードの開始・終了と所要時間をログに出す（並列に走る score_one は件数が多いので対象外）
    def wrapper(x):
        t = time.time()
        log(f"▶ {name}")
        out = fn(x)
        log(f"✓ {name}（{time.time() - t:.1f}s）")
        return out

    wrapper.__name__ = fn.__name__
    return wrapper


def build_graph(checkpointer=None):
    g = StateGraph(ResearchState)
    g.add_node("plan", timed("plan", plan))
    g.add_node("approve_plan", approve_plan)  # 停止：承認①
    g.add_node("fetch_source", fetch_source)  # Send で取得元ごとに並列
    g.add_node(
        "cluster_topics",
        timed("cluster_topics", cluster_topics),
        retry_policy=RetryPolicy(max_attempts=3),
    )  # 埋め込み API を呼ぶ
    g.add_node("aggregate", timed("aggregate", aggregate))
    g.add_node("merge", timed("merge", merge))
    g.add_node("score_one", score_one, retry_policy=RetryPolicy(max_attempts=3))  # Send で論文ごとに並列
    g.add_node("shortlist", timed("shortlist", shortlist))
    g.add_node("review", review)  # 停止：承認②
    g.add_node("confirm_empty", confirm_empty)  # 停止：読むものが0件のとき、次を見るか聞く
    g.add_node(
        "summarize",
        timed("summarize", summarize),
        retry_policy=RetryPolicy(max_attempts=3),
    )  # 本文を読んで要約（LLM）
    g.add_node("report", timed("report", report))

    g.add_conditional_edges(START, route_sources, ["fetch_source"])  # map：起動するとまず取得
    g.add_edge("fetch_source", "cluster_topics")  # reduce
    g.add_edge("cluster_topics", "aggregate")
    g.add_edge("aggregate", "plan")  # 集計を見てから計画（キーワードの推薦）
    g.add_edge("plan", "approve_plan")  # 承認①
    g.add_edge("approve_plan", "merge")
    g.add_conditional_edges("merge", route_scoring, ["score_one", "shortlist"])  # map
    g.add_edge("score_one", "shortlist")  # reduce
    g.add_edge("shortlist", "review")
    g.add_conditional_edges(
        "review",
        route_after_review,
        {"shortlist": "shortlist", "confirm_empty": "confirm_empty", "summarize": "summarize"},
    )
    g.add_conditional_edges(
        "confirm_empty",
        route_after_review,
        {"shortlist": "shortlist", "summarize": "summarize"},
    )
    g.add_edge("summarize", "report")
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


def mermaid() -> str:
    """配線を Mermaid 形式の文字列で返す（README の図を更新するときに使う）。"""
    return build_graph().get_graph().draw_mermaid()
