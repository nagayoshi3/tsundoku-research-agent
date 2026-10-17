"""期間の指定（週・月）：ISO 週への読み替え、期間の始まりと終わり、表示用のラベル。"""

import datetime as dt
import re
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


# --- 期間 → 時刻の範囲（日本時間）------------------------------------------------
def period_range(mode: str, period: str) -> tuple[float, float]:
    if mode == "weekly":  # "2026-W40" → その週の月曜 0時〜翌週の月曜 0時
        y, w = period.split("-W")
        start = dt.datetime.combine(dt.date.fromisocalendar(int(y), int(w), 1), dt.time(), JST)
        end = start + dt.timedelta(days=7)
    else:  # "2026-09" → 9月1日 0時〜10月1日 0時
        y, m = map(int, period.split("-"))
        start = dt.datetime(y, m, 1, tzinfo=JST)
        end = dt.datetime(y + m // 12, m % 12 + 1, 1, tzinfo=JST)
    return start.timestamp(), end.timestamp()


def period_label(mode: str, period: str) -> str:
    # 人が読む形：weekly は「2026-W41（10/5〜10/11）」、monthly は「2026-10」
    if mode != "weekly":
        return period
    start, end = period_range(mode, period)
    a, b = (
        dt.datetime.fromtimestamp(start, JST),
        dt.datetime.fromtimestamp(end - 1, JST),
    )
    return f"{period}（{a.month}/{a.day}〜{b.month}/{b.day}）"


def jst(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, JST).isoformat(timespec="minutes") if ts else ""


def _week_of(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def default_period(mode: str, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    if mode == "weekly":  # 直近の「完了した」ISO 週
        return _week_of(today - dt.timedelta(days=7))
    last = today.replace(day=1) - dt.timedelta(days=1)  # 前月
    return f"{last.year}-{last.month:02d}"


def normalize_period(mode: str, period: str | int | None, today: dt.date | None = None) -> str:
    # 人が書きやすい指定を、内部の形（weekly: 2026-W41、monthly: 2026-10）にそろえる
    #   None / "last" / "先週" / "先月" → 直近の完了した週・月
    #   -2 のような負の整数            → 2つ前の週・月
    #   "2026-10-06" のような日付       → その日を含む週（月曜〜日曜）・月
    #   "2026-W41" / "2026-10"          → そのまま
    today = today or dt.date.today()
    if period is None or str(period).strip() in ("", "last", "先週", "先月"):
        return default_period(mode, today)
    if isinstance(period, int) or re.fullmatch(r"-\d+", str(period)):
        n = -int(period)
        if mode == "weekly":
            return _week_of(today - dt.timedelta(days=7 * n))
        y, m = today.year, today.month - n
        return f"{y + (m - 1) // 12}-{(m - 1) % 12 + 1:02d}"
    s = str(period).strip().replace("/", "-")
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
        y, m, d = (int(x) for x in s.split("-"))
        day = dt.date(y, m, d)
        return _week_of(day) if mode == "weekly" else f"{day.year}-{day.month:02d}"
    if mode == "weekly" and re.fullmatch(r"\d{4}-W\d{1,2}", s):
        y, w = s.split("-W")
        return f"{y}-W{int(w):02d}"
    if mode == "monthly" and re.fullmatch(r"\d{4}-\d{1,2}", s):
        y, m = s.split("-")
        return f"{y}-{int(m):02d}"
    raise ValueError(f"期間の指定が読めません: {period!r}（例: 2026-10-06 / 2026-W41 / 2026-10 / last / -1）")
