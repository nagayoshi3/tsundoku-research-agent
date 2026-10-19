"""承認②（review）：読まないものを除外し、候補外から追加する。"""

from langgraph.types import interrupt

from tsundoku.config import CONFIG, log
from tsundoku.models import ResearchState
from tsundoku.nodes.cluster import headline_groups
from tsundoku.nodes.human_merge import apply_merges
from tsundoku.nodes.scoring import (
    SHORTLIST_SIZE,
    ja_title,
    score_detail,
    score_line,
)
from tsundoku.sources import normalize_url


def review(state: ResearchState) -> dict:
    prev = state.get("feedback") or {
        "proposed": [],
        "approved": [],
        "rejected": {},
        "added": [],
    }
    if not state.get("shortlist"):  # 提示するものがなければ、人に聞かずに終える（次点が尽きたときもここ）
        log("[review] 提示する話題が0件なので承認②を飛ばします")
        return {
            "feedback": prev,
            "selected": state.get("selected", []),
            "review_action": "end",
        }
    pool = {c["id"]: c for c in state["pool"]}
    scores = {s["id"]: s for s in state["scored"]}
    groups = headline_groups([pool[i] for i in state["shortlist"]])  # 同じ主語（muse など）を表示上まとめる
    pos = {i: k for k, i in enumerate(state["shortlist"])}
    first = {}
    for i in state["shortlist"]:  # カテゴリの中でだけ、同じ主語を最初に出た位置に寄せる
        first.setdefault((pool[i].get("category"), groups.get(i) or i), pos[i])
    order = sorted(
        state["shortlist"],
        key=lambda i: first[(pool[i].get("category"), groups.get(i) or i)],
    )
    state = {**state, "shortlist": order}
    items = [
        {
            "id": pid,
            "title": ja_title(pool[pid], scores[pid]),
            "title_orig": pool[pid]["title"],
            "group": groups.get(pid, ""),
            "summary": scores[pid].get("summary_ja", ""),
            "score": score_line(scores[pid], pool[pid]),
            "reason": scores[pid]["reason"],
            "detail": score_detail(scores[pid], pool[pid]),  # 総評と、基準ごとの点数・理由
            "abstract_head": pool[pid]["abstract"][:160],
            "related": len(pool[pid].get("related", [])),  # 同じ話題の記事の数
            "category": pool[pid].get("category", ""),  # カテゴリ
            "where": pool[pid].get("outlet") or pool[pid].get("feed", ""),
        }
        for pid in state["shortlist"]
    ]
    # 同じ主語（muse など）の話題は1つの項目にまとめ、取捨選択の単位にする（番号で全部、枝番で1本だけ除外できる）
    merged, seen = [], {}
    for it in items:
        key = (it["category"], it["group"]) if it["group"] else None
        if key and key in seen:
            seen[key]["members"].append(it)
        else:
            entry = {**it, "members": [it]}
            if key:
                seen[key] = entry
            merged.append(entry)
    for e in merged:
        if len(e["members"]) > 1:
            e["id"] = f"group:{e['group']}"
            e["title"] = f"{e['group']}（{len(e['members'])}話題）"
    items = merged

    target = CONFIG.get("shortlist_size", SHORTLIST_SIZE) or len(state["pool"])
    done_n = len(state.get("selected", []))
    msg = (
        f"読まないものがあれば除外してください（何もしなければ全部読む。確定済み {done_n} 件、目安 {target} 件。除外して目安を下回った分は次点で埋めます）"
        if CONFIG.get("backfill", True) and CONFIG.get("shortlist_size")
        else "読まないものがあれば除外してください（何もしなければ全部読む）"
    )
    ids_shown = set(state["shortlist"])
    hints = [  # 同じ話題かもしれない組み合わせ（shortlist が推薦）。表示用にタイトルを添える
        {**h, "title_a": ja_title(pool[h["a"]], scores[h["a"]]), "title_b": ja_title(pool[h["b"]], scores[h["b"]])}
        for h in state.get("merge_hints") or []
        if h["a"] in ids_shown and h["b"] in ids_shown
    ]
    answer = interrupt({"type": "review", "message": msg, "shortlist": items, "merge_hints": hints}) or {}
    # 人がまとめた話題を先に反映する（pool・採点・提示を差し替え、以降の id は残した話題に読み替える）
    n_merged_before = len(prev.get("merges", []))
    extra, alias = apply_merges(state, answer.get("merge", []))
    if extra:
        state = {**state, **extra}
        pool = {c["id"]: c for c in state["pool"]}
        prev = extra["feedback"]
    rejected = {
        alias.get(k, k): v for k, v in answer.get("reject", {}).items() if alias.get(k, k) in state["shortlist"]
    }
    if answer.get("approve_all"):  # 提示したものは、除外した分を除いて全部承認（保存済み記事はすでに本人が選んだもの）
        approved = [i for i in state["shortlist"] if i not in rejected]
    else:  # 番号で承認する形（デモや自動テストが使う）
        approved = []
        for i in answer.get("approve", []):
            i = alias.get(i, i)
            if i in state["shortlist"] and i not in rejected and i not in approved:
                approved.append(i)
    # 追加は、代表の id のほか、同じ話題にまとめた記事の URL でも受け付ける（URL は正規化して照合）
    lookup = {}
    for pid, c in pool.items():
        lookup[pid] = pid
        lookup[normalize_url(c["url"])] = pid
        for r in c.get("related", []):
            lookup[normalize_url(r["url"])] = pid
    added = []
    for a in answer.get("add", []):
        key = a if a in lookup else normalize_url(a) if a.startswith("http") else a
        if key in lookup and lookup[key] not in added and lookup[key] not in approved:
            added.append(lookup[key])
    feedback = {
        "proposed": prev["proposed"] + state["shortlist"],
        "approved": prev["approved"] + approved,
        "rejected": {**prev["rejected"], **rejected},
        "added": prev["added"] + added,
        "merges": prev.get("merges", []),  # 人がまとめた話題（apply_merges が記録）
    }  # 何回かに分けて見せた分を合算
    selected = state.get("selected", []) + approved + [a for a in added if a not in state.get("selected", [])]
    shown = state.get("shown", []) + state["shortlist"]
    log(
        f"[review] 提示 {len(state['shortlist'])} → 読む {len(approved)}・除外 {len(rejected)}・追加 {len(added)}"
        f"・まとめ {len(prev.get('merges', [])) - n_merged_before} 組（確定 {len(selected)} / 目安 {target}）"
    )
    ignored = [
        a
        for a in answer.get("add", [])
        if a not in lookup and (not a.startswith("http") or normalize_url(a) not in lookup)
    ]
    if ignored:
        log(f"[review] 追加できなかった指定（採点対象にない）: {', '.join(ignored)}")

    remaining = len(state["pool"]) - len(set(shown))
    if (
        CONFIG.get("backfill", True)
        and CONFIG.get("shortlist_size")
        and len(selected) < target
        and rejected
        and remaining > 0
    ):
        # 除外した分を次点で埋める：shortlist に戻って、まだ見せていない話題から不足分を出す
        log(f"[review] 不足 {target - len(selected)} 件を次点（残り {remaining} 件）から出します")
        return {
            **extra,
            "feedback": feedback,
            "selected": selected,
            "shown": shown,
            "review_action": "next",
        }
    # 全部除外で次点もない（または全件表示のとき）は confirm_empty で聞く。ここで2回目の interrupt をすると、
    # 再開時にこのノードが頭からやり直され、まとめた話題の採点（LLM）がもう一度走るため、ノードを分けている
    return {
        **extra,
        "feedback": feedback,
        "selected": selected,
        "shown": shown,
        "review_action": "done" if selected else "ask",
    }


def confirm_empty(state: ResearchState) -> dict:
    """読むものが0件のとき：終えるか、次の候補を見るかを聞く（停止）。"""
    choice = (
        interrupt(
            {
                "type": "question",
                "message": "読むものが0件です。次の候補を出しますか？",
                "options": ["next", "end"],
            }
        )
        or {}
    )
    remaining = len(state["pool"]) - len(set(state.get("shown", [])))
    return {"review_action": "next" if choice.get("action") == "next" and remaining > 0 else "done"}


def route_after_review(state: ResearchState) -> str:
    return {"next": "shortlist", "ask": "confirm_empty"}.get(state["review_action"], "summarize")
