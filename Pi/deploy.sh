#!/usr/bin/env bash
# 把 Pi 侧的脚本与 systemd 单元装到树莓派上（不装 Python 依赖）。
#
# 上传什么：
#   serve.py / rotate_screen.py / journal_snapshot.py → ${PI_USER}/pi-pipeline/
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
#   /dev/shm/rss-hub（内存盘产物）—— 运行产物，由各单元的 ExecStartPre 自己建，
#   这里不传也不建；只有 docs/ 里那两个静态页**源文件**要上传（见下面）。
#   /home/user/kindle（旧出图目录）—— 已停用，归 kindle/ 那套管，不去动它。
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
# 主页产物目录（moreaix-site.service 的 --root，也是公网根域指向的地方）
SITE_DIR="/home/${PI_USER}/site"

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
rss-rotate.service rss-rotate.timer moreaix-site.service \
journal-snapshot.service journal-snapshot.timer \
ai-digest.service ai-digest.timer"
# 只有带 [Install] 的才能 enable：三个 oneshot（rss-build / rss-rotate /
# journal-snapshot / ai-digest）都由 timer 触发，enable 它们会直接报「no installation config」。
ENABLE_UNITS="rss-build.timer rss-rotate.timer \
moreaix-site.service journal-snapshot.timer ai-digest.timer"

echo "==> 目标 ${PI_USER}@${PI_HOST}"
# ${SITE_DIR} 一定要一起建：moreaix-site.service 的 WorkingDirectory 与
# serve.py 的 --root 都指它，目录不存在时单元以 status=200/CHDIR 直接失败，
# 而 Restart=always 会让它每 5 秒重启一次 —— 连带 --alias /rss 与 /kindle
# 两个别名一起挂掉（公网出口全断），systemctl status 看上去却只是「不停重启」。
# 现在这台机器上的目录是当初手工建的，脚本里没有这一步就是换机部署时的坑。
"${SSH[@]}" "python3 -V && mkdir -p '${REMOTE_DIR}' '${FEED_ROOT}' '${SITE_DIR}' && echo paths-ok"

echo "==> 上传引擎代码（根目录那几个文件）"
for f in build_feed.py render.py rotate.py version.py feeds_sync.py arrival.py; do
    "${SCP[@]}" "${HERE}/../${f}" "${PI_USER}@${PI_HOST}:${HUB_ROOT}/${f}"
done

echo "==> 上传脚本"
# 三个 Pi 侧脚本：静态服务、轮播、每日日志快照。Kindle 那两个渲染器虽然也落在
# 同一个远程目录，但归 kindle/deploy.sh 传 —— 两套 pipeline 各管一半，不会互相覆盖。
for f in serve.py rotate_screen.py journal_snapshot.py ai_digest.py; do
    "${SCP[@]}" "${HERE}/${f}" "${PI_USER}@${PI_HOST}:${REMOTE_DIR}/${f}"
done

echo "==> 上传主页静态文件（site/ → ${SITE_DIR}）"
# 只补不删：站点里可能还有手工放上去的东西，这里不做清理。
# 目录已经在上面 mkdir 建好，serve.py 不会因为根目录不存在而退出。
for f in "${HERE}"/site/*; do
    [ -f "$f" ] || continue
    "${SCP[@]}" "$f" "${PI_USER}@${PI_HOST}:${SITE_DIR}/$(basename "$f")"
done

echo "==> 上传 /rss 静态页（内存盘冷启动的源文件）"
# moreaix-site 的 ExecStartPre 会把这几页 cp 进 /dev/shm/rss-hub。
# 不传的话 Pi 上那份是旧副本，页脚版本号会一直停在过去，而部署看起来完全成功。
# 只传 HTML：feed.xml / feed-wrap.xml / rotate.json 都是构建产物，绝不从仓库覆盖。
# 新增页面记得同步这里与 moreaix-site.service 的 ExecStartPre cp 行，否则线上 404。
for f in index.html screen.html reader.html; do
    "${SCP[@]}" "${HERE}/../docs/${f}" "${PI_USER}@${PI_HOST}:${FEED_ROOT}/${f}"
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
rss-build.timer rss-rotate.timer journal-snapshot.timer || true"

echo "==> 本机自检（docs/ 里还没 feed.xml 时 404 属正常）"
"${SSH[@]}" 'curl -s -o /dev/null -w "127.0.0.1:8081/rss/feed.xml -> %{http_code}\n" \
http://127.0.0.1:8081/rss/feed.xml || true'
# 看板图那条路由（--alias /kindle）是 Pi 这台 serve.py 出、但内容归 kindle/ 管，
# 端到端是否通请跑 kindle/deploy.sh 自检，这里不再重复。

echo "完成：脚本已更新，单元已装好并设为开机自启。"
