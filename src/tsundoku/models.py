"""データの型：Candidate / Plan / Score / Summary と、LangGraph の State。"""

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


# デザインパターン — Agent adapter：どの取得元も、この Candidate に揃えてから後段に渡す
class Candidate(BaseModel):
    id: str  # arXiv ID（記事が arXiv の論文なら）。なければ正規化した記事の URL（重複除去のキー）
    title: str
    url: str
    source: str = "rss"  # 取得元の種類（レジストリの名前）
    published: str = ""
    abstract: str = ""
    rank: int = 0  # 取得元の中での順位（Inoreader は新しく保存した順）
    feed: str = ""  # どの取得元から来たか（CONFIG["sources"] の name）。枠の配分に使う
    outlet: str = ""  # 記事の媒体（Inoreader の購読フィード名）
    saved_at: str = ""  # 保存した日時（日本時間）。期間の絞り込みと集計に使う
    tags: list[str] = []  # Inoreader のタグ・フォルダ
    entities: list[str] = []  # 固有名詞（cluster.py で付ける）
    keywords: list[str] = []  # 話題キーワード（cluster.py で付ける。規則＋LLM）
    category: str = ""  # 大きなカテゴリ（cluster.py で付ける。CONFIG["categories"] のキーか「その他」）
    related: list[dict] = []  # 同じ話題としてまとめた他の記事（cluster.py で付ける）


# デザインパターン — Prompt/response optimiser：LLM の出力スキーマを固定する
class Suggestion(BaseModel):
    keyword: str = Field(description="推薦するキーワード（記事に実際に出ている語）")
    reason: str = Field(description="今週重視する価値がある理由（1文）")


class Plan(BaseModel):
    focus_keywords: list[str] = Field(description="今回の採点で重視するキーワード（与えたもの＋推薦。3〜8個）")
    max_items: int = Field(ge=10, le=150, description="採点にかける件数の合計（通常は100）")
    suggestions: list[Suggestion] = Field(
        default_factory=list,
        description="与えたもの以外で、この期間の記事から推薦する語（最大3個、理由付き）",
    )


class EntityRow(BaseModel):
    i: int = Field(description="入力の [番号]")
    names: list[str] = Field(default_factory=list, description="固有名詞（最大8個）")
    keywords: list[str] = Field(default_factory=list, description="話題キーワード（最大5個、英語の小文字）")
    category: str = Field(default="", description="カテゴリ（一覧から1つ。なければ「その他」）")


class EntityBatch(BaseModel):
    items: list[EntityRow]


class Score(BaseModel):
    id: str
    relevance: int = Field(ge=0, le=5, description="テーマとの関連度")
    novelty: int = Field(ge=0, le=5, description="自分にとっての新規性")
    impact: int = Field(default=0, ge=0, le=5, description="世間の注目度・業界への影響の大きさ")
    reason: str = Field(description="総評（1文）")
    relevance_reason: str = Field(default="", description="relevance の点数の理由（1文）")
    novelty_reason: str = Field(default="", description="novelty の点数の理由（1文）")
    impact_reason: str = Field(default="", description="impact の点数の理由（1文）")
    title_ja: str = Field(default="", description="タイトルの日本語訳（元が日本語ならそのまま）")
    summary_ja: str = Field(default="", description="記事の要点の日本語要約（2〜3文）")


class Summary(BaseModel):
    text: str = Field(description="話題の要約（日本語、1段落）")
    sources_used: int = Field(default=0, ge=0, description="本文を読めた記事の数")


class ResearchState(TypedDict, total=False):
    # 入力
    mode: Literal["weekly", "monthly"]
    period: str  # "2026-W41" または "2026-10"
    theme: str
    sources: list[dict]
    # 第1段：計画と収集
    plan: dict  # LLM の提案
    approved_plan: dict  # 人の確定（承認①）— 提案とは別のキー
    candidates: Annotated[list[dict], operator.add]
    failed_sources: Annotated[list[str], operator.add]
    topics: list[dict]  # 話題ごとに1件（代表）。同じ話題の記事は related に
    pool: list[dict]
    stats: dict  # 集計（日別・媒体別・タグ別・話題別・固有名詞別）
    funnel: dict  # 取得元ごとの「読んだ件数 → 話題の数 → 採点に回した件数」
    # 第2段：採点と選定
    scored: Annotated[list[dict], operator.add]
    shortlist: list[str]
    shown: list[str]  # これまでに承認②で見せた話題（次点で埋めるときに除く）
    merge_hints: list[dict]  # 承認②で推薦する「同じ話題かもしれない」組み合わせ（{a, b, score, why}）。human_merge.py
    selected: list[str]
    feedback: dict  # 承認②の記録 = 評価データ（proposed / approved / rejected / added / merges）
    review_action: Literal["done", "next", "ask", "end"]
    summaries: dict  # 読むと決めた話題の要約（id → {text, sources_used, articles}）。summarize.py
    # 横断
    guardrail_flags: Annotated[list[str], operator.add]
    metrics: dict
    report_md: str
