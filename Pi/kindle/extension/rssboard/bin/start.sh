#!/bin/sh
# 把常驻循环挂到后台，KUAL 菜单随即退出（menu.json 里 exitmenu: true）。
# 已经在跑就什么都不做——board.sh 自己有锁，重复拉只会互相抢屏。
. /mnt/us/extensions/rssboard/bin/common.sh

need_board || exit 0

pid=$(loop_pid)
if [ -n "$pid" ]; then
    echo "$(date '+%F %T') start: already running (pid $pid)" >> "$LOG"
    exit 0
fi

# 上一轮被 kill 之后锁文件可能还在，里面的 pid 已经不属于任何进程，留着会让
# 后面每次 start 都误判成「已经在跑」，所以这里必须清掉。
rm -f "$LOCK"

# nohup 在个别精简过的 busybox 上不存在：没有它就退回普通后台 + 忽略 HUP 的写法，
# 不能因为这一条把整个循环停掉。
if type nohup >/dev/null 2>&1; then
    nohup sh "$BOARD" >> "$LOG" 2>&1 &
else
    ( trap '' HUP; sh "$BOARD" >> "$LOG" 2>&1 & )
fi

# 给 board.sh 一点时间拿到锁再报状态：起来得太快反而会误报「没跑」
sleep 2
pid=$(loop_pid)
if [ -n "$pid" ]; then
    echo "$(date '+%F %T') start: pid $pid, url $(board_url)" >> "$LOG"
else
    # 起不来通常是 board.sh 缺、或 shell 直接报错；把 log 里的最后一行画到屏上
    eips -c
    eips 1 1 "RSS Board: loop did NOT start"
    eips 1 3 "$(tail -n 1 "$LOG" 2>/dev/null)"
    eips 1 5 "see /mnt/us/extensions/rssboard/log.txt"
fi
exit 0
