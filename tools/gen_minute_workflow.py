#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 .github/workflows/minute.yml（5 分钟节拍发布工作流）。

为什么不用 cron 直接每 5 分钟触发：
  GitHub Actions 的 schedule 最小间隔虽是 5 分钟，但官方明确「高负载时会延迟甚至丢档」，
  实测新仓库 50 分钟内 0 次触发。所以改成「每小时触发一次，作业内自己循环 12 轮」，
  每轮：采集 → 打包 docs/ → 部署 Pages → 对齐到下一个 5 分钟整点。

为什么用 artifact 部署而不是 commit：
  Pages 传统构建（从分支发布）有 10 次/小时软限制，5 分钟一次虽然不会超，
  但官方文档明确「用自定义 GitHub Actions 工作流构建并发布时不受该限制」，artifact 更稳。

节拍与轮播的关系：
  设备 5 分钟刷新一次，后台也 5 分钟一轮。轮播游标（docs/rotate.json）用「时间窗」判断，
  同一窗内重复构建不推进，所以重跑、手动触发、作业重叠都不会让屏幕跳条。

用法：python tools/gen_minute_workflow.py [轮数]
"""

import io
import os
import sys

PERIOD = 300                                   # 每轮节拍（秒）= 设备刷新周期
CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 13
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, ".github", "workflows", "minute.yml")

HEAD = """name: RSS 五分钟节拍发布

# 本文件由 tools/gen_minute_workflow.py 生成，改结构请改脚本后重新生成。
#
# 节奏：北京时间 06:00–23:59（UTC 22,23,0-15）每整点 :07 触发一次，作业内连续跑 {cycles} 轮，
#   每轮 5 分钟；北京时间 00:00–06:00 交给 nightly.yml，每小时一次。
#   :07 起跑 → 各轮对齐到 :10 :15 … :55 :00 :05，跑完约 58 分钟，等下一个 :07。
#   每轮 = 采集 49 源 × 5 条 → 打包 docs/ → 部署到 Pages → 对齐到下一个 5 分钟整点
# 为什么这么绕：GitHub 的 cron 最短 5 分钟且会丢档，只有「小时级 cron + 作业内循环」
#   才能稳定。5 分钟节拍也让 Pages 部署降到 216 次/天（原来 1 分钟节拍是 990 次）。
# 轮播：设备版 feed-wrap.xml 每轮只发布「当前该显示的那一条」，游标记在 docs/rotate.json，
#   随 Pages 一起发布；同一时间窗内重复构建不推进，所以多跑几轮也不会跳条。
# 备份：每 10 轮把 docs/feed.xml 提交一次（每天约 26 条），历史不会爆掉。

on:
  schedule:
    # 只在北京时间 06:00–23:59 跑 5 分钟节拍（UTC 22:00–15:59，跨天写成两段）
    # 北京时间 00:00–06:00 由 nightly.yml 接管，每小时一次，两个时段互补不重叠
    - cron: "7 22,23,0-15 * * *"
  workflow_dispatch:

concurrency:
  # 与 nightly.yml 同组：Pages 同一时刻只允许一个部署。
  # cancel-in-progress: true 保证每个整点都重新对齐——万一上一次被排过队、拖过了整点，
  # 这一次直接接管，触发时刻不会一路后移累积。
  group: rss-pages
  cancel-in-progress: true

permissions:
  contents: write
  pages: write
  id-token: write

jobs:
  publish:
    runs-on: ubuntu-latest
    timeout-minutes: 80
    environment:
      name: github-pages
      url: ${{{{ steps.d01.outputs.page_url }}}}
    steps:
      - name: 检出仓库
        uses: actions/checkout@v4

      - name: 准备 Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
"""

CYCLE_TPL = """
      # ------------------------- 第 {n:02d} 轮 -------------------------
      - name: 轮 {n:02d} 采集
        env:
          REFRESH_SECONDS: "0"
          FEED_URL: "https://cheney-hub.github.io/rss-hub/feed.xml"
          CONCURRENCY: "12"
          FETCH_TIMEOUT: "20"
          SCREEN_LINE_WIDTH: "13"
          SCREEN_BODY_MAX: "90"
          FEED_BODY_MAX: "0"
          PER_SOURCE: "5"
          ITEM_LIMIT: "0"
          ROTATE_SECONDS: "{period}"
          ROTATE_POOL_MAX: "120"
          CYCLE: "{n}"

        run: |
          python build_feed.py || echo "本轮采集异常，沿用上一轮 feed"
          if [ $(( CYCLE % 10 )) -eq 1 ]; then
            git config user.name  "rss-hub-bot"
            git config user.email "rss-hub-bot@users.noreply.github.com"
            git add docs/feed.xml
            if git diff --cached --quiet; then
              echo "内容无变化，跳过备份提交"
            else
              git commit -q -m "feed: $(date -u '+%Y-%m-%d %H:%M UTC') 备份"
              git pull --rebase --autostash -q origin main || true
              git push -q origin HEAD:main || echo "备份推送失败，不影响发布"
            fi
          fi

      # 静态页（index.html / screen.html）始终取远端最新版本：
      # 作业是整点 checkout 的，这期间推送的新页面进不来，
      # 不拉一下就会被这份旧产物覆盖，表现为新页面 404。
      - name: 轮 {n:02d} 同步静态页
        run: |
          git fetch -q origin main || true
          git checkout -q origin/main -- docs/index.html docs/screen.html 2>/dev/null || true

      - uses: actions/upload-pages-artifact@v3
        with:
          name: site-{n:02d}
          path: docs
          retention-days: 1

      - id: d{n:02d}
        uses: actions/deploy-pages@v4
        with:
          artifact_name: site-{n:02d}

      - name: 轮 {n:02d} 对齐到下一个 5 分钟整点
        run: sleep $(( {period} - $(date +%s) % {period} ))
"""


def main():
    parts = [HEAD.format(cycles=CYCLES)]
    for n in range(1, CYCLES + 1):
        parts.append(CYCLE_TPL.format(n=n, period=PERIOD))
    text = "".join(parts)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("已生成 %s：%d 轮 × %d 秒 / %d 行 / %.1f KB"
          % (OUT, CYCLES, PERIOD, text.count("\n") + 1, len(text.encode()) / 1024))


if __name__ == "__main__":
    main()
