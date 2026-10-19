"""採点（score_one、Send で並列）と提示する話題の選定（shortlist）。"""

import threading
from collections import Counter

from langgraph.types import Send

from tsundoku.config import CONFIG, log
from tsundoku.guardrails import wrap_related, wrap_untrusted
from tsundoku.llm import structured_call
from tsundoku.models import ResearchState, Score
from tsundoku.prompts import SCORE_PROMPT

SHORTLIST_SIZE = 10  # CONFIG["shortlist_size"] がなければこの値


def route_scoring(state: ResearchState):
    done = {s["id"] for s in state.get("scored", [])}
    sends = [
        Send(
            "score_one",
            {"paper": c, "theme": state["theme"], "plan": state["approved_plan"]},
        )
        for c in state["pool"]
        if c["id"] not in done
    ]
    _progress["done"], _progress["total"] = 0, len(sends)
    log(
        f"[score] 採点 {len(sends)} 件（採点済み {len(done)} 件は飛ばす）。並列に LLM を呼ぶので数分かかる。10件ごとに進み具合を出す"
    )
    return sends or ["shortlist"]  # 0件でも止まらずに承認②へ


_progress = {"done": 0, "total": 0, "lock": threading.Lock()}


def score_candidate(c: dict, theme: str, plan: dict) -> dict:
    # 話題1件を採点する（LLM 1回）。同じ話題の他の記事のタイトルも見せる。承認②で人がまとめた話題の採点し直しにも使う
    system = SCORE_PROMPT.format(theme=theme, keywords=", ".join(plan["focus_keywords"]))
    others = [r for r in c.get("related", []) if r["why"] != "同じ ID"]  # 同じ話題を書いた他の記事（URL 違いは除く）
    user = f"id={c['id']} の話題を採点してください。カテゴリ: {c.get('category') or '（なし）'}\n{wrap_untrusted(c)}"
    if others:
        user += f"\n同じ話題を書いた他の記事（{len(others)}本）:\n" + wrap_related([r["title"][:120] for r in others[:10]])
    s = structured_call(Score, system, user)
    s.id = c["id"]
    return s.model_dump()


def score_one(payload: dict) -> dict:
    s = score_candidate(payload["paper"], payload["theme"], payload["plan"])
    with _progress["lock"]:  # 並列なので数えるところだけ排他
        _progress["done"] += 1
        n = _progress["done"]
    if n % 10 == 0 or n == _progress["total"]:
        log(f"[score] {n}/{_progress['total']} 件 採点済み")
    return {"scored": [s]}


def ja_title(c: dict, s: dict | None) -> str:
    # 表示用：日本語タイトルがあればそれ、なければ原文
    return (s or {}).get("title_ja") or c["title"]


def buzz(c: dict) -> int:
    # 注目度の客観的な手がかり：同じ話題を書いた媒体の数（代表を含む）。1媒体なら0点、4媒体以上で3点
    outlets = {c.get("outlet", "")} | {r.get("outlet", "") for r in c.get("related", []) if r.get("why") != "同じ ID"}
    return min(len(outlets - {""}) - 1, 3) if outlets - {""} else 0


def total_score(s: dict, c: dict) -> int:
    # 並べ順：関連度 ＋ 新しさ ＋ 注目度（LLM の impact ＋ 媒体数の加点）
    return s["relevance"] + s["novelty"] + s.get("impact", 0) + buzz(c)


def score_line(s: dict, c: dict) -> str:
    # 1行表示：合計と内訳（例 合計10 ＝ 関連度4 ＋ 新規性3 ＋ 注目度2 ＋ 媒体+1）
    b = buzz(c)
    parts = f"関連度{s['relevance']} ＋ 新規性{s['novelty']} ＋ 注目度{s.get('impact', 0)}" + (
        f" ＋ 媒体+{b}" if b else ""
    )
    return f"合計{total_score(s, c)} ＝ {parts}"


def score_detail(s: dict, c: dict) -> list[str]:
    # 基準ごとの「点数: 理由」。古い採点（基準別の理由がない）は総評で代用
    fallback = s.get("reason", "")
    rows = [
        f"関連度 {s['relevance']}/5: {s.get('relevance_reason') or fallback}",
        f"新規性 {s['novelty']}/5: {s.get('novelty_reason') or fallback}",
        f"注目度 {s.get('impact', 0)}/5: {s.get('impact_reason') or fallback}",
    ]
    outlets = sorted(
        ({c.get("outlet", "")} | {r.get("outlet", "") for r in c.get("related", []) if r.get("why") != "同じ ID"})
        - {""}
    )
    if buzz(c):
        rows.append(f"媒体 +{buzz(c)}: {len(outlets)}媒体が同じ話題を書いた（{', '.join(outlets[:5])}）")
    return rows


