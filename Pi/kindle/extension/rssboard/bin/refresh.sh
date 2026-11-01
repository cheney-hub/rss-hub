#!/bin/sh
# 拉一张最新图立刻上屏。用来确认「图床 → Kindle」这段链路是通的，
# 不碰常驻循环（就算循环正在跑也能点，画面会被这一帧覆盖到下一轮为止）。
. /mnt/us/extensions/rssboard/bin/common.sh

need_board || exit 0

echo "$(date '+%F %T') refresh: $(board_url)" >> "$LOG"
sh "$BOARD" --once
rc=$?

# board.sh 自身失败时不写屏（它可能会保留上一张图），只在屏上留一行提示，
# 真正的细节在 log.txt 里 —— eips 能放的字太少，塞不下 wget 的报错。
if [ "$rc" -ne 0 ]; then
    eips -c
    eips 1 1 "RSS Board: fetch failed (rc=$rc)"
    eips 1 3 "url: $(board_url)"
    eips 1 5 "see /mnt/us/extensions/rssboard/log.txt"
fi
exit 0
