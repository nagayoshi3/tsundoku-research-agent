# リサーチ・エージェント v1 機能要件書

2026-10-14 · @Nagayoshi Yamashita

## 1. 文書の目的と読み方

本書は、要件定義書「リサーチ・エージェント v1 要件定義書」の第8章で挙げた17機能（F-01〜F-17）について、入力・処理・出力・例外・受け入れ条件を定める。実装の正は `src/tsundoku/` であり、各機能に対応するモジュール（`config.py` など）を併記する。

各機能は次の項目で記述する。

| 項目 | 意味 |
|---|---|
| 役割 | その機能が全体の中で果たすこと |
| 入力 | State のキー、Send の payload、設定、人の入力のいずれか |
| 処理 | 判断の基準と順序。コードを読まずに振る舞いが分かる粒度 |
| 出力 | State に返すキー、またはファイル・画面表示 |
| 例外 | うまくいかないときの振る舞い |
| 受け入れ条件 | その機能ができたと言える確かめ方（主に `tests/` の assert） |

要件の優先度は、「必須」（v1 の受け入れに要る）と「任意」（設定で切り替える）の2段階とする。

## 2. 機能一覧

グラフの12ノードは F-02〜F-12 と F-17 に対応し、F-01・F-13〜F-16・F-18 はノードの外側（設定、横断、実行の入口）にある。
| ID | 機能 | ノード・関数 | LLM | 優先度 | モジュール |
|---|---|---|---|---|---|
| F-01 | 設定とキー管理 | `CONFIG`、`load_secret()` | — | 必須 | `config.py` |
| F-02 | 計画の提案（キーワードの推薦） | `plan` | 使う | 必須 | `prompts.py`、`nodes/planning.py` |
| F-03 | 承認① | `approve_plan` | — | 必須 | `nodes/planning.py` |
| F-04 | 取得元の対応表と並列取得 | `route_sources`、`fetch_source`、`_REGISTRY` | — | 必須 | `sources/__init__.py`、`nodes/fetch.py` |
| F-05 | Inoreader 取得 | `fetch_inoreader` と認証・変換の関数群 | — | 必須 | `sources/inoreader.py` |
| F-06 | 同じ話題のまとめと分類 | `cluster_topics`、`rule_keywords`、`rule_category`、`llm_entities` | 固有名詞・キーワード・カテゴリの抽出と埋め込み | 必須 | `nodes/cluster.py` |
| F-07 | 集計 | `aggregate` | — | 必須 | `nodes/aggregate.py` |
| F-08 | 枠の配分 | `merge`、`allocate`、`keyword_hits` | — | 必須 | `nodes/fetch.py` |
| F-09 | 話題の採点 | `route_scoring`、`score_one` | 使う | 必須 | `nodes/scoring.py` |
| F-10 | 上位選定 | `shortlist` | — | 必須 | `nodes/scoring.py` |
| F-11 | 承認② | `review`、`confirm_empty`、`route_after_review`、`merge_hints`、`apply_merges`、`merge_finals` | まとめた話題の採点し直しだけ | 必須 | `nodes/review.py`、`nodes/human_merge.py` |
| F-12 | レポート | `report`、`stats_md` | — | 必須 | `nodes/report.py` |
| F-13 | ガードレール | `sanitize_abstract`、`wrap_untrusted`、`wrap_related`、`check_report` | — | 必須 | `guardrails.py` |
| F-14 | 状態保存と再開 | `build_graph`、SqliteSaver、interrupt | — | 必須 | `graph.py` |
| F-15 | 対話実行 | `run_interactive`、`ask`、`default_period` | — | 必須 | `cli.py` |
| F-16 | デモと動作確認 | `tsundoku demo`、`tests/test_pipeline.py` | 偽 LLM | 必須 | `__main__.py`、`tests/` |
| F-17 | 要約 | `summarize`、`fetch_body` | 使う | 必須 | `nodes/summarize.py` |
| F-18 | 実行コストの記録 | `count_tokens`、`metrics["tokens"]`、`metrics["cost_jpy"]` | — | 必須 | `llm.py`、`nodes/report.py` |
| （任意） | RSS 取得 | `fetch_rss` | — | 任意 | `sources/__init__.py` |

## 3. F-01 設定とキー管理

動かし方を決める切り替え（`settings`）と、テーマ・モデル・取得元の設定 `CONFIG`（`config.yaml`）、API キーの探索を行う。キーをコードや設定ファイルに書かない。

| 項目 | 内容 |
|---|---|
| 入力 | コマンドラインの引数（`--fake-llm`、`--sample`、`--workdir`、`--config`）、`config.yaml`（なければ同梱の `default_config.yaml`）、環境変数、`.env`、Colab シークレット、入力欄 |
| 処理 | `setup()`：(1) `config.yaml`（なければ同梱の既定値）を読んで `CONFIG` にする。(2) 保存先 `workdir` と、SQLite を置く `local_dir` を作り、`CONFIG["checkpoint"]`（ローカル）と `CONFIG["checkpoint_backup"]`（保存先の複製）を決める。(3) `fake_llm` が無効のとき `OPENAI_API_KEY` を必須で読む。(4) `LANGSMITH_API_KEY` があれば `LANGSMITH_TRACING=true` にする。(5) 現在の設定（LLM・データ・保存先・設定ファイル）を表示する |
| キーの探索順 | `load_secret(name, required)`：環境変数 → `.env` → Colab シークレット（未登録・未許可の例外は握りつぶす）→ `required=True` のときだけ非表示の入力欄。見つけた値は環境変数に入れる |
| 出力 | `CONFIG`、`settings`（workdir / local_dir）、環境変数 |
| 例外 | 必須キーがどこにもなければ入力欄で聞く。任意キーは None を返す |
| 受け入れ条件 | `tsundoku demo`（偽 LLM・サンプル）でキーを1つも要求されずに通る |

切り替えの定義（`settings`。コマンドラインの引数で決まる）。

| 項目 | 初期値 | 有効のとき | 無効のとき |
|---|---|---|---|
| `fake_llm`（`--fake-llm`） | False | `llm.py` の偽 LLM が決まった値を返す。キー不要 | OpenAI を呼ぶ。`OPENAI_API_KEY` が必要 |
| `sample_data`（`--sample`） | False | Inoreader API と同じ形のサンプル160本を使う。ネットワーク不要 | Inoreader API を読む。`INOREADER_APP_ID`・`INOREADER_APP_KEY` が必要 |
| `workdir` / `local_dir`（`--workdir` / `--local-dir`） | `./tsundoku_data` / 同じ | 保存先。Colab ではドライブのフォルダを workdir に、`/content` を local_dir にする | — |

取得元（`CONFIG["sources"]` の1要素）のキー。

| キー | 意味 | v1 の値 |
|---|---|---|
| `name` | 取得元の名前。レポートの件数表示とファイル名に使う | `inoreader-saved` |
| `type` | `@register` した取得関数の名前 | `inoreader_api` |
| `stream` | Inoreader で読む一覧 | `user/-/state/com.google/starred` |
| `date_field` | 期間で絞る日時の項目（`saved` / `crawl` / `published`） | `saved` |
| `limit` | 1回の実行で読む件数の上限 | 1000 |
| `quota` | 採点枠の配分比（取得元が複数のときに効く） | 100 |

カテゴリと重視キーワード（v1.1）。どちらも絞り込みには使わない。

| キー | 形 | 意味 | 初期値 |
|---|---|---|---|
| `categories` | `{カテゴリ名: [手がかり語, …]}` | 各話題に1つ付ける大きな分類。手がかり語は規則による分類と話題キーワードの抽出に使う（英語は単語境界で照合、複数形も拾う。日本語は部分一致）。カテゴリ名は LLM の選択肢にもなる | 下の9カテゴリ |
| `focus_keywords` | `[語, …]` | 重視したいキーワード。plan が最大3個足して提案し、承認①で確定。採点に回す順番（F-08）と採点の文脈（F-09）に使う | `[VLA, world model, agent benchmark]` |
| `theme` | 文字列 | 自分の関心領域の説明。採点の文脈にだけ使う | AI 全般の技術動向（9カテゴリを列挙） |

カテゴリの初期値（9つ）。どれにも当たらない話題は「その他」。

