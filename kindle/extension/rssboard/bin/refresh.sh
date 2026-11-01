#!/bin/sh
# 拉一张最新图立刻上屏。用来确认「图床 → Kindle」这段链路是通的，
# 不碰常驻循环（就算循环正在跑也能点，画面会被这一帧覆盖到下一轮为止）。
# 无论成败都往 rssboard-diag.txt 留一行，拔线插电脑就能看到发生了什么。
#
# 一个小尾巴：KUAL 一退出，系统会把书库重画上去，盖掉我刚画的那帧。所以抓完图
# 之后再挂一个 6 秒后的「重画一次」——那时书库已经画完，这一帧留得住。
. /mnt/us/extensions/rssboard/bin/common.sh

need_board || exit 0

diag "refresh: start, url=$(board_url)"
# stdout/stderr 必须接进 $DIAG：KUAL 不显示脚本输出（见 common.sh 第 1 条），
# 而 wget 的报错原文是 board.sh 用 echo 打到 stdout 的 —— 不接的话，失败时这里
# 只剩两行 refresh: start / rc=N，看不出是 TLS、DNS 还是 404。这个菜单项恰恰是
# 文档里指定用来确认链路的那一招，断了它等于断了排障主路径。
/bin/sh "$BOARD" --once >> "$DIAG" 2>&1
rc=$?
diag "refresh: board.sh --once rc=$rc"

if [ "$rc" -eq 0 ]; then
    # 重画一次，抢在书库重绘之后上屏；失败也无声无息，不影响主流程
    ( sleep 6; eips -c; eips -g "$IMG" ) >/dev/null 2>&1 &
else
    eips -c
    eips 1 1 "RSS Board: fetch failed (rc=$rc)"
    eips 1 3 "see rssboard-diag.txt at Kindle root"
fi
exit 0
