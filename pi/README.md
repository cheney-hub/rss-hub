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

### 2. 生成凭证文件（留在仓库之外）

这个仓库**不含任何 token 或口令**，凭证只放在本机 `/etc/rss-hub/rss-hub.env`（权限 `600`，只有 root 与该服务能读）。

```bash
sudo install -d -m 700 /etc/rss-hub
sudo install -m 600 pi/rss-hub.env.example /etc/rss-hub/rss-hub.env
sudo nano /etc/rss-hub/rss-hub.env
```

填两行（`GITHUB_BRANCH` 一般不用改）：

```ini
GITHUB_TOKEN=你的PAT
GITHUB_REPO=你的用户名/rss-hub
GITHUB_BRANCH=main
```

unit 文件里通过 `EnvironmentFile=/etc/rss-hub/rss-hub.env` 读取，**不需要也不应该**去改 `rss-aggregator.service`。

> 若你的用户名不是 `pi`、或代码不在 `/home/pi/rss-aggregator`，不用手改 unit：`install.sh` 会按实际路径和当前用户自动改写后再安装。

### 3. 先手动跑一次，确认能推通

```bash
cd /home/pi/rss-aggregator
set -a; . /etc/rss-hub/rss-hub.env; set +a     # 临时载入凭证到当前 shell
python3 aggregate_rss.py
```

期望输出：

```
[聚合] 源 43 成功 43 失败 0 条目 42 (去重 1) 耗时 -
已推送到 GitHub <你的仓库>/docs/feed.xml（分支 main）
```

失败常见于 token 没勾 `repo`（401/404）、仓库名写错、或树莓派网络没通。

### 4. 安装为 systemd 服务

```bash
bash /home/pi/rss-aggregator/pi/install.sh
# 或者一步到位，顺便把凭证写进文件（不会回显到仓库里）：
# GITHUB_TOKEN=你的PAT GITHUB_REPO=你的用户名/rss-hub bash pi/install.sh
```

脚本会：检查 `aggregate_rss.py` / `feeds.txt` 是否在位 → 创建或沿用 `/etc/rss-hub/rss-hub.env`（`600`）→ 按实际路径与用户改写 unit → 安装并启用定时器 → 凭证齐全时立刻跑一轮验证。

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

## 凭证安全

- 仓库里**不存在**任何 token / 口令，`.gitignore` 也屏蔽了 `.env` 一类文件；
- 凭证只放在 `/etc/rss-hub/rss-hub.env`，权限 `600`，`install.sh` 创建时即设好；
- unit 里只有 `EnvironmentFile=`，没有明文值；`rss-hub.env.example` 里的值是空的；
- `tools/check_secrets.py` 会在每次推送时扫一遍仓库（`.github/workflows/guard.yml`），一旦混进真 token 直接失败；
- 怀疑泄露时的处置顺序：GitHub → 吊销该 token → 生成新的 → 更新 `/etc/rss-hub/rss-hub.env` → `sudo systemctl restart rss-aggregator.timer`。

## 常见问题

| 现象 | 原因 | 处置 |
|---|---|---|
| `status=203/EXEC` | `ExecStart` 路径不对，或 python3 不在 `/usr/bin/` | `which python3` 后重跑 `install.sh`（可带 `PYTHON_BIN=`） |
| 服务不出日志、状态 `condition failed` | `/etc/rss-hub/rss-hub.env` 不存在或为空 | 按第 2 步创建并填入两行 |
| `401 Unauthorized` | token 过期 / 没勾 `repo` | 重新生成 token，更新 env 文件 |
| `404 Not Found` | `GITHUB_REPO` 写错，或分支名不是 `main` | 检查 env 文件里的 `GITHUB_REPO` / `GITHUB_BRANCH` |
| `409 Conflict` | 同时还有 Actions 在提交 | 关掉其中一条通道 |
| 全源超时 | 树莓派 DNS 或出网有问题 | `curl -I https://www.chinanews.com.cn/rss/scroll-news.xml` 验证 |
| 日志里满是 `[回退]` | 源站被树莓派 IP 限流 | 降低频率，或把源换成镜像 |

## 卸载

```bash
sudo systemctl disable --now rss-aggregator.timer
sudo rm /etc/systemd/system/rss-aggregator.{service,timer}
sudo systemctl daemon-reload
sudo rm -rf /etc/rss-hub          # 连同凭证一起删掉
```
