"""
中国A股实时行情查询工具
支持通过股票代码或股票名称查询实时行情数据
数据来源：东方财富 / 新浪财经

用法:
    python stock_query.py 600396              # 按代码查询
    python stock_query.py 600396 -d 5         # 按代码查询，同时显示近5日涨跌幅
    python stock_query.py 华电辽能            # 按名称查询
    python stock_query.py 华电辽能 -d 10      # 按名称查询，同时显示近10日涨跌幅
    python stock_query.py                    # 交互模式
"""

import sys
import json
import re
import time
import urllib.request
import urllib.parse
from datetime import datetime


# ── 配置 ──────────────────────────────────────────
# 数据源: "sina" = 新浪财经, "eastmoney" = 东方财富, "auto" = 东方财富优先+新浪兜底
SOURCE = "auto"

# 完整浏览器 UA（过短的 UA 容易被服务端识别并断连）
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def http_get(url: str, headers: dict | None = None, retries: int = 3,
             timeout: int = 10, encoding: str = "utf-8",
             fallback_hosts: tuple[str, ...] = ()) -> str:
    """带重试的 HTTP GET，返回响应文本。
    - 远端断连、超时等瞬时错误按指数退避自动重试（0.5s/1s/2s）
    - 指定 fallback_hosts 时，每轮依次用备用域名替换 host 重试
    """
    candidates = [url]
    if fallback_hosts:
        parsed = urllib.parse.urlparse(url)
        for alt in fallback_hosts:
            if parsed.hostname and parsed.hostname != alt:
                candidates.append(parsed._replace(netloc=alt).geturl())
    full_headers = {"User-Agent": BROWSER_UA}
    if headers:
        full_headers.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        for u in candidates:
            try:
                req = urllib.request.Request(u, headers=full_headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode(encoding)
            except Exception as e:
                last_err = e
        if attempt < retries - 1:
            time.sleep(0.5 * (2 ** attempt))
    assert last_err is not None
    raise last_err

# 东方财富市场映射
MARKET_MAP = {
    "sh": "1",  # 上海
    "sz": "0",  # 深圳
    "bj": "0",  # 北交所（归入深圳通道）
}


def get_secid(code: str) -> str:
    """根据 6 位代码推断市场并返回东方财富 secid"""
    code = code.strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"1.{code}"  # 上海
    elif code.startswith(("0", "3", "4", "8")):
        return f"0.{code}"  # 深圳 / 北交所
    else:
        return f"1.{code}"


def search_stock(keyword: str) -> list[dict]:
    """按关键词搜索股票，返回匹配列表 [{"code","name","market"}, ...]"""
    # 优先使用搜索建议 API（更稳定）
    url = (
        "https://searchadapter.eastmoney.com/api/suggest/get"
        f"?input={urllib.parse.quote(keyword)}&type=14"
    )
    try:
        raw = http_get(url, headers={"Referer": "https://www.eastmoney.com/"})
    except Exception as e:
        print(f"[错误] 搜索请求失败: {e}")
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("[错误] 搜索返回数据解析失败")
        return []

    results = []
    if isinstance(data, dict):
        records = data.get("QuotationCodeTable", {}).get("Data", [])
        if isinstance(records, list):
            for item in records:
                code = item.get("Code", "")
                name = item.get("Name", "")
                mkt = item.get("MktNum", "")
                if code and name:
                    results.append({"code": code, "name": name, "market": mkt})
    return results


def code_to_sina_symbol(code: str) -> str:
    """将 6 位代码转为新浪 symbol: sh600396 / sz000001 / bj430047"""
    code = code.strip().zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    elif code.startswith(("4", "8")):
        return f"bj{code}"
    else:
        return f"sz{code}"


def _fetch_kline_sina(secid: str, days: int) -> list[dict]:
    """新浪财经历史日线（push2his.eastmoney.com 不可达时的兜底数据源）。
    注意：无成交额/换手率字段（amount/amplitude/turnover 填 0），
    pct/change 由前收盘价推算，首条因无前收置 0。
    """
    _, code = secid.split(".")
    symbol = code_to_sina_symbol(code)
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/"
        "CN_MarketDataService.getKLineData"
        f"?symbol={symbol}&scale=240&ma=no&datalen={days + 5}"
    )
    try:
        raw = http_get(url, headers={"Referer": "https://finance.sina.com.cn"})
    except Exception as e:
        print(f"[K线数据] 新浪兜底请求失败: {e}")
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list) or not data:
        return []

    results = []
    prev_close = 0.0
    for d in data:
        close = float(d["close"])
        results.append({
            "date": d["day"],
            "open": float(d["open"]),
            "close": close,
            "high": float(d["high"]),
            "low": float(d["low"]),
            "volume": int(float(d["volume"])),
            "amount": 0.0,
            "amplitude": 0.0,
            "pct": round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0.0,
            "change": round(close - prev_close, 2) if prev_close > 0 else 0.0,
            "turnover": 0.0,
        })
        prev_close = close
    return results


