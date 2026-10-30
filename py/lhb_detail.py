"""
龙虎榜股票详细分析工具
结合 lhb_analysis.py 的龙虎榜代码筛选 + query_single.py 的 -d 参数多日分析
对每只龙虎榜沪深主板股票，输出详细行情 + 近N日K线明细到文件

用法:
    python lhb_detail.py                    # 上一交易日龙虎榜，默认回看5日
    python lhb_detail.py -d 10              # 指定回看10日
    python lhb_detail.py 2026-05-14 -d 10   # 指定日期 + 回看10日

输出:
    lhb_detail_YYYY-MM-DD.txt   （文本报告）
    lhb_detail_YYYY-MM-DD.csv   （CSV 汇总）
"""

import sys
import os
import re
import json
import urllib.request
import urllib.parse
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# 导入 query_single 的分析函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from query_single import (
    get_secid,
    fetch_kline_history,
    calc_lookback_change,
    fetch_quote_eastmoney,
    format_money,
    http_get,
)


# ── 配置 ──────────────────────────────────────────
MAX_WORKERS = 3         # 并发请求数（过高会触发东方财富限流断连）
REQUEST_TIMEOUT = 10    # 单次请求超时（秒）
DEFAULT_LOOKBACK = 5    # 默认回看天数
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════
# 龙虎榜部分（来自 lhb_analysis.py）
# ══════════════════════════════════════════════════

def get_last_trading_day() -> datetime:
    """获取上一交易日（跳过周末）"""
    today = datetime.now()
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def is_main_board(code: str) -> bool:
    """判断是否为沪深主板股票"""
    code = code.strip().zfill(6)
    if code[:3] in ("600", "601", "603", "605"):
        return True
    if code[:3] in ("000", "001", "002", "003", "004"):
        return True
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
    for page in range(1, 6):
        url = (
            "https://vip.stock.finance.sina.com.cn/q/go.php/"
            f"vInvestConsult/kind/lhb/index.phtml?p={page}"
        )
        try:
            html = http_get(url, timeout=REQUEST_TIMEOUT, encoding="gbk")
        except Exception:
            break

        found = re.findall(r'<a[^>]*>(\d{6})</a>', html)
        if not found:
            break
        codes_set.update(found)

    return sorted(codes_set)


# ══════════════════════════════════════════════════
# 分析核心
# ══════════════════════════════════════════════════

def analyze_stock(code: str, lookback_days: int) -> dict | None:
    """
    对单只股票进行详细分析：
    - 实时行情
    - 近 N 日 K 线及区间涨跌幅
    返回分析结果字典，失败返回 None
    """
    secid = get_secid(code)

    # 1. 获取实时行情
    quote = fetch_quote_eastmoney(secid)
    if not quote:
        return None

    # 2. 获取近 N 日 K 线（一次请求，同时供回看计算与明细输出使用）
    lookback = None
    klines_raw = []
    if lookback_days > 0:
        klines_raw = fetch_kline_history(secid, lookback_days + 20)
        lookback = calc_lookback_change(
            secid, code, quote["price"], lookback_days, klines=klines_raw
        )
        # 只保留最近 lookback_days+1 条（含当日）
        if len(klines_raw) > lookback_days + 1:
            klines_raw = klines_raw[-(lookback_days + 1):]

    return {
        "quote": quote,
        "lookback": lookback,
        "klines": klines_raw,
    }


