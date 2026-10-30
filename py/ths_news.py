#!/usr/bin/env python3
"""
同花顺 7x24 实时快讯爬取工具
从 https://news.10jqka.com.cn/realtimenews.html 逐页拉取，直到无新数据，按关键词筛选

用法:
    python ths_news.py 金山办公          # 搜全部快讯（加载到底）
    python ths_news.py 600396 -d 7      # 只看最近 7 天的

原理:
    1. 调 tapp/news/push/stock API（页面点击「加载更多」实际调用的接口）
    2. 逐页拉取 → seq 去重 → 追加
    3. 直到 API 返回空 or 全页都是重复
    4. 关键词筛选 → 输出
"""

import sys
import os
import json
import re
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# ═══════════════════════════════════════
DAYS       = 7
MAX_PAGES  = 200          # 最大翻页次数（安全上限）
PAGE_SIZE  = 200          # 每次加载条数
TIMEOUT    = 12
DELAY      = 0.3

# 实际 API（页面「点击加载更多」底层使用的接口）
API_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"

# ═══════════════════════════════════════
#  文件缓存：按日期存为 cache/YYYYMMDD.json
#  若一次爬取中读到了该日期及后一天的数据，则该日期标记为 final
# ═══════════════════════════════════════
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")

def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_path(date_str):
    return os.path.join(CACHE_DIR, f"{date_str}.json")

def _load_final_caches():
    """加载所有已标记为 final 的缓存文件 → {date_str: [items]}"""
    _ensure_cache_dir()
    caches = {}
    for fname in os.listdir(CACHE_DIR):
        if fname.endswith('.json') and len(fname) == 13:       # YYYYMMDD.json
            date_str = fname[:8]
            try:
                with open(os.path.join(CACHE_DIR, fname), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('final'):
                    caches[date_str] = data.get('items', [])
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
    return caches

def _save_cache(date_str, items, is_final):
    """保存某日期的快讯到缓存文件"""
    _ensure_cache_dir()
    # 只保留必要字段，ctime 存为整数方便回读
    slim = []
    for it in items:
        slim.append({
            "seq":     it.get("seq", ""),
            "title":   it.get("title", ""),
            "content": it.get("content", ""),
            "ctime":   int(it["time"].timestamp()) if isinstance(it.get("time"), datetime) else it.get("ctime", 0),
            "link":    it.get("link", ""),
        })
    with open(_cache_path(date_str), 'w', encoding='utf-8') as f:
        json.dump({"final": is_final, "items": slim}, f, ensure_ascii=False)

_ctx = None
def _ssl():
    global _ctx
    if _ctx is None:
        _ctx = ssl.create_default_context()
        _ctx.check_hostname = False
        _ctx.verify_mode = ssl.CERT_NONE
    return _ctx

def _h(ref="https://news.10jqka.com.cn/realtimenews.html"):
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": ref,
    }

def _get_json(url):
    """GET JSON API，返回解析后的 dict"""
    req = urllib.request.Request(url, headers=_h())
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl()) as r:
        raw = r.read()
    return json.loads(raw.decode('utf-8'))

