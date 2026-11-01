#!/bin/sh
# 把屏幕还给系统：停掉循环、放开屏保限制、让书库重新画一遍。
#
# 为什么需要它：看板是直接往帧缓冲上画图（eips -g），它盖住的是整个界面，而不是
# 一个窗口 —— 没有「关闭按钮」。看图的时候想回去读书，最省事的是**轻点一下屏幕**
# （系统收到触摸就会重绘书库）；但如果你还想让它别再每 5 分钟回来抢屏，就点这一项。
#
# 两个动作分开是有意的：轻点屏幕＝临时回去（循环还在跑，5 分钟后又会盖上来）；
# 点这里＝真的退出（循环停掉、屏保恢复）。
. /mnt/us/extensions/rssboard/bin/common.sh

/bin/sh "$EXT/bin/stop.sh"

lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null

# 让书库重新成为前台：appmgrd 会重绘整个界面，把看板图盖掉。
# 万一这条 lipc 在你的固件上不认，轻点一下屏幕也能达到同样效果。
lipc-set-prop com.lab126.appmgrd start app://com.lab126.booklet.home 2>/dev/null

diag "restore: loop stopped, screensaver released, home requested"
exit 0
