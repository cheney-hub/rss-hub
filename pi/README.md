# 树莓派 / Linux 常驻部署

这条通道用 `aggregate_rss.py` 在本地抓取，再用 GitHub Contents API 把 `docs/feed.xml` 直接推到仓库，
由 systemd 负责**开机自启**和**定时 + 补跑**。适合想要「分钟级刷新」又有台常开设备的情况。

> 与 GitHub Actions 的关系：两条通道写的是同一个文件，**同时开启会产生提交冲突（API 409）**。
> 建议二选一；若想两条并行，把 Actions 的 `schedule` 段注释掉，只保留手动触发，让 Actions 变成备份。

## 前置条件

- 树莓派（或任意 Linux）已装 `python3`（3.8+ 即可，零第三方依赖）；
- 已生成 PAT：GitHub → Settings → Developer settings → Personal access tokens → **Generate new token (classic)** → 勾 **`repo`**；
- 目标仓库已开启 Pages（Source = `main` + `/docs`）。

## 部署步骤

### 1. 放代码

```bash
sudo mkdir -p /home/pi
cd /home/pi
git clone https://github.com/cheney-hub/rss-hub.git rss-aggregator
# 没有 git 就把整个目录拷进去，路径必须是 /home/pi/rss-aggregator
```

### 2. 填 token 与仓库

```bash
nano /home/pi/rss-aggregator/pi/rss-aggregator.service
```

改这两行（其余不动）：

```ini
Environment=GITHUB_TOKEN=ghp_你的token
Environment=GITHUB_REPO=cheney-hub/rss-hub
```

> 若你的用户名不是 `pi`，把文件里的 `User=pi`、`WorkingDirectory=/home/pi/...` 一起改掉。
> 觉得 unit 里放明文 token 不放心，可改成 `EnvironmentFile=/etc/rss-hub.env`（权限 `600`）再填 `GITHUB_TOKEN=...`。

### 3. 先手动跑一次，确认能推通

```bash
cd /home/pi/rss-aggregator
GITHUB_TOKEN=ghp_xxx GITHUB_REPO=cheney-hub/rss-hub python3 aggregate_rss.py
```

期望输出：

```
[聚合] 源 43 成功 43 失败 0 条目 42 (去重 1) 耗时 -
已推送到 GitHub cheney-hub/rss-hub/docs/feed.xml（分支 main）
```

失败常见于 token 没勾 `repo`（401/404）、仓库名写错、或树莓派网络没通。

### 4. 安装为 systemd 服务

```bash
bash /home/pi/rss-aggregator/pi/install.sh
```

脚本会校验占位符是否还留着、`aggregate_rss.py` 与 `feeds.txt` 是否在位，然后拷贝 unit、`daemon-reload`、`enable --now` 定时器。

### 5. 验证

```bash
sudo systemctl start rss-aggregator.service      # 立刻跑一轮
journalctl -u rss-aggregator.service -n 30       # 看输出
systemctl list-timers rss-aggregator.timer       # 看下次触发时间
```

## 自愈是怎么做到的

三层，各管一段：

| 层 | 机制 | 覆盖的故障 |
|---|---|---|
| 单轮内 | 脚本自带失败重试一次 + 回退上次条目 | 某个源临时 403/超时 |
| 进程级 | `Type=oneshot`，跑完即退；下一轮重新起进程 | 脚本崩溃、内存泄漏 |
| 系统级 | `OnBootSec=2min` 开机补跑；`Persistent=true` 补上关机期间错过的任务；`After=network-online.target` 等网络就绪 | 断电、重启、断网 |

`Persistent=true` 是关键：树莓派关机几小时后开机，systemd 会立刻补跑一次，而不是傻等下一个整点。

## 常用命令

```bash
sudo systemctl start rss-aggregator.service     # 手动跑一轮
sudo systemctl status rss-aggregator.timer      # 定时器状态
systemctl list-timers | grep rss                # 下次触发时间
journalctl -u rss-aggregator.service -f         # 实时日志
journalctl -u rss-aggregator.service --since today
sudo systemctl disable --now rss-aggregator.timer   # 停用
sudo systemctl enable --now rss-aggregator.timer    # 重新启用
```

## 调频率

编辑 `/etc/systemd/system/rss-aggregator.timer`：

```ini
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min     # 想 1 分钟一次就改这里
Persistent=true
```

```bash
sudo systemctl daemon-reload && sudo systemctl restart rss-aggregator.timer
```

注意：GitHub 对 Contents API 的写入没有硬性频率限制，但 Pages 构建有软上限（约 10 次/小时）。**1 分钟一轮 = 每小时 60 次提交**，会让 Pages 构建排队甚至被节流，对外可见的更新反而可能变慢。建议 5～10 分钟一档，这与 Actions 通道的实际效果相当。

## 常见问题

| 现象 | 原因 | 处置 |
|---|---|---|
| `status=203/EXEC` | `ExecStart` 路径不对，或 python3 不在 `/usr/bin/` | `which python3` 后改 unit |
| `401 Unauthorized` | token 过期 / 没勾 `repo` | 重新生成 token |
| `404 Not Found` | `GITHUB_REPO` 写错，或分支名不是 `main` | 检查 `GITHUB_REPO` / `GITHUB_BRANCH` |
| `409 Conflict` | 同时还有 Actions 在提交 | 关掉其中一条通道 |
| 全源超时 | 树莓派 DNS 或出网有问题 | `curl -I https://www.chinanews.com.cn/rss/scroll-news.xml` 验证 |
| 日志里满是 `[回退]` | 源站被树莓派 IP 限流 | 降低频率，或把源换成镜像 |

## 卸载

```bash
sudo systemctl disable --now rss-aggregator.timer
sudo rm /etc/systemd/system/rss-aggregator.{service,timer}
sudo systemctl daemon-reload
```