def fetch_kline_history(secid: str, days: int) -> list[dict]:
    """
    通过东方财富 K 线 API 获取历史日线数据，失败时回退新浪财经。
    返回最近 `days` 个交易日的列表，每项包含 date/open/close/high/low/pct 等。
    """
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=1&end=20500101&lmt={days + 5}"
    )
    try:
        raw = http_get(url, headers={"Referer": "https://www.eastmoney.com/"})
    except Exception as e:
        print(f"[K线数据] 东方财富不可达（{e}），回退新浪...")
        return _fetch_kline_sina(secid, days)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("[K线数据] 解析失败")
        return []

    klines = payload.get("data", {}).get("klines", [])
    if not klines:
        return []

    results = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        results.append({
            "date": parts[0],
            "open": float(parts[1]) if parts[1] != "-" else 0,
            "close": float(parts[2]) if parts[2] != "-" else 0,
            "high": float(parts[3]) if parts[3] != "-" else 0,
            "low": float(parts[4]) if parts[4] != "-" else 0,
            "volume": int(float(parts[5])) if parts[5] != "-" else 0,
            "amount": float(parts[6]) if parts[6] != "-" else 0,
            "amplitude": float(parts[7]) if parts[7] != "-" else 0,
            "pct": float(parts[8]) if parts[8] != "-" else 0,
            "change": float(parts[9]) if parts[9] != "-" else 0,
            "turnover": float(parts[10]) if parts[10] != "-" else 0,
        })
    return results


def calc_lookback_change(secid: str, code: str, current_price: float, days: int,
                         klines: list[dict] | None = None) -> dict | None:
    """
    计算 N 个交易日前的收盘价及区间涨跌幅。
    返回 {"date", "close", "change", "pct", "daily": [...]} 或 None。
    daily 列表按日期升序（最旧->最新），每项含 date/close/pct。
    klines: 可选，调用方已获取的 K 线数据（升序），传入可避免重复请求。
    """
    if days <= 0:
        return None

    # 多拉一些数据确保覆盖交易日（含周末节假日）
    if klines is None:
        klines = fetch_kline_history(secid, days + 20)
    if len(klines) < days + 1:
        print(f"[提示] K线数据不足（需要{days+1}条，实际{len(klines)}条）")
        return None

    # klines 按日期升序（最旧在前），最后一条是最新交易日
    # 区间参考点为倒数第 days+1 条（即 days 个交易日前的收盘价）
    ref_idx = len(klines) - days - 1  # 从末尾往前数
    prev = klines[ref_idx]
    prev_close = prev["close"]

    if prev_close <= 0:
        return None

    change = current_price - prev_close
    pct = (change / prev_close) * 100

    # 区间内的每日明细：从 ref_idx 到末尾（不含最后一条今日数据，因为今日涨幅已在实时行情中显示）
    # 但最后一条是今天，K线中的 pct 是当日相对前日的涨幅，保留它
    daily_data = klines[ref_idx:]  # 升序：从 N 天前到今天

    return {
        "date": prev["date"],
        "close": prev_close,
        "change": round(change, 2),
        "pct": round(pct, 2),
        "daily": [
            {"date": d["date"], "close": d["close"], "pct": d["pct"]}
            for d in daily_data
        ],
    }


