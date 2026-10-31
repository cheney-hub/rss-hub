#!/bin/sh
# 拉一张最新图立刻上屏。用来确认「Pi 的图床 → Kindle」这段链路是通的，
# 不碰常驻循环（就算循环正在跑也能点，画面会被这一帧覆盖到下一轮为止）。
. /mnt/us/extensions/rssboard/bin/common.sh

sh "$BOARD" --once
exit 0
