"""Tonghuashun 7x24 newswire (同花顺 7x24 快讯) — Chinese-language A-share news.

The public JSON endpoint behind https://news.10jqka.com.cn/realtimenews.html
(the one its "load more" button calls) serves the newest items first, 200 to a
page, and every item tags the listed companies it is about::

    "stock": [{"name": "金安国纪", "stockCode": "002636", "stockMarket": "33"}]

That tag is what selects the news. An item is about an A-share when the
symbol's six-digit code is among its tags; untagged items (market wraps, sector
moves) are matched on the company name instead, which is resolved once from
Tencent's keyless quote endpoint.

No key, no auth. The wire is live and recent-only — it keeps roughly two to
three weeks — so a window reaching further back is reported through
``coverage_gap`` as unavailable rather than as an absence of news.

Unlike Yahoo Finance, whose news feed tags almost no A-share listing, this
source is what makes ``--ticker 002636.SZ`` report the company's own
announcements and coverage instead of "no news".
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

NEWS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
QUOTE_URL = "https://qt.gtimg.cn/q="
REFERER = "https://news.10jqka.com.cn/realtimenews.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Items per page, and the request timeout. The wire answers a 200-item page in
# well under a second; 20s leaves room for a slow link without hanging a run.
PAGE_SIZE = 200
REQUEST_TIMEOUT = 20

# The wire holds roughly two to three weeks of items (~4-5 pages a day); this
# bounds the worst case for a window that reaches past the end of it.
MAX_PAGES = 120

# Pages fetched in parallel per round. Four keeps a six-day window to a couple
# of rounds without hammering a public endpoint.
CONCURRENCY = 4
ROUND_DELAY = 0.1

# Page contents shift as the wire grows, so a page is only reused for a short
# while — enough that an analyst's repeated tool calls share the work of one run.
PAGE_CACHE_TTL = 300.0

# The wire timestamps in Beijing time, and an analysis date is a trading day
# there; pinning the zone keeps the window correct on a host in another one.
_CST = timezone(timedelta(hours=8))

_A_SHARE = re.compile(r"^(?:(?:SH|SZ|BJ)\.?)?(\d{6})(?:\.(?:SS|SH|SZ|BJ))?$")

_page_cache: dict[int, tuple[float, list[dict]]] = {}
_name_cache: dict[str, str | None] = {}


def a_share_code(ticker: str) -> str | None:
    """The six-digit code of an A-share listing, or None for anything else.

    Recognizes what the CLI and the broker-style symbols carry:
    ``002636.SZ``, ``600519.SS``, ``600519.SH``, ``SZ002636``, ``002636``.
    Hong Kong (``0700.HK``), US (``AAPL``), futures (``GC=F``) and index
    (``^GSPC``) symbols return None, so this vendor steps aside for them.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return None
    canonical = normalize_symbol(ticker).strip().replace(" ", "")
    match = _A_SHARE.match(canonical)
    return match.group(1) if match else None


def quote_symbol(code: str) -> str:
    """The code as Tencent quotes it: ``sh600519``, ``sz002636``, ``bj430047``."""
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return f"sz{code}"


