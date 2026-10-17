"""取得（fetch_source、Send で並列）と枠の配分（merge）。"""

import time
from collections import Counter

from langgraph.types import Send

from tsundoku.config import log
from tsundoku.guardrails import sanitize_abstract
from tsundoku.models import ResearchState
from tsundoku.sources import get_fetcher

MAX_RETRY = 2


def route_sources(state: ResearchState) -> list[Send]:
    return [
        Send(
            "fetch_source",
            {"source": s, "mode": state["mode"], "period": state["period"]},
        )
        for s in state["sources"]
    ]


def fetch_source(payload: dict) -> dict:
    # Send で渡された payload だけを受け取る（State 全体ではない）
    s = payload["source"]
    fetch = get_fetcher(s["type"])
    err = None
    for attempt in range(1 + MAX_RETRY):
        try:
            rows = fetch(
                s,
                mode=payload["mode"],
                period=payload["period"],
                limit=s.get("limit", 100),
            )
            log(f"[fetch_source] {s['name']}: {len(rows)} 件")
            return {"candidates": [{**r, "feed": s["name"]} for r in rows]}  # 枠の配分のため取得元名を付ける
        except Exception as e:  # 取得元ごとに縮退して先へ進む
            err = e
            log(f"[fetch_source] {s['name']} 失敗 ({attempt + 1}/{1 + MAX_RETRY}): {e}")
            time.sleep(0.1 * (attempt + 1))
    return {"failed_sources": [f"{s['name']}: {err}"]}


def allocate(groups: dict[str, list[dict]], quotas: dict[str, int], total: int) -> dict[str, list[dict]]:
    # 取得元ごとの枠で切る。枠の合計が total と違えば比率で按分し、
    # 候補が枠に足りない取得元の余りは、他の取得元の次の順位から順番に埋める
    qsum = sum(quotas.get(n, 0) for n in groups) or 1
    picked = {n: rows[: int(total * quotas.get(n, 0) / qsum)] for n, rows in groups.items()}
    left = total - sum(len(v) for v in picked.values())
    while left > 0:
        moved = False
        for n, rows in groups.items():
            if left > 0 and len(picked[n]) < len(rows):
                picked[n].append(rows[len(picked[n])])
                left -= 1
                moved = True
        if not moved:
            break
    return picked


def keyword_hits(c: dict, keywords: list[str]) -> int:
    # 承認①で確定した重視キーワードが、タイトルと要旨に何回出てくるか（大文字小文字は区別しない）
    text = f"{c['title']} {c['abstract']} {' '.join(c.get('keywords', []))}".lower()  # 抜き出した話題キーワードも数える
    return sum(text.count(k.lower()) for k in keywords)


def merge(state: ResearchState) -> dict:
    # 重複（同じ ID・同じ話題）は前段の cluster_topics（cluster.py）で除いてある
    # 1) 取得元ごとに並べ、枠で切る。キーワードの一致数の多い順（同数なら、同じ話題を書いた記事が多い順 → 新しい順）
    groups: dict[str, list[dict]] = {}
    for c in sorted(state.get("topics", []), key=lambda c: c["rank"] or 10**6):
        groups.setdefault(c["feed"], []).append(c)
    kws = state["approved_plan"]["focus_keywords"]
    for rows in groups.values():
        rows.sort(key=lambda c: (-keyword_hits(c, kws), -len(c["related"]), c["rank"]))
    quotas = {s["name"]: s.get("quota", 0) for s in state["sources"]}
    picked = allocate(groups, quotas, state["approved_plan"]["max_items"])
    read = Counter(c["feed"] for c in state.get("candidates", []))
    funnel = {
        n: {
            "read": read[n],
            "topics": len(groups.get(n, [])),
            "scored": len(picked.get(n, [])),
        }
        for n in read
    }
    # 2) 入力側ガード：要旨を無害化し、指示文らしきものは消さずに記録する
    flags, pool = [], []
    for name in groups:
        for c in picked[name]:
            abstract, flagged = sanitize_abstract(c["abstract"])
            if flagged:
                flags.append(f"input:suspicious_text:{c['id']}")
            pool.append({**c, "abstract": abstract})
    for n, v in funnel.items():
        log(
            f"[merge] {n}: 読んだ {v['read']} → 話題 {v['topics']} → 採点対象 {v['scored']}"
            + (
                f"（枠から外れた {v['topics'] - v['scored']} 話題は trace.csv の pool_rank が空）"
                if v["topics"] > v["scored"]
                else ""
            )
        )
    if flags:
        log(f"[merge] 入力側ガードの検出 {len(flags)} 件: {', '.join(flags[:5])}")
    return {"pool": pool, "funnel": funnel, "guardrail_flags": flags}
