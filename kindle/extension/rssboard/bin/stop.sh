#!/bin/sh
# 停掉常驻循环。看板图留在屏幕上不会被抹掉，只是不再更新。
#
# **必须顺手把 preventScreenSaver 关掉**：board.sh 启动时把它设成了 1（不让屏保
# 来抢屏），如果 stop 不还原，设备就再也不会自动休眠 —— 你以为停掉了，其实一直在
# 耗电（2026-11-01 发现）。这里用 0 还原，和 board.sh 那边一一对应。
. /mnt/us/extensions/rssboard/bin/common.sh

if [ -f "$LOCK" ]; then
    PID=$(cat "$LOCK" 2>/dev/null)
    kill "$PID" 2>/dev/null
fi
rm -f "$LOCK"

lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
diag "stop: loop stopped, preventScreenSaver released"
exit 0
