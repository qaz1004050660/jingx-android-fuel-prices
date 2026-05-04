# JingX Android 油价自动化

JingX 控制器 Android App 的油价自动更新数据源(iOS 用平行 repo `bmw-china-fuel-prices`)。

## 工作原理

```
GitHub Actions cron (周二/四/六 01:00 UTC = 北京 09:00)
  → Python 抓 qiyoujiage.com 北京 92# 当前价
  → merge 到 fuel-prices.json (按 effectiveDate 幂等去重)
  → git push 回 main 分支
  → jsDelivr CDN 自动镜像 (cdn.jsdelivr.net/gh/...)
  → Android App 启动时拉取(7 天最小间隔节流)
  → FuelPriceUpdater.checkAndApply() → FuelPriceTable.applyRemoteEntries()
```

## JSON Schema(与 iOS feed 对齐)

```json
{
  "version": 1,
  "generatedAt": "2026-05-04T08:00:00+00:00",
  "entries": [
    {
      "effectiveDate": "2026-04-09",
      "prices": {
        "北京": { "p92": 7.85, "p95": 8.35, "p98": 9.39, "p0": 7.63 }
      }
    }
  ]
}
```

> 当前 Android repo 只抓北京 92# 一个数据点(95/98/0 由稳定差价派生)。
> 各省价差由 App 端 `FuelPriceTable.PROVINCE_DIFF` 多年统计矩阵处理(精度 ±2-3%)。
> 未来扩展可改 Python 脚本同时抓 31 个省的页面。

## 为什么用 qiyoujiage.com 而不是 AKShare

- iOS repo 用 AKShare `energy_oil_detail()`,但实测当前返回 2022-05-17 老期(数据源 stale,upstream 问题)
- qiyoujiage.com 是面向消费者的油价网站,**每日同步发改委公告**,2022-2026 实测稳定
- 解析 HTML `<dt>北京92#汽油</dt><dd>价格</dd>` 结构,正则匹配,无需 SDK

## 部署步骤

### 1. 一次性创建 GitHub repo
```bash
cd /Users/abaodeji/Desktop/控制器+灯控/油价自动化
git init
git add .
git commit -m "initial: android fuel price automation"
gh repo create qaz1004050660/jingx-android-fuel-prices --public --source=. --push
```

### 2. 触发首次运行
```bash
gh workflow run update-fuel-prices.yml --repo qaz1004050660/jingx-android-fuel-prices
gh run watch --repo qaz1004050660/jingx-android-fuel-prices
```

或在 Actions tab → "Update fuel prices" → "Run workflow" 手动触发。

### 3. 验证
```bash
# 等 5-10 秒让 jsDelivr 镜像
curl https://cdn.jsdelivr.net/gh/qaz1004050660/jingx-android-fuel-prices@main/fuel-prices.json
```

## 手动运行(本地测试)

```bash
cd 油价自动化
python3 update_fuel_prices.py --dry-run         # 干跑只输出 stdout
python3 update_fuel_prices.py -o fuel-prices.json   # 写文件
python3 update_fuel_prices.py --force           # 即使无变化也输出(刷 generatedAt)
```

## App 端如何用上新油价

1. App 启动时(隐私同意后)调 `FuelPriceUpdater.checkAndApply(this)`
2. 7 天最小间隔(避免每次启动打公网)
3. 拉到 fuel-prices.json → 解析 entries[].北京.p92 → `FuelPriceTable.applyRemoteEntries`
4. 后续 `FuelPriceTable.priceForProvince` 查询时自动合并 BEIJING_BASE + remoteOverlay

新油价**当月**就能在所有用户的 App 里生效(用户启动 App 拉到即时刷新)。

## 与 iOS repo 的关系

| repo | 用途 | 数据源 | cron 频率 |
|------|------|--------|-----------|
| `bmw-china-fuel-prices` | iOS 端 feed | AKShare(31 省 × 4 油号) | 周二/四/六 09:00 |
| `jingx-android-fuel-prices` | Android 端 feed | qiyoujiage.com(北京 92#) | 周二/四/六 09:00 |

两个 repo 是冗余的 — 数据源不同,任何一个挂了另一个还能用。Android client 默认拉自己的 repo,
回归更稳定的发改委同步源。

## 失败兜底

- Python 脚本 `parse_beijing_92_price` 失败 → exit 1 → workflow 失败 → 不推空文件覆盖好数据
- workflow 推不上去 → 老 fuel-prices.json 留在 main → jsDelivr 继续 serve 老版本
- App 端 `FuelPriceUpdater` 拉取失败 → 静默用 SP 缓存 + 本地 BEIJING_BASE 兜底
- 任意一层挂掉,App 不会崩,UI 显示精度退化 ±3-5%
