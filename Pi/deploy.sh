#!/usr/bin/env bash
# 在树莓派上部署 serve.py + systemd 开机自启。
#
# 用法（在能 SSH 到 Pi 的机器上执行）：
#   PI_USER=pi PI_HOST=100.88.176.80 ./deploy.sh
#   PI_USER=pi PI_HOST=raspberrypi ./deploy.sh
#
# 前提：Pi 已开启 SSH，且本机对该用户是**免密（密钥）**登录。
#       若只有密码，见 TUNNEL.md「密码登录」一节（需 paramiko）。
set -euo pipefail

PI_USER="${PI_USER:-pi}"
PI_HOST="${PI_HOST:-100.88.176.80}"
REMOTE_DIR="/home/${PI_USER}/pi-pipeline"
FEED_ROOT="/home/${PI_USER}/rss-hub/docs"

SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
SSH=(ssh "${SSH_OPTS[@]}" "${PI_USER}@${PI_HOST}")
SCP=(scp "${SSH_OPTS[@]}")

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> 目标 ${PI_USER}@${PI_HOST}"
"${SSH[@]}" "python3 -V && mkdir -p '${REMOTE_DIR}' '${FEED_ROOT}' && echo paths-ok"

echo "==> 上传 serve.py 与服务单元"
"${SCP[@]}" "${HERE}/serve.py" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/serve.py"
"${SCP[@]}" "${HERE}/rss-serve.service" "${PI_USER}@${PI_HOST}:/tmp/rss-serve.service"

echo "==> 安装 systemd 单元（按实际路径替换 User / 目录）"
"${SSH[@]}" "sudo sed \
  -e 's#^User=pi#User=${PI_USER}#' \
  -e 's#/home/pi/pi-pipeline#${REMOTE_DIR}#g' \
  -e 's#/home/pi/rss-hub#${FEED_ROOT%/docs}#g' \
  /tmp/rss-serve.service | sudo tee /etc/systemd/system/rss-serve.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now rss-serve
sleep 1
systemctl is-active rss-serve"

echo "==> 本机自检（docs/ 里还没 feed.xml 时 404 属正常）"
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8080/rss/feed.xml -> %{http_code}\n" http://127.0.0.1:8080/rss/feed.xml || true'

echo "完成：rss-serve 已设为开机自启。"
