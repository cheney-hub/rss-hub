#!/usr/bin/env bash
# kindle/ 自己的部署脚本：把「看板的两个渲染器 + 两个 systemd 单元」装上树莓派。
#
# **本脚本与 Pi/deploy.sh 互不引用。** 看板子系统和 Pi 的静态服务是各自独立的项目，
# 只是在同一台机器上共用 Python 与 systemd 而已。以前这两份东西混在 Pi/deploy.sh 里，
# 改 Kindle 一个参数就得等那边一起跑一遍，反过来也一样；现在各说各的，
# 动 Kindle 只跑这一个脚本。
#
# 上传什么：
#   kindle_screen.py / kindle_page.py → ${PI_USER}/pi-pipeline/（渲染器）
#   services/kindle-screen.{path,service} → /etc/systemd/system/（经 /tmp 中转）
# 不上传什么：
#   board.sh 与 extension/ —— 那是**跑在 Kindle 设备上**的客户端，压根不到树莓派，
#   它们的分发走 build-extension-zip.sh / build-mrpackage.sh。
#   /home/user/kindle（出图目录）—— 运行产物，本脚本只保证目录存在。
#
# 幂等：重复跑只是把同样内容再传一遍。已启用的单元用 try-restart ——
# `enable --now` 对已经 active 的单元不会重启，传了新脚本却还在跑旧进程（踩过）。
#
# 用法（在能 SSH 到树莓派的机器上执行）：
#   PI_USER=user PI_HOST=100.88.176.80 ./deploy.sh
#   PI_USER=user PI_HOST=raspberrypi ./deploy.sh
#
# 前提：
#   1. 本机对该用户是**免密（密钥）**登录 —— BatchMode 下不会弹密码，只有密码时
#      先 ssh-copy-id 把公钥推上去。密钥文件名不是 id_* 那几种默认名时
#      （比如 pi_deploy_ed25519），ssh 不会自动试它，直接跑会报
#      Permission denied (publickey)。先把它喂给 agent：
#        eval "$(ssh-agent -s)" && ssh-add ~/.ssh/pi_deploy_ed25519
#   2. 该用户能免密 sudo（安装单元、daemon-reload、restart 都要 root）。
#   3. 另外两样东西本脚本不管，缺了看板照样出不来：feed.xml 由根目录引擎的
#      rss-build 生成；图的公网出口由 Pi/ 那边的 moreaix-site.service 提供
#      （它的 --alias /kindle=… 指到出图目录）。
set -euo pipefail

PI_USER="${PI_USER:-user}"
PI_HOST="${PI_HOST:-100.88.176.80}"
REMOTE_DIR="/home/${PI_USER}/pi-pipeline"
FEED="/home/${PI_USER}/rss-hub/docs/feed.xml"
KINDLE_DIR="/home/${PI_USER}/kindle"

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
SSH=(ssh "${SSH_OPTS[@]}")
SCP=(scp "${SSH_OPTS[@]}")

HERE="$(cd "$(dirname "$0")" && pwd)"

# 单元里的路径按 /home/user 写死（Pi 上的唯一部署位置）。换用户时在这里统一改写，
# 而不是让每份单元自带一套模板 —— 两份路径迟早漂移成「服务在 A、脚本读 B」。
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
for u in "${HERE}"/services/*; do
    sed -e "s#^User=user#User=${PI_USER}#" -e "s#/home/user#/home/${PI_USER}#g" \
        "$u" > "${STAGE}/$(basename "$u")"
done

# 只有 .path 能 enable：kindle-screen.service 是 oneshot，由 .path 在 feed.xml
# 变化时触发，enable 它会直接报「no installation config」。加新单元记得两处都同步。
INSTALL_UNITS="kindle-screen.service kindle-screen.path"
ENABLE_UNITS="kindle-screen.path"

echo "==> 目标 ${PI_USER}@${PI_HOST}"
"${SSH[@]}" "python3 -V && mkdir -p '${REMOTE_DIR}' '${KINDLE_DIR}' && echo paths-ok"

echo "==> 上传渲染器"
# 两个都得传：kindle_screen.py 出 PNG、kindle_page.py 出网页看板，它们是同一个
# oneshot 里的两条 ExecStart。漏传的话单元每轮都有一半命令在「找不到文件」——
# 图上得来、网页永远停在旧页，而这种症状很容易被归因为「HTML 写错了」。
for f in kindle_screen.py kindle_page.py; do
    "${SCP[@]}" "${HERE}/${f}" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/${f}"
done

echo "==> 上传并安装 systemd 单元"
"${SCP[@]}" "${STAGE}"/* "${PI_USER}@${PI_HOST}:/tmp/"
"${SSH[@]}" "set -e
for u in ${INSTALL_UNITS}; do sudo install -m 644 \"/tmp/\$u\" \"/etc/systemd/system/\$u\"; done
sudo systemctl daemon-reload"

echo "==> 启用并重启"
"${SSH[@]}" "sudo systemctl enable --now ${ENABLE_UNITS}"
"${SSH[@]}" "sudo systemctl try-restart ${ENABLE_UNITS} || true"

echo "==> 状态"
"${SSH[@]}" "systemctl is-active ${ENABLE_UNITS} || true"
"${SSH[@]}" "test -s '${FEED}' && echo feed.xml: \$(wc -c < '${FEED}') 字节 || echo 'feed.xml 还没有（先跑一次 rss-build）'"
# 出口是 Pi/ 那份 serve.py 的 --alias /kindle，这里只是验证端到端通不通
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8081/kindle/rss-pw2.png -> %{http_code}\n" \
http://127.0.0.1:8081/kindle/rss-pw2.png || true'

echo "完成：渲染器已更新，看板 path 单元已设为开机自启。"
echo "设备端要更新的话，另跑 sh kindle/build-extension-zip.sh 打包扩展。"
