#!/bin/sh
# 把常驻循环挂到后台，KUAL 菜单随即退出（menu.json 里 exitmenu: true）。
# 已经在跑就什么都不做——board.sh 自己有锁，重复拉只会互相抢屏。
. /mnt/us/extensions/rssboard/bin/common.sh

if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    echo "$(date '+%F %T') start: already running (pid $(cat "$LOCK"))" >> "$LOG"
    exit 0
fi

# 上一轮被 kill 之后锁文件可能还在，里面的 pid 已经不属于任何进程，留着会让
# 后面每次 start 都误判成「已经在跑」，所以这里必须清掉。
rm -f "$LOCK"

nohup sh "$BOARD" >> "$LOG" 2>&1 &
exit 0
