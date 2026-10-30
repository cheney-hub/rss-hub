# Pi/ — 树莓派独立部署（rss-hub 子目录）

本目录保存**在树莓派上独立运行 rss-hub 的部署代码**，与仓库**根目录的 GitHub Actions 发布系统**
严格分离，**互不影响、互不触发**。

## 两套系统的边界（请勿搞乱）

| 系统 | 位置 | 谁驱动 | 是否变动 |
|---|---|---|---|
| RSS 聚合引擎 | 仓库**根目录**（`build_feed.py` / `render.py` / `rotate.py` / `version.py` / `feeds.txt` …） | GitHub Actions（`minute.yml` / `rss.yml` / `nightly.yml` / `review.yml`） | **不变** |
| Pi 本地部署 | 本目录 `Pi/` | 树莓派 systemd 自管（7×24） | 本次新增，无 Actions |

根目录的 Actions 只管根目录的 RSS 引擎；`Pi/` 不依赖 GitHub Actions，也不会被它们运行。
两者共用同一份 RSS 引擎代码（根目录那份），但部署链路完全独立。

## 本目录内容

- `serve.py` — 零依赖静态服务，给 Cloudflare Tunnel 拉取（默认 `127.0.0.1`）。
- `kindle_screen.py` — 把 `feed.xml` 渲染成 Kindle 用的灰度 PNG 看板（依赖 Pillow）。
- `services/`
  - `rss-serve.service` — feed 静态服务（端口 8080）
  - `rss-build.service` + `rss-build.timer` — Pi 本地构建，白天 5 分钟一档 / 夜间整点
  - `moreaix-site.service` — 根域主页静态服务（端口 8081）
  - `kindle-screen.service` + `kindle-screen.path` — feed 一变就重画看板（**不轮询**）
  - `kindle-serve.service` — 看板图床（端口 8082，**唯一绑 `0.0.0.0` 的服务**，仅局域网可达）
- `kindle/board.sh` — Kindle 端取图上屏脚本（需越狱后使用）
- `KINDLE.md` — 看板链路与上屏步骤
- `JAILBREAK.md` — KPW2（90D4 / 5.12.2.2）完整越狱流程（WatchThis）
- `site/` — 根域主页 `index.html` + logo（`logo.svg` / `logo-lockup.svg`）
- `sshd-99-hardening.conf` — SSH 加固 drop-in（关密码、仅公钥）
- `TUNNEL.md` — Cloudflare Tunnel 接入指引
- `deploy.sh` — 历史部署脚本（参考，路径已随实际环境调整）

## Pi 上的实际部署路径

- 服务文件：`/etc/systemd/system/`（`rss-serve.service` 等）
- `serve.py`：`/home/user/pi-pipeline/serve.py`
- `kindle_screen.py`：`/home/user/pi-pipeline/kindle_screen.py`
- 主页：`/home/user/site/`
- 看板图输出：`/home/user/kindle/`（`kindle.png` = `kindle-1024x758.png`，KPW2 第 6 代）
- RSS 引擎：`/home/user/rss-hub/`（= 仓库根目录代码在 Pi 的本地副本）

端口归属（改动前先看这张表，别把监听地址改错）：

| 端口 | 绑定 | 用途 | 对外暴露 |
|---|---|---|---|
| 8080 | `127.0.0.1` | feed 静态服务 | 经 Cloudflare Tunnel → `moreaix.com/rss/` |
| 8081 | `127.0.0.1` | 根域主页 | 经 Cloudflare Tunnel → `moreaix.com/` |
| 8082 | `0.0.0.0` | Kindle 看板图 | **仅局域网**，Tunnel 无此路由 |

## 关于 GitHub Actions

本目录**不含任何 workflow 文件**，也不会被根目录的 Actions 运行。根目录动作只管根目录引擎；
Pi 侧由 systemd 自管。保存本目录到仓库只是**代码存档**，不新增、不触发任何动作。
