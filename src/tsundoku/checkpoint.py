"""チェックポイント（SQLite）の開閉：ローカルで動かし、保存先（Drive など）には複製を置く。"""

import os
import shutil
import sqlite3
import time

from langgraph.checkpoint.sqlite import SqliteSaver

from tsundoku.config import CONFIG, log


def _sqlite_ok(path: str) -> bool:
    try:
        with sqlite3.connect(path) as c:
            return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except Exception:
        return False


def open_checkpointer() -> "SqliteSaver":
    # ローカルの SQLite を開く。なければ Drive の複製から復元する。壊れていれば脇に退けて新しく始める
    local, backup = CONFIG["checkpoint"], CONFIG.get("checkpoint_backup")
    if not os.path.exists(local) and backup and os.path.exists(backup):
        shutil.copy(backup, local)
        log(f"[checkpoint] Drive の複製から復元 → {local}")
    for path in [local] + ([backup] if backup and backup != local else []):
        if os.path.exists(path) and not _sqlite_ok(path):
            broken = f"{path}.corrupt-{int(time.time())}"
            os.rename(path, broken)
            log(f"⚠ チェックポイントが壊れていたので退けました → {broken}（途中経過は失われる。最初からやり直す）")
    if not os.path.exists(local) and backup and os.path.exists(backup):  # ローカルだけ壊れていた場合
        shutil.copy(backup, local)
    return SqliteSaver(sqlite3.connect(local, check_same_thread=False))


def sync_checkpoint(saver: "SqliteSaver"):
    # ローカルの SQLite を Drive に複製する（SQLite の backup API で、書き込み中でも整合した複製が取れる）
    backup = CONFIG.get("checkpoint_backup")
    if not backup or backup == CONFIG["checkpoint"]:
        return
    try:
        with sqlite3.connect(backup) as dst:
            saver.conn.backup(dst)
    except Exception as e:
        log(f"⚠ Drive への複製に失敗（ローカルには残っている）: {e}")
