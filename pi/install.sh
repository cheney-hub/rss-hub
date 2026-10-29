#!/usr/bin/env bash
# 树莓派一键安装：把 RSS 聚合器注册为 systemd 定时任务（开机自启 + 故障自恢复）
# 用法：把整个 rss-aggregator 目录放到 /home/pi/ 下，填好 service 里的 token，然后执行：
#   bash /home/pi/rss-aggregator/pi/install.sh
set -e

SERVICE_DIR="$(cd "$(dirname "$0")" && pwd)"

# 基本校验：token 是否还是占位符
if grep -q "替换成你的token" "$SERVICE_DIR/rss-aggregator.service"; then
  echo "错误：请先编辑 rss-aggregator.service，填入 GITHUB_TOKEN 和 GITHUB_REPO"
  exit 1
fi

# 确保脚本与数据文件在位
for f in aggregate_rss.py feeds.txt; do
  if [ ! -f "/home/pi/rss-aggregator/$f" ]; then
    echo "错误：缺少 /home/pi/rss-aggregator/$f"
    exit 1
  fi
done

sudo cp "$SERVICE_DIR/rss-aggregator.service" /etc/systemd/system/
sudo cp "$SERVICE_DIR/rss-aggregator.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rss-aggregator.timer

echo "安装完成。立即手动触发一次验证："
echo "  sudo systemctl start rss-aggregator.service"
echo "查看日志："
echo "  journalctl -u rss-aggregator.service -f"
