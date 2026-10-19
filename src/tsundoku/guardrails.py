"""ガードレール：外部の文章を指示として扱わない（入力側）、出力が実在するか確かめる（出力側）。"""

import re

SUSPICIOUS = re.compile(
    r"(ignore (all|any|the)? ?(previous|above) instructions|system prompt|you are now|"
    r"以前の指示を無視|上記の指示を無視|システムプロンプト)",
    re.IGNORECASE,
)


def sanitize_abstract(text: str) -> tuple[str, bool]:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text or "")
    text = text.replace("<paper", "&lt;paper").replace("</paper", "&lt;/paper")
    return text[:2000], bool(SUSPICIOUS.search(text))


def wrap_untrusted(c: dict) -> str:
    return f'<paper id="{c["id"]}">\nTITLE: {c["title"]}\nABSTRACT: {c["abstract"]}\n</paper>'


def wrap_related(titles: list[str]) -> str:
    # 同じ話題の他の記事のタイトル（外部の文章）を、データとして <related> で囲む。改行や囲みのタグで抜け出せないようにする
    lines = []
    for t in titles:
        t = re.sub(r"[\x00-\x1f]+", " ", t or "")
        t = re.sub(r"<(/?)(paper|related)", r"&lt;\1\2", t, flags=re.IGNORECASE)
        lines.append(f"- {t}")
    return "<related>\n" + "\n".join(lines) + "\n</related>"


def check_report(selected: list[str], pool: list[dict]) -> list[str]:
    known = {c["id"]: c for c in pool}
    flags = []
    for pid in selected:
        c = known.get(pid)
        if c is None:
            flags.append(f"output:unknown_id:{pid}")
        elif not c["url"].startswith("https://"):  # 出典は取得時の URL。https 以外は怪しい
            flags.append(f"output:unexpected_url:{pid}")
    return flags
