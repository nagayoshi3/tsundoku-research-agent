"""同じ話題の記事を1件にまとめる（cluster_topics）。"""

import hashlib
import re
from collections import Counter

import numpy as np

from tsundoku.config import CONFIG, log, settings
from tsundoku.guardrails import wrap_untrusted
from tsundoku.llm import structured_call
from tsundoku.models import EntityBatch, ResearchState
from tsundoku.prompts import ENTITY_PROMPT
from tsundoku.sources import ARXIV_RE

# --- 調整するつまみ（本番の埋め込みでは実データを見て調整する）-------------
W_EMB, W_KW = 0.7, 0.3  # 意味の近さ（埋め込み＋話題キーワード）と固有名詞の一致の重み
W_TOPIC_KW = 0.3  # 「意味の近さ」の中で、話題キーワードの一致に割く重み（両方にキーワードがあるときだけ）
SAME_TOPIC = 0.62  # 総合スコアがこれ以上なら同じ話題
VETO_COS = 0.95  # 見出しの固有名詞が食い違うのにまとめてよいのは、意味がほぼ同一のときだけ
ENTITY_BATCH = 25  # LLM で固有名詞を抜くときに1回で送る件数

# --- 固有名詞（規則）----------------------------------------------------------------
GENERIC_WORDS = {  # 大文字で書かれても固有名詞とみなさない一般語・略語
    "ai",
    "llm",
    "llms",
    "api",
    "apis",
    "gpu",
    "gpus",
    "cpu",
    "ceo",
    "cto",
    "ml",
    "rl",
    "sdk",
    "gui",
    "ui",
    "os",
    "us",
    "uk",
    "eu",
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "for",
    "in",
    "on",
    "with",
    "to",
    "how",
    "why",
    "what",
    "new",
    "inc",
    "corp",
    "labs",
    "lab",
    "sample",
    "update",
    "news",
    "blog",
    "week",
    "today",
}
GENERIC_KATAKANA = {
    "エージェント",
    "モデル",
    "ロボット",
    "データ",
    "ベンチマーク",
    "ニュース",
    "サービス",
    "システム",
    "アップデート",
    "リリース",
    "ツール",
    "プラットフォーム",
    "ユーザー",
    "テクノロジー",
    "ソフトウェア",
    "オープンソース",
    "クラウド",
    "セキュリティ",
    "ブログ",
}
ENTITY_ALIASES = {
    "グーグル": "google",
    "オープンエーアイ": "openai",
    "エヌビディア": "nvidia",  # 表記ゆれ → 右に寄せる
    "アンソロピック": "anthropic",
    "マイクロソフト": "microsoft",
    "メタ": "meta",
}
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-.'][A-Za-z0-9]+)*")
KATAKANA_RE = re.compile(r"[ァ-ヴー・]{3,}")
BOUNDARY = set(".!?。！？「『[]()【】:：")


def _norm(w: str) -> str:
    w = w.lower().rstrip(".").removesuffix("'s")
    return ENTITY_ALIASES.get(w, w)


def _strong(w: str) -> bool:
    # 型番・製品名らしい語：数字を含む（GPT-5、H100）か、大文字が2つ以上（OpenAI、NVIDIA）
    return any(ch.isdigit() for ch in w) or sum(ch.isupper() for ch in w) >= 2


def _mid_sentence_caps(text: str) -> set[str]:
    # 文中（文頭以外）で大文字始まりの語。英語の見出しは全語が大文字始まりなので、本文側で判定する
    out = set()
    for m in WORD_RE.finditer(text):
        prev = text[: m.start()].rstrip()
        if m.group(0)[0].isupper() and prev and prev[-1] not in BOUNDARY:
            out.add(_norm(m.group(0)))
    return out


def to_entity_tokens(text: str, caps: set[str] = frozenset()) -> set[str]:
    # 文字列 → 固有名詞のトークン集合（規則と LLM の結果を同じ形にそろえる）
    ents = set()
    for w in WORD_RE.findall(text):
        n = _norm(w)
        if n not in GENERIC_WORDS and len(n) >= 2 and (_strong(w) or n in caps):
            ents.add(n)
    for k in KATAKANA_RE.findall(text):
        k = k.strip("・")
        if k not in GENERIC_KATAKANA:
            ents.add(_norm(k))
    return ents


