#!/bin/sh
# 停掉常驻循环。看板图留在屏幕上不会被抹掉，只是不再更新。
. /mnt/us/extensions/rssboard/bin/common.sh

if [ -f "$LOCK" ]; then
    PID=$(cat "$LOCK" 2>/dev/null)
    kill "$PID" 2>/dev/null
fi
rm -f "$LOCK"
exit 0
