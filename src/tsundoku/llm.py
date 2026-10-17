"""LLM の呼び出し（構造化出力）と、API を使わない決定的な偽 LLM。"""

import hashlib

from tsundoku.config import CONFIG, settings
from tsundoku.models import EntityBatch, Plan, Score, Suggestion, Summary


def structured_call(schema, system: str, user: str):
    if settings.fake_llm:
        return _fake(schema, user)
    from langchain.chat_models import init_chat_model

    # timeout：1回の呼び出しが60秒で打ち切られ、再試行（max_retries）→ それでも駄目なら graph.py の RetryPolicy。応答待ちで無言のまま固まらないため
    llm = init_chat_model(CONFIG["model"], temperature=0, timeout=60, max_retries=2).with_structured_output(schema)
    return llm.invoke([("system", system), ("human", user)])


def _fake(schema, user: str):
    h = int(hashlib.sha256(user.encode()).hexdigest(), 16)
    if schema is Plan:
        return Plan(
            focus_keywords=["agent", "reasoning", "robot"],
            max_items=100,
            suggestions=[Suggestion(keyword="robot", reason="（偽LLM）ロボット関連の話題が多い")],
        )
    if schema is EntityBatch:
        return EntityBatch(items=[])  # 偽LLMでは規則による抜き出しだけを使う
    if schema is Score:
        pid = user.split("id=", 1)[1].split()[0] if "id=" in user else "unknown"
        return Score(
            id=pid,
            relevance=h % 6,
            novelty=(h // 7) % 6,
            impact=(h // 49) % 6,
            reason="（偽LLM）決定的なテスト用スコア",
            relevance_reason="（偽LLM）関連度の理由",
            novelty_reason="（偽LLM）新規性の理由",
            impact_reason="（偽LLM）注目度の理由",
            title_ja="",
            summary_ja="",
        )  # 偽LLMは翻訳しない（表示は原文に戻る）
    if schema is Summary:
        n = user.count("<article")
        return Summary(text=f"（偽LLM）{n}本の記事をまとめた要約。" * 6, sources_used=n)
    raise ValueError(schema)
