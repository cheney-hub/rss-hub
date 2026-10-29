#!/usr/bin/env bash
# 树莓派 / Linux 一键安装：注册为 systemd 定时任务（开机自启 + 定时 + 补跑）
#
# 用法：
#   bash pi/install.sh                                            # 只安装，凭证稍后填
#   GITHUB_TOKEN=... GITHUB_REPO=owner/repo bash pi/install.sh     # 一并写入凭证
#
# 凭证落在 /etc/rss-hub/rss-hub.env（权限 600），只被本机服务读取，
# 不进入仓库、不落在项目目录里。
set -euo pipefail

ENV_DIR="/etc/rss-hub"
ENV_FILE="$ENV_DIR/rss-hub.env"
UNIT_DIR="/etc/systemd/system"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${RUN_USER:-$(id -un)}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || echo /usr/bin/python3)}"

# ---------- 0. 前置检查 ----------
for f in aggregate_rss.py feeds.txt; do
  if [ ! -f "$REPO_DIR/$f" ]; then
    echo "错误：缺少 $REPO_DIR/$f"
    exit 1
  fi
done
if [ ! -x "$PYTHON_BIN" ]; then
  echo "错误：找不到 python3（可用 PYTHON_BIN=/path/to/python3 指定）"
  exit 1
fi

echo "==> 项目目录：$REPO_DIR"
echo "==> 运行用户：$RUN_USER"
echo "==> Python  ：$PYTHON_BIN"

# ---------- 1. 凭证文件（仓库之外） ----------
sudo install -d -m 700 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
  sudo install -m 600 /dev/null "$ENV_FILE"
  sudo tee "$ENV_FILE" > /dev/null <<'TPL'
# RSS 聚合器凭证 —— 本文件不进入仓库，请勿提交
GITHUB_TOKEN=
GITHUB_REPO=
GITHUB_BRANCH=main
TPL
  echo "==> 已创建 $ENV_FILE（权限 600）"
else
  echo "==> 沿用已有 $ENV_FILE"
fi

# 通过环境变量传入的值写进文件；不打印明文
upsert() {
  key="$1"
  val="${2:-}"
  [ -n "$val" ] || return 0
  if sudo grep -q "^${key}=" "$ENV_FILE"; then
    sudo sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" | sudo tee -a "$ENV_FILE" > /dev/null
  fi
}
upsert GITHUB_TOKEN "${GITHUB_TOKEN:-}"
upsert GITHUB_REPO  "${GITHUB_REPO:-}"
upsert GITHUB_BRANCH "${GITHUB_BRANCH:-}"
sudo chmod 600 "$ENV_FILE"

# ---------- 2. 生成并安装 unit ----------
TMP_UNIT="$(mktemp)"
sed -e "s|^User=.*|User=${RUN_USER}|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=${REPO_DIR}|" \
    -e "s|^ExecStart=.*|ExecStart=${PYTHON_BIN} ${REPO_DIR}/aggregate_rss.py|" \
    "$REPO_DIR/pi/rss-aggregator.service" > "$TMP_UNIT"

sudo install -m 644 "$TMP_UNIT" "$UNIT_DIR/rss-aggregator.service"
sudo install -m 644 "$REPO_DIR/pi/rss-aggregator.timer" "$UNIT_DIR/rss-aggregator.timer"
rm -f "$TMP_UNIT"

sudo systemctl daemon-reload
sudo systemctl enable --now rss-aggregator.timer

# ---------- 3. 自检 ----------
if sudo grep -q '^GITHUB_TOKEN=.\+' "$ENV_FILE" && sudo grep -q '^GITHUB_REPO=.\+' "$ENV_FILE"; then
  echo "==> 凭证已就绪，先跑一轮验证"
  sudo systemctl start rss-aggregator.service || true
  echo "    查看日志：journalctl -u rss-aggregator.service -n 40 --no-pager"
else
  echo "==> 还缺凭证，请编辑："
  echo "    sudo nano $ENV_FILE        # 填 GITHUB_TOKEN 与 GITHUB_REPO"
  echo "    sudo systemctl start rss-aggregator.service"
fi

echo "==> 定时器已启用：$(systemctl is-active rss-aggregator.timer)"
