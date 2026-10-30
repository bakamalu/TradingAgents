"""
龙虎榜股票分析工具
获取上一交易日龙虎榜沪深主板股票，按涨跌幅排序展示（含市盈率）

用法:
    python lhb_analysis.py              # 分析上一交易日龙虎榜
    python lhb_analysis.py 2026-05-14   # 分析指定日期
"""

import sys
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import time


# ── 配置 ──────────────────────────────────────────
MAX_WORKERS = 10        # 并发请求数
REQUEST_TIMEOUT = 10    # 单次请求超时（秒）
SORT_BY = "pct"         # 排序字段: "pct"(涨跌幅) / "pe"(市盈率) / "code"(代码)
SORT_ORDER = "desc"     # "desc"(降序) / "asc"(升序)


def get_last_trading_day() -> datetime:
    """获取上一交易日（跳过周末）"""
    today = datetime.now()
    # 往前推直到非周末
    d = today - timedelta(days=1)
    while d.weekday() >= 5:  # 5=周六, 6=周日
        d -= timedelta(days=1)
    return d


def get_target_date() -> str:
    """获取目标日期（默认上一交易日）"""
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return date_str
        print(f"[警告] 日期格式错误 '{date_str}'，使用默认上一交易日")
    return get_last_trading_day().strftime("%Y-%m-%d")


def is_main_board(code: str) -> bool:
    """判断是否为沪深主板股票"""
    code = code.strip().zfill(6)
    # 上海主板: 600-609
    if code[:3] in ("600", "601", "603", "605"):
        return True
    # 深圳主板: 000-004 (排除 300/301 创业板)
    if code[:3] in ("000", "001", "002", "003", "004"):
        return True
    # 上海 B 股
    if code.startswith("900"):
        return True
    return False


def is_shanghai(code: str) -> bool:
    """判断是否沪市"""
    code = code.strip().zfill(6)
    return code.startswith(("6", "9"))


def fetch_lhb_codes(target_date: str) -> list[str]:
    """从新浪财经获取指定日期的龙虎榜股票代码"""
    codes_set = set()
    for page in range(1, 6):  # 最多查 5 页
        url = (
            "https://vip.stock.finance.sina.com.cn/q/go.php/"
            f"vInvestConsult/kind/lhb/index.phtml?p={page}"
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                html = resp.read().decode("gbk")
        except Exception as e:
            print(f"[新浪] 第 {page} 页请求失败: {e}")
            break

        # 提取股票代码（链接中的 6 位数字）
        found = re.findall(r'<a[^>]*>(\d{6})</a>', html)
        if not found:
            break
        codes_set.update(found)

    return sorted(codes_set)


def fetch_stock_info(code: str) -> dict | None:
    """通过 push2his 获取股票行情信息（含 PE、涨跌幅、换手率、市值）"""
    market = "1" if is_shanghai(code) else "0"
    secid = f"{market}.{code}"

    fields = "f43,f57,f58,f162,f169,f170,f168,f116,f117"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/get"
        f"?secid={secid}&fields={fields}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None

    d = data.get("data")
    if not d:
        return None

    name = d.get("f58", "--")
    price = d.get("f43", 0) / 100 if d.get("f43") else 0
    pe = d.get("f162", 0) / 100 if d.get("f162") else 0
    change = d.get("f169", 0) / 100 if d.get("f169") else 0
    pct = d.get("f170", 0) / 100 if d.get("f170") else 0
    turnover = d.get("f168", 0) / 100 if d.get("f168") else 0
    total_value = d.get("f116", 0)
    circ_value = d.get("f117", 0)

    return {
        "code": code,
        "name": name,
        "price": price,
        "change": change,
        "pct": pct,
        "pe": pe,
        "turnover": turnover,
        "total_value": total_value,
        "circ_value": circ_value,
    }


def fetch_all_stock_info(codes: list[str]) -> list[dict]:
    """并发获取所有股票的行情信息"""
    results = []
    total = len(codes)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_stock_info, c): c for c in codes}
        for i, future in enumerate(as_completed(futures), 1):
            info = future.result()
            if info:
                results.append(info)
            if i % 10 == 0 or i == total:
                print(f"\r  查询进度: {i}/{total}", end="", flush=True)
    print()  # newline
    return results


