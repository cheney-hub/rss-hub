#!/bin/sh
# 自检：把「能不能出图」拆成四段挨个验，结果写成**文本文件**放在 Kindle 根目录：
#   E:\rssboard-diag.txt        （拔线插电脑直接打开，不用 SSH、不用看屏）
# 屏上只留一行结论 —— eips 的点阵字体只有 ASCII、塞不下细节，屏也不是给人读长文用的。
#
# 四段依次是：
#   1 board.sh 在不在
#   2 有没有 wget
#   3 网络能不能真的取到那张图（最慢，超时 15 秒）
#   4 常驻循环在不在跑
#
# 文件是「新报告在最前、旧内容接在后面」的，每跑一次自动留档，不会把上次的线索冲掉。
. /mnt/us/extensions/rssboard/bin/common.sh

URL=$(board_url)
ERR=/tmp/rssboard-wget.err
TMP=/tmp/rssboard-diag.png
OLD=/tmp/rssboard-diag.old
rm -f "$TMP" "$ERR" "$OLD"

# ---- 1 board.sh ----
if [ -f "$BOARD" ]; then c1="OK"; else c1="MISSING"; fi

# ---- 2 wget ----
if type wget >/dev/null 2>&1; then
    c2="OK ($(command -v wget 2>/dev/null))"
else
    c2="MISSING"
fi

# ---- 3 真的取一次图（这一步顺带验证 DNS、TLS、Cloudflare 三层）----
c3="SKIP (no url)"
detail=""
if [ -n "$URL" ] && [ "${c2#OK}" != "$c2" ]; then
    if wget -T 15 -O "$TMP" "$URL" 2>"$ERR"; then
        c3="OK ($(wc -c < "$TMP" | tr -d ' ') bytes)"
    else
        c3="FAIL"
        detail=$(tail -n 2 "$ERR" 2>/dev/null | tr -d '\r' | cut -c1-160)
    fi
fi
rm -f "$TMP" "$ERR"

# ---- 4 循环 / 图 ----
pid=$(loop_pid)
if [ -n "$pid" ]; then c4="running (pid $pid)"; else c4="NOT running"; fi
if [ -s "$IMG" ]; then
    c5="$(wc -c < "$IMG" | tr -d ' ') bytes, $(stat -c %y "$IMG" 2>/dev/null | cut -c1-16)"
else
    c5="none yet"
fi

# 旧内容先截断到 120 行再接到新报告后面，文件不会无限长
tail -n 120 "$DIAG" > "$OLD" 2>/dev/null
{
    echo "==== RSS Board doctor ===="
    echo "time     : $(date '+%F %T')"
    echo "device   : $(uname -a 2>/dev/null | cut -c1-80)"
    echo "board.sh : $BOARD  [$c1]"
    echo "url      : ${URL:-<none>}"
    echo "wget     : $c2"
    echo "fetch    : $c3"
    [ -n "$detail" ] && echo "wget err : $detail"
    echo "loop     : $c4"
    echo "image    : $c5"
    echo "ext dir  : $(ls "$EXT" 2>/dev/null | tr '\n' ' ')"
    echo "--- older entries below ---"
    [ -s "$OLD" ] && cat "$OLD"
} > "$DIAG"
rm -f "$OLD"

verdict="PASS"
[ "$c1" = "OK" ] || verdict="FAIL: no board.sh"
case "$c3" in FAIL*) verdict="FAIL: fetch";; esac

eips -c
eips 1 1 "RSS Board doctor: $verdict"
eips 1 3 "details -> rssboard-diag.txt (Kindle root)"
exit 0
