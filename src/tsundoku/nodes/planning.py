"""計画（plan）と承認①（approve_plan）。"""

from langgraph.types import interrupt

from tsundoku.config import CONFIG, log
from tsundoku.llm import structured_call
from tsundoku.models import Plan, ResearchState
from tsundoku.prompts import PLAN_PROMPT


def plan(state: ResearchState) -> dict:
    # 取得・まとめ・集計の後に動く：その期間の記事に実際に出ている語から、重視キーワードを推薦する
    span = "週" if state["mode"] == "weekly" else "月"
    st = state.get("stats") or {}

    def join(pairs):
        return ", ".join(f"{k}({n})" for k, n in pairs) or "（なし）"

    cat_n = ", ".join(f"{k}({v['topics']})" for k, v in st.get("by_category", [])) or "（なし）"
    user = (
        f"関心領域: {state['theme']}\n対象: RSS リーダー（Inoreader）でこの{span}に保存した記事（期間 {state['period']}）\n"
        f"カテゴリ: {', '.join(CONFIG.get('categories', {}))}\n"
        f"与えられた重視キーワード: {', '.join(CONFIG.get('focus_keywords', []))}\n\n"
        f"## この期間の集計\n"
        f"- 保存した記事 {len(state.get('candidates', []))} 本 → 話題 {len(state.get('topics', []))} 件\n"
        f"- カテゴリ別の話題数: {cat_n}\n"
        f"- よく出た話題キーワード（話題数）: {join(st.get('by_keyword', [])[:20])}\n"
        f"- よく出た固有名詞（記事数）: {join(st.get('top_entities', [])[:20])}\n"
        f"- 2本以上の記事がある話題: "
        + (
            "; ".join(
                f"{t['title'][:40]}（{t['articles']}本）"
                for t in st.get("top_topics", [])[:8]
                if t.get("articles", 1) > 1
            )
            or "（なし）"
        )
    )
    p = structured_call(Plan, PLAN_PROMPT, user)
    if p.suggestions:
        log("[plan] 今週の記事からの推薦: " + "；".join(f"{s.keyword} — {s.reason}" for s in p.suggestions))
    return {"plan": p.model_dump()}


def approve_plan(state: ResearchState) -> dict:
    # ここでグラフが止まり、State がチェックポイントに保存される。
    # 再開時は Command(resume=<人の入力>) の値が interrupt() の戻り値になる。
    # 注意：空の {} は再開とみなされないので、変更がなくても {"approved": True} を渡す
    # 与えたキーワードは必ず残す（LLM が落としても足し戻す）
    given = [
        k
        for k in CONFIG.get("focus_keywords", [])
        if k.lower() not in {x.lower() for x in state["plan"]["focus_keywords"]}
    ]
    state = {
        **state,
        "plan": {
            **state["plan"],
            "focus_keywords": given + state["plan"]["focus_keywords"],
        },
    }
    if CONFIG.get("ask_plan", True):
        edited = (
            interrupt(
                {
                    "type": "approve_plan",
                    "message": "キーワードと採点件数（合計、通常100）を確認してください（空 Enter でそのまま承認）",
                    "proposal": state["plan"],
                }
            )
            or {}
        )
    else:  # 承認①を省略：提案をそのまま確定
        edited = {}
        log(
            f"[approve_plan] ask_plan = False のため承認①を省略（キーワード: {', '.join(state['plan']['focus_keywords'])}、採点 {state['plan']['max_items']} 件）"
        )
    approved = {
        **state["plan"],
        **{k: edited[k] for k in ("focus_keywords", "max_items") if edited.get(k)},
    }
    log(f"[approve_plan] 確定: キーワード {', '.join(approved['focus_keywords'])}／採点 {approved['max_items']} 件")
    return {"approved_plan": Plan(**approved).model_dump()}  # 人の入力も型で検証する
