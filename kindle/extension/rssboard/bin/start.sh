#!/bin/sh
# 把常驻循环挂到后台，KUAL 菜单随即退出（menu.json 里 exitmenu: true）。
# 已经在跑就什么都不做——board.sh 自己有锁，重复拉只会互相抢屏。
#
# **为什么要延迟 8 秒才启动**：KUAL 一退出，系统就把书库界面重画上去，会盖掉
# 我们刚画到屏上的那一帧；而 board.sh 是一启动就立刻画第一张。两者一前一后，
# 用户看到的就是「点了、回到主页、什么都没有」。把启动推后 8 秒（那时 KUAL 早已
# 退干净、书库也画完了），第一帧就正好落在最上层，能一直留着。
. /mnt/us/extensions/rssboard/bin/common.sh

need_board || exit 0

pid=$(loop_pid)
if [ -n "$pid" ]; then
    diag "start: already running (pid $pid)"
    eips -c
    eips 1 1 "RSS Board: already running (pid $pid)"
    exit 0
fi

# 上一轮被 kill 之后锁文件可能还在，里面的 pid 已经不属于任何进程，留着会让
# 后面每次 start 都误判成「已经在跑」，所以这里必须清掉。
rm -f "$LOCK"

# nohup 在个别精简过的 busybox 上不存在：没有它就退回普通后台 + 忽略 HUP 的写法，
# 不能因为这一条把整个循环停掉。
# 输出统一追加到诊断文件：board.sh 里 wget 的报错会原样留在这里，正是排查要看的。
bg() {
    if type nohup >/dev/null 2>&1; then
        nohup "$@" >> "$DIAG" 2>&1 &
    else
        ( trap '' HUP; "$@" >> "$DIAG" 2>&1 & )
    fi
}

bg /bin/sh -c "sleep 8; exec /bin/sh $BOARD"

# 此刻循环还没起（要等 8 秒），所以这里不能拿锁判成败；只告诉用户「稍等」
diag "start: scheduled, first frame in ~8s, url=$(board_url)"
eips -c
eips 1 1 "RSS Board: starting, first frame in ~8s"
eips 1 3 "board.png appears at Kindle root"
exit 0
