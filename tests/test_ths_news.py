"""同花顺 7x24 快讯 vendor: A-share-only routing, tag/name matching, window
trimming, coverage reporting, and integration with the router.

All HTTP is mocked, so these run without a network connection.
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors import ths_news

_CST = timezone(timedelta(hours=8))


def _day(offset: int) -> str:
    """YYYY-MM-DD for today in Beijing time, shifted by ``offset`` days."""
    return (datetime.now(_CST) + timedelta(days=offset)).strftime("%Y-%m-%d")


def _at(offset_days: float) -> datetime:
    return datetime.now(_CST) + timedelta(days=offset_days)


def _raw(title, when, digest="", codes=(), link="https://news.10jqka.com.cn/x.shtml"):
    return {
        "title": title,
        "digest": digest,
        "url": link,
        "ctime": str(int(when.timestamp())),
        "stock": [{"stockCode": code, "name": "stub"} for code in codes],
    }


def _wire(monkeypatch, items, name="金安国纪"):
    """Serve ``items`` as page 1 and end the wire on page 2."""
    monkeypatch.setattr(ths_news, "_fetch_page", lambda page: items if page == 1 else [])
    monkeypatch.setattr(ths_news, "company_name", lambda code: name)


# --- symbol handling ---------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "ticker,code",
    [
        ("002636.SZ", "002636"),
        ("600519.SS", "600519"),
        ("600519.SH", "600519"),
        ("SZ002636", "002636"),
        ("002636", "002636"),
    ],
)
def test_a_share_symbols_resolve_to_their_code(ticker, code):
    assert ths_news.a_share_code(ticker) == code


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["AAPL", "0700.HK", "GC=F", "^GSPC", "BTC-USD", ""])
def test_other_instruments_are_not_a_shares(ticker):
    assert ths_news.a_share_code(ticker) is None


@pytest.mark.unit
def test_non_a_share_raises_no_data_so_the_chain_moves_on():
    with pytest.raises(NoMarketDataError):
        ths_news.get_news_ths("AAPL", _day(-7), _day(0))


# --- matching and windowing --------------------------------------------------

@pytest.mark.unit
def test_tagged_items_are_served_and_untagged_ones_only_by_company_name(monkeypatch):
    _wire(
        monkeypatch,
        [
            _raw("金安国纪：前三季度净利润同比增长", _at(-2), codes=["002636"]),
            _raw("元件板块走高，金安国纪涨停", _at(-3), codes=["300274"]),
            _raw("元件板块走高，金安国纪涨停", _at(-4)),
            _raw("无关公司：发布业绩预告", _at(-5), codes=["300274"]),
        ],
    )

    out = ths_news.get_news_ths("002636.SZ", _day(-7), _day(0))

    assert out.startswith("## 002636.SZ News")
    assert "前三季度净利润同比增长" in out
    assert "无关公司" not in out
    # Two of the three 金安国纪 items are named that way: one by tag, one by name.
    assert out.count("元件板块走高") == 2
    assert out.count("Link: https://news.10jqka.com.cn/x.shtml") == 3


@pytest.mark.unit
def test_items_outside_the_window_are_not_served(monkeypatch):
    _wire(
        monkeypatch,
        [
            _raw("金安国纪：今天的公告", _at(-1), codes=["002636"]),
            _raw("金安国纪：更早的公告", _at(-10), codes=["002636"]),
            # Published after the analysis date: a backtest must not see it.
            _raw("金安国纪：分析日之后的公告", _at(0) + timedelta(days=1), codes=["002636"]),
        ],
    )

    out = ths_news.get_news_ths("002636.SZ", _day(-3), _day(0))

    assert "今天的公告" in out
    assert "更早的公告" not in out
    assert "分析日之后的公告" not in out


@pytest.mark.unit
def test_feed_is_trimmed_to_the_configured_article_limit(monkeypatch):
    _wire(
        monkeypatch,
        [_raw(f"金安国纪：第{i}条", _at(-1), codes=["002636"]) for i in range(30)],
    )
    set_config({"news_article_limit": 5})

    out = ths_news.get_news_ths("002636.SZ", _day(-7), _day(0))

    assert out.count("### ") == 5


@pytest.mark.unit
def test_a_covered_window_with_no_news_is_reported_as_no_news(monkeypatch):
    # The wire reaches back ten days, well past the window's first day.
    _wire(monkeypatch, [_raw("无关公司：公告", _at(-10), codes=["300274"])])

    out = ths_news.get_news_ths("002636.SZ", _day(-5), _day(-1))

    assert out == "No news found for 002636.SZ between {} and {}".format(_day(-5), _day(-1))


@pytest.mark.unit
def test_a_window_past_the_wire_is_unavailable_not_empty(monkeypatch):
    # The wire only serves the last couple of days, so the window was never
    # observed: "no news" would claim an absence nobody saw.
    _wire(monkeypatch, [_raw("无关公司：公告", _at(-2), codes=["300274"])])

    out = ths_news.get_news_ths("002636.SZ", _day(-30), _day(-20))

    assert out.startswith("<同花顺 7x24 快讯 unavailable for")
    assert "not an absence of news for 002636.SZ" in out


@pytest.mark.unit
def test_a_failing_wire_raises_unavailable_for_the_next_vendor(monkeypatch):
    from tradingagents.dataflows.errors import VendorUnavailableError

    def boom(page):
        raise VendorUnavailableError("blocked")

    monkeypatch.setattr(ths_news, "_fetch_page", boom)

    with pytest.raises(VendorUnavailableError):
        ths_news.get_news_ths("002636.SZ", _day(-7), _day(0))


# --- router integration ------------------------------------------------------

@pytest.mark.unit
def test_router_leads_with_ths_news_for_an_a_share(monkeypatch):
    _wire(monkeypatch, [_raw("金安国纪：公告", _at(-1), codes=["002636"])])
    yahoo = mock.Mock()
    with mock.patch.dict(router.VENDOR_METHODS["get_news"], {"yfinance": yahoo}):
        set_config({"data_vendors": {"news_data": "ths_news,yfinance"}})
        out = router.route_to_vendor("get_news", "002636.SZ", _day(-7), _day(0))

    assert "金安国纪：公告" in out
    yahoo.assert_not_called()


@pytest.mark.unit
def test_router_falls_through_to_yfinance_for_a_us_listing():
    yahoo = mock.Mock(return_value="## AAPL News")
    with mock.patch.dict(router.VENDOR_METHODS["get_news"], {"yfinance": yahoo}):
        set_config({"data_vendors": {"news_data": "ths_news,yfinance"}})
        out = router.route_to_vendor("get_news", "AAPL", _day(-7), _day(0))

    assert out == "## AAPL News"
    yahoo.assert_called_once()


@pytest.mark.unit
def test_default_config_leads_the_news_chain_with_ths_news():
    chain = default_config.DEFAULT_CONFIG["data_vendors"]["news_data"]
    assert chain.split(",")[0] == "ths_news"
    assert "yfinance" in chain
