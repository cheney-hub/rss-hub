#!/bin/sh
# 状态写屏：循环在不在、当前这张图什么时候拉的、最后一次日志说了什么。
# 注意常驻循环跑着的时候，这一屏最多活 INTERVAL（默认 5 分钟）就会被下一帧盖掉。
. /mnt/us/extensions/rssboard/bin/common.sh

if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    running="loop running, pid $(cat "$LOCK")"
else
    running="loop not running"
fi

if [ -s "$IMG" ]; then
    picture="image $(date -r "$IMG" '+%F %T') $(wc -c < "$IMG" | tr -d ' ') bytes"
else
    picture="no image yet"
fi

eips -c
eips 1 1 "RSS board status"
eips 1 3 "$running"
eips 1 4 "$picture"
eips 1 6 "$(tail -n 1 "$LOG" 2>/dev/null)"
exit 0
