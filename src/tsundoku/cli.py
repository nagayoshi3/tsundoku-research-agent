"""対話での実行（run_interactive）と、承認①②の入力。"""

import json
import os
import time

from langgraph.types import Command

from tsundoku.checkpoint import open_checkpointer, sync_checkpoint
from tsundoku.config import CONFIG, log, log_start, settings
from tsundoku.graph import build_graph
from tsundoku.llm import reset_tokens
from tsundoku.periods import normalize_period, period_label
from tsundoku.sources.inoreader import inoreader_token

AUTO_APPROVE = False  # True なら承認①②で入力を求めず、提案をそのまま承認する（--yes、CI、デモ用）


def _input(prompt: str) -> str:
    if AUTO_APPROVE:
        print(prompt + "（自動承認）")
        return ""
    return input(prompt).strip()


def ask(value: dict) -> dict:
    print("\n" + "=" * 70 + f"\n⏸ {value['message']}")
    if value["type"] == "approve_plan":
        p = value["proposal"]
        given = CONFIG.get("focus_keywords", [])
        print(f"  与えたキーワード: {', '.join(given)}")
        for sg in p.get("suggestions", []):  # この期間の記事から LLM が推薦した語（理由付き）
            print(f"  ＋ おすすめ: {sg['keyword']} — {sg['reason']}")
        print(f"  → 今回のキーワード: {', '.join(p['focus_keywords'])}\n  採点件数（合計）: {p['max_items']}")
        print("  （おすすめを外すなら、残したい語だけをカンマ区切りで入力。与えた語は自動で残る）")
        kw = _input("キーワード（カンマ区切り、空でそのまま）> ")
        mx = _input("採点件数（合計、空でそのまま）> ")
        out = {"approved": True}
        if kw:
            out["focus_keywords"] = [k.strip() for k in kw.split(",") if k.strip()]
        if mx:
            out["max_items"] = int(mx)
        return out
    if value["type"] == "review":
        items = value["shortlist"]
        last_cat = None
        letters = "abcdefghijklmnopqrstuvwxyz"

        def show(m, label, indent):
            more = f"・同じ話題 +{m['related']}" if m.get("related") else ""
            print(f"{indent}{label} [{m['score']}] {m['title']}  ({m['where']}{more})")
            if m.get("title_orig") and m["title_orig"] != m["title"]:
                print(f"{indent}    原題: {m['title_orig']}")
            if m.get("summary"):
                print(f"{indent}    {m['summary']}")
            for d in m.get("detail") or [f"採点理由: {m['reason']}"]:  # 基準ごとの点数と理由
                print(f"{indent}    {d}")
            print(f"{indent}    総評: {m['reason']}")

        num_of = {}  # 話題 id → 一覧の番号（3 や 3b）。まとめ候補の表示に使う
        for i, it in enumerate(items, 1):
            if it.get("category") != last_cat:  # カテゴリが変わるところに見出し
                last_cat = it.get("category")
                print(f"\n  ── {last_cat or 'その他'} ──")
            members = it.get("members") or [it]
            if len(members) > 1:  # 同じ主語のまとまり：番号1つ＋枝番
                print(f"  {i:>2}. ▸ {it['title']}  （{i} で全部、{i}a のように枝番で1本だけ除外）")
                for k, m in enumerate(members):
                    show(m, f"{i}{letters[k]}.", "      ")
                    num_of[m["id"]] = f"{i}{letters[k]}"
            else:
                show(members[0], f"{i:>2}.", "  ")
                num_of[members[0]["id"]] = str(i)
        n_all = sum(len(it.get("members") or [it]) for it in items)
        hints = [h for h in value.get("merge_hints", []) if h["a"] in num_of and h["b"] in num_of]
        if hints:  # 同じ話題かもしれない組み合わせ（自動ではまとめなかったもの）。まとめるかは人が決める
            print("\n  💡 同じ話題かもしれない組み合わせ（まとめるなら番号を + でつなぐ）:")
            for h in hints:
                print(f"     {num_of[h['a']]} + {num_of[h['b']]}: {h['title_a'][:40]} ／ {h['title_b'][:40]}")
                print(f"        {h['why']}")
        print(f"  → 何もしなければ上の {n_all} 話題すべてを「読む」として確定します")
        mg = _input(
            "まとめる番号（+ でつなぐ。例 3+7; 2+5+9。まとめると評価と要約も1つになる。空 Enter でまとめない）> "
        )
        rj = _input("除外する番号（理由は任意。例 2:既読; 4b; 7。空 Enter で除外なし）> ")
        ad = _input("候補外から追加する記事（URL か arXiv ID、カンマ区切り。空 Enter で追加なし）> ")

        def pick(
            n,
        ):  # 一覧の番号 → id のリスト。"5" はその項目の全部、"5b" は枝番の1本。範囲外は無視して知らせる
            n = n.strip().lower()
            num, sub = (n[:-1], n[-1]) if n and n[-1].isalpha() else (n, "")
            if num.isdigit() and 1 <= int(num) <= len(items):
                members = items[int(num) - 1].get("members") or [items[int(num) - 1]]
                if not sub:
                    return [m["id"] for m in members]
                if sub in letters[: len(members)]:
                    return [members[letters.index(sub)]["id"]]
            print(f"  （無視: 番号 {n!r} は範囲外）")
            return []

        merge = []
        for a in mg.replace("；", ";").replace("＋", "+").split(";"):
            ids = []
            for n in a.split("+"):
                ids += [i for i in pick(n) if i not in ids] if n.strip() else []
            if len(ids) >= 2:
                merge.append(ids)
            elif a.strip():
                print(f"  （無視: {a.strip()!r} は2つ以上の番号を + でつなぐ）")
        reject = {}
        for a in rj.replace("；", ";").replace("：", ":").split(";"):  # 全角の区切りも受け付ける
            if not a.strip():
                continue
            num, _, why = a.partition(":")
            for i in pick(num):  # 理由は省略可（空なら「理由なし」と記録）
                reject[i] = why.strip() or "理由なし"
        return {
            "approve_all": True,
            "merge": merge,
            "reject": reject,
            "add": [a.strip() for a in ad.split(",") if a.strip()],
        }
    if value["type"] == "question":
        print(f"  {value['message']}")
        return {"action": _input(f"{'/'.join(value['options'])} > ") or "end"}
    return {"approved": True}


