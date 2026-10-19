"""LLM の呼び出し（構造化出力）と、API を使わない決定的な偽 LLM。"""

import hashlib
import threading

from tsundoku.config import CONFIG, settings
from tsundoku.models import EntityBatch, Plan, Score, Suggestion, Summary

FAKE_TOKENS = (1000, 200)  # 偽LLMが1回の呼び出しで使ったことにする入力・出力のトークン数

# 実行全体のトークン数（プロセス内の合計。run_interactive の頭で reset_tokens する）
_tokens = {"input": 0, "output": 0, "lock": threading.Lock()}


def count_tokens(input_tokens: int, output_tokens: int) -> None:
    with _tokens["lock"]:  # score_one・summarize は並列に呼ぶので足すところだけ排他
        _tokens["input"] += input_tokens
        _tokens["output"] += output_tokens


def token_usage() -> dict:
    with _tokens["lock"]:
        return {"input": _tokens["input"], "output": _tokens["output"]}


def reset_tokens() -> None:
    with _tokens["lock"]:
        _tokens["input"] = _tokens["output"] = 0


def cost_jpy(usage: dict) -> float:
    # 100万トークンあたりの円（default_config.yaml の price_per_1m_input / price_per_1m_output）
    return (
        usage["input"] * CONFIG.get("price_per_1m_input", 0) + usage["output"] * CONFIG.get("price_per_1m_output", 0)
    ) / 1_000_000


def structured_call(schema, system: str, user: str):
    if settings.fake_llm:
        count_tokens(*FAKE_TOKENS)
        return _fake(schema, user)
    from langchain.chat_models import init_chat_model

    # timeout：1回の呼び出しが60秒で打ち切られ、再試行（max_retries）→ それでも駄目なら graph.py の RetryPolicy。応答待ちで無言のまま固まらないため
    # include_raw：トークン数（usage_metadata）を数えるために元の応答も受け取る
    llm = init_chat_model(CONFIG["model"], temperature=0, timeout=60, max_retries=2).with_structured_output(
        schema, include_raw=True
    )
    out = llm.invoke([("system", system), ("human", user)])
    usage = getattr(out["raw"], "usage_metadata", None) or {}
    count_tokens(usage.get("input_tokens", 0), usage.get("output_tokens", 0))  # 形式が崩れた応答も費用はかかる
    if out.get("parsing_error") is not None:
        raise out["parsing_error"]
    if out.get("parsed") is None:
        raise ValueError(f"{schema.__name__} の構造化出力が空でした")
    return out["parsed"]


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
