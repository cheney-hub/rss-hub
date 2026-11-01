#!/bin/sh
# 把 board.sh + KUAL 扩展打成一个「解压到 Kindle 根目录即可」的 zip。
#
# 产物：dist/rssboard-extension.zip
#   board.sh                         ← 根目录，扩展的菜单全部调它
#   extensions/rssboard/...          ← KUAL 菜单、config.xml、bin/*.sh
#
# 为什么不用 MRPI 的 .bin：那个要 kindletool 且签名，适合「重装整机时一次装好」；
# 日常只更新一个脚本，解压覆盖比 `;log mrpi` 快得多。两者产物内容完全一样。
#
# 用法：sh Pi/kindle/build-extension-zip.sh
# 注意：zip 里的 board.sh 是**当前工作区**那份，改完 URL 之类记得重跑一次。
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
OUT_DIR="$HERE/dist"
OUT="$OUT_DIR/rssboard-extension.zip"

python3 - "$HERE" "$OUT" <<'PY'
import os
import sys
import zipfile

here, out = sys.argv[1], sys.argv[2]
ext = os.path.join(here, "extension", "rssboard")
files = [(os.path.join(here, "board.sh"), "board.sh")]
for name in ("config.xml", "menu.json"):
    files.append((os.path.join(ext, name), "extensions/rssboard/" + name))
for name in sorted(os.listdir(os.path.join(ext, "bin"))):
    files.append((os.path.join(ext, "bin", name), "extensions/rssboard/bin/" + name))

os.makedirs(os.path.dirname(out), exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for src, arc in files:
        z.write(src, arc)

print("打包完成: %s (%d 字节)" % (out, os.path.getsize(out)))
with zipfile.ZipFile(out) as z:
    for i in z.infolist():
        print("  %-38s %6d" % (i.filename, i.file_size))
PY
