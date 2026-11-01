#!/bin/sh
# 探针：只做一件事——往根目录写一个文件，把运行环境原样记下来。
#
# 用途：当「点了菜单什么都不发生」时，用它区分几类原因：
#   文件出现了      → KUAL 能执行我们的脚本，问题在脚本内部（往下查 board.sh / 网络）
#   只有 A 有、B 无 → 必须靠 /bin/sh 调，文件没有可执行位（FAT 上很常见）
#   两个都没有      → 动作压根没被执行（KUAL 层 / /mnt/us 只读 / 路径不对）
# 它不 source common.sh，也不依赖任何别的脚本，尽量少一个变量。
# 参数 $1 是标签（A / B），用来区分是哪个菜单项跑起来的。
L=${1:-X}
OUT=/mnt/us/probe-$L.txt

{
    echo "probe $L ok"
    echo "time : $(date '+%F %T')"
    echo "user : $(id 2>/dev/null)"
    echo "shell: $0 / $#"
    echo "pwd  : $(pwd)"
    echo "path : $PATH"
    echo "root : $(ls -ld /mnt/us 2>/dev/null)"
    echo "test : $(touch /mnt/us/.probe-write-test 2>&1 && echo writable || echo READ-ONLY)"
    rm -f /mnt/us/.probe-write-test
    echo "ext  : $(ls /mnt/us/extensions/rssboard 2>/dev/null | tr '\n' ' ')"
    echo "bin  : $(ls -l /mnt/us/extensions/rssboard/bin/probe.sh 2>/dev/null)"
} > "$OUT" 2>&1

eips -c
eips 1 1 "probe $L: see probe-$L.txt at Kindle root"
exit 0
