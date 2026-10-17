"""本文を読んで要約する（summarize）。承認②の後ろに置く。"""

from concurrent.futures import ThreadPoolExecutor

import requests

from tsundoku.config import CONFIG, log, settings
from tsundoku.guardrails import sanitize_abstract
from tsundoku.llm import structured_call
from tsundoku.models import ResearchState, Summary
from tsundoku.prompts import SUMMARY_PROMPT


def fetch_body(url: str, limit: int = 6000) -> str:
    # 記事の本文を取る。失敗したら空文字（呼び出し側が RSS の要旨に落とす）
    if settings.sample_data or not CONFIG.get("summary_fetch_body", True):
        return ""
    try:
        import trafilatura

        html = trafilatura.fetch_url(url)
        if not html:  # ブラウザ扱いでないと返さないサイト向けに requests でもう一度
            r = requests.get(
                url,
                timeout=20,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"
                },
            )
            html = r.text if r.ok else ""
        text = trafilatura.extract(html, include_comments=False, include_tables=False) if html else None
        return (text or "")[:limit]
    except Exception as e:  # ネットワーク・解析の失敗は要約を止めない
        log(f"[summarize] 本文を取れず（{url[:60]}…）: {type(e).__name__}")
        return ""


def _summarize_one(c: dict, theme: str) -> dict:
    # 代表＋同じ話題の記事（URL 違いの重複は除く）の本文を集め、1回の LLM 呼び出しで横断要約する
    arts = [c] + [r for r in c.get("related", []) if r.get("why") != "同じ ID"]
    arts = arts[: CONFIG.get("summary_max_articles", 4)]
    parts, used = [], 0
    for a in arts:
        body = fetch_body(a["url"])
        if body:
            used += 1
        text, _ = sanitize_abstract(body or a.get("abstract", ""))
        parts.append(
            f'<article source="{a.get("outlet", "")}" body="{"full" if body else "abstract-only"}">\n'
            f"TITLE: {a['title']}\n{text}\n</article>"
        )
    chars = CONFIG.get("summary_chars", 300)
    mx = (
        CONFIG.get("summary_chars_multi", 400) if len(arts) > 1 else int(chars * 1.15)
    )  # 複数本をまとめる話題は 400 字まで
    system = SUMMARY_PROMPT.format(
        theme=theme,
        chars=chars if len(arts) == 1 else f"{chars}〜{mx}",
        min_chars=int(chars * 0.8),
        max_chars=mx,
    )
    user = f"次の {len(arts)} 本の記事を1つの話題として要約してください。\n\n" + "\n\n".join(parts)
    s = structured_call(Summary, system, user)
    text = s.text.strip()
    if used and len(text) < int(
        chars * 0.8
    ):  # 本文があるのに短すぎる：1回だけ書き直させる（要旨しかないときは短くてよい）
        s = structured_call(
            Summary,
            system,
            user + f"\n\n前回の要約は {len(text)} 字で短すぎました。本文の具体的な内容（数字・仕組み・背景）を足して "
            f"{int(chars * 0.8)}〜{mx} 字で書き直してください。",
        )
        text = s.text.strip() if len(s.text.strip()) > len(text) else text
    return {"text": text, "sources_used": used, "articles": len(arts)}


def summarize(state: ResearchState) -> dict:
    pool = {c["id"]: c for c in state["pool"]}
    done = dict(state.get("summaries") or {})
    selected = [pid for pid in state.get("selected", []) if pid in pool]
    others = (
        [c["id"] for c in state["pool"] if c["id"] not in set(selected)]
        if CONFIG.get("summary_scope", "all") == "all"
        else []
    )
    todo = [pid for pid in selected + others if pid not in done]  # 読むものを先に、付録（読まなかった話題）を後に
    if not todo:
        log("[summarize] 要約する話題はありません")
        return {"summaries": done}
    log(
        f"[summarize] {len(todo)} 件を要約（読むもの {len(selected)}、付録 {len(others)}。本文を取得 → 同じ話題の記事を横断して "
        f"{CONFIG.get('summary_chars', 300)} 字、複数本なら {CONFIG.get('summary_chars_multi', 400)} 字まで）。10件ごとに進み具合を出す"
    )
    n = 0
    with ThreadPoolExecutor(max_workers=6) as ex:  # 本文取得と LLM 呼び出しを並列に
        for pid, r in ex.map(lambda pid: (pid, _summarize_one(pool[pid], state["theme"])), todo):
            done[pid] = r
            n += 1
            if pid in selected:
                log(
                    f"[summarize] ✓ {pool[pid]['title'][:50]}（{r['articles']}本、本文 {r['sources_used']}本、{len(r['text'])}字）"
                )
            elif n % 10 == 0 or n == len(todo):
                log(f"[summarize] {n}/{len(todo)} 件 要約済み")
    return {"summaries": done}