def fetch_quote_eastmoney(secid: str) -> dict | None:
    """通过东方财富 API 拉取实时行情（数据最全）"""
    fields = (
        "f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,"
        "f60,f116,f117,f162,f167,f168,f169,f170,f171"
    )
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields={fields}"
    )
    try:
        raw = http_get(url, headers={"Referer": "https://www.eastmoney.com/"},
                       fallback_hosts=("push2delay.eastmoney.com",))
    except Exception as e:
        print(f"[东方财富] 行情请求失败: {e}")
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("[东方财富] 行情数据解析失败")
        return None

    d = payload.get("data")
    if not d:
        return None

    return {
        "name": d.get("f58", "--"),
        "code": d.get("f57", "--"),
        "price": d.get("f43", 0) / 100 if d.get("f43") else 0,
        "high": d.get("f44", 0) / 100 if d.get("f44") else 0,
        "low": d.get("f45", 0) / 100 if d.get("f45") else 0,
        "open": d.get("f46", 0) / 100 if d.get("f46") else 0,
        "pre_close": d.get("f60", 0) / 100 if d.get("f60") else 0,
        "change": d.get("f169", 0) / 100 if d.get("f169") else 0,
        "pct": d.get("f170", 0) / 100 if d.get("f170") else 0,
        "volume": d.get("f47", 0),
        "amount": d.get("f48", 0),
        "turnover": d.get("f168", 0) / 100 if d.get("f168") else 0,
        "pe": d.get("f162", 0) / 100 if d.get("f162") else 0,
        "total_value": d.get("f116", 0),
        "circ_value": d.get("f117", 0),
        "limit_up": d.get("f51", 0) / 100 if d.get("f51") else 0,
        "limit_down": d.get("f52", 0) / 100 if d.get("f52") else 0,
        "source": "东方财富",
    }


def fetch_quote_sina(code: str) -> dict | None:
    """通过新浪财经 API 拉取实时行情（东方财富不可用时的备选）"""
    symbol = code_to_sina_symbol(code)
    url = f"https://hq.sinajs.cn/list={symbol}"
    try:
        raw = http_get(url, headers={"Referer": "https://finance.sina.com.cn"},
                       encoding="gbk")
    except Exception as e:
        print(f"[新浪财经] 行情请求失败: {e}")
        return None

    # 解析新浪返回格式: var hq_str_sh600396="名称,今开,昨收,现价,最高,最低,..."
    match = re.search(r'"([^"]*)"', raw)
    if not match:
        print("[新浪财经] 行情数据解析失败")
        return None

    parts = match.group(1).split(",")
    if len(parts) < 32:
        print("[新浪财经] 行情数据不完整")
        return None

    try:
        name = parts[0]
        open_price = float(parts[1]) if parts[1] else 0
        pre_close = float(parts[2]) if parts[2] else 0
        price = float(parts[3]) if parts[3] else 0
        high = float(parts[4]) if parts[4] else 0
        low = float(parts[5]) if parts[5] else 0
        volume = int(float(parts[8])) if parts[8] else 0  # 股
        amount = float(parts[9]) if parts[9] else 0       # 元
        date_str = parts[30] if len(parts) > 30 else ""
        time_str = parts[31] if len(parts) > 31 else ""
    except (ValueError, IndexError):
        print("[新浪财经] 行情数据数值转换失败")
        return None

    change = price - pre_close
    pct = (change / pre_close * 100) if pre_close else 0

    return {
        "name": name,
        "code": code,
        "price": price,
        "high": high,
        "low": low,
        "open": open_price,
        "pre_close": pre_close,
        "change": round(change, 2),
        "pct": round(pct, 2),
        "volume": volume // 100,  # 股 → 手
        "amount": amount,
        "turnover": 0,
        "pe": 0,
        "total_value": 0,
        "circ_value": 0,
        "limit_up": 0,
        "limit_down": 0,
        "source": "新浪财经",
    }


