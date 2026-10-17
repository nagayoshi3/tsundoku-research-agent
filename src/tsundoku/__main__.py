"""コマンドライン：`tsundoku run` / `demo` / `check-inoreader` / `graph`。"""

from __future__ import annotations

import argparse
import sys


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="設定ファイル（既定: ./config.yaml → 同梱の default_config.yaml）")
    p.add_argument("--workdir", help="レポート・CSV・ログ・チェックポイントの保存先（既定: ./tsundoku_data）")
    p.add_argument("--local-dir", help="チェックポイント（SQLite）を置く場所。保存先がネットワークドライブのとき用")
    p.add_argument("--fake-llm", action="store_true", help="API を呼ばない偽 LLM で動かす（費用ゼロ）")
    p.add_argument("--sample", action="store_true", help="Inoreader の代わりにサンプル記事で動かす")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tsundoku",
        description="Inoreader に保存した記事を、まとめ・採点・要約し、人の承認を経てレポートにする",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="本番の実行（承認①②で入力を求める）")
    _add_common(run)
    run.add_argument("mode", nargs="?", default="weekly", choices=["weekly", "monthly"])
    run.add_argument(
        "period",
        nargs="?",
        help="期間：日付 2026-10-15、週 2026-W42、月 2026-09、last、-1 など。省略で直近の完了週・月",
    )
    run.add_argument("--fresh", action="store_true", help="その期間の途中経過・結果を捨てて最初からやり直す")
    run.add_argument("--yes", action="store_true", help="承認①②で止まらず、提案をそのまま承認する")

    demo = sub.add_parser("demo", help="サンプル記事＋偽 LLM で最後まで動かす（API キー不要）")
    _add_common(demo)

    chk = sub.add_parser("check-inoreader", help="Inoreader の認証と、保存日時の意味を確かめる（初回だけ）")
    _add_common(chk)

    sub.add_parser("graph", help="グラフの配線を Mermaid 形式で表示する")

    a = ap.parse_args(argv)
    if a.cmd == "graph":
        from tsundoku.graph import mermaid

        print(mermaid())
        return 0

    from tsundoku import cli
    from tsundoku.config import setup

    if a.cmd == "demo":
        setup(fake_llm=True, sample_data=True, workdir=a.workdir, local_dir=a.local_dir, config_path=a.config)
        cli.AUTO_APPROVE = True
        cli.run_interactive("weekly", "2026-W40", fresh=True)  # サンプル記事はこの週に保存した想定
        return 0

    setup(fake_llm=a.fake_llm, sample_data=a.sample, workdir=a.workdir, local_dir=a.local_dir, config_path=a.config)
    if a.cmd == "check-inoreader":
        from tsundoku.sources.inoreader import check_inoreader

        check_inoreader()
        return 0
    cli.AUTO_APPROVE = a.yes
    cli.run_interactive(a.mode, a.period, fresh=a.fresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
