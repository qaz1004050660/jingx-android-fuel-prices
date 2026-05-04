#!/usr/bin/env python3
"""
update_fuel_prices.py — 每月自动抓取北京 92# 油价并更新 fuel-prices.json

工作流:
  1. 从 qiyoujiage.com 抓北京 92# 当前价(无需 key,公开页面)
  2. 读本地 fuel-prices.json(若不存在则建空骨架)
  3. 如果今日 effectiveDate 不在 entries 中 → append 新条目
  4. 输出 iOS-compatible schema 到 fuel-prices.json:
     {
       "version": 1,
       "generatedAt": "2026-05-04T12:00:00+08:00",
       "entries": [
         { "effectiveDate": "2026-05-03",
           "prices": { "北京": { "p92": 7.92, "p95": 8.42, "p98": 9.46, "p0": 7.70 } }
         }
       ]
     }
  5. workflow 之后会把变更 commit + push 回 repo,jsDelivr CDN 自动镜像

依赖:仅 Python 标准库,不需 requirements.txt。

为什么用 qiyoujiage.com 而不是 AKShare:
  iOS repo 用 AKShare energy_oil_detail() 当前返回的"最新"数据是 2022-05-17 老期(数据源 stale),
  qiyoujiage.com 实测每日同步发改委公告,2022-2026 稳定可靠。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import urllib.request

# ============================================================
#  配置
# ============================================================
SOURCE_URL = "http://www.qiyoujiage.com/beijing.shtml"  # http(站点 HTTPS 证书 hostname mismatch)
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
TIMEOUT = 15  # 秒

# 北京时区(发改委调价公告基于此)
BEIJING_TZ = timezone(timedelta(hours=8))

# 油号差价(对齐 FuelPriceTable.kt / FuelPriceTable.swift)
GRADE_DELTA_95 = 0.50
GRADE_DELTA_98 = 1.54
GRADE_DELTA_0 = -0.22


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_beijing_92_price(html: str) -> Optional[float]:
    """
    实测页面结构(2026-05 稳定):
        <dt>北京92#汽油</dt><dd>8.46</dd>
    """
    patterns = [
        r"<dt>\s*北京\s*92\s*[#号]?\s*汽油\s*</dt>\s*<dd>\s*(\d+\.\d{1,3})\s*</dd>",
        r"北京\s*92\s*[#号]?\s*汽油\s*</dt>\s*<dd>\s*(\d+\.\d{1,3})",
        r"92\s*[#号]?\s*汽油.{0,80}?(\d+\.\d{1,3})\s*元\s*/?\s*升",
    ]
    for pat in patterns:
        m = re.search(pat, html, flags=re.DOTALL)
        if m:
            try:
                p = float(m.group(1))
                # 物理边界(2022-2026 历史范围 5.5-12,留 ±10% 余量)
                if 4.5 <= p <= 14.0:
                    return p
            except ValueError:
                continue
    return None


def parse_beijing_adjustment_date(html: str) -> Optional[str]:
    """
    抽取页面里"最近调价日"作为 effectiveDate。
    qiyoujiage.com 页面顶部一般有"今日(YYYY年M月D日)油价"或"X月X日24时调价"。
    抽不到就用今天日期。
    """
    candidates = [
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"(\d{4})-(\d{1,2})-(\d{1,2})",
    ]
    for pat in candidates:
        m = re.search(pat, html)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                # 合理性检查:仅接受最近 90 天内的日期
                today = datetime.now(BEIJING_TZ).date()
                got = datetime(y, mo, d).date()
                if 0 <= (today - got).days <= 90:
                    return got.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="fuel-prices.json", help="输出文件路径")
    ap.add_argument("--dry-run", action="store_true", help="不写文件,只输出状态")
    ap.add_argument("--force", action="store_true", help="即使无变化也输出(刷 generatedAt)")
    args = ap.parse_args()

    out_path = Path(args.output)

    # 1. 抓最新价
    print("[1/4] fetching latest beijing 92# price...", file=sys.stderr)
    html = fetch_text(SOURCE_URL)
    new_p92 = parse_beijing_92_price(html)
    if new_p92 is None:
        print("[error] failed to parse price from qiyoujiage.com", file=sys.stderr)
        print("       HTML head 500 bytes:", file=sys.stderr)
        print(html[:500], file=sys.stderr)
        sys.exit(1)

    eff_date = parse_beijing_adjustment_date(html) or datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    print(f"       got: effectiveDate={eff_date}, p92={new_p92}", file=sys.stderr)

    # 2. 读本地 fuel-prices.json
    print(f"[2/4] reading local {out_path}...", file=sys.stderr)
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] local JSON parse failed: {e} — starting fresh", file=sys.stderr)
            existing = {"version": 1, "entries": []}
    else:
        existing = {"version": 1, "entries": []}

    entries: list[dict] = existing.get("entries", [])

    # 3. 决策:append / skip
    existing_dates = {e.get("effectiveDate") for e in entries}
    if eff_date in existing_dates:
        # 同 effectiveDate 已存在 → 检查 p92 是否一致
        for e in entries:
            if e.get("effectiveDate") == eff_date:
                old_p92 = e.get("prices", {}).get("北京", {}).get("p92")
                if old_p92 is not None and abs(old_p92 - new_p92) < 0.001:
                    print(f"[3/4] effectiveDate {eff_date} 已存在且 p92 一致 ({new_p92}),无变化", file=sys.stderr)
                    if not args.force:
                        sys.exit(0)
                else:
                    # 同日不同价(发改委极少出现):覆盖
                    e["prices"] = {"北京": _build_grades(new_p92)}
                    print(f"[3/4] same date 但 p92 改变: {old_p92} → {new_p92}", file=sys.stderr)
                break
    else:
        # 真正的新调价 → append
        entries.append({
            "effectiveDate": eff_date,
            "prices": {"北京": _build_grades(new_p92)}
        })
        # 按 effectiveDate 升序保持
        entries.sort(key=lambda x: x.get("effectiveDate", ""))
        print(f"[3/4] new entry appended: {eff_date} → p92 {new_p92}", file=sys.stderr)

    # 4. 元数据 + 写文件
    existing["version"] = 1
    existing["generatedAt"] = datetime.now(timezone.utc).isoformat()
    existing["entries"] = entries

    out_text = json.dumps(existing, ensure_ascii=False, indent=2)
    if args.dry_run:
        print(out_text)
    else:
        out_path.write_text(out_text + "\n", encoding="utf-8")
        print(f"[4/4] wrote {out_path}", file=sys.stderr)


def _build_grades(p92: float) -> dict:
    """从 p92 派生 95/98/0 价(对齐 FuelPriceTable 端的差价)。"""
    return {
        "p92": round(p92, 2),
        "p95": round(p92 + GRADE_DELTA_95, 2),
        "p98": round(p92 + GRADE_DELTA_98, 2),
        "p0": round(p92 + GRADE_DELTA_0, 2),
    }


if __name__ == "__main__":
    main()
