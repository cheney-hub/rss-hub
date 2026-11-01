#!/bin/sh
# look 一眼就能改的路径集中在这里，六个菜单脚本共用。
#
# 三条约定：
# 1) KUAL 里看不到脚本的 stdout。要给人看的东西写两处：
#    - $DIAG：**主要手段**，纯文本落在根目录，拔线插电脑就能当 E:\rssboard-diag.txt 打开；
#    - eips：辅助手段，屏上只放一两行结论（Kindle 的点阵字体只有 ASCII，中文会是空白）。
# 2) 动作一律写成 "sh <绝对路径>"，不依赖文件有没有 +x 位——USB 从 Windows 拷过去
#    执行位经常丢，用 sh 调就永远不会因为权限而不响应。
# 3) 屏幕/文件里都只写英文：文件虽然能放中文，但同一份内容还要写屏，干脆统一 ASCII。

EXT=/mnt/us/extensions/rssboard
BOARD=/mnt/us/board.sh
LOCK=/mnt/us/board.lock
IMG=/mnt/us/board.png
LOG="$EXT/log.txt"
# 诊断报告：放在根目录而不是扩展目录里，方便在 E 盘上一眼看到
DIAG=/mnt/us/rssboard-diag.txt

# 从 board.sh 的 URL= 那一行把地址抠出来（改了 BOARD_URL 也跟得上）。
# 只认 ^URL= 开头的行：board.sh 的注释里也出现了地址（说明历史口径），
# 直接全文 grep 会把注释里那个旧地址抓出来。
board_url() {
    [ -f "$BOARD" ] || return 0
    grep '^URL=' "$BOARD" 2>/dev/null | grep -o 'https\?://[^}"[:space:]]*' | head -n 1
}

# 循环到底在不在跑：锁文件里的 pid 还活着才算
loop_pid() {
    [ -f "$LOCK" ] || return 1
    pid=$(cat "$LOCK" 2>/dev/null)
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

# 追加一行到诊断文件（任何菜单动作都往这里留痕，顺序即时间线）
diag() {
    echo "[$(date '+%F %T')] $*" >> "$DIAG"
}

# board.sh 不在就没什么可做的：写文件 + 屏上一行，别让人对着黑屏猜
need_board() {
    [ -f "$BOARD" ] && return 0
    diag "ERROR board.sh MISSING at $BOARD (extract the zip into the Kindle root)"
    eips -c
    eips 1 1 "RSS Board: board.sh is MISSING"
    eips 1 3 "see rssboard-diag.txt at Kindle root"
    return 1
}
