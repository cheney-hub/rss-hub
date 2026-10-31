#!/bin/sh
# 只删这个扩展目录本身，保留 /mnt/us/board.sh 和已经拉下来的 board.png——
# 删干净以后的复盘材料比省那点空间值钱。真要连图一起删，手动删这两个文件即可。
. /mnt/us/extensions/rssboard/bin/common.sh

sh /mnt/us/extensions/rssboard/bin/stop.sh

# 顺序是「先停循环、再删目录」。删到最后这个文件本身也没关系：bin/common.sh
# 在脚本开头就被读进 shell 了，删文件不影响这一次的执行。
rm -rf "$EXT"

eips -c
eips 1 1 "RSS board extension removed"
eips 1 3 "board.sh and board.png kept"
exit 0
