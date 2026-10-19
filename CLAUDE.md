# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概要

Inoreader の「後で読む」に保存した記事を、週次・月次でまとめ、採点・要約してレポート（Markdown と Word）にする LangGraph エージェント。Python 3.11 以上、`src/` レイアウト（パッケージは `tsundoku`）。仕様は `docs/requirements.md`、`docs/functional-spec.md` にある。

## コマンド

```sh
pip install -e ".[dev]"            # または conda env create -f environment.yml
ruff check src tests               # lint（CI と同じ。line-length 120、E501 は無視）
python -m pytest                   # 全テスト。API を呼ばない（偽 LLM ＋ サンプルデータ）
python -m pytest tests/test_pipeline.py::test_human_merge   # 1件だけ
tsundoku demo                      # API キーなしで最後まで通す → ./tsundoku_data/report-weekly-2026-W40.md
tsundoku run weekly 2026-W42 --fake-llm --sample --yes   # 本番の経路を費用ゼロ・止まらずに
tsundoku graph                     # 配線を Mermaid で出す（README の図を更新するとき）
```

- テストは `demo` という module スコープの fixture が、パイプライン全体（承認①②・プロセスを落とした想定での再開を含む）を1回だけ動かし、各テストがその結果を確かめる。1件だけ実行しても、パイプライン全体は動く。
- 本番の `tsundoku run` では `OPENAI_API_KEY`、`INOREADER_APP_ID`、`INOREADER_APP_KEY` が必要（`.env`）。`config.yaml` がなければ同梱の `src/tsundoku/default_config.yaml` を使う。

## アーキテクチャ

### グラフ（`graph.py`）
`START →(Send で並列) fetch_source → cluster_topics → aggregate → plan → approve_plan ⏸ → merge →(Send で並列) score_one → shortlist → review ⏸ →(除外で目安を下回れば shortlist へ戻る／読むものが0件なら confirm_empty ⏸ → shortlist か summarize) summarize → report → END`

- ノードは `nodes/` に1ファイル1段階で置き、`ResearchState`（`models.py`、TypedDict）を読んで、変えたキーだけを dict で返す。
- `candidates`、`scored`、`failed_sources`、`guardrail_flags` には `Annotated[list, operator.add]` が付いている。並列ノードの結果は連結されるので、上書きのつもりで返さないこと。
- ノードの間で受け渡すのは Pydantic モデルではなく `model_dump()` した dict（チェックポイントに保存するため）。
- `plan`（LLM の提案）と `approved_plan`（人が確定したもの）は別のキー。承認②の結果は `feedback` に評価データとして残る。

### 人の承認と再開
- `approve_plan`（`nodes/planning.py`）、`review` と `confirm_empty`（`nodes/review.py`）は `interrupt()` で止まる。再開時は `Command(resume=...)` で渡した値が `interrupt()` の戻り値になる。承認②に渡す値の形は `{"approve": [...], "reject": {id: 理由}, "add": [...], "merge": [[id, id], ...]}`（例は `tests/test_pipeline.py` の fixture）。
- 再開するとノードは頭からやり直される。LLM 呼び出しなどの副作用の後に、同じノードで `interrupt()` を置かない（`review` で `apply_merges` が採点し直した後の「読むものが0件」の問いは、そのため別ノードの `confirm_empty` にある）。
- `cli.run_interactive` が止まるたびに `cli.ask()` で入力を受け取って再開させる。`cli.AUTO_APPROVE`（`--yes`）が真なら提案をそのまま承認する。
- thread_id は `"{mode}-{period}"`。チェックポイントは `checkpoint.py` が SQLite に保存し、止まるたびに `sync_checkpoint` で保存先へ複製する（Colab で Google ドライブを使うため。ドライブ上の SQLite を直接開かない）。同じ期間を指定すると続きから再開し、`--fresh` で消してやり直す。採点方法や設定を変えた後に古い結果を使い回さないよう注意。

### 設定とグローバル状態（`config.py`）
- `CONFIG`（設定の dict）と `settings`（`fake_llm`、`sample_data`、`workdir` など）はモジュールレベルのグローバル変数。import した時点で既定値が読み込まれる。CLI・Colab・テストの入口はどれも `setup()`。
- 偽 LLM とサンプルデータは `settings` の値で切り替わる。新たに LLM を呼ぶ処理は必ず `llm.structured_call(schema, system, user)` を通し、`llm._fake` にその schema 用の決定的な応答を足す（足さないと `ValueError`）。埋め込みも同じく、偽 LLM のときは `cluster._fake_embed` を使う。
- プロンプトは `prompts.py` にまとめる。記事の本文・タイトルは外部の文章なので、LLM に渡すときは `guardrails.wrap_untrusted`（データとして囲む）か `sanitize_abstract` を通す。同じ話題の他の記事のタイトルを並べるときは `wrap_related`（`<related>` で囲む）を使う。レポートは `check_report` で、出てくるものが実在するか照合する。

### 取得元（`sources/`）
- `@register("type名")` で取得関数を登録するレジストリ。`CONFIG["sources"]` の各要素の `type` で `get_fetcher` が選ぶ。関数の形は `fn(source, *, mode, period, limit) -> list[dict]`（Candidate の dict）。
- 記事 ID は arXiv の ID か、`normalize_url` で正規化した URL（重複除去のキー）。

### 話題のまとめ（`nodes/cluster.py`、`nodes/human_merge.py`）
埋め込みのコサイン類似度、話題キーワード、見出しの固有名詞を組み合わせた `topic_score` で同じ話題かを判定する。しきい値はそれぞれのファイル先頭の定数（自動でまとめる判定は `cluster.py` の `SAME_TOPIC`、`VETO_COS` など、承認②で推薦する判定は `human_merge.py` の `NEAR_TOPIC`、`NEAR_COS`）。まとめた記事は代表の記事の `related` に入る。`human_merge.py` は、自動ではまとめなかった近い組み合わせを承認②で推薦し、人がまとめた話題を採点し直す。

### 出力（`nodes/report.py`）
`settings.workdir` にレポート（.md/.docx）、記事1本1行の経路の記録 `*-trace.csv`（段階は `STAGES`）、記事 CSV、統計 JSON、実行ログを書き出す。古いチェックポイントのスコアでも動くように書かれている（`test_report_tolerates_old_scores`）。