def rule_entities(c: dict) -> set[str]:
    body = c["abstract"][:600]
    return to_entity_tokens(f"{c['title']} {body}", caps=_mid_sentence_caps(body))


def title_part(c: dict, ents: set[str]) -> set[str]:
    # 固有名詞のうち見出しに出てくるもの = 記事の主語（どの会社の・どの製品の話か）
    words = {_norm(w) for w in WORD_RE.findall(c["title"])} | {
        _norm(k.strip("・")) for k in KATAKANA_RE.findall(c["title"])
    }
    title = c["title"].lower()
    return {
        e for e in ents if e in words or (" " in e and e in title)
    }  # 複数語の製品名（muse wonder）は見出しの文字列で照合


def is_product(tok: str) -> bool:
    # 製品・モデル名らしい固有名詞：複数語（muse wonder）か、数字を含む（gpt-5、robomind-2）。会社名1語（meta）は含めない
    return " " in tok or any(ch.isdigit() for ch in tok)


def headline_groups(cands: list[dict]) -> dict[str, str]:
    # 話題にまとまらなくても同じ主語を持つものを、表示上のグループにする（例: Muse Spark と Muse Wonder は別の話題だが「muse」の下に並ぶ）
    # 各話題の見出しの固有名詞のうち、2つ以上の話題で共有されるものを数え、最も多く共有される語をグループ名にする
    heads = {c["id"]: title_part(c, set(c.get("entities", []))) for c in cands}
    freq = Counter(t for ts in heads.values() for t in ts)
    out = {}
    for pid, ts in heads.items():
        shared = [t for t in ts if freq[t] >= 2 and t not in GENERIC_WORDS]
        if shared:
            out[pid] = max(shared, key=lambda t: (freq[t], " " in t, len(t)))
    return out


# --- 話題キーワードとカテゴリ（規則）：重視キーワードとカテゴリの手がかり語が本文に出ていれば拾う ---------------
def rule_keywords(c: dict) -> set[str]:
    text = f"{c['title']} {c['abstract'][:600]}".lower()
    # 手がかり語はカテゴリの定義だけから取る。重視キーワードは採点と並べ順の役割なので、分類・まとめには使わない
    hints = {h for hs in CONFIG.get("categories", {}).values() for h in hs}
    out = set()
    for h in hints:
        hl = h.lower()
        if hl.isascii():  # 英語は単語の境界で照合（rag が storage に当たらないように）
            if re.search(r"(?<![a-z0-9])" + re.escape(hl) + r"(?:e?s)?(?![a-z0-9])", text):  # 複数形も拾う
                out.add(hl)
        elif hl in text:  # 日本語は部分一致
            out.add(hl)
    return out


def rule_category(kws: set[str]) -> str:
    # 手がかり語が多く当たったカテゴリ。どれにも当たらなければ「その他」
    hits = {name: len(kws & {h.lower() for h in hs}) for name, hs in CONFIG.get("categories", {}).items()}
    best = max(hits, key=hits.get) if hits else ""
    return best if best and hits[best] > 0 else "その他"


def llm_entities(items: list[dict]) -> tuple[list[set[str]], list[set[str]], list[str]]:
    # デザインパターン — Prompt/response optimiser：出力は EntityBatch に固定（models.py）
    # デザインパターン — Multimodal guardrails：記事は wrap_untrusted で「データ」として渡す（guardrails.py）
    out = [set() for _ in items]
    kws = [set() for _ in items]
    cats = ["" for _ in items]
    system = ENTITY_PROMPT.format(categories="、".join(CONFIG.get("categories", {})) or "（なし）")
    for start in range(0, len(items), ENTITY_BATCH):
        chunk = items[start : start + ENTITY_BATCH]
        user = "\n".join(
            f"[{start + k}]\n{wrap_untrusted({**c, 'abstract': c['abstract'][:300]})}" for k, c in enumerate(chunk)
        )
        for row in structured_call(EntityBatch, system, user).items:
            if start <= row.i < start + len(chunk):
                toks = set()
                for n in row.names:
                    ws = [_norm(w) for w in WORD_RE.findall(n)]
                    toks |= to_entity_tokens(n, caps=set(ws))
                    if len(ws) >= 2 and all(w not in GENERIC_WORDS for w in ws):
                        toks.add(" ".join(ws))  # 複数語の製品名は1語としても持つ（muse wonder）
                out[row.i] = toks
                kws[row.i] = {k.strip().lower() for k in row.keywords if k.strip()}
                cats[row.i] = (
                    row.category.strip()
                    if row.category.strip() in CONFIG.get("categories", {})
                    else ("その他" if row.category.strip() else "")
                )
    return out, kws, cats


