#!/bin/sh
# 把「看板客户端 + KUAL 扩展」打包成 Update_rssboard_install.bin，丢进 Kindle 根目录的
# mrpackages/ 再 ;log mrpi 就能装上，省掉 scp + chmod 那几步。
#
# 用法：  sh Pi/kindle/build-mrpackage.sh [kindletool 的路径] [设备代号]
# 默认：  kindletool 在 PATH 里，设备代号 pw2（Kindle Paperwhite 2，和 JAILBREAK.md 一致）
#
# kindletool 要自己编译（没有官方的 Windows 成品，树莓派上最省事）：
#   sudo apt-get install -y gcc make libssl-dev zlib1g-dev pkg-config
#   git clone https://github.com/NiLuJe/KindleTool.git && cd KindleTool && make
#   ./KindleTool/KindleTool version
#
# 为什么是 sig + -U：这两个开关合起来出的是自定义更新的 userdata 包（魔法数 SP01），
# 签名用 kindletool 自带的那个公开越狱 key，MRPI 认它，所以我们不需要自己的密钥。
#
# ⚠️ 还没在真机上验过（2026-11 记录）。第一次建议先按 JAILBREAK.md 用 scp 把目录放进去
# 试通再打包；.bin 装的也是同一批文件，装不上不耽误事。
set -eu

KINDLETOOL="${1:-kindletool}"
DEVICE="${2:-pw2}"

ROOT=$(cd "$(dirname "$0")" && pwd)
NAME=Update_rssboard_install.bin
DIST="$ROOT/dist"

command -v "$KINDLETOOL" >/dev/null || {
    echo "找不到 kindletool：$KINDLETOOL" >&2
    echo "先按本文件开头注释里的两条命令编译一个，再把路径作为第一个参数传进来。" >&2
    exit 1
}

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# 这种包是把内容解压到用户区根目录的，所以 staging 目录的结构必须和装好之后的
# /mnt/us 一模一样：board.sh 在根上，扩展在 extensions/ 底下。
mkdir -p "$STAGE/extensions"
cp "$ROOT/board.sh" "$STAGE/board.sh"
cp -r "$ROOT/extension/rssboard" "$STAGE/extensions/rssboard"
chmod +x "$STAGE/board.sh" "$STAGE/extensions/rssboard"/bin/*.sh

# 少放了 menu.json 时 KUAL 只是静静地不显示菜单，很难查，所以这里先堵一道。
for f in board.sh extensions/rssboard/config.xml extensions/rssboard/menu.json; do
    [ -f "$STAGE/$f" ] || { echo "staging 缺文件：$f" >&2; exit 1; }
done

mkdir -p "$DIST"
# 必须 cd 进去再喂 "."：kindletool 默认把参数路径原样存档，给绝对路径的话包里会带上
# /tmp/xxx 那串前缀，上线位置全错。
( cd "$STAGE" && "$KINDLETOOL" create sig -U -d "$DEVICE" . "$DIST/$NAME" )

echo "产出：$DIST/$NAME"
echo "装法：拷到 Kindle 根目录 mrpackages/ → 搜索框 ;log mrpi → 等 UI 重启"