| カテゴリ | 入るもの | 手がかり語 |
|---|---|---|
| Physical AI | ロボット学習、VLA、ヒューマノイド、sim-to-real | robot, vla, manipulation, humanoid, embodied, sim-to-real, locomotion, world model, ロボット |
| AIエージェント | エージェント設計、ツール利用、MCP、マルチエージェント | agent, agentic, tool-use, tool use, planning, mcp, multi-agent, computer use, エージェント |
| RAG・知識活用 | RAG、GraphRAG、知識グラフ、検索、長文脈 | rag, retrieval, graphrag, knowledge graph, vector, search, long-context, 検索, ナレッジ |
| LLMモデル動向 | 新モデルの発表、スケーリング、推論モデル | gpt, claude, gemini, llama, qwen, deepseek, frontier, scaling, reasoning model, release, 新モデル |
| 学習・適応 | ファインチューニング、RL/RLHF/GRPO、蒸留、合成データ | fine-tuning, finetuning, rlhf, grpo, reinforcement learning, distillation, synthetic data, post-training, ファインチューニング, 強化学習 |
| 効率化・SLM | SLM、量子化、推論高速化、エッジ・オンデバイス | slm, small language model, quantization, inference, on-device, latency, 軽量 |
| マルチモーダル | VLM、画像・動画・音声の理解と生成 | vision, vlm, multimodal, video, speech, audio, image generation, マルチモーダル |
| 評価・安全性 | ベンチマーク、評価手法、ガードレール、規制 | benchmark, evaluation, eval, guardrail, safety, alignment, regulation, jailbreak, 評価, 安全 |
| 産業・ビジネス | 資金調達、提携、製品発表、導入事例（業界ニュースの受け皿） | funding, acquisition, acquires, partnership, launches, enterprise, revenue, earnings, 資金調達, 提携, 買収 |

## 4. F-02 計画の提案（plan）と F-03 承認①（approve_plan）`prompts.py`、`nodes/planning.py`

保存した記事はすでに自分が選んだものなので、plan は絞り込まない。plan は取得・まとめ・集計の**後**に動き、その期間の記事に実際によく出ている語の中から、重視キーワードに足す価値のある語を理由付きで推薦し、採点件数を提案する。approve_plan がそこで止まって人の修正を受ける。取得も採点もまだ始まっていないので、ここでの修正は費用なしで全体に効く。

### F-02 計画の提案

| 項目 | 内容 |
|---|---|
| 入力 | State の `mode`、`period`、`theme`、`stats`（集計：カテゴリ別の話題数、よく出た話題キーワード・固有名詞、2本以上ある話題）、`candidates`、`topics`。`CONFIG` の `categories`（カテゴリ名）と `focus_keywords` |
| 処理 | `PLAN_PROMPT` を System、「関心領域、対象、期間、カテゴリ、与えられた重視キーワード、この期間の集計」を Human として `structured_call(Plan, …)` を1回呼ぶ。推薦があればログに出す |
| プロンプトの制約 | 推薦（`suggestions`）は、与えたキーワード以外で、この期間の記事に実際に多く出ていて関心領域に照らして重視する価値がある語を最大3個、各語に理由1文。一般語（ai、model など）と与えた語の言い換えは推薦しない。固有名詞（製品名・モデル名）でもよい。`focus_keywords` は与えたものに推薦を足したもの（3〜8個）。件数は通常100。手順や取得元は提案しない |
| 出力 | `plan = {focus_keywords: [3〜8個], max_items: 10〜150, suggestions: [{keyword, reason}, …]}` |
| 例外 | LLM が範囲外の値を返すと Pydantic の検証エラー。LLM が与えたキーワードを落とした場合は approve_plan が足し戻す。偽 LLM は常に `["agent", "reasoning", "robot"]`、100 と、推薦 `robot` を返す |
| 受け入れ条件 | 起動後、取得・まとめ・集計を経て approve_plan の interrupt で止まり、提案に与えたキーワード（`VLA` など）と推薦（`suggestions`）が含まれている |

### F-03 承認①

| 項目 | 内容 |
|---|---|
| 入力 | State の `plan`、`CONFIG["focus_keywords"]`。再開値 `{"approved": True, "focus_keywords": [...], "max_items": N}`（変えない項目は省略可） |
| 処理 | (1) 与えた重視キーワードのうち提案にないものを先頭に足し戻す。(2) `CONFIG["ask_plan"]` が true なら `interrupt({"type": "approve_plan", "message": …, "proposal": plan})` で止まり、State をチェックポイントに保存する。false なら止まらず提案をそのまま使う（ログに残す）。(3) 再開値のうち `focus_keywords` と `max_items` だけを提案に重ねる。(4) `Plan(**approved)` で型と範囲を検証する |
| 出力 | `approved_plan`（提案 `plan` とは別のキー。上書きしない） |
| 例外 | 範囲外の件数（例 500）は検証エラーになり、入力し直しになる。空の `{}` は再開とみなされず同じ interrupt で止まり続けるため、変更がなくても `{"approved": True}` を渡す |
| 制約 | 再開時にノードは頭からやり直される。`interrupt()` より前に副作用（通知・API 書き込み）を置かない |
| 受け入れ条件 | `Command(resume={"approved": True, "max_items": 20})` で再開すると `approved_plan.max_items == 20` になり、`plan.max_items` は 100 のまま残る |

## 5. F-04 取得元の対応表と並列取得（fetch_source）と F-05 Inoreader 取得 `sources/__init__.py`、`sources/inoreader.py`、`nodes/fetch.py`

取得元は「種類名 → 取得関数」の対応表から引き、取得元の数だけ並列に起動する。各取得関数は外部の形式を `Candidate` にそろえて返す。

### F-04 対応表と並列取得

| 項目 | 内容 |
|---|---|
| 入力 | `route_sources`：State の `sources`、`mode`、`period`。`fetch_source`：Send の payload `{source, mode, period}`（State 全体ではない） |
| 処理 | (1) 取得元ごとに `Send("fetch_source", payload)` を作り並列起動する。(2) `get_fetcher(type)` で対応表から取得関数を引く。(3) 最大3回（1回＋再試行2回、待機 0.1秒×試行回数）呼ぶ。(4) 成功したら各候補に `feed = source.name` を付けて返す |
| 対応表の登録 | `@register("名前")` デコレータ。登録済み：`inoreader_api`、`rss`（デモでは `always_fail` も） |
| 取得関数の契約 | `fn(source: dict, *, mode, period, limit) -> list[dict]`。戻り値は `Candidate.model_dump()` のリスト。失敗は例外で知らせる |
| 出力 | `candidates`（連結）。失敗時は `failed_sources` に `"名前: エラー"`（連結） |
| 例外 | 未登録の `type` は `KeyError`（登録済みの名前を並べて表示）。3回失敗した取得元は記録して終わり、他の取得元はそのまま進む |
| 受け入れ条件 | `always_fail` を足したデモで `failed_sources` に `broken-feed` が記録され、レポートまで通る。対応表に関数を1つ足し、設定に1行足すだけでグラフを変えずに取得元が増える |

### F-05 Inoreader 取得

| 項目 | 内容 |
|---|---|
| 入力 | 取得元設定（`stream`、`date_field`、`limit`）、`mode`、`period`、App ID / App Key、保存済みトークン |
| 期間の解釈 | `period_range()`：weekly `2026-W40` → その週の月曜0時〜翌週月曜0時、monthly `2026-09` → 9/1 0時〜10/1 0時（12月の次は翌年1月）。日本時間 |
| 認証 | `inoreader_token()`：(1) 保存済みトークンがあと1分以上有効ならそのまま使う。(2) 期限切れは refresh token で更新。(3) トークンがなければ許可 URL（scope=read、state 付き）を表示し、許可後の URL または code の値を入力してもらう。URL なら state を照合する。(4) `oauth2/token` でトークンを得て `inoreader_token.json` に 0o600 で保存 |
| App ID / Key の探索 | 環境変数・`.env` → 保存先の `inoreader_credentials.json` → Colab シークレット → 入力欄 |
| ページ取得 | `inoreader_stream()`：`reader/api/0/stream/contents/{stream}` を `n=100` で呼び、`continuation` で続きを読む。続きがないか、1ページ全件が期間の始まりより古くなったら止める。連続呼び出しは0.5秒あける。各ページで今日の API 使用回数を表示する |
| 生 JSON の保存 | 取ったままの全件（期間で絞る前）を保存先の `inoreader-{name}-{mode}-{period}.json` に保存する |
| 期間での絞り込み | `saved_ts()` で `date_field` に応じた日時を取り（`saved` = `timestampUsec`、`crawl` = `crawlTimeMsec`、`published` = `published`）、`start <= ts < end` の記事だけを残す |
| Candidate への変換 | `inoreader_to_candidate()`：`canonical[0].href`（なければ `alternate`）→ `url`、arXiv ID があればそれ、なければ `normalize_url()` → `id`、`title`（HTML エンティティを戻す）、`summary.content`（タグ除去・空白詰め）→ `abstract`、`published` → JST 文字列、保存日時 → `saved_at`、`origin.title` → `outlet`、`categories` の `/label/` 以降 → `tags`、保存した順 → `rank`、`source = "rss"` |
| URL の正規化 | `normalize_url()`：https にそろえる、`www.` を外す、末尾 `/` を外す、`#` 以降を捨てる、`utm_`・`fbclid`・`gclid`・`mc_`・`ref` で始まるパラメータを捨てる |
| 出力 | 期間内の `Candidate` のリスト（保存した順）、生 JSON ファイル、トークンファイル |
| 例外 | トークン更新に失敗したらトークンファイルを消して初回の手順からやり直す。state 不一致はエラー。HTTP エラーは例外として F-04 の再試行へ |
| 初回確認 | `check_inoreader()`：一覧の種類（`tag/list`）と、先颂3本の `saved`・`crawl`・`published` を並べて表示し、自分の保存日時と合う列を `date_field` に設定する |
| サンプル | `sample_inoreader_items()`：API と同じ形の160本（2026-W40 の150本＋前週の10本）。2本目の要旨に指示文を仕込んである |
| 受け入れ条件 | サンプルで生 JSON が160件、期間内が150件。`normalize_url("http://www.a.example.com/x/?utm_source=rss#top") == "https://a.example.com/x"`。`period_range("weekly", "2026-W40")` が 9/28 0時〜10/5 0時 JST |

