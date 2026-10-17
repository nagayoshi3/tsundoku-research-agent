"""設定の読み込み（config.yaml）、実行時の切り替え（settings）、保存先、実行ログ、API キー。

優先順位：環境変数 > .env > config.yaml > 同梱の default_config.yaml
"""

from __future__ import annotations

import datetime as _dt
import os
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _HERE / "default_config.yaml"


# --- 実行時の切り替え（コマンドライン・テスト・Colab から変える）----------------------------
@dataclass
class Settings:
    fake_llm: bool = False  # True: API を呼ばない決定的な偽 LLM（デモ・テスト用）
    sample_data: bool = False  # True: Inoreader の代わりにサンプル記事（実在しない）
    workdir: str = "./tsundoku_data"  # レポート・CSV・生 JSON・ログ・チェックポイントの複製の保存先
    local_dir: str = ""  # チェックポイント（SQLite）を実際に置く場所。空なら workdir と同じ
    config_path: str = ""  # 読み込んだ config.yaml（表示用）
    extra: dict = field(default_factory=dict)

    def apply_paths(self) -> None:
        """保存先を作り、CONFIG のチェックポイントのパスを決める。workdir を変えたら呼び直す。"""
        self.local_dir = self.local_dir or self.workdir
        os.makedirs(self.workdir, exist_ok=True)
        os.makedirs(self.local_dir, exist_ok=True)
        CONFIG["checkpoint"] = os.path.join(self.local_dir, "checkpoints.sqlite")  # 実際に使う
        CONFIG["checkpoint_backup"] = os.path.join(self.workdir, "checkpoints.sqlite")  # 複製（Drive など）


settings = Settings()
CONFIG: dict = {}


def load_config(path: str | os.PathLike | None = None) -> dict:
    """config.yaml を読んで CONFIG を置き換える。path が None なら、カレントの config.yaml → 同梱の既定値の順。"""
    candidates = [Path(path)] if path else [Path("config.yaml"), DEFAULT_CONFIG_PATH]
    for p in candidates:
        if p.exists():
            with open(p, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            CONFIG.clear()
            CONFIG.update(data)
            settings.config_path = str(p)
            settings.apply_paths()
            return CONFIG
    raise FileNotFoundError(f"設定ファイルが見つかりません: {candidates}")


# --- 実行ログ：時刻と経過秒を付けて画面に出し、ファイルにも残す ------------------------------
_LOG = {"path": None, "t0": None}


def log_start(path: str | None) -> None:
    _LOG["path"], _LOG["t0"] = path, _time.time()
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {_dt.datetime.now().isoformat(timespec='seconds')} =====\n")


def log(msg: str) -> None:
    t0 = _LOG["t0"] or _time.time()
    line = f"[{_dt.datetime.now().strftime('%H:%M:%S')} +{_time.time() - t0:6.1f}s] {msg}"
    print(line, flush=True)
    if _LOG["path"]:
        with open(_LOG["path"], "a", encoding="utf-8") as f:
            f.write(line + "\n")


# --- API キー：環境変数 → .env → Colab のシークレット → 入力欄 ---------------------------------
def load_secret(name: str, required: bool) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    try:
        from dotenv import load_dotenv

        load_dotenv()  # カレントの .env（git には入れない）
        if os.environ.get(name):
            return os.environ[name]
    except ImportError:
        pass
    try:
        from google.colab import userdata  # Colab のシークレット

        value = userdata.get(name)
        if value:
            os.environ[name] = value
            return value
    except Exception:
        pass
    if required:
        import getpass

        os.environ[name] = getpass.getpass(f"{name} を入力してください: ")
        return os.environ[name]
    return None


def setup(
    fake_llm: bool | None = None,
    sample_data: bool | None = None,
    workdir: str | None = None,
    local_dir: str | None = None,
    config_path: str | None = None,
    quiet: bool = False,
) -> None:
    """設定と保存先をまとめて決める。CLI・Colab・テストの入口。"""
    load_config(config_path)
    if fake_llm is not None:
        settings.fake_llm = fake_llm
    if sample_data is not None:
        settings.sample_data = sample_data
    if workdir:
        settings.workdir = workdir
    settings.local_dir = local_dir or ""
    settings.apply_paths()
    if not settings.fake_llm:
        load_secret("OPENAI_API_KEY", required=True)
    if load_secret("LANGSMITH_API_KEY", required=False):  # 任意：あれば LangSmith に実行を記録する
        os.environ["LANGSMITH_TRACING"] = "true"
    if not quiet:
        print(
            "LLM:",
            "偽LLM（キー不要）" if settings.fake_llm else CONFIG["model"],
            "／ データ:",
            "サンプル" if settings.sample_data else "Inoreader API",
            "／ 保存先:",
            os.path.abspath(settings.workdir),
            "／ 設定:",
            settings.config_path,
        )


# import しただけでも CONFIG が使えるように既定値を読む（保存先のフォルダは apply_paths で作られる）
load_config()
