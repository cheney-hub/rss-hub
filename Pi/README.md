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

- `serve.py` — 零依赖静态服务，给 Cloudflare Tunnel 拉取（只绑 `127.0.0.1`）。
  **Pi 上只跑一份**：`--root` 是根域主页，再用两个 `--alias` 把 `/rss`（feed 目录）
  与 `/kindle`（看板图）挂到同一台实例上。
- `kindle_screen.py` — 把 `feed.xml` 渲染成 Kindle 用的灰度 PNG 看板（依赖 Pillow）。
- `rotate_screen.py` — 显示时间线：只读 `feed.xml`，每 5 分钟挑一条写 `feed-wrap.xml`。
- `services/`
  - `rss-build.service` + `rss-build.timer` — Pi 本地构建（池子），白天 5 分钟一档 / 夜间整点
  - `rss-rotate.service` + `rss-rotate.timer` — 轮播（设备版），**全天每 5 分钟推进一条**
  - `moreaix-site.service` — **唯一**的静态服务（端口 8081）：根域主页 + `/rss/*` + `/kindle/*`
  - `kindle-screen.service` + `kindle-screen.path` — feed 一变就重画看板（**不轮询**）
  - `kindle-serve.service` — 看板图床（端口 8082，**已停用**）。保留文件是为了将来局域网
    修好后一条 `systemctl enable --now` 就能回来；公网已由 8081 的 `--alias /kindle` 承担
- `kindle/board.sh` — Kindle 端取图上屏脚本（需越狱后使用）
- `kindle/extension/rssboard/` — KUAL 扩展：把上面那个脚本包成「立即刷新/启动/停止/看状态」菜单
- `kindle/build-mrpackage.sh` — 把它俩打成 MRPI 的 `.bin`（需要先编一个 kindletool）
- `KINDLE.md` — 看板链路与上屏步骤
- `JAILBREAK.md` — KPW2（90D4 / 5.12.2.2）完整越狱流程（WatchThis）
- `site/` — 根域主页 `index.html` + logo（`logo.svg` / `logo-lockup.svg`）
- `sshd-99-hardening.conf` — SSH 加固 drop-in（关密码、仅公钥）
- `TUNNEL.md` — Cloudflare Tunnel 接入指引
- `deploy.sh` — 部署脚本：只上传本目录的三个 `.py` 与 `services/*`，不搬引擎代码

> **抓取时间线 ≠ 显示时间线**（这是本目录最容易搞错的一点）：
> `rss-build.timer` 决定「多久去源站抓一次」（白天 5 分钟 / 夜间整点），
> `rss-rotate.timer` 决定「屏幕多久换一条」（全天 5 分钟）。两者分开设计，
> 抓取一小时一次时屏幕仍然 5 分钟一换。

## Pi 上的实际部署路径

- 服务文件：`/etc/systemd/system/`（`moreaix-site.service` 等）
- 辅助脚本：`/home/user/pi-pipeline/`（`serve.py` / `rotate_screen.py` / `kindle_screen.py` / `kindle_page.py`）
- 主页：`/home/user/site/`
- 看板图输出：`/home/user/kindle/`（`rss-pw2.png` = `kindle-1024x758.png` 的副本，KPW2 第 6 代；
  对外地址 `https://moreaix.com/kindle/rss-pw2.png`，网页版 `https://moreaix.com/kindle/`）
- RSS 引擎：`/home/user/rss-hub/`（= 仓库根目录代码在 Pi 的本地副本）

时区：`timedatectl set-timezone Asia/Shanghai`（并确认 `timedatectl` 输出里
`Time zone: Asia/Shanghai`）。原因：`rss-build.timer` 的 `07..23` 与 `00..06` 两条
`OnCalendar` 按**系统时区**解释，`Environment=TZ=` 对 timer 无效；服务单元里另写了
`TZ=Asia/Shanghai`，只是为了让脚本内部的时间与之一致。时区不对，日夜节拍会整体偏一个时区。

端口归属（改动前先看这张表，别把监听地址改错）：

| 端口 | 绑定 | 用途 | 对外暴露 |
|---|---|---|---|
| 8081 | `127.0.0.1` | 全部静态内容（主页 / `/rss` / `/kindle`） | 经 Cloudflare Tunnel → `moreaix.com/` 一条规则全覆盖 |
| 8082 | `0.0.0.0` | Kindle 看板图（**已停用**） | 无。单元还在，`systemctl enable --now kindle-serve` 可恢复 |

**8080 去哪了**（2026-11-01 合并）：它原来是独立的 `rss-serve`，跑的是**同一个 `serve.py`**，
只是 `--root` 指向 feed 目录。加一个 `--alias /rss=…` 之后 8081 一台就能同时出主页、feed 和
看板图，于是 8080 与它在 Tunnel 上那条 `/rss` 的 path 规则一起下掉了。副作用全是好的：

- Pi 上常驻进程从 3 个（cloudflared / 8080 / 8081）降到 **2 个**
- Tunnel 只剩**一条**根域规则，`/rss` 那条 path 规则没了 —— 顺序匹配、非锚定正则那两个坑
  从此不会再出现（它们都是 path 规则特有的毛病）

**8082 为什么停了**（2026-11-01）：它的设计用途是让局域网里的 Kindle 就近取图、不绕
Cloudflare。但 Pi 把 wlan0 的入站 TCP 全 DROP 了（22/53/139/445/8082 一律超时，手机与
本机都试过），这条路实际是废的 —— 24 小时内零外部访问。公网改由 8081 出图之后，
它就是纯多余的攻击面。`deploy.sh` 也不再 enable 它。

## 关于 GitHub Actions

本目录**不含任何 workflow 文件**，也不会被根目录的 Actions 运行。根目录动作只管根目录引擎；
Pi 侧由 systemd 自管。保存本目录到仓库只是**代码存档**，不新增、不触发任何动作。