def fetch_quote(code_or_secid: str, *, code: str = "") -> dict | None:
    """
    统一行情入口，根据 SOURCE 配置选择数据源。
    - SOURCE="eastmoney": 只用东方财富
    - SOURCE="sina": 只用新浪财经
    - SOURCE="auto": 东方财富优先，失败则自动切换新浪
    """
    actual_code = code if code else code_or_secid

    if SOURCE == "sina":
        return fetch_quote_sina(actual_code)

    elif SOURCE == "eastmoney":
        return fetch_quote_eastmoney(code_or_secid)

    else:  # auto
        # 先尝试东方财富
        print("[数据源] 尝试东方财富...")
        result = fetch_quote_eastmoney(code_or_secid)
        if result:
            return result
        # 东方财富失败，切换新浪
        print("[数据源] 东方财富不可用，切换新浪财经...")
        return fetch_quote_sina(actual_code)


def format_money(val: float) -> str:
    """金额格式化"""
    if val >= 1e12:
        return f"{val/1e12:.2f}万亿"
    elif val >= 1e8:
        return f"{val/1e8:.2f}亿"
    elif val >= 1e4:
        return f"{val/1e4:.2f}万"
    else:
        return f"{val:.2f}"


def print_quote(q: dict, lookback: dict | None = None):
    """美化打印行情"""
    arrow = "🔺" if q["change"] > 0 else ("🔻" if q["change"] < 0 else "➖")
    color_pct = (
        "\033[31m" if q["pct"] > 0 else ("\033[32m" if q["pct"] < 0 else "")
    )
    reset = "\033[0m"

    print("=" * 52)
    print(f"  {q['name']}（{q['code']}）  ──  实时行情")
    print("=" * 52)
    print(f"  {'最新价':<8s}: {q['price']:.2f} 元")
    print(f"  {'涨跌额':<8s}: {color_pct}{q['change']:+.2f}{reset} 元")
    print(f"  {'涨跌幅':<8s}: {color_pct}{q['pct']:+.2f}%{reset}  {arrow}")

    # 多日涨跌幅
    if lookback:
        lb_arrow = "🔺" if lookback["pct"] > 0 else ("🔻" if lookback["pct"] < 0 else "➖")
        lb_color = "\033[31m" if lookback["pct"] > 0 else ("\033[32m" if lookback["pct"] < 0 else "")
        print(f"  {'区间涨跌':<8s}: {lb_color}{lookback['pct']:+.2f}%{reset}  {lb_arrow}")
        print(f"  {'区间基准':<8s}: {lookback['date']} 收盘价 {lookback['close']:.2f}")

        # 每日明细
        daily = lookback.get("daily", [])
        if daily:
            print("  " + "-" * 44)
            print(f"  {'日期':<12s} {'收盘':>8s}  {'涨幅':>8s}")
            print("  " + "-" * 44)
            for d in daily:
                d_arrow = "🔺" if d["pct"] > 0 else ("🔻" if d["pct"] < 0 else "➖")
                d_color = "\033[31m" if d["pct"] > 0 else ("\033[32m" if d["pct"] < 0 else "")
                print(f"  {d['date']:<12s} {d['close']:>8.2f}  {d_color}{d['pct']:>+7.2f}%{reset} {d_arrow}")

    print(f"  {'今开':<8s}: {q['open']:.2f}")
    print(f"  {'最高':<8s}: {q['high']:.2f}")
    print(f"  {'最低':<8s}: {q['low']:.2f}")
    print(f"  {'昨收':<8s}: {q['pre_close']:.2f}")
    if q.get("limit_up"):
        print(f"  {'涨停价':<8s}: {q['limit_up']:.2f}")
    if q.get("limit_down"):
        print(f"  {'跌停价':<8s}: {q['limit_down']:.2f}")
    print(f"  {'成交量':<8s}: {q['volume']:,} 手")
    print(f"  {'成交额':<8s}: {format_money(q['amount'])}")
    if q.get("turnover"):
        print(f"  {'换手率':<8s}: {q['turnover']:.2f}%")
    if q.get("pe"):
        print(f"  {'市盈率':<8s}: {q['pe']:.2f}")
    if q.get("total_value"):
        print(f"  {'总市值':<8s}: {format_money(q['total_value'])}")
    print(f"  {'数据源':<8s}: {q.get('source', '--')}")
    print(f"  {'查询时间':<8s}: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 52)


def query_by_code(code: str, lookback_days: int = 0):
    """按代码查询"""
    secid = get_secid(code)
    q = fetch_quote(secid, code=code)
    if q:
        lookback = None
        if lookback_days > 0:
            lookback = calc_lookback_change(secid, code, q["price"], lookback_days)
        print_quote(q, lookback)


def query_by_name(name: str, lookback_days: int = 0):
    """按名称模糊搜索并查询"""
    results = search_stock(name)
    if not results:
        print(f"[提示] 未找到与 \"{name}\" 相关的股票")
        return

    if len(results) == 1:
        # 只有一个结果，直接查询
        item = results[0]
        print(f"[匹配] {item['name']}（{item['code']}）")
        query_by_code(item["code"], lookback_days)
    else:
        # 多个结果，让用户选择
        print(f"\n找到 {len(results)} 个匹配的股票：")
        for i, item in enumerate(results, 1):
            mkt = "沪" if item["code"].startswith("6") else ("深" if item["code"].startswith(("0", "3")) else "京")
            print(f"  [{i}] {item['name']:<8s}  {item['code']}  ({mkt})")

        try:
            choice = input("\n请选择序号（默认 1）: ").strip()
            idx = int(choice) - 1 if choice else 0
            if 0 <= idx < len(results):
                item = results[idx]
                query_by_code(item["code"], lookback_days)
            else:
                print("[错误] 无效选择")
        except (ValueError, EOFError):
            print("[取消]")


def interactive_mode():
    """交互式查询"""
    print("=" * 52)
    print("  中国 A 股实时行情查询工具")
    print("  输入股票代码（6 位）或股票名称进行查询")
    print("  输入 q / quit / exit 退出")
    print("=" * 52)

    while True:
        try:
            inp = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not inp:
            continue
        if inp.lower() in ("q", "quit", "exit"):
            print("再见！")
            break

        # 判断是代码还是名称：6位纯数字 = 代码
        if re.fullmatch(r"\d{6}", inp):
            query_by_code(inp)
        else:
            query_by_name(inp)


def main():
    args = sys.argv[1:]

    # 解析 -d 参数
    lookback_days = 0
    clean_args = []
    i = 0
    while i < len(args):
        if args[i] == "-d" and i + 1 < len(args):
            try:
                lookback_days = int(args[i + 1])
                if lookback_days <= 0:
                    print("[警告] -d 参数必须为正整数，已忽略")
                    lookback_days = 0
            except ValueError:
                print(f"[警告] -d 参数格式错误: {args[i+1]}，已忽略")
            i += 2
        else:
            clean_args.append(args[i])
            i += 1

    if not clean_args:
        interactive_mode()
        return

    query = " ".join(clean_args)

    # 提取可能的纯数字代码
    codes = re.findall(r"\b(\d{6})\b", query)
    if codes:
        query_by_code(codes[0], lookback_days)
    else:
        query_by_name(query, lookback_days)


if __name__ == "__main__":
    main()