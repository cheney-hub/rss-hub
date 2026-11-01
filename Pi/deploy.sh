#!/usr/bin/env bash
# 把 Pi 侧的脚本、systemd 单元与 RSS 引擎代码装到树莓派上（不装 Python 依赖）。
#
# 上传什么：
#   build_feed.py / render.py / rotate.py / version.py / feeds_sync.py → ${HUB_ROOT}（引擎）
#   serve.py / rotate_screen.py / kindle_screen.py / kindle_page.py → ${PI_USER}/pi-pipeline/
#   services/*.service / *.path / *.timer            → /etc/systemd/system/（经 /tmp 中转）
# 不上传什么：
#   feeds.txt —— Pi 那份由引擎每轮构建前自己去仓库拉（见 rss-build.service 的
#   FEEDS_SYNC_URL）。这里再传一份等于两条路同时管同一个文件，迟早打架。
#   /home/user/site（主页产物）、/home/user/kindle（看板图，由 kindle-screen 渲染生成）
#   ——这两个是运行产物，只在下面保证目录存在。
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
UNITS="rss-build.service rss-build.timer \
rss-rotate.service rss-rotate.timer kindle-screen.service kindle-screen.path moreaix-site.service"
# 只有带 [Install] 的才能 enable：三个 oneshot（rss-build / rss-rotate / kindle-screen）
# 分别由 timer 与 path 触发，enable 它们会直接报「no installation config」。
ENABLE_UNITS="rss-build.timer rss-rotate.timer kindle-screen.path \
moreaix-site.service"

echo "==> 目标 ${PI_USER}@${PI_HOST}"
"${SSH[@]}" "python3 -V && mkdir -p '${REMOTE_DIR}' '${FEED_ROOT}' '${KINDLE_DIR}' && echo paths-ok"

echo "==> 上传引擎代码（根目录那几个文件）"
for f in build_feed.py render.py rotate.py version.py feeds_sync.py arrival.py; do
    "${SCP[@]}" "${HERE}/../${f}" "${PI_USER}@${PI_HOST}:${HUB_ROOT}/${f}"
done

echo "==> 上传脚本"
# kindle_page.py 出网页看板，kindle-screen.service 的第二条 ExecStart 会调它；
# 漏传的话那个单元每轮都有一半命令在「找不到文件」，图上得来、网页永远不更新。
for f in serve.py rotate_screen.py kindle_screen.py kindle_page.py; do
    "${SCP[@]}" "${HERE}/${f}" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/${f}"
done

echo "==> 上传并安装 systemd 单元"
"${SCP[@]}" "${STAGE}"/* "${PI_USER}@${PI_HOST}:/tmp/"
"${SSH[@]}" "set -e
for u in ${UNITS}; do sudo install -m 644 \"/tmp/\$u\" \"/etc/systemd/system/\$u\"; done
sudo systemctl daemon-reload"

echo "==> 启用并重启"
"${SSH[@]}" "sudo systemctl enable --now ${ENABLE_UNITS}"
"${SSH[@]}" "sudo systemctl try-restart moreaix-site.service \
kindle-screen.path || true"

echo "==> 状态"
"${SSH[@]}" "systemctl is-active moreaix-site.service \
rss-build.timer rss-rotate.timer kindle-screen.path || true"

echo "==> 本机自检（docs/ 里还没 feed.xml 时 404 属正常）"
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8081/rss/feed.xml -> %{http_code}\n" \
http://127.0.0.1:8081/rss/feed.xml || true'
# 看板图由 8081 的 --alias /kindle 出（8082 那台已彻底删除）
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8081/kindle/rss-pw2.png -> %{http_code}\n" \
http://127.0.0.1:8081/kindle/rss-pw2.png || true'

echo "完成：脚本已更新，单元已装好并设为开机自启。"
