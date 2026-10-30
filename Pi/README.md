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
- `services/`
  - `rss-serve.service` — feed 静态服务（端口 8080）
  - `rss-build.service` + `rss-build.timer` — Pi 本地构建，5 分钟一档
  - `moreaix-site.service` — 根域主页静态服务（端口 8081）
- `site/` — 根域主页 `index.html` + logo（`logo.svg` / `logo-lockup.svg`）
- `sshd-99-hardening.conf` — SSH 加固 drop-in（关密码、仅公钥）
- `TUNNEL.md` — Cloudflare Tunnel 接入指引
- `deploy.sh` — 历史部署脚本（参考，路径已随实际环境调整）

## Pi 上的实际部署路径

- 服务文件：`/etc/systemd/system/`（`rss-serve.service` 等）
- `serve.py`：`/home/user/pi-pipeline/serve.py`
- 主页：`/home/user/site/`
- RSS 引擎：`/home/user/rss-hub/`（= 仓库根目录代码在 Pi 的本地副本）

## 关于 GitHub Actions

本目录**不含任何 workflow 文件**，也不会被根目录的 Actions 运行。根目录动作只管根目录引擎；
Pi 侧由 systemd 自管。保存本目录到仓库只是**代码存档**，不新增、不触发任何动作。
