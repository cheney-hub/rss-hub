#!/bin/sh
# 只删这个扩展目录本身，保留 /mnt/us/board.sh、board.png 和 rssboard-diag.txt——
# 删干净以后的复盘材料比省那点空间值钱。真要连图一起删，手动删这几个文件即可。
. /mnt/us/extensions/rssboard/bin/common.sh

# 用 $EXT 而不是把路径再抄一遍：restore.sh 已经这么写，两处各写一套迟早只改一处
sh "$EXT/bin/stop.sh"

diag "uninstall: removing $EXT (board.sh / board.png / this file are kept)"

# 顺序是「先停循环、再删目录」。删到最后这个文件本身也没关系：bin/common.sh
# 在脚本开头就被读进 shell 了，删文件不影响这一次的执行。
rm -rf "$EXT"

eips -c
eips 1 1 "RSS Board extension removed"
eips 1 3 "kept: board.sh, board.png, rssboard-diag.txt"
exit 0
