#!/bin/sh
# 把常驻循环挂到后台，KUAL 菜单随即退出（menu.json 里 exitmenu: true）。
# 已经在跑就什么都不做——board.sh 自己有锁，重复拉只会互相抢屏。
. /mnt/us/extensions/rssboard/bin/common.sh

need_board || exit 0

pid=$(loop_pid)
if [ -n "$pid" ]; then
    diag "start: already running (pid $pid)"
    exit 0
fi

# 上一轮被 kill 之后锁文件可能还在，里面的 pid 已经不属于任何进程，留着会让
# 后面每次 start 都误判成「已经在跑」，所以这里必须清掉。
rm -f "$LOCK"

# nohup 在个别精简过的 busybox 上不存在：没有它就退回普通后台 + 忽略 HUP 的写法，
# 不能因为这一条把整个循环停掉。
# 输出统一追加到诊断文件：board.sh 里 wget 的报错会原样留在这里，正是排查要看的。
if type nohup >/dev/null 2>&1; then
    nohup sh "$BOARD" >> "$DIAG" 2>&1 &
else
    ( trap '' HUP; sh "$BOARD" >> "$DIAG" 2>&1 & )
fi

# 给 board.sh 一点时间拿到锁再下结论：查得太快会误判成「没跑」
sleep 2
pid=$(loop_pid)
if [ -n "$pid" ]; then
    diag "start: ok, pid $pid, url=$(board_url)"
    eips -c
    eips 1 1 "RSS Board: loop started (pid $pid)"
else
    diag "start: FAILED to launch loop"
    eips -c
    eips 1 1 "RSS Board: loop did NOT start"
    eips 1 3 "see rssboard-diag.txt at Kindle root"
fi
exit 0
