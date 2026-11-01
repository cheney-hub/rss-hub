#!/usr/bin/env bash
# 把 Pi 侧的脚本与 systemd 单元装到树莓派上（不装 Python 依赖）。
#
# 上传什么：
#   serve.py / rotate_screen.py                       → ${PI_USER}/pi-pipeline/
#   build_feed.py / render.py / rotate.py / version.py / feeds_sync.py / arrival.py
#                                                     → ${HUB_ROOT}（根目录的引擎）
#   services/*.service / *.path / *.timer             → /etc/systemd/system/（经 /tmp 中转）
# **不管 Kindle**：那个是自己独立的一套（board.sh 跑在设备上、两个渲染器虽机身同名但
# 由 kindle/deploy.sh 负责），本脚本不再上传它，也不安装 kindle-screen.* 那两个单元
# —— 以前混在一起，改 Kindle 一个参数就得跑一遍 Pi 的全套部署。
# 网页版看板的 index.html 由 kindle_page.py 出，同样不归这里。
#
# 不上传什么：
#   feeds.txt —— Pi 那份由引擎每轮构建前自己去仓库拉（见 rss-build.service 的
#   FEEDS_SYNC_URL）。这里再传一份等于两条路同时管同一个文件，迟早打架。
#   /home/user/site（主页产物）—— 运行产物，只在下面保证目录存在。
#   /home/user/kindle（出图目录）—— 归 kindle/ 那套管，这里**不**去动它：
#   本脚本连目录都不建，免得两边重复着 daemon-reload。
#
# 为什么连引擎一起传：2026-11-01 实测过一次漂移 —— 仓库改了代码没同步到 Pi，
# 两边各出一版 feed，屏幕上完全看不出来（这正是那次事故里唯一可怕的地方）。
# 所以这里不再「缺引擎就报错退出」，而是直接把根目录那份传上去补平。
#
# 幂等：重复跑只是把同样内容再传一遍。已在运行的单元用 try-restart 重启 ——
# `enable --now` 对 active 单元不会重启，传了新 serve.py 却还在跑旧进程（踩过）。
#
# 用法（在能 SSH 到 Pi 的机器上执行）：
#   PI_USER=user PI_HOST=100.88.176.80 ./deploy.sh
#   PI_USER=user PI_HOST=raspberrypi ./deploy.sh
#
# 前提：
#   1. Pi 已开启 SSH，且本机对该用户是**免密（密钥）**登录 —— BatchMode 下不会弹密码，
#      只有密码时先 ssh-copy-id 把公钥推上去。
#      密钥文件名不是 id_* 那几种默认名时（比如 pi_deploy_ed25519），ssh 不会自动试它，
#      直接跑会报 Permission denied (publickey)。先把它喂给 agent：
#        eval "$(ssh-agent -s)" && ssh-add ~/.ssh/pi_deploy_ed25519
#   2. 该用户能免密 sudo（安装单元、daemon-reload、restart 都要 root）。
set -euo pipefail

PI_USER="${PI_USER:-user}"
PI_HOST="${PI_HOST:-100.88.176.80}"
REMOTE_DIR="/home/${PI_USER}/pi-pipeline"
HUB_ROOT="/home/${PI_USER}/rss-hub"
FEED_ROOT="${HUB_ROOT}/docs"

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
SSH=(ssh "${SSH_OPTS[@]}" "${PI_USER}@${PI_HOST}")
SCP=(scp "${SSH_OPTS[@]}")

HERE="$(cd "$(dirname "$0")" && pwd)"

# 单元里所有路径都按 /home/user 写死（Pi 上的唯一部署位置）。换用户时在这里统一改写，
# 而不是让每份单元自带一套模板 —— 两份路径迟早漂移成「服务在 A、脚本读 B」。
STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
for u in "${HERE}"/services/*; do
    sed -e "s#^User=user#User=${PI_USER}#" -e "s#/home/user#/home/${PI_USER}#g" \
        "$u" > "${STAGE}/$(basename "$u")"
done

# 要装的单元清单（加新单元记得同步这里，否则会漏装）
UNITS="rss-build.service rss-build.timer \
rss-rotate.service rss-rotate.timer moreaix-site.service"
# 只有带 [Install] 的才能 enable：两个 oneshot（rss-build / rss-rotate）
# 分别由 timer 触发，enable 它们会直接报「no installation config」。
ENABLE_UNITS="rss-build.timer rss-rotate.timer \
moreaix-site.service"

echo "==> 目标 ${PI_USER}@${PI_HOST}"
"${SSH[@]}" "python3 -V && mkdir -p '${REMOTE_DIR}' '${FEED_ROOT}' && echo paths-ok"

echo "==> 上传引擎代码（根目录那几个文件）"
for f in build_feed.py render.py rotate.py version.py feeds_sync.py arrival.py; do
    "${SCP[@]}" "${HERE}/../${f}" "${PI_USER}@${PI_HOST}:${HUB_ROOT}/${f}"
done

echo "==> 上传脚本"
# 只剩两个：静态服务与轮播。Kindle 那两个渲染器虽然也落在同一个远程目录，
# 但归 kindle/deploy.sh 传 —— 两套 pipeline 各管一半，不会互相覆盖对方的成果。
for f in serve.py rotate_screen.py; do
    "${SCP[@]}" "${HERE}/${f}" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/${f}"
done

echo "==> 上传并安装 systemd 单元"
"${SCP[@]}" "${STAGE}"/* "${PI_USER}@${PI_HOST}:/tmp/"
"${SSH[@]}" "set -e
for u in ${UNITS}; do sudo install -m 644 \"/tmp/\$u\" \"/etc/systemd/system/\$u\"; done
sudo systemctl daemon-reload"

echo "==> 启用并重启"
"${SSH[@]}" "sudo systemctl enable --now ${ENABLE_UNITS}"
"${SSH[@]}" "sudo systemctl try-restart moreaix-site.service || true"

echo "==> 状态"
"${SSH[@]}" "systemctl is-active moreaix-site.service \
rss-build.timer rss-rotate.timer || true"

echo "==> 本机自检（docs/ 里还没 feed.xml 时 404 属正常）"
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8081/rss/feed.xml -> %{http_code}\n" \
http://127.0.0.1:8081/rss/feed.xml || true'
# 看板图那条路由（--alias /kindle）是 Pi 这台 serve.py 出、但内容归 kindle/ 管，
# 端到端是否通请跑 kindle/deploy.sh 自检，这里不再重复。

echo "完成：脚本已更新，单元已装好并设为开机自启。"
