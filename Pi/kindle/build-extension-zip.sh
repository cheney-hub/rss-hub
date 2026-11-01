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
# **打进去之前一律把 CRLF 转成 LF**：Kindle 的 ash 不认 CRLF，行尾那个 \r 会被
# 当成命令的一部分，症状是 `sh: missing ]` / `syntax error: unexpected end of file`
# —— 脚本一行都不执行，屏上却什么提示都没有（2026-11-01 实测踩过，排查了一轮）。
# 工作区在 Windows 上很容易又变回 CRLF，所以这一步放在打包里做，别指望编辑器。
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
        body = open(src, "rb").read()
        if arc.endswith((".sh", ".json", ".xml")):
            body = body.replace(b"\r\n", b"\n")     # Kindle 的 ash 不认 CRLF
        z.writestr(arc, body)

print("打包完成: %s (%d 字节)" % (out, os.path.getsize(out)))
with zipfile.ZipFile(out) as z:
    bad = 0
    for i in z.infolist():
        crlf = 0
        if i.filename.endswith(".sh"):
            crlf = z.read(i.filename).count(b"\r\n")
            bad += crlf
        print("  %-38s %6d  CRLF=%d" % (i.filename, i.file_size, crlf))
    if bad:
        sys.exit("打包里有 %d 处 CRLF，Kindle 上会解析失败" % bad)
PY