def company_name(code: str) -> str | None:
    """The listing's short name, or None when the quote endpoint does not answer.

    Only used to catch news the wire leaves untagged, so a failure downgrades
    the match (code tags still work) rather than failing the call.
    """
    if code in _name_cache:
        return _name_cache[code]
    name = None
    try:
        response = requests.get(
            f"{QUOTE_URL}{quote_symbol(code)}",
            headers={"User-Agent": USER_AGENT, "Referer": "https://gu.qq.com/"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        response.encoding = "gbk"
        match = re.search(r'="\d*~([^~]{1,32})~', response.text)
        name = match.group(1).strip() if match else None
    except (requests.RequestException, UnicodeError) as e:
        logger.debug("Tencent quote lookup failed for %s: %s", code, e)
    _name_cache[code] = name
    return name


def _fetch_page(page: int) -> list[dict]:
    """One page of the wire, newest first; cached briefly in-process."""
    now = time.monotonic()
    cached = _page_cache.get(page)
    if cached and now - cached[0] < PAGE_CACHE_TTL:
        return cached[1]

    try:
        response = requests.get(
            NEWS_URL,
            params={"page": page, "tag": "", "track": "website", "pagesize": PAGE_SIZE},
            headers={"User-Agent": USER_AGENT, "Referer": REFERER},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        raise VendorUnavailableError(
            f"同花顺 7x24 快讯 request failed on page {page}: {e}"
        ) from e

    if str(payload.get("code")) != "200":
        raise VendorUnavailableError(
            f"同花顺 7x24 快讯 returned code={payload.get('code')} on page {page}"
        )

    items = ((payload.get("data") or {}).get("list")) or []
    _page_cache[page] = (now, items)
    return items


def _published(raw: dict) -> datetime | None:
    try:
        stamp = int(raw.get("ctime") or raw.get("rtime") or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(stamp, tz=_CST) if stamp else None


def _item(raw: dict) -> dict:
    """A wire row in the shape the formatter and the filters need."""
    tags = raw.get("stock")
    return {
        "title": (raw.get("title") or "").strip(),
        "summary": (raw.get("digest") or raw.get("short") or "").strip(),
        "link": (raw.get("url") or "").strip(),
        "published": _published(raw),
        "codes": {
            str(tag.get("stockCode") or "")
            for tag in tags or []
            if isinstance(tag, dict)
        },
    }


def _fetch_wire(start_dt: datetime, max_pages: int = MAX_PAGES) -> tuple[list[dict], datetime | None]:
    """Items from the wire, newest first, paging back until ``start_dt``.

    Pages are requested in parallel rounds; a round whose pages are not all full
    is the end of the wire. Returns the items collected and the oldest timestamp
    seen, which is what coverage is measured against.
    """
    collected: list[dict] = []
    oldest: datetime | None = None
    page = 1
    while page <= max_pages:
        pages = list(range(page, min(page + CONCURRENCY, max_pages + 1)))
        with ThreadPoolExecutor(max_workers=len(pages)) as pool:
            rounds = list(pool.map(_fetch_page, pages))
        page += CONCURRENCY

        for raw_items in rounds:
            for raw in raw_items:
                item = _item(raw)
                collected.append(item)
                if item["published"] is not None and (
                    oldest is None or item["published"] < oldest
                ):
                    oldest = item["published"]

        if any(not raw_items for raw_items in rounds):
            break  # the wire ends here
        if oldest is not None and oldest.date() < start_dt.date():
            break
        time.sleep(ROUND_DELAY)
    return collected, oldest


def _is_about(item: dict, code: str, name: str | None) -> bool:
    """Whether the item reports on this listing: by code tag, else by name."""
    if code in item["codes"]:
        return True
    if not name or len(name) < 2:
        return False
    return name in f"{item['title']} {item['summary']}"


def _in_window(published: datetime | None, start_dt: datetime, end_dt: datetime) -> bool:
    return published is not None and start_dt <= published < end_dt + timedelta(days=1)


def _format(item: dict) -> str:
    stamp = f" · {item['published']:%Y-%m-%d %H:%M}" if item["published"] else ""
    out = f"### {item['title']} (source: 同花顺 7x24 快讯{stamp})\n"
    if item["summary"] and item["summary"] != item["title"]:
        out += f"{item['summary'][:500]}\n"
    if item["link"]:
        out += f"Link: {item['link']}\n"
    return out + "\n"


def get_news_ths(ticker: str, start_date: str, end_date: str) -> str:
    """News about an A-share listing published in ``[start_date, end_date]``.

    A non-A-share symbol raises ``NoMarketDataError`` immediately, so the router
    hands it to the next vendor (Yahoo Finance for US listings) at no cost. For
    an A-share the 7x24 wire is paged back to the window's first day and filtered
    to the items tagged with the code (or naming the company), keeping the
    requested window only: nothing published after ``end_date`` is served.
    """
    code = a_share_code(ticker)
    if code is None:
        raise NoMarketDataError(
            ticker, ticker, "同花顺 7x24 快讯 covers A-share listings only"
        )

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=_CST)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=_CST)
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    name = company_name(code)

    items, oldest = _fetch_wire(start_dt)
    hits = [
        item
        for item in items
        if _is_about(item, code, name) and _in_window(item["published"], start_dt, end_dt)
    ]
    hits.sort(key=lambda item: item["published"], reverse=True)

    limit = get_config()["news_article_limit"]
    hits = hits[:limit]

    if hits:
        body = "".join(_format(item) for item in hits)
        return (
            f"## {ticker}{resolved} News, from {start_date} to {end_date} "
            f"(同花顺 7x24 快讯):\n\n{body}"
        )

    gap = coverage_gap(
        [oldest] if oldest is not None else [],
        start_date,
        end_date,
        "同花顺 7x24 快讯",
        f"news for {ticker}{resolved}",
    )
    return gap or f"No news found for {ticker}{resolved} between {start_date} and {end_date}"
