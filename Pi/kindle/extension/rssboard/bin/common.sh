#!/bin/sh
# look 一眼就能改的路径集中在这里，五个菜单脚本共用。
#
# 三条约定：
# 1) KUAL 里看不到脚本的 stdout，凡是要给人看的东西，要么用 eips 写屏，要么写 $LOG。
# 2) 动作一律写成 "sh <绝对路径>"，不依赖文件有没有 +x 位——USB 从 Windows 拷过去
#    执行位经常丢，用 sh 调就永远不会因为权限而不响应。
# 3) **屏幕上只能写英文**：eips 用的是 Kindle 自带点阵字体，没有汉字点阵，中文会
#    变成空白或方块。KUAL 菜单名可以用中文（系统字体渲染），eips 那几行必须是 ASCII。

EXT=/mnt/us/extensions/rssboard
BOARD=/mnt/us/board.sh
LOCK=/mnt/us/board.lock
IMG=/mnt/us/board.png
LOG="$EXT/log.txt"

# 从 board.sh 的 URL= 那一行把地址抠出来，状态页显示用（改了 BOARD_URL 也跟得上）。
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

# board.sh 不在就没什么可做的，直接在屏上说清楚，别让人对着黑屏猜
need_board() {
    [ -f "$BOARD" ] && return 0
    eips -c
    eips 1 1 "RSS Board: board.sh is MISSING"
    eips 1 3 "Extract the zip into the Kindle root"
    eips 1 5 "(expected /mnt/us/board.sh)"
    return 1
}