# --- 埋め込み ------------------------------------------------------------------------
def embed_text(c: dict) -> str:
    return f"{c['title']}\n{c['abstract'][:300]}"  # 見出し＋冒頭300字


def embed(texts: list[str]) -> np.ndarray:
    if settings.fake_llm:
        return np.array([_fake_embed(t) for t in texts])
    from langchain.embeddings import init_embeddings

    vecs = np.array(init_embeddings(CONFIG["embed_model"]).embed_documents(texts))
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def _fake_embed(text: str, dim: int = 512) -> np.ndarray:
    # 偽の埋め込み：文字3-gram をハッシュで数える。言い回しが近いほど cos が高い（意味は分からない）
    t = re.sub(r"\[sample[^\]]*\]", "", text.lower())  # サンプルの目印（全件に共通の文字列）は除く
    t = re.sub(r"\s+", " ", t).strip()
    v = np.zeros(dim)
    for i in range(len(t) - 2):
        v[int(hashlib.md5(t[i : i + 3].encode()).hexdigest(), 16) % dim] += 1
    return v / (np.linalg.norm(v) or 1)


# --- 2件が同じ話題か ------------------------------------------------------------------
def topic_score(
    a: dict,
    b: dict,
    cos: float,
    ea: set,
    eb: set,
    ta: set,
    tb: set,
    ka: set = frozenset(),
    kb: set = frozenset(),
) -> tuple[float, str]:
    # ea, eb：固有名詞（見出し＋本文）、ta, tb：そのうち見出しに出てくるもの、ka, kb：話題キーワード
    if ta and tb and not (ta & tb) and cos < VETO_COS:
        return (
            0.0,
            f"cos {cos:.2f} だが見出しの固有名詞が食い違う（{', '.join(sorted(ta))} ／ {', '.join(sorted(tb))}）",
        )
    prod = {t for t in ta & tb if is_product(t)}
    if (
        len(ta & tb) >= 2 or prod
    ):  # 見出しの固有名詞が2つ以上一致、または製品名（muse wonder、gpt-5）が一致 → 言語が違っても同じ話題
        return max(
            SAME_TOPIC, W_EMB * cos + W_KW
        ), f"見出しの固有名詞が一致（{', '.join(sorted(ta & tb))}）・cos {cos:.2f}"
    shared = ea & eb
    jac = len(shared) / len(ea | eb) if ea and eb else 0.0
    kw = jac * min(1.0, len(shared) / 2)  # 共通の固有名詞が1つだけなら半分に割り引く
    # 意味の近さ：埋め込みの cos に、両方に話題キーワードがあればその一致（Jaccard）を混ぜる
    sem, kw_note = cos, ""
    if ka and kb:
        tj = (
            len(ka & kb) / len(ka | kb) * min(1.0, len(ka & kb) / 2)
        )  # 共通が1語だけなら半分に割り引く（launches だけの一致でまとめない）
        sem = cos + W_TOPIC_KW * tj * (
            1 - cos
        )  # キーワードの一致は cos を押し上げるだけ（不一致で下げない。規則の語はまばらなので）
        kw_note = f"・キーワード {tj:.2f}（共通: {', '.join(sorted(ka & kb)) or 'なし'}）"
    return (
        W_EMB * sem + W_KW * kw,
        f"cos {cos:.2f}{kw_note}・固有名詞 {kw:.2f}（共通: {', '.join(sorted(shared)) or 'なし'}）",
    )


