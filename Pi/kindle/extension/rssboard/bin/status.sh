#!/bin/sh
# 状态写屏：循环在不在、board.sh 在不在、图多大、用的是哪个地址、日志最后一行。
# 常驻循环跑着的时候，这一屏最多活 INTERVAL（默认 5 分钟）就会被下一帧盖掉。
. /mnt/us/extensions/rssboard/bin/common.sh

pid=$(loop_pid)
if [ -n "$pid" ]; then
    running="loop: running (pid $pid)"
else
    running="loop: NOT running"
fi

if [ -f "$BOARD" ]; then
    board="board.sh: ok"
else
    board="board.sh: MISSING  <- extract zip to root"
fi

if [ -s "$IMG" ]; then
    picture="image: $(wc -c < "$IMG" | tr -d ' ') bytes"
    when=$(stat -c %y "$IMG" 2>/dev/null | cut -c1-16)
    [ -n "$when" ] && picture="$picture   $when"
else
    picture="image: none yet"
fi

eips -c
eips 1 1 "RSS Board status"
eips 1 3 "$running"
eips 1 4 "$board"
eips 1 5 "$picture"
eips 1 7 "url: $(board_url)"
eips 1 9 "log: $(tail -n 1 "$LOG" 2>/dev/null)"
exit 0