def run_interactive(mode: str = "weekly", period: str | int | None = None, fresh: bool = False):
    # fresh=True：その期間の途中経過・完了済みの結果を捨てて最初からやり直す（採点のやり直し、設定変更の反映に）
    period = normalize_period(mode, period)  # 日付・last・-1 などを内部の形にそろえる
    reset_tokens()  # トークン数は実行ごとに数え直す（Colab で同じプロセスから何度も実行するため）
    log_start(os.path.join(settings.workdir, f"log-{mode}-{period}.txt"))  # 実行ログをファイルにも残す
    log(f"期間: {period_label(mode, period)}")
    if settings.fake_llm or settings.sample_data:
        log(
            "⚠ デモ設定で動いています: "
            + "、".join(
                (["偽 LLM（settings.fake_llm = True）"] if settings.fake_llm else [])
                + (["サンプルデータ（settings.sample_data = True）"] if settings.sample_data else [])
            )
            + "。本番は --fake-llm / --sample を付けずに実行する"
        )
    if not settings.sample_data and any(s["type"] == "inoreader_api" for s in CONFIG["sources"]):
        inoreader_token()  # 初回はここでブラウザの許可を求める（グラフの外で済ませる）
    saver = open_checkpointer()
    app = build_graph(saver)
    cfg = {"configurable": {"thread_id": f"{mode}-{period}"}}
    if fresh and app.get_state(cfg).values:
        try:
            saver.delete_thread(cfg["configurable"]["thread_id"])
            log(f"[fresh] {mode}-{period} の途中経過を消して最初から始めます")
        except Exception as e:  # 古い版の SqliteSaver に delete_thread がないとき
            cfg = {"configurable": {"thread_id": f"{mode}-{period}-{int(time.time())}"}}
            log(f"[fresh] 新しいスレッド {cfg['configurable']['thread_id']} で最初から始めます（{e}）")
    snap = app.get_state(cfg)
    if snap.next:
        print(f"▶ 再開: {mode}-{period}（待機中: {snap.next}）")
        if not snap.interrupts:
            app.invoke(None, cfg)
            sync_checkpoint(saver)
    elif snap.values.get("report_md"):
        print(
            f"✓ この期間は完了済み（前回の結果をそのまま表示。採点方法や設定を変えた後は "
            f"run_interactive({mode!r}, {period!r}, fresh=True) で最初からやり直してください）\n\n{snap.values['report_md']}"
        )
        return
    else:
        print(f"▶ 起動: {mode}-{period}")
        app.invoke(
            {
                "mode": mode,
                "period": period,
                "theme": CONFIG["theme"],
                "sources": CONFIG["sources"],
            },
            cfg,
        )
        sync_checkpoint(saver)  # 止まるたびに Drive へ複製（ランタイムが切れても再開できる）
    while (snap := app.get_state(cfg)).interrupts:
        app.invoke(Command(resume=ask(snap.interrupts[0].value)), cfg)
        sync_checkpoint(saver)
    v = app.get_state(cfg).values
    print("\n" + v["report_md"] + "\nmetrics: " + json.dumps(v["metrics"], ensure_ascii=False))
    base = os.path.join(settings.workdir, f"report-{mode}-{period}")
    print(f"\n📄 レポート: {base}.md（Markdown）／ {base}.docx（Word）")
