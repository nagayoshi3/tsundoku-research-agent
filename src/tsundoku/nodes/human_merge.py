"""承認②で人が話題をまとめる。

- merge_hints：提示する話題どうしを、自動のまとめ（cluster_topics）と同じ判定で比べ直し、
  「自動でまとめるには足りないが同じ話題かもしれない」組み合わせを人に推薦する
- apply_merges：人が指定した組み合わせを1つの話題にし、評価（採点）をまとめ直す。要約は後ろの summarize が
  まとめた記事を横断して書くので、ここでは古い要約を消すだけ
"""

from tsundoku.config import log
from tsundoku.nodes.cluster import (
    SAME_TOPIC,
    embed,
    embed_text,
    headline_groups,
    member,
    rule_entities,
    title_part,
    topic_score,
)
from tsundoku.nodes.scoring import score_candidate, total_score

NEAR_TOPIC = 0.45  # 総合スコアがこれ以上 SAME_TOPIC 未満：自動ではまとめないが「同じ話題かも」と見せる
NEAR_COS = 0.80  # 見出しの固有名詞が食い違って自動では弾いたが、言い回しがここまで近ければ人に判断してもらう
MAX_HINTS = 5
WHY_HUMAN = "人がまとめた（承認②）"


def merge_hints(cands: list[dict]) -> list[dict]:
    """提示する話題の中から、同じ話題かもしれない2件の組を返す（スコアの高い順、最大 MAX_HINTS）。"""
    if len(cands) < 2:
        return []
    ents = [set(c.get("entities") or rule_entities(c)) for c in cands]
    kws = [set(c.get("keywords") or []) for c in cands]
    heads = [title_part(c, e) for c, e in zip(cands, ents)]
    vecs = embed([embed_text(c) for c in cands])
    cos = vecs @ vecs.T
    groups = headline_groups(cands)  # 同じ主語として既に並べて見せている組は除く
    out = []
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = cands[i], cands[j]
            if groups.get(a["id"]) and groups.get(a["id"]) == groups.get(b["id"]):
                continue
            s, why = topic_score(a, b, float(cos[i, j]), ents[i], ents[j], heads[i], heads[j], kws[i], kws[j])
            if NEAR_TOPIC <= s < SAME_TOPIC:
                out.append({"a": a["id"], "b": b["id"], "score": round(s, 2), "why": f"近い（{why}）"})
            elif s == 0.0 and cos[i, j] >= NEAR_COS:
                out.append({"a": a["id"], "b": b["id"], "score": round(float(cos[i, j]), 2), "why": why})
    out.sort(key=lambda h: -h["score"])
    return out[:MAX_HINTS]


def merge_finals(merges: list[dict]) -> list[str]:
    """承認②の記録（feedback["merges"]）ごとに、今残っている行き先の id を返す。

    記録の履歴（merged → into）をたどるので、final を持たない古い記録や、後のまとめでさらに吸収された
    final も、今残っている id まで進む。吸収された id は pool から消えて二度と into にならないので、輪にはならない。
    """
    hist = {j: m["into"] for m in merges for j in m["merged"]}
    out = []
    for m in merges:
        i, seen = m.get("final", m["into"]), set()
        while i in hist and i not in seen:  # seen は壊れた記録で止まらなくなるのを防ぐだけ
            seen.add(i)
            i = hist[i]
        out.append(i)
    return out


def apply_merges(state: dict, groups: list[list[str]]) -> tuple[dict, dict[str, str]]:
    """人の指定（id のリストのリスト）を反映した State の差分と、吸収された id → 残した id の対応を返す。

    残すのは合計点が最も高い話題（同点なら提示順で先のもの）。吸収した話題は代表ごと related に入れ、
    固有名詞・キーワードを合わせてから、まとめた話題を1件として採点し直す（LLM 1回）。
    """
    pool = {c["id"]: c for c in state["pool"]}
    scores = {s["id"]: s for s in state["scored"]}
    topics = {t["id"]: t for t in state.get("topics", [])}
    order = {i: k for k, i in enumerate(state.get("shortlist", []))}
    alias: dict[str, str] = {}
    new_scores, records = [], []

    def resolve(i):  # 吸収が重なる（a←b の後に c←a）こともあるので、残った id までたどる
        while i in alias:
            i = alias[i]
        return i

    for g in groups or []:
        ids = []
        for i in g:
            i = resolve(i)
            if i in pool and i not in ids:
                ids.append(i)
        if len(ids) < 2:
            continue
        keep = max(
            ids,
            key=lambda i: (total_score(scores[i], pool[i]) if i in scores else -1, -order.get(i, 10**6)),
        )
        base = pool[keep]
        ents, kws = set(base.get("entities", [])), set(base.get("keywords", []))
        reps, tail = [], []  # まとめた話題の代表は related の先頭に（要約が横断する記事に必ず入るように）
        for i in ids:
            if i == keep:
                continue
            c = pool.pop(i)
            topics.pop(i, None)
            reps.append(member(c, WHY_HUMAN, set(c.get("entities", [])), set(c.get("keywords", []))))
            tail += list(c.get("related", []))
            ents |= set(c.get("entities", []))
            kws |= set(c.get("keywords", []))
            alias[i] = keep
        merged = {
            **base,
            "related": reps + list(base.get("related", [])) + tail,
            "entities": sorted(ents),
            "keywords": sorted(kws),
        }
        pool[keep] = merged
        if keep in topics:
            topics[keep] = {
                **topics[keep],
                "related": merged["related"],
                "entities": merged["entities"],
                "keywords": merged["keywords"],
            }
        before = scores.get(keep)
        s = score_candidate(merged, state["theme"], state["approved_plan"])  # まとめた話題として評価し直す
        scores[keep] = s
        new_scores.append(s)
        records.append({"into": keep, "merged": [i for i in ids if i != keep]})
        log(
            f"[review] 人がまとめた: {base['title'][:40]} ← {len(reps)} 話題（{', '.join(r['title'][:30] for r in reps)}）"
            f"。評価 {total_score(before, base) if before else '未採点'} → {total_score(s, merged)}"
        )
    if not records:
        return {}, {}

    def remap(ids):
        out = []
        for i in ids:
            i = resolve(i)
            if i not in out:
                out.append(i)
        return out

    prev = state.get("feedback") or {"proposed": [], "approved": [], "rejected": {}, "added": []}
    drop = set(alias) | {r["into"] for r in records}
    merges = prev.get("merges", []) + records
    return {
        "pool": [pool[c["id"]] for c in state["pool"] if c["id"] in pool],
        "topics": [topics[t["id"]] for t in state.get("topics", []) if t["id"] in topics],
        "scored": new_scores,  # reducer が足す。id で引くときは後のものが勝つ
        "shortlist": remap(state.get("shortlist", [])),
        "shown": remap(state.get("shown", [])),
        "selected": remap(state.get("selected", [])),
        "summaries": {k: v for k, v in (state.get("summaries") or {}).items() if k not in drop},
        "feedback": {
            **prev,
            "proposed": remap(prev["proposed"]),
            "approved": remap(prev["approved"]),
            "added": remap(prev["added"]),
            "rejected": {resolve(k): v for k, v in prev["rejected"].items()},
            # into は記録した時点の行き先（履歴）。final は後のまとめでさらに吸収された先までたどった、今残っている id
            "merges": [{**m, "final": f} for m, f in zip(merges, merge_finals(merges))],
        },
    }, {i: resolve(i) for i in alias}  # 呼び出し元は1段引くだけで済むように、たどった先を返す