def member(c: dict, why: str, ents: set = frozenset(), kws: set = frozenset()) -> dict:
    # 代表の related に残す1本分（集計 aggregate.py でも使う）
    return {
        "title": c["title"],
        "url": c["url"],
        "feed": c["feed"],
        "outlet": c.get("outlet", ""),
        "saved_at": c.get("saved_at", ""),
        "entities": sorted(ents),
        "keywords": sorted(kws),
        "why": why,
    }


def _priority(c: dict):
    # 代表になりやすい順：arXiv ID を持つ記事 → フィードの上の方の記事
    return (
        not ARXIV_RE.fullmatch(c["id"]),
        c["rank"] or 10**6,
    )


def cluster_topics(state: ResearchState) -> dict:
    # 1) id が同じ（同じ arXiv ID・正規化後に同じ URL）ものを1件に。消した方は related に残す
    by_id: dict[str, dict] = {}
    for c in sorted(state.get("candidates", []), key=_priority):
        if c["id"] in by_id:
            by_id[c["id"]]["related"].append(member(c, "同じ ID"))
        else:
            by_id[c["id"]] = {**c, "related": []}
    items = list(by_id.values())
    if not items:
        return {"topics": []}

    # 2) 固有名詞・話題キーワード・カテゴリ（規則、本物の LLM なら LLM も）と埋め込み（意味）を用意する
    ents = [rule_entities(c) for c in items]
    kws = [rule_keywords(c) for c in items]
    cats = ["" for _ in items]
    if CONFIG.get("entity_llm") and not settings.fake_llm:
        le, lk, lc = llm_entities(items)
        ents = [r | x for r, x in zip(ents, le)]
        kws = [r | x for r, x in zip(kws, lk)]
        cats = lc
    cats = [c or rule_category(k) for c, k in zip(cats, kws)]  # LLM が付けなければ規則で
    heads = [title_part(c, e) for c, e in zip(items, ents)]
    vecs = embed([embed_text(c) for c in items])
    cos = vecs @ vecs.T

    # 3) 代表を順に決め、各記事を「最も近い代表」に寄せる（連鎖でつながりすぎないよう、比べるのは代表とだけ）
    reps: list[int] = []
    for i in range(len(items)):
        best, best_s, best_why = None, SAME_TOPIC, ""
        for r in reps:
            s, why = topic_score(
                items[r],
                items[i],
                float(cos[r, i]),
                ents[r],
                ents[i],
                heads[r],
                heads[i],
                kws[r],
                kws[i],
            )
            if s >= best_s:
                best, best_s, best_why = r, s, why
        if best is None:
            reps.append(i)
        else:
            c = items[i]
            items[best]["related"] += [member(c, f"話題 {best_s:.2f}：{best_why}", ents[i], kws[i])] + c["related"]
            kws[best] = kws[best] | kws[i]  # まとめた記事のキーワードは代表に集める
    topics = [
        {
            **items[r],
            "entities": sorted(ents[r]),
            "keywords": sorted(kws[r]),
            "category": cats[r],
        }
        for r in reps
    ]
    merged = [t for t in topics if t["related"]]
    log(
        f"[cluster_topics] {len(state['candidates'])} 件 → ID で {len(items)} 件 → 話題で {len(topics)} 件"
        f"（2本以上まとめた話題 {len(merged)}、カテゴリ: {dict(Counter(t['category'] for t in topics).most_common(5))}）"
    )
    for t in merged[:10]:  # まとめた話題は先頭10件まで内訳を出す
        log(
            f"  ・{t['title'][:60]} ← {len(t['related'])} 本（{'、'.join(sorted({r.get('outlet', '') for r in t['related']} - {''}))}）"
        )
    return {"topics": topics}


def explain_pair(a: dict, b: dict) -> str:
    # 2件を比べた内訳（しきい値を調整するときに使う）
    ea, eb = rule_entities(a), rule_entities(b)
    va, vb = embed([embed_text(a), embed_text(b)])
    s, why = topic_score(
        a,
        b,
        float(va @ vb),
        ea,
        eb,
        title_part(a, ea),
        title_part(b, eb),
        rule_keywords(a),
        rule_keywords(b),
    )
    return f"{'同じ話題' if s >= SAME_TOPIC else '別の話題'}（{s:.2f}）— {why}"
