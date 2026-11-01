#!/bin/sh
# 状态：把当前状态同时「写文件 + 写屏」。
# 文件是 E:\rssboard-diag.txt（主要看这个），屏上只有五行摘要，方便当场瞄一眼。
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
    board="board.sh: MISSING"
fi

if [ -s "$IMG" ]; then
    picture="image: $(wc -c < "$IMG" | tr -d ' ') bytes"
    when=$(stat -c %y "$IMG" 2>/dev/null | cut -c1-16)
    [ -n "$when" ] && picture="$picture   $when"
else
    picture="image: none yet"
fi

diag "status: $running | $board | $picture | url=$(board_url)"

eips -c
eips 1 1 "RSS Board status"
eips 1 3 "$running"
eips 1 4 "$board"
eips 1 5 "$picture"
eips 1 7 "url: $(board_url)"
exit 0