# ── 显示 ──────────────────────────────────────────

def format_money(val: float) -> str:
    """金额格式化"""
    if not val:
        return "  --"
    if val >= 1e12:
        return f"{val/1e12:.2f}万亿"
    elif val >= 1e8:
        return f"{val/1e8:.2f}亿"
    elif val >= 1e4:
        return f"{val/1e4:.2f}万"
    else:
        return f"{val:.2f}"


def color_pct(pct: float) -> str:
    """涨跌幅着色"""
    if pct > 0:
        return f"\033[31m{pct:+.2f}%\033[0m"
    elif pct < 0:
        return f"\033[32m{pct:+.2f}%\033[0m"
    else:
        return f"{pct:+.2f}%"


def print_table(results: list[dict], target_date: str):
    """打印结果表格"""
    # 排序
    if SORT_BY == "pe":
        key = lambda x: x["pe"] or 0
    elif SORT_BY == "code":
        key = lambda x: x["code"]
    else:
        key = lambda x: x["pct"]
    reverse = SORT_ORDER == "desc"
    results.sort(key=key, reverse=reverse)

    up_count = sum(1 for r in results if r["pct"] > 0)
    down_count = sum(1 for r in results if r["pct"] < 0)
    flat_count = sum(1 for r in results if r["pct"] == 0)

    print()
    print("=" * 100)
    print(f"  龙虎榜沪深主板分析 ── {target_date}")
    print(f"  共 {len(results)} 只股票 | 🔺{up_count} 🔻{down_count} ➖{flat_count}")
    print("=" * 100)
    print(f"  {'代码':<8s} {'名称':<8s} {'现价':>7s} {'涨跌幅':>9s} {'换手':>6s} {'市盈率':>7s} {'总市值':>10s}")
    print("  " + "-" * 94)

    for r in results:
        pe_str = f"{r['pe']:.2f}" if r["pe"] else "  --"
        turnover_str = f"{r['turnover']:.1f}%" if r["turnover"] else "  --"
        print(
            f"  {r['code']:<8s} "
            f"{r['name']:<8s} "
            f"{r['price']:>7.2f} "
            f"{color_pct(r['pct']):>15s} "
            f"{turnover_str:>6s} "
            f"{pe_str:>7s} "
            f"{format_money(r['total_value']):>10s}"
        )

    print("=" * 100)
    print(f"  排序: {SORT_BY} ({'降序' if SORT_ORDER == 'desc' else '升序'})")
    print(f"  数据源: 新浪财经(龙虎榜) + 东方财富(行情)")
    print(f"  查询时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def main():
    target_date = get_target_date()
    print(f"[龙虎榜] 目标日期: {target_date}")
    print(f"[龙虎榜] 筛选条件: 沪深主板 (600xxx/000xxx/002xxx)")

    # 1. 获取龙虎榜代码
    print("[龙虎榜] 正在获取龙虎榜数据...")
    start = time.time()
    all_codes = fetch_lhb_codes(target_date)
    if not all_codes:
        print("[错误] 未获取到龙虎榜数据，请检查日期是否正确")
        return

    # 2. 筛选沪深主板
    filtered = [c for c in all_codes if is_main_board(c)]
    print(f"[筛选] 龙虎榜 {len(all_codes)} 只 → 沪深主板 {len(filtered)} 只")

    if not filtered:
        print("[提示] 当日龙虎榜无沪深主板股票")
        return

    # 3. 获取行情数据
    print("[行情] 正在获取行情数据（涨跌幅 & 市盈率）...")
    results = fetch_all_stock_info(filtered)

    if not results:
        print("[错误] 未获取到行情数据")
        return

    # 4. 展示
    elapsed = time.time() - start
    print_table(results, target_date)
    print(f"\n  耗时: {elapsed:.1f}s")


if __name__ == "__main__":
    main()