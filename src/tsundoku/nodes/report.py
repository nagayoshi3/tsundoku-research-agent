"""レポート（Markdown / Word）、経路の記録（trace.csv）、指標。"""

import csv
import os
import re
from collections import Counter

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from tsundoku.config import log, settings
from tsundoku.guardrails import check_report
from tsundoku.models import ResearchState
from tsundoku.nodes.cluster import headline_groups
from tsundoku.nodes.fetch import keyword_hits
from tsundoku.nodes.scoring import (
    buzz,
    ja_title,
    score_detail,
    score_line,
    score_table,
    total_score,
)
from tsundoku.periods import period_label


def stats_md(st: dict, mode: str) -> list[str]:
    # 集計を Markdown にする（表1つと、ランキング4つ）
    if not st:
        return []
    join = lambda pairs: "、".join(f"{k} {v}" for k, v in pairs) or "なし"  # noqa: E731
    L = [
        "## 集計",
        "",
        f"保存した記事 {st['saved']} 本 → 同じ話題をまとめて {st['topics']} 件",
        "",
        f"| {'日付' if mode == 'weekly' else '週'} | 本数 |",
        "|---|---|",
    ]
    L += [f"| {k} | {v} |" for k, v in st["by_time"]]
    L += ["", "**よく保存した話題**", ""]
    L += [
        f"{i}. [{t['category'] or 'その他'}] [{t['title']}]({t['url']}) — {t['articles']}本（{'、'.join(t['outlets'])}）"
        for i, t in enumerate(st["top_topics"], 1)
    ]
    cats = "、".join(f"{k} {v['topics']}話題/{v['articles']}本" for k, v in st.get("by_category", [])) or "なし"
    L += [
        "",
        f"- カテゴリ: {cats}",
        f"- 話題キーワード: {join(st.get('by_keyword', []))}",
        f"- 媒体: {join(st['by_outlet'])}",
        f"- タグ: {join(st['by_tag'])}",
        f"- 固有名詞: {join(st['top_entities'])}",
        "",
    ]
    return L


STAGES = ["取得", "期間内", "まとめ", "代表", "採点対象", "提示", "確定"]


def write_trace(state: ResearchState, scores: dict, fb: dict, out_flags: list[str]) -> str:
    # 記事1本1行で、どの段階まで進んだかを1つのファイルで追えるようにする（LLM は使わない）
    pool_rank = {c["id"]: i + 1 for i, c in enumerate(state["pool"])}
    shortlisted = set(fb["proposed"])
    selected = set(state.get("selected", []))
    susp = {f.split(":", 2)[2] for f in state.get("guardrail_flags", []) if f.startswith("input:")}
    decision = {
        **{i: "approved" for i in fb["approved"]},
        **{i: f"rejected: {v}" for i, v in fb["rejected"].items()},
        **{i: "added" for i in fb["added"]},
    }
    path = os.path.join(settings.workdir, f"{state['mode']}-{state['period']}-trace.csv")
    cols = [
        "stage",
        "topic",
        "representative",
        "category",
        "title",
        "title_ja",
        "summary_ja",
        "url",
        "outlet",
        "saved_at",
        "why",
        "keywords",
        "keyword_hits",
        "pool_rank",
        "relevance",
        "novelty",
        "impact",
        "outlets",
        "total_score",
        "relevance_reason",
        "novelty_reason",
        "impact_reason",
        "reason",
        "shortlisted",
        "decision",
        "guardrail",
    ]
    kws = state.get("approved_plan", {}).get("focus_keywords", [])
    ranked = sorted(state.get("topics", []), key=lambda t: (-1 - len(t["related"]), t["rank"]))
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for n, t in enumerate(ranked, start=1):
            pid = t["id"]
            s = scores.get(pid)
            if pid in selected:
                stage = "確定"
            elif pid in shortlisted:
                stage = "提示"
            elif pid in pool_rank:
                stage = "採点対象"
            else:
                stage = "代表"
            w.writerow(
                [
                    stage,
                    n,
                    1,
                    t.get("category", ""),
                    t["title"],
                    (s or {}).get("title_ja", ""),
                    (s or {}).get("summary_ja", ""),
                    t["url"],
                    t["outlet"],
                    t["saved_at"],
                    "",
                    " ".join(t.get("keywords", [])),
                    keyword_hits(t, kws) if kws else "",
                    pool_rank.get(pid, ""),
                    s["relevance"] if s else "",
                    s["novelty"] if s else "",
                    s.get("impact", "") if s else "",
                    buzz(t) + 1,
                    total_score(s, t) if s else "",
                    s.get("relevance_reason", "") if s else "",
                    s.get("novelty_reason", "") if s else "",
                    s.get("impact_reason", "") if s else "",
                    s["reason"] if s else "",
                    1 if pid in shortlisted else "",
                    decision.get(pid, ""),
                    "suspicious_text" if pid in susp else "",
                ]
            )
            for r in t["related"]:
                w.writerow(
                    [
                        "まとめ",
                        n,
                        0,
                        t.get("category", ""),
                        r["title"],
                        "",
                        "",
                        r["url"],
                        r.get("outlet", ""),
                        r.get("saved_at", ""),
                        r["why"],
                        " ".join(r.get("keywords", [])),
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        f"→ 代表: {t['title'][:40]}",
                        "",
                    ]
                )
    counts = Counter()
    for t in ranked:
        pid = t["id"]
        counts[
            "確定" if pid in selected else "提示" if pid in shortlisted else "採点対象" if pid in pool_rank else "代表"
        ] += 1
        counts["まとめ"] += len(t["related"])
    log(f"[report] 経路の記録 → {path}（" + "、".join(f"{k} {counts[k]}" for k in STAGES if counts[k]) + "）")
    return path