## 6. F-06 同じ話題のまとめと分類（cluster_topics）`nodes/cluster.py`

保存した記事をテーマで絞らないので、「数百本を読める形に整理する」この機能が v1 の主役である。同じ記事の URL 違いと、別の媒体が書いた同じ話題の記事を1件（代表）にまとめ、各話題にカテゴリを1つ付ける。まとめた記事は捨てずに代表の `related` に残す。判定の手がかりは埋め込み（意味の近さ）、話題キーワード（何の話か）、固有名詞（どの会社・製品の話か）の3つ。

| 項目 | 内容 |
|---|---|
| 入力 | State の `candidates`。`CONFIG` の `embed_model`・`entity_llm`・`categories`・`focus_keywords` |
| 処理 | (1) 同じ ID をまとめる：代表になりやすい順（arXiv ID を持つ記事が先）に並べ、同じ `id` の2件目以降を代表の `related` に移す（理由「同じ ID」）。(2) 固有名詞・話題キーワード・カテゴリを取る：まず規則で。`entity_llm` が True で本物の LLM なら、1回の呼び出しで3つをまとめて返させ、固有名詞とキーワードは和集合、カテゴリは LLM を優先。LLM がカテゴリを付けなければ規則で。(3) 埋め込み：見出し＋冒頭300字を埋め込み、長さ1にそろえ、全組の cos を計算する。(4) 代表に寄せる：優先順に1件ずつ、今ある代表とだけ比べ、スコアがしきい値以上の代表があればいちばん近い代表の `related` に移し、キーワードは代表に集める。なければ新しい代表になる |
| 同じ話題の判定（`topic_score`） | (a) 両方の見出しに固有名詞があり1つも重ならず、cos が `VETO_COS = 0.95` 未満 → 別の話題。(b') 見出しの固有名詞が2つ以上一致（例: Meta と Muse Wonder）→ 同じ話題（日英など言語が違って埋め込みが低くても）。(c) それ以外 → 意味の近さ = cos ＋ 0.3 × キーワードの一致度 × (1 − cos)（両方に話題キーワードがあるとき。一致度は Jaccard、共通が1語なら半分。一致は cos を押し上げるだけで、不一致では下げない）。スコア = 0.7 × 意味の近さ ＋ 0.3 × 固有名詞の一致度。`SAME_TOPIC = 0.62` 以上なら同じ話題 |
| 固有名詞の一致度 | Jaccard 係数（共通の数 ÷ どちらかにある数）。共通が1つだけなら 0.5 を掛ける（会社名だけが同じ記事をまとめにくくする） |
| 固有名詞の規則 | 数字を含む語（GPT-5、RoboMind-2）、大文字が2つ以上の語（OpenAI、NVIDIA）、本文の文中で大文字始まりの語（文頭は除く）、3文字以上のカタカナ。一般語の辞書 `GENERIC_WORDS` と表記ゆれの寄せ先辞書を持つ。小文字のトークンにそろえる |
| 話題キーワードの規則（`rule_keywords`） | `focus_keywords` と `categories` の手がかり語のうち、見出し＋冒頭600字に出てくるもの（英語は単語境界で照合し複数形も拾う。日本語は部分一致）。費用なしで常に動く |
| カテゴリの規則（`rule_category`） | 話題キーワードと手がかり語の重なりが最も多いカテゴリ。どれにも当たらなければ「その他」 |
| LLM（`llm_entities`） | `ENTITY_BATCH = 25` 件ずつ `<paper>` で包んで送り、`EntityBatch` で固有名詞（最大8）・話題キーワード（最大5、英語小文字）・カテゴリ（一覧から1つ、なければ「その他」）を受ける。一覧にないカテゴリ名は「その他」扱い。返った番号が範囲外なら捨てる。偽 LLM のときは呼ばない（規則だけ） |
| 埋め込み | 本番：`init_embeddings(embed_model)`。偽 LLM：3文字の並びの頻度（サンプル共通の目印は除く） |
| `related` の1本分 | title、url、source、outlet、saved_at、entities、keywords、why（「同じ ID」またはスコアと内訳） |
| 出力 | `topics`（代表だけ。各代表に `entities`・`keywords`・`category`・`related` が付く）。件数の変化を表示 |
| 例外 | 埋め込み API の一時エラーは `RetryPolicy(max_attempts=3)` でノード単位に再試行 |
| 調整 | `explain_pair(a, b)` で2件の cos・キーワード・固有名詞・スコアの内訳を見る。別のニュースがまとまる → `SAME_TOPIC` を上げるか一般語を辞書に足す。同じ話題がまとまらない → 下げるか表記ゆれを辞書に足す。「その他」が多い → `categories` の手がかり語を足す。日英がまとまらない → `entity_llm` を True |
| 受け入れ条件 | サンプル150本が147話題になる（URL の揺れ1、同じ話題2）。RoboMind-2 の記事に roboweekly・labnotes・technews の3ドメインがまとまり、カテゴリが「Physical AI」、キーワードに `robot` を含む。言い回しがほぼ同じ VeloBench-3 は別の話題に残る。全話題にカテゴリが付く |

デモのサンプルでの判定例（偽の埋め込みの値。本番では値の出方が違う）。

| 組 | cos | キーワード | 固有名詞 | スコア | 結果 |
|---|---|---|---|---|---|
| #120 と #121（同じ話題、言い回しが違う） | 0.67 | 0.40（agent, robot） | 0.67 | 0.70 | 同じ話題 |
| #120 と #126（同じ話題、言い回しが大きく違う） | 0.56 | 0.12（robot） | 1.00 | 0.70 | 同じ話題（埋め込みだけなら見逃す） |
| #120 と #128（言い回しが同じ、会社と製品が違う） | 0.76 | — | 見出しが食い違う | 0 | 別の話題（埋め込みだけならまとめてしまう） |
| 同じ会社の別のニュース | 0.62 | — | 0.50（会社名だけ） | 0.58 | 別の話題 |
| 別の会社の「launches」ニュースどうし | 高め | 共通1語なので半分 | 見出しが食い違う | 0 | 別の話題（キーワード1語の一致だけではまとめない） |

## 7. F-07 集計（aggregate）と F-08 枠の配分（merge）`nodes/aggregate.py`、`nodes/fetch.py`

aggregate は cluster_topics の直後に動き、期間内に保存した記事を5つの切り口で数えてファイルに出す。merge は話題をキーワードの一致数で並べ、取得元ごとの枠で採点対象を切る。どちらも LLM を使わない。

### F-07 集計

| 項目 | 内容 |
|---|---|
| 入力 | State の `candidates`（期間内の全記事）、`topics`、`mode`、`period` |
| 記事単位の切り口 | 日別（weekly は `10-01` の日、monthly は `W40` の週）、媒体別（多い順10件）、タグ・フォルダ別（1本に複数のタグがあればそれぞれ数える。多い順10件） |
| 話題単位の切り口 | 話題別（同じ話題の記事数の多い順、同数なら新しく保存した順。10件。タイトル・URL・本数・媒体・カテゴリ）、カテゴリ別（話題の数と記事の数。記事の多い順）、話題キーワード別（その語が付いた話題の数。10件）、固有名詞別（その語が出てくる記事の数。同じ ID の重複は1本と数える。10件） |
| 出力 | `stats = {saved, topics, by_time, by_outlet, by_tag, top_topics, by_category, by_keyword, top_entities}`。ファイル `{mode}-{period}-articles.csv`（記事1本1行。列：topic、representative、category、keywords、title、url、outlet、saved_at、tags、why。UTF-8 BOM 付き）と `{mode}-{period}-stats.json` |
| 受け入れ条件 | サンプルで saved 150・topics 147、7日分の日別の合計が150、最多の話題が4本3媒体、タグ別が `{business: 47, robotics: 5, agents: 2}`、カテゴリ別の話題数の合計が147、CSV が存在する |

### F-08 枠の配分

| 項目 | 内容 |
|---|---|
| 入力 | State の `topics`、`candidates`、`sources`、`approved_plan`（`focus_keywords`、`max_items`） |
| 処理 | (1) `feed` ごとに分け `rank` 順に並べる。(2) 取得元ごとに①重視キーワードの一致数（タイトル＋要旨＋抽出した話題キーワード、大文字小文字を区別しない）が多い順 ②`related` が多い順 ③新しい順に並べ直す。(3) `allocate()` で `max_items` を `quota` の比率で配分（切り捨て）し、候補が枠に足りない取得元の余りは他の取得元の次の順位から1件ずつ埋める。(4) 取得元ごとの「読んだ件数 → 話題の数 → 採点件数」を `funnel` に記録する。(5) 各候補の要旨を `sanitize_abstract()` で無害化し、指示文があれば `input:suspicious_text:<id>` を記録する |
| 考え方 | キーワードは並べ順であって絞り込みではない。一致が0の話題も後ろに並ぶだけで捨てない。話題数が `max_items` 以下なら全話題が採点に回る |
| 出力 | `pool`（採点対象）、`funnel`、`guardrail_flags`（連結） |
| v1 での振る舞い | 取得元が Inoreader の1つだけなので、並べた上から `max_items` 件。保存がそれより少なければ全部が採点に回る |
| 受け入れ条件 | サンプルで `funnel["inoreader-saved"] == {read: 150, topics: 147, scored: 20}`。採点対象の先頨2件が RoboMind-2 と VeloBench-3。`allocate({a: 5件, b: 50件}, {a: 70, b: 30}, 20)` が a 5件・b 15件 |

## 8. F-09 採点（score_one）と F-10 上位選定（shortlist）`nodes/scoring.py`

採点の単位は記事ではなく話題。話題1件につき LLM を1回呼び、話題の数だけ並列に起動する。費用が一番かかるステップ。上位選定は LLM を使わない純粋関数。

### F-09 採点

| 項目 | 内容 |
|---|---|
| 入力 | `route_scoring`：State の `pool`、`scored`、`theme`、`approved_plan`。`score_one`：Send の payload `{paper, theme, plan}` |
| 処理 | (1) 採点済みの id を除いた話題ごとに `Send("score_one", …)` を作る。0件なら shortlist へ直行する。(2) `SCORE_PROMPT` に関心領域（theme）と重視キーワードを差し込み System にする。(3) 代表を `wrap_untrusted()` で `<paper id=…>` に包み、カテゴリと、同じ話題を書いた他の記事のタイトル（URL 違いを除く、最大10本。各120字まで）を `wrap_related()` で `<related>` に包んで添え、Human にする。(4) `structured_call(Score, …)` を1回呼ぶ。(5) LLM が返した id を信用せず、入力の id で上書きする |
| 採点の軸 | relevance（まず関心領域への近さで 0〜4、重視キーワードのどれかが主題なら +1 で上限5。関心領域が守備範囲、重視キーワードはその中の焦点。保存した記事はすべて読者が選んだものなので 0 は例外的）、novelty（0〜5：読み手にとっての新しさ）、impact（0〜5：世間の注目度・業界への影響の大きさ。主要企業の大きな発表は5、個人の感想は0）。点数ごとに理由を1文ずつ返す（relevance_reason、novelty_reason、impact_reason）＋ 総評 reason（1文）。5・3・0 の目安と採点例4件をプロンプトに固定。多くの媒体が書いた話題は注目度の根拠として扱う。表示は total_score ＝ relevance ＋ novelty ＋ impact ＋ 媒体数の加点（buzz）を「合計N ＝ 関連度a ＋ 新規性b ＋ 注目度c ＋ 媒体+d」の形で出す（score_line）、基準ごとの「点数/5: 理由」を1行ずつ出す（score_detail。古い採点で基準別の理由がなければ総評で代用） |
| 出力 | `scored`（連結。1件ずつのリスト） |
| 例外 | LLM の一時エラーは `RetryPolicy(max_attempts=3)` でノード単位に再試行。それでも失敗すると実行は止まるが、成功した採点はチェックポイントに残り、再開で失敗分だけが実行される。範囲外の点数は検証エラー |
| 費用 | 話題1件約600〜800トークン（他の記事のタイトル分が加わる）。偽 LLM は入力の SHA-256 から決定的に点数を作る |
| 受け入れ条件 | `pool` の全件に `scored` があり、各 id が `pool` の id と一致する。偽 LLM では同じ入力から常に同じ点数 |

### F-10 上位選定

| 項目 | 内容 |
|---|---|
| 入力 | State の `scored`、`pool`、`s``elected`（確定済み）、`shown`（提示済み id）。`CONFIG` の `shortlist_size`（読む件数の目安。初期20。0 なら全件）、`shortlist_``min_score`（合計点のしきい値。初期 None ＝ 自動）、`backfill`（初期 True） |
| 処理 | (1) `pool` にある id の採点だけを、relevance ＋ novelty ＋ impact ＋ 媒体数の加点（同じ話題を書いた媒体が2つで1点、4つ以上で3点）の合計（total_score）が高い順、同点は新しく保存した順に並べる。(2) 初回（`shown` が空）は件数で切らず、合計点がしきい値以上の話題を全部提示する。しきい値は `shortlist_min_score`（数値なら固定）、None なら自動で「上から `shortlist_size` 番目の合計点」（同点まで含むので件数は目安の前後になる。採点数が目安未満なら全件）。(3) 2回目以降（除外で戻ってきたとき）は、目安 − 確定済み（`selected`）の件数だけ、まだ提示していない話題から上位を出す。(4) 提示はカテゴリ別（提示する話題の多い順）→ 合計点順に並べ直す（選ぶ集合は変えない） |
| 考え方 | 保存済みの記事はすべて本人が選んだものなので、採点は「読む順」であって絞り込みではない。読む本数を最初から固定しない。合計点が一定以上の話題は全部出し、その件数が20件前後になるようしきい値を自動で決める（固定したければ `shortlist_min_score`）。承認②で除外して目安を下回った分だけ次点で埋める（backfill） |
| 出力 | `shortlist`（id のリスト） |
| まとめ候補の推薦 | 提示する話題どうしを `merge_hints()`（`nodes/human_merge.py`）で比べ直し、`merge_hints` に入れる。判定は cluster_topics と同じ `topic_score` で、しきい値を下げる：総合スコアが 0.45 以上 SAME_TOPIC（0.62）未満の組、または見出しの固有名詞が食い違って自動では弾いたが cos が 0.80 以上の組。同じ主語（▸ muse）として並べて見せる組は除く。スコアの高い順に最大5組 |
| 例外 | 0件なら空リスト（review は承認②を飛ばす） |
| 受け入れ条件 | 同じ `scored`・`selected`・`shown` からは必ず同じ `shortlist`。デモ（目安10件）では上位10番目の合計点以上の話題が全部提示され（同点を含むの〇11件）、その点未満は1件も提示されない。初期値（20）のサンプルでは22件。7件除外して戻ると、未提示から5件だけ提示される。0 では採点した全話題がカテゴリの見出し付きで提示される |

## 9. F-11 承認②（review）`nodes/review.py`

処理を止め、合計点がしきい値以上の話題（20件前後）をカテゴリ・基準ごとの点数と理由・同じ話題の本数付きで人に見せる。保存済みの記事はすでに本人が選んだものなので、人は**読まないものだけを除外**し（理由は任意）、残りはすべて「読む」として確定する。除外して確定が目安 `shortlist_size`（20件）を下回ったときは、不足分だけ次点の話題を同じ形で提示して埋め、目安に達するか次点が尽きるまで繰り返す。候補外からの追加もできる。この記録が採点の質を測るデータになる。

| 項目 | 内容 |
|---|---|
| 入力 | State の `shortlist`、`pool`、`scored`、前回までの `feedback`・`selected`・`shown`。再開値（下表） |
| 提示内容 | 1本につき id、タイトル（日本語訳と原題）、点数の内訳「合計N ＝ 関連度a ＋ 新規性b ＋ 注目度c ＋ 媒体+d」、基準ごとの「点数/5: 理由」（detail）、総評（reason）、要旨（summary_ja）、要旨の冒頭160字、同じ話題の記事数（related）、カテゴリ（category）、媒体（なければ取得元名）。提示はカテゴリ別（提示する話題の多い順）→ 合計点順。同じ主語（見出しの固有名詞。例: muse）を持つ話題は1つの項目（members）にまとめ、取捨選択の単位にする |
| 処理 | (1) `interrupt({"type": "review", "message": …, "shortlist": items, "merge_hints": [...]})` で止まる（推薦にはタイトルを添える）。(1b) 再開値の `merge`（id のリストのリスト）を `apply_merges()` で先に反映する：まとめられるのは今回の shortlist にある話題だけで、それ以外の id（前の回に確定した話題など）はログに出して無視する（有効な id が2つ未満の組は捨てる）。各組で合計点が最も高い話題（同点なら提示順で先）を残し、他の話題は代表ごと `related` の先頭に入れる（`why` = 「人がまとめた（承認②）」）。固有名詞・キーワードを合わせる。全部の組をまとめ終えてから、残った話題ごとに1回だけ `score_candidate()` で1件として採点し直す（連鎖して途中で吸収された話題は採点しない。`scored` には追記され、id で引くときは後のものが勝つ）。pool・topics・shortlist・shown・selected・feedback の id を残した話題に読み替え、古い要約を消す（summarize が横断して書き直す）。`feedback["merges"]` の各記録は `into`（記録した時点の行き先）と `final`（今残っている id）を持ち、`final` は `merge_finals()` が全記録の履歴（merged → into）をたどって求め直す（`final` のない古い記録や、後でさらに吸収された `final` も今残っている id に直る）。以降の approve の id は残した話題に読み替える。reject は読み替えない（下記）。(2) 再開値を検証：除外（reject）は shortlist にある id だけ（理由は任意。空なら「理由なし」）。同じ回答でまとめて吸収された側の id の除外は無視してログに出す（まとめた話題は残る）。話題全体を除外するときは残した側の id を指定する。approve_all が True なら提示から除外分を引いたものが承認、False なら approve の id 指定（デモ・テスト用）。追加は採点対象（pool）にある話題だけで、代表の id・URL か、同じ話題にまとめた記事の URL（正規化して照合）で指定できる。それ以外は捨てる。(3) `feedback = {proposed, approved, rejected, added, merges}` を前回分と合算して記録し、`selected` に承認＋追加を足し、`shown` に今回提示した id を足す。(4) 次点補充（backfill）：`CONFIG["backfill"]` が True、`shortlist_size` > 0、確定が目標に届かず、今回除外があり、未提示の話題が残っていれば `review_action = "next"`（shortlist に戻り、不足分だけ提示する）。(5) 確定が0件で追加もなければ、ここでは止まらず `review_action = "ask"` にする。続く `confirm_empty` が `interrupt({"type": "question", "options": ["next", "end"]})` で止まり「次の候補を出すか」を聞く。(6) それ以外は `review_action = "done"` |
| 全却下の分岐 | 確定0件のとき（`confirm_empty`）：`next` で未提示が残っていれば `review_action = "next"`（shortlist に戻り、未提示から改めて提示）。`end`、または未提示が残っていなければ `review_action = "done"`（`selected` は空のまま summarize へ）。除外があって未提示が残る通常の場合は聞かずに `next` |
| 出力 | review：`feedback`、`selected`、`shown`、`review_action`（`next` / `ask` / `done`。提示が0件なら `end`）。confirm_empty：`review_action`（`next` / `done`）だけ。人がまとめたときは `pool`、`topics`、`scored`（追記）、`shortlist`、`summaries` も |
| 行き先 | `route_after_review`（review と confirm_empty の後で共通）：`next` なら shortlist、`ask` なら confirm_empty、それ以外は summarize |
| 制約 | review の interrupt は1つだけにする。再開時にノードは頭からやり直されるため、`apply_merges()` の採点し直し（LLM）の後に2つ目の interrupt を置くと採点が二重に走る。0件のときの問いは別ノード `confirm_empty` に置く。`scored` は追記型なので吸収した話題の古い採点は残る（pool にないので shortlist・report では使われない）。採点し直しは全組の反映後に残った話題ごとに1回なので、連鎖したまとめ（a+b の後に a+c）でも LLM は1回 |
| 受け入れ条件 | 承認3本・却下1本（理由「既読」）・追加1本・まとめ1組で再開すると `selected == approve + add` になり、吸収した話題は pool・提示・要約から消えて残した話題の `related` 先頭に入り、残した話題は採点し直され、`feedback["merges"]` とレポートの「選定の記録」に残る。連鎖したまとめ（[a,b] の後に [a,c]）では採点し直しが残った c の1回だけで、中身は a・b を含む。今回の shortlist にない id を含む組はその id を除いて扱う（前の回に確定した話題は吸収されず selected に二重に入らない）。a と b をまとめて b を除外しても a は確定に残り、a を除外すれば話題全体が除外される。`final` のない古い記録（a←b の後に c←a）も、`merge_finals` で両方 c になり、レポートでは c に2本まとめたと数える。サンプル（目安20）で22件提示→1件除外→確定21件（目安以上なので次点なし）。22件提示→7件除外→次点5件提示→除外なしで確定20件（偽 LLM の対話シミュレーションで確認） |

再開値の形。

| 場面 | 再開値 |
|---|---|
| 承認②（入力欄から） | `{"approve_all": True, "reject": {id: "理由", …}, "add": [id, …]}` |
| 承認②（番号指定。デモ・テスト用） | `{"approve": [id, …], "reject": {id: "理由", …}, "add": [id, …], "merge": [[id, id, …], …]}` |
| 全除外の問い | `{"action": "next"}` または `{"action": "end"}`（それ以外は `end` 扱い） |

## 9b. F-17 要約（summarize）`nodes/summarize.py`

承認②で「読む」と確定した話題を先に、続いて（`summary_scope` が all のとき）読むものに入らなかった採点対象の話題も、記事の本文を取りに行って LLM に要約させる。費用のかかる処理（本文の取得・長い入力）は承認②の後ろにしか置かない。

| 項目 | 内容 |
|---|---|
| 入力 | State の `selected`、`pool`、`theme`、`summaries`（要約済み）。`CONFIG` の `summary_chars`、`summary_chars_multi`、`summary_max_articles`、`summary_fetch_body`、`summary_scope` |
| 本文の取得 | `fetch_body(url)`：trafilatura で本文だけを抜き出す（最大6,000字）。取れなければブラウザ扱いの User-Agent で requests からもう一度。それでも取れなければ空を返し、RSS の要旨で代用する。サンプルのときと `summary_fetch_body` が false のときは取りに行かない |
| 処理 | 話題ごとに、代表＋同じ話題の記事（URL 違いの重複は除く。最大 `summary_max_articles` 本）の本文を `<article source=… body=full/abstract-only>` に包み、`SUMMARY_PROMPT` で1回の LLM 呼び出しに横断要約させる。字数は1本なら `summary_chars`、複数本なら `summary_chars_multi` まで。意味のまとまりごとに2〜3段落。本文があるのに字数の8割未満なら1回だけ書き直させる。要約済みの話題は飛ばす（再開に強い）。6並列 |
| 出力 | `summaries`（id → `{text, sources_used, articles}`） |
| 例外 | 本文の取得失敗（有料記事、Google ニュースの転送 URL など）は要約を止めず、要旨で書いて「本文を読めた N本」として記録する。LLM の一時エラーは `RetryPolicy` で再試行 |
| 費用・時間 | 読むもの20話題で 5〜10円・1〜2分。付録まで（100話題）で 20〜40円・5分前後 |
| 受け入れ条件 | 確定した全話題に要約が付き、レポートの「今回読むもの」に本文の要約と「（要約の元: N本、本文を読めた M本）」が出る。`summary_scope` が all なら採点対象の全話題に要約が付き、付録に出る |

## 10. F-12 レポート（report）`nodes/report.py`

集計と確定した記事を Markdown 1ページにして保存する。LLM は使わず、決まった形に流し込む。出典は取得時の URL をそのまま書く。

| 項目 | 内容 |
|---|---|
| 入力 | State の `pool`、`scored`、`feedback`、`selected`、`stats`、`funnel`、`failed_sources`、`guardrail_flags`、`approved_plan`、`candidates`、`topics` |
| 処理 | (1) `check_report(selected, pool)` で出典の実在を照合。(2) 指標を計算。(3) 見出し、テーマとキーワード、集計、今回読むもの、全話題、付録、除外したものと理由、選定の記録の順に組み立てる。Markdown のほか、`md_to_docx()` で同じ内容の Word（見出し・箇条書き・表・太字・リンク）も書く。(4) 保存先の `report-{mode}-{period}.md` と `.docx` に保存 |
| 構成 | `# リサーチレポート {period}` → テーマ・キーワード → `## 集計`（保存数→話題数、日別の表、よく保存した話題（カテゴリ付き）、カテゴリ別・話題キーワード別・媒体・タグ・固有名詞）→ `## 今回読むもの`（`### N. [カテゴリ] 日本語タイトル`、原題、本文の要約（段落、要約の元の本数）、媒体・保存日時、点数の内訳「合計N ＝ 関連度a ＋ 新規性b ＋ 注目度c ＋ 媒体+d」、基準ごとの「点数/5: 理由」と総評、出典、同じ話題: N本とその出典最大5本、追加分に「（追加）」）→ `## 全話題（カテゴリ別・採点順）`（採点に回した全話題を1項目ずつ。✔ 確定・✖ 却下の印、点数の内訳、タイトル、同じ話題の本数、その下に要点・関連度・新規性・注目度・媒体の各行。承認しなかった話題も見える）→ `## 付録：読むものに入らなかった話題の要約`（`summary_scope` が all のとき。カテゴリ別・採点順に、タイトル・合計点の内訳・要約）→ `## 除外したものと理由` → `## 選定の記録`（件数の流れ、指標、取得元ごとの件数、承認②で人がまとめた話題、取れなかった取得元、ガードレールの検出） |
| 出力 | `report_md`、`metrics`、`guardrail_flags`（出力側の検出を連結）、レポートファイル |
| 例外 | 確定が0本なら「（今回はなし）」。`pool` にない id は書かず、検出として記録 |
| 受け入れ条件 | `report_md` に `## 集計``、同じ話題: 3本、[Physical AI]` を含む。`metrics.adoption_rate == 0.3` |

経路の記録（`write_trace()`）。記事1本1行の `{mode}-{period}-trace.csv` をレポートと同時に書き、どの記事がどこまで進んだかを1ファイルで追える。期間外で落ちた記事は生 JSON にだけ残る。

| 列 | 内容 |
|---|---|
| `stage` | 到達した段階。まとめ（代表にまとめられた）／代表（採点の枠から外れた）／採点対象／提示／確定 |
| `topic`、`representative` | 話題番号（話題別の順位）、代表なら1 |
| `category`、`keywords` | カテゴリ、話題キーワード |
| `title`、`url`、`outlet`、`saved_at` | 記事 |
| `why` | まとめた理由（同じ ID、またはスコアと内訳） |
| `keyword_hits`、`pool_rank` | 重視キーワードの一致数、採点対象の順位（空なら枠から外れた） |
| relevance、novelty、impact、outlets、total_score、relevance_reason、novelty_reason、impact_reason、reason | 採点 |
| `shortlisted`、`decision` | 承認②に出たか、結果（approved／rejected: 理由／added） |
| `guardrail` | 指示文の検出 |

実行ログ（`log()`）。画面の各行に時刻と経過秒を付け、`run_interactive()` が `log-{mode}-{period}.txt` にも同じ内容を残す。ノードごとの開始・終了と所要時間（`▶ cluster_topics` ／ `✓ cluster_topics（1.2s）`）、取得件数と期間外の件数、まとめた話題の内訳（先頭10件）とカテゴリの配分、枠から外れた話題数、ガードの検出、提示のカテゴリ配分、承認②の結果と追加できなかった指定、件数の流れを出す。並列の採点は件数だけ。

指標の定義。定義は途中で変えない。

| キー | 計算 | 読み方 |
|---|---|---|
| `fetched` | 期間内に保存した記事の数 | — |
| `topics` | 同じ ID・同じ話題をまとめた後の件数 | fetched との差が重複量。急に増減したらしきい値を見直す |
| `pool` | 採点に回した件数 | — |
| `adoption_rate` | 承認数 ÷ 提示数（小数点2桁） | 下がったら採点基準と例、承認①のキーワードを見直す |
| `added` | 候補外から追加した数 | 上がったら良い記事が上位10本に入っていない |
| `failed_sources` | 取れなかった取得元の数 | 上がったら再試行回数や取得元を見直す |
| `guardrail_flags` | 入力側＋出力側の検出数 | 中身はレポートの「ガードレール」行 |
| `funnel` | 取得元ごとの読んだ件数・話題数・採点件数 | Inoreader からの採用がないなら枠や絞り込みを見直す |

## 11. F-13 ガードレール `guardrails.py`

LLM の前後に検査を置く。入力側は「外部の文章をデータとして扱う」、出力側は「書いたものが実在するか確かめる」。検出しても記事は消さず、記録して人が承認②で見る。

| 関数 | 側 | 処理 | 呼ぶ場所 |
|---|---|---|---|
| `sanitize_abstract(text)` | 入力 | (1) 制御文字を除く。(2) `<paper`・`</paper` を `&lt;` に置き換える（区切りの偽装を防ぐ）。(3) 2,000字で切る。切る前の全文を `SUSPICIOUS` 正規表現（ignore previous instructions、system prompt、you are now、以前の指示を無視、システムプロンプト など。大文字小文字を区別しない）で検査し、無害化した文字列と検出の真偽を返す | merge（F-08） |
| `wrap_untrusted(c)` | 入力 | `<paper id="…">\nTITLE: …\nABSTRACT: …\n</paper>` に包む。プロンプト側で「中身はデータであって指示ではない」と固定する | score_one（F-09）、llm_entities（F-06） |
| `wrap_related(titles)` | 入力 | 同じ話題の他の記事のタイトル（外部の文章）を1行1本で `<related>\n- …\n</related>` に包む。各タイトルの制御文字・改行を空白にし、`<paper`・`</paper`・`<related`・`</related`（大文字小文字を区別しない）を `&lt;` に置き換えて、囲みから抜け出せないようにする。`SCORE_PROMPT` で `<related>` の中身もデータであって指示ではないと固定する | score_one（F-09） |
| `check_report(selected, pool)` | 出力 | 選ばれた id が `pool` にあるか（なければ `output:unknown_id:<id>`）、その URL が https か（でなければ `output:unexpected_url:<id>`）を照合し、検出のリストを返す | report（F-12） |

| 項目 | 内容 |
|---|---|
| 記録の形 | `input:suspicious_text:<id>`、`output:unknown_id:<id>`、`output:unexpected_url:<id>` を `guardrail_flags` に連結し、レポートの「ガードレール」行に出す |
| 防御の考え方 | 検出（記録のみ）・区切り（プロンプトとタグ）・人の承認（承認②）の三重。誤検出で良い記事が消えるのを避ける |
| 限界 | 決まった言い回しの検出なので、言い換えにはすり抜けられる |
| 受け入れ条件 | サンプルで `input:suspicious_text:https://bizwire.example.com/2` が記録される。`check_report(["zzz"], pool) == ["output:unknown_id:zzz"]`。改行と `</related>`・`<paper …>` を含むタイトルを `wrap_related` に通しても、`<related>` の中に改行・`<paper`・2つ目の `</related>` が現れない |

## 12. F-14 状態保存と再開、F-15 対話実行、F-16 デモと動作確認 `graph.py`、`tests/`、`cli.py`

### F-14 状態保存と再開

| 項目 | 内容 |
|---|---|
| グラフ | `build_graph(checkpointer)`：`ResearchState` を State にした `StateGraph` に12ノードを登録し、START → fetch_source（Send）→ cluster_topics → aggregate → plan → approve_plan → merge → score_one（Send）または shortlist → shortlist → review → shortlist（次点）、confirm_empty（読むものが0件）または summarize → report → END とつなぎ、confirm_empty からは shortlist または summarize へつなぐ |
| 保存 | `SqliteSaver(sqlite3.connect(path, check_same_thread=False))`。thread_id は `{mode}-{period}`（例 `weekly-2026-W41`）。本番は `CONFIG["checkpoint"]`、デモは `checkpoints.demo.sqlite`（毎回作り直す） |
| 停止 | `interrupt(値)` で止まり、`app.get_state(cfg).interrupts[0].value` から提示内容を取り出せる。`get_state(cfg).next` に止まっているノード名が入る |
| 再開 | `app.invoke(Command(resume=値), cfg)`。ノードは頭からやり直され、`interrupt()` が渡した値を返す。新しい接続・新しいグラフからでも同じ thread_id で再開できる |
| 再試行 | `cluster_topics` と `score_one` に `RetryPolicy(max_attempts=3)` |
| 制約 | State は JSON 化できる値（dict / list / str / int）だけ。空の `{}` は再開とみなされない。副作用は interrupt の後ろか別のノードに置く |
| 受け入れ条件 | 承認②で止めた後、別の接続で作ったグラフから再開して `get_state(cfg).next` が空になる |

### F-15 対話実行（run_interactive）

| 項目 | 内容 |
|---|---|
| 入力 | `mode`（`weekly` / `monthly`）、`period`（省略可。下表の書き方）、`CONFIG` |
| 期間の読み替え | `normalize_period()`：人が書きやすい指定を内部の形（weekly `2026-W41`、monthly `2026-10`）にそろえる。読めない指定は例を添えて `ValueError` |
| 既定の期間 | `default_period()`：weekly は今日の7日前が属する ISO 週（10/14 なら `2026-W41`）、monthly は前月（10月なら `2026-09`） |
| 処理 | (1) 期間を読み替え、`2026-W41（10/5〜10/11）` の形で表示する。(2) 本番で Inoreader を使うならグラフを動かす前に `inoreader_token()` で認証を済ませる。(3) 本番のチェックポイントでグラフを作る。(4) 途中なら再開（interrupt でなければ `invoke(None)`）、完了済みならレポートを表示して終わる、初めてなら起動する。(5) interrupt がある間、`ask()` で入力を受けて再開を繰り返す。(6) レポートと指標を表示する |
| 入力欄（承認①） | 与えたキーワード、推薦（`＋ おすすめ: 語 — 理由`）、今回のキーワードを表示。キーワード（カンマ区切り、空でそのまま）、採点件数（数字、空でそのまま）。常に `{"approved": True}` を含める。`--yes`（`cli.AUTO_APPROVE`）なら入力を求めず空で進む |
| 入力欄（承認②） | まとめる番号（`3+7; 2+5+9`。枝番も可。推薦があれば一覧の後に「💡 同じ話題かもしれない組み合わせ」として番号付きで表示）、一覧（番号、点数、カテゴリ、タイトル、媒体、同じ話題 +N、理由）を表示。`除外 2:既読; 4``b:範囲外（番号でその項目の全部、枝番でまとまりの中の1本。``空 Enter で除外なし＝全部読む）、追加は記事の URL か arXiv ID（カンマ区切り）。承認の番号入力はない。番号は id に変換し、範囲外は無視して知らせ`る |
| 入力欄（全却下） | `next` か `end` |
| 出力 | 画面表示（期間、起動/再開/完了、各承認の提示、採点・要約の進み具合（10件ごと）、レポート、metrics、保存したファイルのパス） |
| 例外 | 入力の途中で止めても（Ctrl+C）状態は SQLite に残り、同じ週・月に読み替えられる指定で呼び直すと止まった承認から再開する |
| 受け入れ条件 | 2026-10-14 時点で `normalize_period("weekly", "2026-10-06")` と `"2026/10/11"` が `2026-W41`、省略が `2026-W41`、`-2` が `2026-W40`、`normalize_period("monthly", -10)` が `2025-12`。本番設定で `run_interactive("weekly")` が承認①②を経て `report-weekly-….md` を出す |

期間の書き方。ISO 週（`2026-W41`）は内部の形で thread_id とファイル名に使い、人は日付で指定する。「10月第1週」のような呼び方は月曜がどこかで解釈が割れるので使わない。

| 書き方 | 意味 | 例 |
|---|---|---|
| 省略、`"last"`、`"先週"`、`"先月"` | 直近の完了した週（monthly は前月） | `run_interactive("weekly")` |
| 日付 `YYYY-MM-DD`（`/` 区切りも可） | その日を含む月曜〜日曜の週（monthly はその月） | `run_interactive("weekly", "2026-10-06")` |
| 負の整数 | n 個前の週・月 | `run_interactive("weekly", -2)` |
| `YYYY-Www` / `YYYY-MM` | そのまま | `run_interactive("monthly", "2026-09")` |

レポートの見出しと起動時の表示には `period_label()` で `2026-W41（10/5〜10/11）` のように日付範囲を併記する。

### F-16 デモと動作確認

| 項目 | 内容 |
|---|---|
| デモ `tests/` | `always_fail` を対応表に足し、取得元2つ（サンプル Inoreader＋失敗する取得元）で `weekly-2026-W40` を起動し、承認①で止まる |
| デモ `tests/` | 採点件数を20に直して再開し、承認②で止まる。10本、失敗した取得元、ガード検出、funnel、集計の一部、まとめた話題、`explain_pair` の内訳を表示 |
| デモ `tests/` | 新しい接続・新しいグラフで再開し、承認3本・却下1本（既読）・追加1本で確定。RoboMind-2 の記事が必ず確定に入るようにする。レポートと指標を表示 |
| 動作確認 `tests/` | 要件定義書第12章の A-01〜A-12 を `assert` で確かめ、1つでも外れるとエラーで止まる。通れば `OK: すべての確認が通りました` |
| 前提 | 初期設定（偽 LLM・サンプル）で入力なし・ネットワークなしで通る（`graph.py` の図の描画だけはネットワークが要り、失敗しても先へ進む） |


### F-18 実行コストの記録

| 項目 | 内容 |
|---|---|
| 入力 | 各 LLM 呼び出しの入力・出力トークン数（structured_call の戻りから取る） |
| 処理 | 呼び出しごとに数えて合計。並列（score_one、summarize）でも壊れないようにロックを使う |
| 出力 | metrics["tokens"]（入力・出力の合計）と metrics["cost_jpy"]。レポートの「選定の記録」に「- LLM トークン: 入力 N ・ 出力 M（約 X 円）」の行 |
| 設定 | default_config.yaml に price_per_1m_input / price_per_1m_output（円） |
| 受け入れ条件 | デモ（偽 LLM）でも metrics["tokens"] が 0 より大きく、report_md に「LLM トークン」を含む行がある。偽 LLM は固定のトークン数を返す |


## 13. データ要件 `models.py`

ノード間のデータは State だけを経由する。Pydantic モデルは LLM の出力形式の固定と外部データの検証にだけ使い、State には `model_dump()` した dict を入れる。

### Candidate（候補1件の共通の形）

| 項目 | 型 | 中身 | Inoreader から |
|---|---|---|---|
| `id` | str | arXiv ID、なければ正規化した URL。重複除去のキー | `canonical[0].href` から |
| `title` | str | タイトル | `title`（エンティティを戻す） |
| `url` | str | 出典 | `canonical[0].href` |
| `source` | str | 取得元の種類（レジストリの名前） | `"rss"` |
| `published` | str | 公開日時（JST） | `published` |
| `abstract` | str | 要旨（HTML タグ除去） | `summary.content` |
| `rank` | int | 取得元の中での順位 | 保存した順 |
| `feed` | str | 取得元の名前（`CONFIG` の `name`） | fetch_source で付ける |
| `outlet` | str | 媒体（購読フィード名） | `origin.title` |
| `saved_at` | str | 保存日時（JST、`2026-10-04T23:00+09:00`） | `date_field` の項目 |
| `tags` | list[str] | Inoreader のタグ・フォルダ | `categories` の `/label/` 以降 |
| `entities` | list[str] | 固有名詞（小文字トークン） | cluster_topics で付ける |
| `keywords` | list[str] | 話題キーワード（小文字。規則＋LLM。まとめた記事の分も代表に集める） | cluster_topics で付ける |
| `category` | str | カテゴリ（`CONFIG["categories"]` のキーか「その他」） | cluster_topics で付ける |
| `related` | list[dict] | 同じ話題にまとめた他の記事（title、url、source、outlet、saved_at、entities、keywords、why） | cluster_topics で付ける |

### LLM の出力の形

| モデル | 項目 | 制約 | 使う場所 |
|---|---|---|---|
| `Plan` | `focus_keywords: list[str]`、`max_items: int`、`suggestions: list[Suggestion]`（`keyword`、`reason`） | `max_items` は 10〜150 | plan、approve_plan |
| `Summary` | `text: str`、`sources_used: int` | — | summarize |
| `EntityRow` / `EntityBatch` | `i: int`、`names: list[str]`、`keywords: list[str]`、`category: str`；`items: list[EntityRow]` | 固有名詞は最大8個、キーワードは最大5個（プロンプトで指示）。カテゴリは一覧になければ「その他」扱い | cluster_topics |
| `Score` | id: str、relevance: int、novelty: int、impact: int（省略可）、reason: str（総評）、relevance_reason / novelty_reason / impact_reason: str（基準ごとの理由、省略可）、title_ja: str、summary_ja: str | 点数は 0〜5 | score_one |

### ResearchState

| フィールド | 更新 | 書くノード | 中身 |
|---|---|---|---|
| `mode`、`period`、`theme`、`sources` | 起動時 | run_interactive | `weekly`/`monthly`、`2026-W41`/`2026-10`、テーマ、取得元一覧 |
| `plan` | 上書き | plan | LLM の提案 |
| `approved_plan` | 上書き | approve_plan | 人が確定した計画 |
| `candidates` | 連結 | fetch_source | 並列取得の結果 |
| `failed_sources` | 連結 | fetch_source | 取れなかった取得元と理由 |
| `topics` | 上書き | cluster_topics | 話題ごとの代表 |
| `stats` | 上書き | aggregate | 集計 |
| `pool` | 上書き | merge | 採点対象 |
| `funnel` | 上書き | merge | 取得元ごとの件数の流れ |
| `scored` | 連結 | score_one | 並列採点の結果 |
| `shortlist`、`shown`、`merge_hints` | 上書き | shortlist、review | 今回提示する話題、提示済みの累積、同じ話題かもしれない組の推薦 |
| `summaries` | 上書き | summarize | 話題 id → {text, sources_used, articles} |
| `selected`、`feedback`、`review_action` | 上書き | review（`review_action` は confirm_empty も） | 確定分、承認②の記録、次の行き先（`done` / `next` / `ask` / `end`） |
| `guardrail_flags` | 連結 | merge、report | 検出したもの |
| `metrics`、`report_md` | 上書き | report | 指標とレポート本文 |

「連結」は `Annotated[list, operator.add]`。Send で並列に走ったノードの戻り値が上書きされずにすべて残る。

## 14. ファイル出力一覧と設定項目一覧

1回の実行で保存先（`--workdir`）に7つのファイルが残る。認証とチェックポイントのファイルは実行をまたいで使い回す。

| ファイル | 書く機能 | 中身 | 扱い |
|---|---|---|---|
| `inoreader-{name}-{mode}-{period}.json` | F-05 | 取ったままの JSON（期間で絞る前の全件） | 再集計に使う。API を呼び直さなくてよい。期間外で落ちた記事はここにだけ残る |
| `{mode}-{period}-articles.csv` | F-07 | 記事1本1行（topic、representative、category、keywords、title、url、outlet、saved_at、tags、why）。UTF-8 BOM 付き | スプレッドシートで別の切り口の集計を作る |
| `{mode}-{period}-stats.json` | F-07 | 7つの集計そのもの | — |
| `{mode}-{period}-trace.csv` | F-12 | 記事1本1行の経路の記録（到達した段階、まとめ、採点、承認②の結果）。UTF-8 BOM 付き | 「あの記事はなぜ出てこなかったか」を調べる |
| `report-{mode}-{period}.md` / `.docx` | F-12 | レポート本文（Markdown と、同じ内容の Word） | 読む |
| `log-{mode}-{period}.txt` | 全体 | 実行ログ（時刻・経過秒付き。画面と同じ内容。再開のたびに追記） | 所要時間とエラーの確認 |
| `checkpoints.sqlite` | F-14 | LangGraph のチェックポイント（thread_id ごと） | 消すと途中の状態が消える |
| `inoreader_token.json` | F-05 | OAuth トークン（権限 0o600） | 自分の Inoreader を読める鍵。共有フォルダに置かない |
| `inoreader_credentials.json` | F-05（読むだけ） | `{"app_id": …, "app_key": …}`（任意。シークレットの代わり） | 同上 |

設定項目の一覧。

| 項目 | 場所 | 初期値 | 意味 |
|---|---|---|---|
| `settings.fake_llm` / `settings.sample_data` | `config.py` | False / False | F-01 の切り替え（`--fake-llm` / `--sample`） |
| `CONFIG["theme"]` | `config.py` | AI 全般の技術動向（9カテゴリを列挙。狭く書くとそれ以外のカテゴリの採点が一律に下がる） | 関心領域の説明。採点の文脈にだけ使い、絞り込みには使わない |
| `CONFIG["categories"]` | `config.py` | 9カテゴリ（F-01 の表。手がかり語付き） | 話題に付ける大きなカテゴリ。「その他」が多ければ手がかり語を足す |
| `CONFIG["focus_keywords"]` | `config.py` | AI agent, frontier model, VLA, world model | 守備範囲の中で今とくに押し上げたい焦点（3〜6語）。関連度の加点と採点に回す順番にだけ効き、分類・まとめには使わない。plan が推薦を足す |
| `CONFIG["ask_plan"]` | `config.py` | true | 承認①で止めるか。false なら提案をそのまま使う |
| `CONFIG["summary_chars"]` / `summary_chars_multi` | `config.py` | 300 / 400 | 要約の字数（1本の話題／複数本をまとめる話題の上限） |
| `CONFIG["summary_max_articles"]` / `summary_fetch_body` / `summary_scope` | `config.py` | 4 / true / all | 横断する記事の上限、本文を取りに行くか、付録まで要約するか（all / selected） |
| `CONFIG["model"]` | `config.py` | `openai:gpt-4.1-mini` | 計画・抽出・採点・要約に使う LLM（1回の呼び出しは60秒で打ち切り、2回まで再試行） |
| `CONFIG["embed_model"]` | `config.py` | `openai:text-embedding-3-small` | 話題のまとめの埋め込み |
| `CONFIG["entity_llm"]` | `config.py` | True | 固有名詞・キーワード・カテゴリの抽出に LLM も使うか |
| `CONFIG["sources"]` | `config.py` | Inoreader 保存済み1件 | 取得元の一覧（F-01 の表） |
| `CONFIG["inoreader_redirect_uri"]` | `config.py` | `http://localhost` | アプリ登録のリダイレクト先と1文字違わず同じにする |
| `settings.workdir` / `CONFIG["checkpoint"]` | `config.py` | `./tsundoku_data` / `{local_dir}/checkpoints.sqlite` | 保存先と、SQLite の置き場所 |
| `MAX_RETRY` | `nodes/fetch.py` | 2 | 取得元ごとの再試行回数 |
| `W_EMB` / `W_KW` | `nodes/cluster.py` | 0.7 / 0.3 | 意味の近さ（埋め込み＋話題キーワード）と固有名詞の重み |
| `W_TOPIC_KW` | `nodes/cluster.py` | 0.3 | 話題キーワードの一致が cos を押し上げる強さ（両方にキーワードがあるときだけ。不一致では下げない） |
| `SAME_TOPIC` | `nodes/cluster.py` | 0.62 | 同じ話題とみなすしきい値 |
| `VETO_COS` | `nodes/cluster.py` | 0.95 | 見出しの固有名詞が食い違ってもまとめてよい cos の下限 |
| `ENTITY_BATCH` | `nodes/cluster.py` | 25 | LLM に1回で送る件数 |
| `GENERIC_WORDS`、表記ゆれ辞書 | `nodes/cluster.py` | — | 固有名詞とみなさない語、寄せ先 |
| `TOP_N` | `nodes/aggregate.py` | 10 | 集計のランキングに出す件数 |
| `SHORTLIST_SIZE` | `nodes/scoring.py` | 10 | 承認②に出す件数の予備値。通常は CONFIG["shortlist_size"]（読む件数の目安。初期20。0＝全件）、CONFIG["shortlist_min_score"]（合計点のしきい値。初期 None＝上から shortlist_size 番目の点を自動で使う。数値を入れれば固定）、CONFIG["backfill"]（初期 True。除外で目安を下回った分を次点で埋める）を使う。デモは 10 / None / False に一時的に上書きし、`tests/` の最後で元に戻す |
| `SUSPICIOUS` | `guardrails.py` | 正規表現 | 指示文らしい言い回し |
| `PLAN_PROMPT` / `SCORE_PROMPT` / `ENTITY_PROMPT` | `prompts.py` | — | 指示のひな形。基準を変えるときはここだけを直す |
| `RetryPolicy(max_attempts=3)` | `graph.py` | 3 | cluster_topics・score_one・summarize の再試行 |
| シークレット | `.env` / 環境変数 / Colab | — | `INOREADER_APP_ID`、`INOREADER_APP_KEY`、`OPENAI_API_KEY`（必須）、`LANGSMITH_API_KEY`（任意） |
