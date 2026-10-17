"""保存した記事の集計（aggregate）。"""

import csv
import datetime as dt
import json
import os
from collections import Counter

from tsundoku.config import log, settings
from tsundoku.models import ResearchState

TOP_N = 20  # 各ランキングに出す件数


def bucket(saved_at: str, mode: str) -> str:
    # weekly は日ごと（10-01）、monthly は週ごと（W40）にまとめる
    d = dt.datetime.fromisoformat(saved_at)
    return d.strftime("%m-%d") if mode == "weekly" else f"W{d.isocalendar()[1]:02d}"


def aggregate(state: ResearchState) -> dict:
    cands = state.get("candidates", [])
    topics = state.get("topics", [])
    mode = state["mode"]
    # 1) 記事単位の集計（期間内に保存した記事すべて）
    by_time = Counter(bucket(c["saved_at"], mode) for c in cands if c.get("saved_at"))
    by_outlet = Counter(c["outlet"] or "（不明）" for c in cands)
    by_tag = Counter(t for c in cands for t in c.get("tags", []))
    # 1') 話題単位の集計：カテゴリ別（話題の数と記事の数）、話題キーワード別（その語が付いた話題の数）
    by_category = {}
    for t in topics:
        d = by_category.setdefault(t.get("category") or "その他", {"topics": 0, "articles": 0})
        d["topics"] += 1
        d["articles"] += 1 + sum(1 for r in t["related"] if r["why"] != "同じ ID")
    by_keyword = Counter(k for t in topics for k in set(t.get("keywords", [])))
    # 2) 話題単位の集計：同じ話題をまとめた後、記事の多い話題から
    ranked = sorted(topics, key=lambda t: (-1 - len(t["related"]), t["rank"]))
    top_topics = [
        {
            "title": t["title"],
            "url": t["url"],
            "articles": 1 + len(t["related"]),
            "category": t.get("category", ""),
            "outlets": sorted({t["outlet"]} | {r.get("outlet", "") for r in t["related"]} - {""}),
        }
        for t in ranked[:TOP_N]
    ]
    # 3) 固有名詞：その語が出てくる記事の数（同じ ID の重複は1本と数える）
    by_entity = Counter()
    for t in topics:
        by_entity.update(set(t["entities"]))
        for r in t["related"]:
            if r["why"] != "同じ ID":
                by_entity.update(set(r.get("entities", [])))
    stats = {
        "saved": len(cands),
        "topics": len(topics),
        "by_time": sorted(by_time.items()),
        "by_outlet": by_outlet.most_common(TOP_N),
        "by_tag": by_tag.most_common(TOP_N),
        "top_topics": top_topics,
        "by_category": sorted(by_category.items(), key=lambda kv: -kv[1]["articles"]),
        "by_keyword": by_keyword.most_common(TOP_N),
        "top_entities": by_entity.most_common(TOP_N),
    }
    # 4) ファイルに出す：記事1本1行の CSV（話題の番号付き）と、集計の JSON
    base = os.path.join(settings.workdir, f"{mode}-{state['period']}")
    with open(base + "-articles.csv", "w", encoding="utf-8-sig", newline="") as f:  # Excel で文字化けしない
        w = csv.writer(f)
        w.writerow(
            [
                "topic",
                "representative",
                "category",
                "keywords",
                "title",
                "url",
                "outlet",
                "saved_at",
                "tags",
                "why",
            ]
        )
        for n, t in enumerate(ranked, start=1):
            w.writerow(
                [
                    n,
                    1,
                    t.get("category", ""),
                    " ".join(t.get("keywords", [])),
                    t["title"],
                    t["url"],
                    t["outlet"],
                    t["saved_at"],
                    " ".join(t.get("tags", [])),
                    "",
                ]
            )
            for r in t["related"]:
                w.writerow(
                    [
                        n,
                        0,
                        t.get("category", ""),
                        " ".join(r.get("keywords", [])),
                        r["title"],
                        r["url"],
                        r.get("outlet", ""),
                        r.get("saved_at", ""),
                        "",
                        r["why"],
                    ]
                )
    with open(base + "-stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    log(f"[aggregate] 保存 {stats['saved']} 本 → 話題 {stats['topics']} 件 → {base}-articles.csv / -stats.json")
    return {"stats": stats}