_INLINE = re.compile(
    r"(\*\*.+?\*\*|\[(?:[^\[\]]|\[[^\]]*\])+\]\(https?://[^)\s]+\))"
)  # リンクの文字列に [..] が入っていてもよい


def _add_runs(par, text: str):
    # Markdown の **太字** と [文字](URL) を Word の書式に変える。それ以外はそのまま
    for piece in _INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**"):
            par.add_run(piece[2:-2]).bold = True
        elif piece.startswith("[") and "](" in piece:
            label, url = piece[1:-1].split("](", 1)
            r = par.add_run(label)
            r.font.color.rgb = RGBColor(0x1A, 0x56, 0xDB)
            r.font.underline = True
            r2 = par.add_run(f" <{url}>")
            r2.font.size = Pt(8)
            r2.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        else:
            par.add_run(piece)


def md_to_docx(md: str, path: str):
    # レポートの Markdown（見出し・箇条書き・表・太字・リンク）を Word（.docx）にする。python-docx のみ使用
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Yu Gothic"
    st.font.size = Pt(10)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Yu Gothic")
    lines = md.splitlines()
    k = 0
    while k < len(lines):
        line = lines[k]
        s = line.strip()
        if not s:
            k += 1
            continue
        if s.startswith("#"):  # 見出し
            level = len(s) - len(s.lstrip("#"))
            doc.add_heading(s[level:].strip(), level=min(level, 4))
            k += 1
            continue
        if s.startswith("|"):  # 表（ヘッダ行 + 区切り行 + 本体）
            rows = []
            while k < len(lines) and lines[k].strip().startswith("|"):
                cells = [c.strip() for c in lines[k].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    rows.append(cells)
                k += 1
            if rows:
                ncol = max(len(r) for r in rows)
                t = doc.add_table(rows=len(rows), cols=ncol)
                t.style = "Light Grid Accent 1"
                t.autofit = False
                widths = (
                    [Cm(2.2), Cm(2.2), Cm(12)] if ncol == 3 else [Cm(16 / ncol)] * ncol
                )  # 評価の表は「文章」の列を広く
                for ci, w in enumerate(widths):  # 列幅（Word・LibreOffice の両方で効くよう列とセルの両方に）
                    t.columns[ci].width = w
                for ri, r in enumerate(rows):
                    for ci, c in enumerate(r):
                        cell = t.cell(ri, ci)
                        cell.width = widths[ci]
                        _add_runs(cell.paragraphs[0], c)
            continue
        m = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)  # 箇条書き・番号付き（字下げで階層）
        if m:
            indent, marker, body = len(m.group(1)), m.group(2), m.group(3)
            style = "List Number" if marker[0].isdigit() else ("List Bullet" if indent < 2 else "List Bullet 2")
            par = doc.add_paragraph(style=style)
            _add_runs(par, body.rstrip())
            k += 1
            while (
                k < len(lines)
                and lines[k].startswith(" ")
                and lines[k].strip()
                and not re.match(r"^\s*([-*]|\d+\.)\s+", lines[k])
            ):
                par.add_run("\n")  # 項目の続きの行（字下げされた本文）
                _add_runs(par, lines[k].strip())
                k += 1
            continue
        par = doc.add_paragraph()
        _add_runs(par, s)
        k += 1
    doc.save(path)


