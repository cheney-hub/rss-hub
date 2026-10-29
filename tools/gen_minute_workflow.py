#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 .github/workflows/minute.yml（分钟级发布工作流）。

为什么不用 cron 直接每分钟触发：
  GitHub Actions 的 schedule 最小间隔是 5 分钟，且官方明确「高负载时会延迟甚至丢档」，
  实测新仓库 50 分钟内 0 次触发。所以改成「每小时触发一次，作业内自己循环 55 分钟」，
  每轮：采集 → 上传 artifact → 部署 Pages → 对齐到下一整分。

为什么用 artifact 部署而不是 commit：
  Pages 传统构建（从分支发布）有 10 次/小时软限制，每分钟一次会被限流丢弃；
  官方文档明确「用自定义 GitHub Actions 工作流构建并发布时不受该限制」。

用法：python tools/gen_minute_workflow.py [轮数]
"""

import io
import os
import sys

CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 55
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, ".github", "workflows", "minute.yml")

HEAD = """name: RSS 分钟级发布

# 本文件由 tools/gen_minute_workflow.py 生成，改结构请改脚本后重新生成。
#
# 节奏：北京时间 06:00–23:59 每整点 :07 触发一次，作业内连续跑 {cycles} 分钟，
#   每分钟一轮（UTC 22:00–15:59）。北京时间 00:00–06:00 交给 nightly.yml，每小时一次。
#   每轮 = 采集 43 源 → 打包 docs/ → 部署到 Pages → 对齐到下一整分
# 为什么这么绕：GitHub 的 cron 最短 5 分钟且会丢档，只有「小时级 cron + 作业内循环」
#   才能做到稳定的 1 分钟。Pages 传统构建有 10 次/小时限制，改用 artifact 部署后不受限。
# 备份：每 10 轮把 docs/feed.xml 提交一次（每天约 110 条），历史不会爆掉。

on:
  schedule:
    # 只在北京时间 06:00–23:59 跑分钟级（UTC 22:00–15:59，跨天写成两段）
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
    timeout-minutes: 70
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
          SCREEN_TITLE_WIDTH: "13"
          SCREEN_ITEMS: "60"
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

      # 文本 API：一屏 4 条（标题 1 行 + 正文 3 行）。没配密钥时自动跳过，
      # 仓库本身不含任何凭证；密钥走仓库 Secret。
      - name: 轮 {n:02d} 推送墨水屏卡片
        env:
          DOT_API_KEY: ${{{{ secrets.DOT_API_KEY }}}}
          DOT_DEVICE_ID: ${{{{ secrets.DOT_DEVICE_ID }}}}
          DOT_TASK_KEY: ${{{{ secrets.DOT_TASK_KEY }}}}
        run: |
          if [ -z "$DOT_API_KEY" ] || [ -z "$DOT_DEVICE_ID" ]; then
            echo "未配置 DOT_API_KEY / DOT_DEVICE_ID，跳过文本卡片（RSS 卡片照常）"
            exit 0
          fi
          python push_text.py || echo "文本卡片推送失败，不影响 feed 发布"

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

      - name: 轮 {n:02d} 对齐到下一整分
        run: sleep $(( 60 - $(date +%s) % 60 ))
"""


def main():
    parts = [HEAD.format(cycles=CYCLES)]
    for n in range(1, CYCLES + 1):
        parts.append(CYCLE_TPL.format(n=n))
    text = "".join(parts)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("已生成 %s：%d 轮 / %d 行 / %.1f KB"
          % (OUT, CYCLES, text.count("\n") + 1, len(text.encode()) / 1024))


if __name__ == "__main__":
    main()