# ═══════════════════════════════════════
#  核心：逐页调用 API，模拟「点击加载更多」到底
# ═══════════════════════════════════════
def fetch_all(time_filter=False, days=0):
    """
    逐页调用实时快讯 API，直到无新数据；优先使用文件缓存。
    返回 list[dict]，按时间倒序。
    """
    if time_filter:
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) if days == 0 else datetime.now() - timedelta(days=days)
    else:
        cutoff = None
    all_items = []
    seen_seq = set()

    # ── 1. 加载已 final 的缓存 ──
    final_caches = _load_final_caches()
    final_dates = set(final_caches.keys())

    if final_dates:
        for date_str, items in final_caches.items():
            for it in items:
                seq = it.get("seq", "")
                if seq in seen_seq:
                    continue
                ctime = datetime.fromtimestamp(int(it["ctime"]))
                if cutoff and ctime < cutoff:
                    continue
                seen_seq.add(seq)
                all_items.append({
                    "title":   it.get("title", ""),
                    "content": it.get("content", ""),
                    "time":    ctime,
                    "link":    it.get("link", ""),
                    "seq":     seq,
                    "source":  "实时快讯",
                })
        print(f"  [缓存] 加载 {len(final_dates)} 个最终日期 → {len(all_items)} 条")

    # ── 2. 判断是否全部目标日期都已缓存 → 跳过爬取 ──
    if time_filter and final_dates:
        all_cached = True
        for d in range(max(days, 1)):
            dstr = (datetime.now() - timedelta(days=d)).strftime("%Y%m%d")
            if dstr not in final_dates:
                all_cached = False
                break
        if all_cached:
            print(f"  [缓存] 目标日期全部已缓存，跳过爬取")
            all_items.sort(key=lambda x: x["time"], reverse=True)
            return all_items

    # ── 3. 逐页爬取 ──
    print(f"  [实时快讯] 逐页抓取（模拟点击'加载更多'）...")
    new_by_date = {}          # date_str → [items]  本次新抓到的
    all_dates_seen = set(final_dates)  # 本次会话见过的所有日期

    for page in range(1, MAX_PAGES + 1):
        time.sleep(DELAY)
        url = f"{API_URL}?page={page}&tag=&track=website&pagesize={PAGE_SIZE}"
        try:
            data = _get_json(url)
        except Exception as e:
            print(f"  [实时快讯] 第{page}页请求失败: {e}")
            break

        if data.get("code") != "200":
            print(f"  [实时快讯] 第{page}页 API 返回异常 code={data.get('code')}")
            break

        lst = data.get("data", {}).get("list", [])
        if not lst:
            print(f"  [实时快讯] 第{page}页无数据 → 已到底")
            break

        before = len(seen_seq)
        for item in lst:
            seq = item.get("seq", "")
            if seq in seen_seq:
                continue
            ctime = datetime.fromtimestamp(int(item["ctime"]))
            date_str = ctime.strftime("%Y%m%d")
            all_dates_seen.add(date_str)

            # 如果该日期已 final 缓存 → 跳过（已经加载过了）
            if date_str in final_dates:
                continue

            if cutoff and ctime < cutoff:
                continue

            seen_seq.add(seq)
            new_it = {
                "title":   item.get("title", ""),
                "content": item.get("digest", "") or item.get("short", ""),
                "time":    ctime,
                "link":    item.get("url", ""),
                "seq":     seq,
                "source":  "实时快讯",
            }
            all_items.append(new_it)
            new_by_date.setdefault(date_str, []).append(new_it)

        new = len(seen_seq) - before
        print(f"  [实时快讯] 第{page}页 +{new} (累计 {len(all_items)} 条)")

        if new == 0:
            print(f"  [实时快讯] 全页重复 → 已到底")
            break

    # ── 4. 保存新数据到缓存，标记 final ──
    for date_str, items in new_by_date.items():
        next_day = (datetime.strptime(date_str, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
        is_final = next_day in all_dates_seen
        _save_cache(date_str, items, is_final)
        status = "✓ 最终" if is_final else "○ 待更新"
        print(f"  [缓存] {date_str} {status} ({len(items)} 条)")

    all_items.sort(key=lambda x: x["time"], reverse=True)
    return all_items

# ═══════════════════════════════════════
def filter_items(items, keyword):
    if not keyword: return items
    kws = [k.strip() for k in re.split(r'[,，\s]+', keyword) if k.strip()]
    return [i for i in items if all(kw in (i["title"]+" "+i.get("content","")) for kw in kws)]

def print_results(items, keyword, days=None):
    tag = "今天" if days == 0 else (f"最近 {days} 天" if days else "加载到底 · 全部")
    print(f"\n{'='*70}")
    print(f"  同花顺实时快讯  ·  \"{keyword}\"  ·  {tag}  ·  {len(items)} 条")
    print(f"{'='*70}\n")
    if not items:
        print("  ⚠️  未找到匹配快讯\n"); return
    for i, item in enumerate(items, 1):
        print(f"  [{i:3d}] {item['time'].strftime('%Y-%m-%d %H:%M')}  {item.get('source','')}")
        print(f"        {item['title'][:120]}")
        c = item.get("content","")
        if c and c != item["title"]:
            print(f"        {c[:200]}")
        if item.get("link"):
            print(f"        🔗 {item['link']}")
        print()
    print(f"{'='*70}\n  共 {len(items)} 条\n")

def main():
    keyword, days = None, DAYS
    args = sys.argv[1:]; i = 0
    while i < len(args):
        if args[i] in ("-d","--days"):
            i += 1
            if i < len(args): days = int(args[i])
        elif args[i] in ("-h","--help"):
            print(__doc__); return
        else: keyword = args[i]
        i += 1
    if not keyword:
        keyword = input("股票代码/名称/关键词 (留空=全部): ").strip()
    keyword = keyword.strip() if keyword else ""

    use_filter = "-d" in sys.argv or "--days" in sys.argv
    tag = f"(最近 {days} 天)" if use_filter else "(下滑到底)"
    print(f"\n🔍 同花顺快讯: \"{keyword}\" {tag}")
    print("="*50)
    raw = fetch_all(time_filter=use_filter, days=days if use_filter else 0)
    print("="*50)
    print(f"[总计] 原始 {len(raw)} 条\n")
    matched = filter_items(raw, keyword)
    print_results(matched, keyword, days if use_filter else None)

if __name__ == "__main__":
    main()