def report(state: ResearchState) -> dict:
    pool = {c["id"]: c for c in state["pool"]}
    scores = {s["id"]: s for s in state["scored"]}
    fb = state.get("feedback") or {
        "proposed": [],
        "approved": [],
        "rejected": {},
        "added": [],
    }
    flags = check_report(state.get("selected", []), state["pool"])
    metrics = {
        "fetched": len(state.get("candidates", [])),  # 期間内に保存した記事
        "topics": len(state.get("topics", [])),  # 話題をまとめた後の件数
        "pool": len(state["pool"]),
        "adoption_rate": round(len(fb["approved"]) / (len(fb["proposed"]) or 1), 2),  # LLM の提案の採用率
        "added": len(fb["added"]),  # 取りこぼしの指標
        "failed_sources": len(state.get("failed_sources", [])),
        "guardrail_flags": len(state.get("guardrail_flags", [])) + len(flags),
        "funnel": state.get("funnel", {}),  # 取得元ごとの読んだ件数と採点件数
    }
    span = "週次" if state["mode"] == "weekly" else "月次"
    L = [
        f"# リサーチレポート {period_label(state['mode'], state['period'])}（Inoreader 保存記事・{span}）",
        "",
        f"テーマ: {state['theme']} ／ キーワード: {', '.join(state['approved_plan']['focus_keywords'])}",
        "",
    ]
    L += stats_md(state.get("stats") or {}, state["mode"])  # 集計（aggregate.py）
    L += ["## 今回読むもの"]
    n_sel = 0
    for pid in state.get("selected", []):
        c, s = pool.get(pid), scores.get(pid)
        if c is None:
            continue
        tag = "（追加）" if pid in fb["added"] else ""
        sc = score_line(s, c) if s else "未採点"
        where = f"{c['outlet'] or c['feed']}・保存 {c['saved_at']}"
        cat = f"[{c['category']}] " if c.get("category") else ""
        t_ja = ja_title(c, s)
        n_sel += 1
        L += ["", f"### {n_sel}. {cat}{t_ja}{tag}", ""]  # 話題ごとに小見出し
        if t_ja != c["title"]:
            L += [f"原題: {c['title']}", ""]
        sm = (state.get("summaries") or {}).get(pid)
        if sm and sm.get("text"):  # summarize.py の要約（段落ごとに空行）
            L += [p.strip() for p in re.split(r"\n\s*\n", sm["text"]) if p.strip()]
            L += [
                "",
                f"（要約の元: {sm.get('articles', 1)}本、本文を読めた {sm.get('sources_used', 0)}本）",
                "",
            ]
        elif s and s.get("summary_ja"):  # 要約がなければ採点時の要旨
            L += [s.get("summary_ja", ""), ""]
        L += [f"{where} ／ {sc}", ""]
        if s:
            L += score_table(s, c) + [""]  # 基準・評価点・文章の表
        L += [f"出典: {c['url']}"]
        if c.get("related"):  # 同じ話題の他の記事
            human = sum(1 for r in c["related"] if r.get("why", "").startswith("人がまとめた"))
            L.append(
                f"同じ話題: {len(c['related'])}本"
                + (f"（うち承認②で人がまとめた話題 {human}）" if human else "")
                + " — "
                + "、".join(f"[{r['url'].split('/')[2]}]({r['url']})" for r in c["related"][:5])
            )
    if not state.get("selected"):
        L.append("- （今回はなし）")
    # 全話題：承認しなかったものも含め、カテゴリ別に採点順で1行ずつ（読むのは数件でも、何があったかは全部見える）
    L += ["", "## 全話題（カテゴリ別・採点順）", ""]
    by_cat: dict[str, list] = {}
    for c in state["pool"]:
        by_cat.setdefault(c.get("category") or "その他", []).append(c)
    mark = {
        **{i: "✔" for i in state.get("selected", [])},
        **{i: "✖" for i in fb["rejected"]},
    }
    groups = headline_groups(state["pool"])
    for cat, rows in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        rows.sort(key=lambda c: -total_score(scores[c["id"]], c) if c["id"] in scores else 0)
        first = {}
        for k, c in enumerate(rows):  # 同じ主語のものを最初に出た位置に寄せる
            first.setdefault(groups.get(c["id"]) or c["id"], k)
        rows.sort(key=lambda c: first[groups.get(c["id"]) or c["id"]])
        L.append(f"**{cat}**（{len(rows)}話題）")
        last_grp = None
        for c in rows:
            if groups.get(c["id"]) != last_grp:
                last_grp = groups.get(c["id"])
                if last_grp:
                    L.append(f"- ▸ **{last_grp}**")
            s = scores.get(c["id"])
            pt = score_line(s, c) if s else "未採点"
            n = len(c.get("related", []))  # 「同じ話題: N本」と同じ数え方
            L.append(
                f"- {mark.get(c['id'], '　')} **{pt}** [{ja_title(c, s)}]({c['url']})"
                + (f"（同じ話題 +{n}）" if n else "")
            )
            if s:  # 基準ごとの点数と理由を1行ずつ
                if s.get("summary_ja"):
                    L.append(f"  - 要点: {s['summary_ja']}")
                L += [f"  - {d}" for d in score_detail(s, c)]
        L.append("")
    # 付録：読むものに入らなかった話題の要約（summary_scope = "all" のとき。カテゴリ別・採点順）
    sms = state.get("summaries") or {}
    rest = [
        c for c in state["pool"] if c["id"] not in set(state.get("selected", [])) and sms.get(c["id"], {}).get("text")
    ]
    if rest:
        L += [
            "",
            "## 付録：読むものに入らなかった話題の要約",
            "",
            f"採点対象のうち「今回読むもの」に入らなかった {len(rest)} 話題。後から気になったときに、本文を読まずに内容を確かめるためのもの。",
            "",
        ]
        rest_by_cat: dict[str, list] = {}
        for c in rest:
            rest_by_cat.setdefault(c.get("category") or "その他", []).append(c)
        for cat, rows in sorted(rest_by_cat.items(), key=lambda kv: -len(kv[1])):
            rows.sort(key=lambda c: -total_score(scores[c["id"]], c) if c["id"] in scores else 0)
            L.append(f"### {cat}（{len(rows)}話題）")
            for c in rows:
                s, sm = scores.get(c["id"]), sms[c["id"]]
                L += [
                    f"- {mark.get(c['id'], '　')} **[{ja_title(c, s)}]({c['url']})**"
                    + (f"（同じ話題 +{len(c['related'])}）" if c.get("related") else "")
                    + (f" — {score_line(s, c)}" if s else ""),
                    *[f"  {p.strip()}  " for p in re.split(r"\n\s*\n", sm["text"]) if p.strip()],
                    f"  （要約の元: {sm.get('articles', 1)}本、本文を読めた {sm.get('sources_used', 0)}本）",
                ]
            L.append("")
    L += ["## 除外したものと理由"]
    L += [f"- {pool[k]['title'] if k in pool else k}: {v}" for k, v in fb["rejected"].items()] or ["- なし"]
    L += [
        "",
        "## 選定の記録",
        f"- 取得 {metrics['fetched']} → 話題 {metrics['topics']} → 採点対象 {metrics['pool']} → 提示 {len(fb['proposed'])} → 確定 {len(state.get('selected', []))}",
        f"- 採用率 {metrics['adoption_rate']}（提示のうち除外しなかった割合）・追加 {metrics['added']}・取得失敗 {metrics['failed_sources']}・ガード検出 {metrics['guardrail_flags']}",
    ]
    L += [
        f"- {n}: 読んだ {v['read']} → 話題 {v['topics']} → 採点 {v['scored']}"
        for n, v in state.get("funnel", {}).items()
    ]
    if fb.get("merges"):
        absorbed: dict[str, int] = {}  # 連鎖してまとめても、今残っている話題ごとに数える（final がない古い記録は into）
        for m in fb["merges"]:
            absorbed[m.get("final", m["into"])] = absorbed.get(m.get("final", m["into"]), 0) + len(m["merged"])
        L.append(
            f"- 承認②で人がまとめた話題: {len(fb['merges'])} 組（"
            + "、".join(f"{pool[k]['title'][:30]} ← {n}" for k, n in absorbed.items() if k in pool)
            + "）"
        )
    L += [f"- 取れなかった取得元: {f}" for f in state.get("failed_sources", [])]
    L += [f"- ガードレール: {f}" for f in state.get("guardrail_flags", []) + flags]
    md_text = "\n".join(L) + "\n"
    write_trace(state, scores, fb, flags)
    base = os.path.join(settings.workdir, f"report-{state['mode']}-{state['period']}")
    with open(base + ".md", "w", encoding="utf-8") as f:  # Markdown
        f.write(md_text)
    try:
        md_to_docx(md_text, base + ".docx")  # Word（同じ内容）
        log(f"[report] Word 版 → {base}.docx")
    except Exception as e:  # Word 変換の失敗でレポート自体は止めない
        log(f"⚠ Word 版の作成に失敗（Markdown は保存済み）: {e}")
    log(
        f"[report] 取得 {metrics['fetched']} → 話題 {metrics['topics']} → 採点対象 {metrics['pool']} → 提示 {len(fb['proposed'])} → 確定 {len(state.get('selected', []))}"
        f"／採用率 {metrics['adoption_rate']} → {base}.md"
    )
    return {"report_md": md_text, "metrics": metrics, "guardrail_flags": flags}
