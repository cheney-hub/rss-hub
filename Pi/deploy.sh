#!/usr/bin/env bash
# 把 Pi 侧的脚本与 systemd 单元装到树莓派上（不搬引擎代码、不装 Python 依赖）。
#
# 上传什么：
#   serve.py / rotate_screen.py / kindle_screen.py   → ${PI_USER}/pi-pipeline/
#   services/*.service / *.path / *.timer            → /etc/systemd/system/（经 /tmp 中转）
# 不覆盖什么：
#   /home/user/rss-hub（引擎代码，来自 git clone）、/home/user/site（主页产物）、
#   /home/user/kindle（看板图，由 kindle-screen 渲染生成）——这些都不归本脚本管，
#   脚本只保证目录存在，缺引擎时直接报错停下，不往下装半套。
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
#   2. 该用户能免密 sudo（安装单元、daemon-reload、restart 都要 root）。
set -euo pipefail

PI_USER="${PI_USER:-user}"
PI_HOST="${PI_HOST:-100.88.176.80}"
REMOTE_DIR="/home/${PI_USER}/pi-pipeline"
HUB_ROOT="/home/${PI_USER}/rss-hub"
FEED_ROOT="${HUB_ROOT}/docs"
KINDLE_DIR="/home/${PI_USER}/kindle"

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
UNITS="rss-serve.service rss-build.service rss-build.timer \
rss-rotate.service rss-rotate.timer kindle-screen.service kindle-screen.path \
kindle-serve.service moreaix-site.service"
# 只有带 [Install] 的才能 enable：三个 oneshot（rss-build / rss-rotate / kindle-screen）
# 分别由 timer 与 path 触发，enable 它们会直接报「no installation config」。
ENABLE_UNITS="rss-build.timer rss-rotate.timer kindle-screen.path \
rss-serve.service kindle-serve.service moreaix-site.service"

echo "==> 目标 ${PI_USER}@${PI_HOST}"
"${SSH[@]}" "python3 -V && mkdir -p '${REMOTE_DIR}' '${FEED_ROOT}' '${KINDLE_DIR}' && echo paths-ok"

# 没有引擎就别装了：装完 rss-build 也只会以「找不到 build_feed.py」失败
"${SSH[@]}" "test -f '${HUB_ROOT}/build_feed.py' \
  || { echo '缺少 ${HUB_ROOT}/build_feed.py：先把本仓库 clone 到 ${HUB_ROOT}'; exit 1; }"

echo "==> 上传脚本"
for f in serve.py rotate_screen.py kindle_screen.py; do
    "${SCP[@]}" "${HERE}/${f}" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/${f}"
done

echo "==> 上传并安装 systemd 单元"
"${SCP[@]}" "${STAGE}"/* "${PI_USER}@${PI_HOST}:/tmp/"
"${SSH[@]}" "set -e
for u in ${UNITS}; do sudo install -m 644 \"/tmp/\$u\" \"/etc/systemd/system/\$u\"; done
sudo systemctl daemon-reload"

echo "==> 启用并重启"
"${SSH[@]}" "sudo systemctl enable --now ${ENABLE_UNITS}"
"${SSH[@]}" "sudo systemctl try-restart rss-serve.service kindle-serve.service \
moreaix-site.service kindle-screen.path || true"

echo "==> 状态"
"${SSH[@]}" "systemctl is-active rss-serve.service kindle-serve.service moreaix-site.service \
rss-build.timer rss-rotate.timer kindle-screen.path || true"

echo "==> 本机自检（docs/ 里还没 feed.xml 时 404 属正常）"
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8080/rss/feed.xml -> %{http_code}\n" \
http://127.0.0.1:8080/rss/feed.xml || true'
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8082/kindle.png -> %{http_code}\n" \
http://127.0.0.1:8082/kindle.png || true'

echo "完成：脚本已更新，单元已装好并设为开机自启。"
