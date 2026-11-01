#!/bin/sh
# 拉一张最新图立刻上屏。用来确认「图床 → Kindle」这段链路是通的，
# 不碰常驻循环（就算循环正在跑也能点，画面会被这一帧覆盖到下一轮为止）。
# 无论成败都往 rssboard-diag.txt 留一行，拔线插电脑就能看到发生了什么。
. /mnt/us/extensions/rssboard/bin/common.sh

need_board || exit 0

diag "refresh: start, url=$(board_url)"
sh "$BOARD" --once
rc=$?
diag "refresh: board.sh --once rc=$rc"

# board.sh 失败时不擦屏（它可能特意保留了上一张图），只在屏上留一行指路
if [ "$rc" -ne 0 ]; then
    eips -c
    eips 1 1 "RSS Board: fetch failed (rc=$rc)"
    eips 1 3 "see rssboard-diag.txt at Kindle root"
fi
exit 0