def build_report(results: list[dict], target_date: str, lookback_days: int) -> str:
    """
    生成文本报告
    results: [{"code", "analysis": analyze_stock结果}, ...]
    """
    lines = []
    sep = "=" * 80
    sub_sep = "-" * 80

    lines.append(sep)
    lines.append(f"  龙虎榜沪深主板详细分析报告")
    lines.append(f"  日期: {target_date}    回看: {lookback_days} 日")
    lines.append(f"  共 {len(results)} 只股票")
    lines.append(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(sep)

    for i, item in enumerate(results, 1):
        code = item["code"]
        analysis = item["analysis"]
        if analysis is None:
            lines.append(f"\n  [{i}] {code}  ── 数据获取失败，跳过\n")
            continue

        q = analysis["quote"]
        lb = analysis["lookback"]

        arrow = "🔺" if q["change"] > 0 else ("🔻" if q["change"] < 0 else "➖")

        lines.append(f"\n  [{i}] {q['name']}（{code}）{arrow}")
        lines.append(sub_sep)
        lines.append(f"    最新价    : {q['price']:.2f} 元")
        lines.append(f"    涨跌额    : {q['change']:+.2f} 元")
        lines.append(f"    涨跌幅    : {q['pct']:+.2f}%")
        lines.append(f"    今开/昨收 : {q['open']:.2f} / {q['pre_close']:.2f}")
        lines.append(f"    最高/最低 : {q['high']:.2f} / {q['low']:.2f}")
        lines.append(f"    换手率    : {q['turnover']:.2f}%")
        lines.append(f"    市盈率    : {q['pe']:.2f}" if q["pe"] else "    市盈率    : --")
        lines.append(f"    总市值    : {format_money(q['total_value'])}")
        lines.append(f"    流通市值  : {format_money(q['circ_value'])}")
        if q.get("limit_up"):
            lines.append(f"    涨停价    : {q['limit_up']:.2f}")
        if q.get("limit_down"):
            lines.append(f"    跌停价    : {q['limit_down']:.2f}")

        # 回看 N 日
        if lb:
            lb_arrow = "🔺" if lb["pct"] > 0 else ("🔻" if lb["pct"] < 0 else "➖")
            lines.append(f"    ── 近 {lookback_days} 日回看 ──")
            lines.append(f"    区间基准  : {lb['date']} 收盘 {lb['close']:.2f}")
            lines.append(f"    区间涨跌  : {lb['change']:+.2f} 元")
            lines.append(f"    区间涨幅  : {lb['pct']:+.2f}%  {lb_arrow}")

            daily = lb.get("daily", [])
            if daily:
                lines.append(f"    {'日期':<12s} {'收盘':>8s}  {'涨幅':>8s}")
                lines.append(f"    " + "-" * 34)
                for d in daily:
                    d_arrow = "🔺" if d["pct"] > 0 else ("🔻" if d["pct"] < 0 else "➖")
                    lines.append(
                        f"    {d['date']:<12s} {d['close']:>8.2f}  {d['pct']:>+7.2f}% {d_arrow}"
                    )

    # 汇总统计
    up_count = sum(1 for r in results if r["analysis"] and r["analysis"]["quote"]["pct"] > 0)
    down_count = sum(1 for r in results if r["analysis"] and r["analysis"]["quote"]["pct"] < 0)
    flat_count = sum(1 for r in results if r["analysis"] and r["analysis"]["quote"]["pct"] == 0)

    lines.append(f"\n{sep}")
    lines.append(f"  汇总: 🔺{up_count}  🔻{down_count}  ➖{flat_count}")
    lines.append(sep)

    return "\n".join(lines)


def build_csv(results: list[dict], target_date: str, lookback_days: int) -> str:
    """
    生成 CSV 汇总
    列: 代码,名称,现价,涨跌幅,换手率,市盈率,总市值,近N日涨幅
    """
    header = f"代码,名称,现价,涨跌幅,换手率,市盈率,总市值,近{lookback_days}日涨幅"
    rows = [header]

    for item in results:
        analysis = item["analysis"]
        if analysis is None:
            continue
        q = analysis["quote"]
        lb = analysis["lookback"]
        lb_pct = f"{lb['pct']:.2f}" if lb else ""

        pe_str = f"{q['pe']:.2f}" if q["pe"] else ""
        rows.append(
            f"{item['code']},{q['name']},{q['price']:.2f},{q['pct']:+.2f}%,"
            f"{q['turnover']:.2f}%,{pe_str},{format_money(q['total_value'])},{lb_pct}%"
        )

    return "\n".join(rows)


def parse_args():
    """解析命令行参数，返回 (target_date, lookback_days)"""
    target_date = get_last_trading_day().strftime("%Y-%m-%d")
    lookback_days = DEFAULT_LOOKBACK

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "-d" and i + 1 < len(args):
            try:
                lookback_days = int(args[i + 1])
                if lookback_days <= 0:
                    print(f"[警告] -d 参数必须为正整数，使用默认 {DEFAULT_LOOKBACK}")
                    lookback_days = DEFAULT_LOOKBACK
            except ValueError:
                print(f"[警告] -d 参数格式错误: {args[i+1]}，使用默认 {DEFAULT_LOOKBACK}")
            i += 2
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", args[i]):
            target_date = args[i]
            i += 1
        else:
            print(f"[警告] 未知参数: {args[i]}，已忽略")
            i += 1

    return target_date, lookback_days


def main():
    start_time = time.time()
    target_date, lookback_days = parse_args()

    print(f"[龙虎榜详细分析]")
    print(f"  目标日期 : {target_date}")
    print(f"  回看天数 : {lookback_days}")
    print(f"  筛选条件 : 沪深主板")

    # 1. 获取龙虎榜代码
    print("\n[1/3] 获取龙虎榜数据...")
    all_codes = fetch_lhb_codes(target_date)
    if not all_codes:
        print("[错误] 未获取到龙虎榜数据，请检查日期是否正确")
        return

    filtered = [c for c in all_codes if is_main_board(c)]
    print(f"  龙虎榜 {len(all_codes)} 只 → 沪深主板 {len(filtered)} 只")

    if not filtered:
        print("[提示] 当日龙虎榜无沪深主板股票")
        return

    # 2. 并发获取详细数据
    print(f"\n[2/3] 获取详细行情 + 近{lookback_days}日K线...")
    results = []
    total = len(filtered)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_stock, code, lookback_days): code
            for code in filtered
        }
        for i, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                analysis = future.result()
            except Exception as e:
                print(f"\n  [{i}/{total}] {code} 分析异常: {e}")
                analysis = None
            results.append({"code": code, "analysis": analysis})
            if i % 5 == 0 or i == total:
                print(f"\r  进度: {i}/{total}", end="", flush=True)
    print()

    valid = sum(1 for r in results if r["analysis"] is not None)
    print(f"  成功: {valid}/{total}")

    # 3. 生成输出文件
    print("\n[3/3] 生成报告文件...")

    # 按涨跌幅排序
    def sort_key(item):
        a = item["analysis"]
        return a["quote"]["pct"] if a else -999
    results.sort(key=sort_key, reverse=True)

    # 文本报告
    txt_path = os.path.join(OUTPUT_DIR, f"lhb_detail_{target_date}.txt")
    report = build_report(results, target_date, lookback_days)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  ✓ 文本报告: {txt_path}")

    # CSV 汇总
    csv_path = os.path.join(OUTPUT_DIR, f"lhb_detail_{target_date}.csv")
    csv_content = build_csv(results, target_date, lookback_days)
    with open(csv_path, "w", encoding="utf-8-sig") as f:  # utf-8-sig 让 Excel 正确识别中文
        f.write(csv_content)
    print(f"  ✓ CSV 汇总 : {csv_path}")

    elapsed = time.time() - start_time
    print(f"\n  总耗时: {elapsed:.1f}s")


if __name__ == "__main__":
    main()