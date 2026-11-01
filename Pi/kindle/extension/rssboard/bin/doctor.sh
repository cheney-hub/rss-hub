#!/bin/sh
# 自检：把「能不能出图」拆成四段挨个验，结果直接画在屏上——不用连电脑也能看出卡在哪。
#
# 每一行都是 PASS / FAIL，四段依次是：
#   1 文件齐不齐（board.sh）
#   2 有没有 wget
#   3 网络能不能真的取到那张图（这一步最慢，超时 15 秒）
#   4 常驻循环在不在跑
. /mnt/us/extensions/rssboard/bin/common.sh

if [ -f "$BOARD" ]; then
    f1="PASS"
else
    f1="FAIL  (no /mnt/us/board.sh)"
fi

if type wget >/dev/null 2>&1; then
    f2="PASS"
else
    f2="FAIL  (no wget)"
fi

url=$(board_url)
f3="SKIP  (no url)"
if [ -n "$url" ] && type wget >/dev/null 2>&1; then
    if wget -q -T 15 -O /tmp/rssboard-diag.png "$url"; then
        f3="PASS  ($(wc -c < /tmp/rssboard-diag.png | tr -d ' ') bytes)"
    else
        f3="FAIL  (see log.txt)"
    fi
    rm -f /tmp/rssboard-diag.png
fi

pid=$(loop_pid)
if [ -n "$pid" ]; then
    f4="PASS  (pid $pid)"
else
    f4="not running"
fi

eips -c
eips 1 1 "RSS Board doctor"
eips 1 3 "1 board.sh : $f1"
eips 1 4 "2 wget     : $f2"
eips 1 5 "3 fetch    : $f3"
eips 1 6 "4 loop     : $f4"
eips 1 8 "url: $url"
eips 1 10 "log: $(tail -n 1 "$LOG" 2>/dev/null)"
exit 0