def score_table(s: dict, c: dict) -> list[str]:
    # 「今回読むもの」用：基準・評価点・文章の3列の表（Markdown）。総評の行には合計点
    fallback = s.get("reason", "")
    rows = [
        ("関連度", f"{s['relevance']}/5", s.get("relevance_reason") or fallback),
        ("新規性", f"{s['novelty']}/5", s.get("novelty_reason") or fallback),
        ("注目度", f"{s.get('impact', 0)}/5", s.get("impact_reason") or fallback),
    ]
    outlets = sorted(
        ({c.get("outlet", "")} | {r.get("outlet", "") for r in c.get("related", []) if r.get("why") != "同じ ID"})
        - {""}
    )
    if buzz(c):
        rows.append(
            (
                "媒体数",
                f"+{buzz(c)}",
                f"{len(outlets)}媒体が同じ話題を書いた（{', '.join(outlets[:5])}）",
            )
        )
    rows.append(("総評", f"合計 {total_score(s, c)}", s.get("reason", "")))

    def esc(t: str) -> str:
        return t.replace("|", "｜").replace("\n", " ")

    return ["| 基準 | 評価点 | 文章 |", "|---|---|---|"] + [f"| {k} | {v} | {esc(t)} |" for k, v, t in rows]


def shortlist(state: ResearchState) -> dict:
    # LLM を使わない純粋な関数：同じ採点からは必ず同じ結果
    # 初回：合計点がしきい値以上の話題を全部提示する（件数で切らない）。しきい値は CONFIG["shortlist_min_score"]、
    #       None なら上から shortlist_size 番目の合計点（同点まで含めるので件数は目安の前後になる）
    # 2回目以降（人が除外して戻ってきたとき）：目安 shortlist_size に足りない分だけ、まだ見せていない話題から上位を出す
    pool = {c["id"]: c for c in state["pool"]}
    target = CONFIG.get("shortlist_size", SHORTLIST_SIZE) or len(state["pool"])  # 読む件数の目安（0 なら全件）
    shown = set(state.get("shown", []))
    scores = {s["id"]: s for s in state["scored"]}  # 同じ id が2回あれば後のもの（人がまとめた後の採点し直し）
    ranked = sorted(
        (s for s in scores.values() if s["id"] in pool and s["id"] not in shown),
        key=lambda s: (total_score(s, pool[s["id"]]), pool[s["id"]].get("saved_at", "")),  # 同点は新しく保存した順
        reverse=True,
    )
    if not shown:  # 初回：しきい値で切る
        thr = CONFIG.get("shortlist_min_score")
        if thr is None:
            thr = total_score(ranked[target - 1], pool[ranked[target - 1]["id"]]) if len(ranked) >= target else 0
        picked = [s["id"] for s in ranked if total_score(s, pool[s["id"]]) >= thr]
        how = f"合計 {thr} 点以上（{'固定' if CONFIG.get('shortlist_min_score') is not None else f'自動：上位 {target} 番目の点'}）"
    else:  # 次点で埋める：目安に足りない分だけ
        size = max(0, target - len(state.get("selected", [])))
        picked = [s["id"] for s in ranked[:size]]
        how = f"次点 {size} 件"
    # 提示はカテゴリ別（提示する話題の多い順）→ 合計点順に並べ直す（選ぶ集合は変えず、読みやすさのため）
    cat_n = Counter(pool[i].get("category") or "その他" for i in picked)
    picked.sort(
        key=lambda i: (
            -cat_n[pool[i].get("category") or "その他"],
            pool[i].get("category") or "その他",
        )
    )
    log(
        f"[shortlist] 残り {len(ranked)} 件のうち {len(picked)} 件を提示：{how}（目安 {target} 件、確定済み {len(state.get('selected', []))} 件）"
        f"（カテゴリ: {dict(cat_n)}）"
    )
    from tsundoku.nodes.human_merge import merge_hints  # 循環 import を避けてここで

    hints = merge_hints([pool[i] for i in picked])  # 同じ話題かもしれない組み合わせを承認②で推薦する
    if hints:
        log(f"[shortlist] 同じ話題かもしれない組み合わせ {len(hints)} 組を承認②で提案します")
    return {"shortlist": picked, "merge_hints": hints}
