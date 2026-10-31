#!/bin/sh
# look 一眼就能改的路径集中在这里，四个菜单脚本共用。
#
# 两条约定：
# 1) KUAL 里看不到脚本的 stdout，凡是要给人看的东西，要么用 eips 写屏，要么写 $LOG。
# 2) 动作一律写成 "sh <绝对路径>"，不依赖文件有没有 +x 位——USB 从 Windows 拷过去
#    执行位经常丢，用 sh 调就永远不会因为权限而不响应。

EXT=/mnt/us/extensions/rssboard
BOARD=/mnt/us/board.sh
LOCK=/mnt/us/board.lock
IMG=/mnt/us/board.png
LOG="$EXT/log.txt"
