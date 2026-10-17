"""取得元のレジストリ（Tool/agent registry）と、オフライン用のサンプルデータ。"""

import random
import re
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tsundoku.config import settings
from tsundoku.models import Candidate

_REGISTRY: dict[str, Callable[..., list[dict]]] = {}


def register(kind: str):
    def deco(fn):
        _REGISTRY[kind] = fn
        return fn

    return deco


def get_fetcher(kind: str):
    if kind not in _REGISTRY:
        raise KeyError(f"未登録の取得元: {kind}（登録済み: {sorted(_REGISTRY)}）")
    return _REGISTRY[kind]


# --- RSS（任意の第2取得元。Inoreader のフォルダ出力フィードなど）------------------
ARXIV_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_", "ref")


def normalize_url(url: str) -> str:
    # 同じ記事の URL の揺れをそろえる：http/https、www.、末尾の /、#以降、追跡用のパラメータ
    p = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(TRACKING_PARAMS)])
    return urlunsplit(
        (
            "https",
            p.netloc.lower().removeprefix("www."),
            p.path.rstrip("/") or "/",
            query,
            "",
        )
    )


@register("rss")
def fetch_rss(source: dict, *, mode: str, period: str, limit: int = 100) -> list[dict]:
    if settings.sample_data:
        entries = sample_rss_entries()
    else:
        import feedparser

        feed = feedparser.parse(source["url"])
        if feed.bozo and not feed.entries:
            raise RuntimeError(f"RSS 取得失敗: {source['url']}")
        entries = feed.entries
    out = []
    for i, e in enumerate(entries[:limit], start=1):  # フィードの並び（新しい順）をそのまま順位にする
        link = e.get("link", "")
        m = ARXIV_RE.search(link + " " + e.get("summary", ""))
        out.append(
            Candidate(
                id=m.group(1) if m else normalize_url(link),
                title=e.get("title", ""),
                url=link,
                source="rss",
                published=e.get("published", ""),
                rank=i,
                abstract=re.sub(r"<[^>]+>", "", e.get("summary", "")),
            ).model_dump()
        )
    return out


# --- オフライン用のサンプル（実在しない会社・製品。2番には指示文を仕込んである）--------
def sample_rss_entries() -> list[dict]:
    # Inoreader のフォルダ出力を想定したサンプル記事150本（新しい順）。ほとんどはテーマと関係の薄い業界ニュースで、
    # キーワードに合う記事はわざと後ろの方に置いてある（新しい順に10件取るだけでは拾えない、を見せるため）。
    # 120・121・126・133 は「Acme Robotics の RoboMind-2」という同じ話題を別々の媒体が書いた記事
    special = {
        3: (
            "Explainer: LLM agent planning",
            "https://arxiv.org/abs/9999.10001",
            "A walkthrough of the paper.",
        ),  # arXiv の論文（ID で重複除去する例）
        120: (
            "Acme Robotics releases RoboMind-2, an agent reasoning benchmark for robots",
            "https://www.technews.example.com/acme-robomind-2/",
            "This week, Acme Robotics released RoboMind-2, a benchmark that tests how robot agents plan and reason.",
        ),
        121: (
            "RoboMind-2 puts robot agents' reasoning to the test",
            "https://roboweekly.example.com/2026/10/robomind-2",
            "Acme Robotics says RoboMind-2 covers 40 manipulation tasks and measures step-by-step reasoning.",
        ),
        126: (
            "Hands-on with Acme's new robot reasoning suite",
            "https://labnotes.example.com/acme-hands-on",
            "We tried RoboMind-2 from Acme Robotics on a few warehouse tasks.",
        ),
        128: (
            "Zentra releases VeloBench-3, an agent reasoning benchmark for robots",  # 言い回しは 120 とほぼ同じ、
            "https://www.technews.example.com/zentra-velobench-3/",  # 会社と製品が違う別の話題
            "This week, Zentra released VeloBench-3, a benchmark that tests how robot agents plan and reason.",
        ),
        133: (
            "Acme Robotics releases RoboMind-2, an agent reasoning benchmark for robots",  # 120 と同じ記事（URL の揺れ）
            "http://technews.example.com/acme-robomind-2?utm_source=rss&utm_medium=feed",
            "This week, Acme Robotics released RoboMind-2, a benchmark that tests how robot agents plan and reason.",
        ),
        135: (
            "Agent memory and reasoning in practice",
            "https://devdiary.example.com/agent-memory",
            "A blog post about agent memory and reasoning in practice.",
        ),
        142: (
            "Notes on robot foundation models",
            "https://devdiary.example.com/robot-fm",
            "A blog post about robot foundation models.",
        ),
    }
    companies = [
        "Norvik",
        "Quillon",
        "Tessaly",
        "Halvard",
        "Mirelle",
        "Kobalt",
        "Pemberly",
        "Orbisco",
        "Calderon",
        "Vantor",
        "Lumeno",
        "Sorrel",
    ]
    news = [
        ("cuts cloud storage prices", "storage tiers will cost less from next month."),
        (
            "opens a new data center in Ohio",
            "the facility near Columbus will start serving customers in spring.",
        ),
        (
            "reports quarterly earnings",
            "revenue grew eight percent while margins narrowed.",
        ),
        (
            "hires a new chief financial officer",
            "the incoming executive previously led treasury at a retailer.",
        ),
        (
            "settles a patent dispute",
            "both sides agreed to drop their lawsuits over display technology.",
        ),
        (
            "launches a smartphone app for banking",
            "customers can now open savings accounts from their phones.",
        ),
        (
            "expands its chip supply deal",
            "the contract now covers memory modules through 2028.",
        ),
        (
            "publishes a sustainability report",
            "emissions fell for the third straight year.",
        ),
        (
            "acquires a payments startup",
            "the deal values the checkout company at about two hundred million dollars.",
        ),
        (
            "raises prices for its office suite",
            "subscription fees go up by a dollar per seat.",
        ),
        (
            "announces layoffs in its ads division",
            "about three hundred roles will be cut.",
        ),
        (
            "partners on satellite internet",
            "rural offices will get coverage by the end of the year.",
        ),
        (
            "updates its privacy policy",
            "users may now opt out of personalised advertising.",
        ),
    ]
    fillers = [(co, n) for n in news for co in companies]
    random.Random(3).shuffle(fillers)
    out = []
    for i in range(1, 151):
        if i in special:
            title, link, body = special[i]
        else:
            co, (n, detail) = fillers[i]
            title, link = f"{co} {n}", f"https://bizwire.example.com/{i}"
            body = f"According to {co}, {detail}"
        out.append(
            {
                "title": f"[Sample blog] {title}",
                "link": link,
                "summary": f"[Sample data for offline demo] {body}",
            }
        )
    return out


# 取得元の実装を読み込んで登録する（@register はモジュールの読み込み時に実行される）
from tsundoku.sources import inoreader as _inoreader  # noqa: E402,F401
