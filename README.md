# rss-hub · RSS 聚合头条

把几十个 RSS 源的**最新一条**抓下来，跨源去重、按发布时间倒序，合成一个新的 RSS，
由 **GitHub Actions** 定时重建、**GitHub Pages** 对外发布。全程免费、无需服务器、无需本机常开。

```
feeds.txt  ──►  build_feed.py  ──►  docs/feed.xml          ──►  GitHub Pages  ──►  RSS 阅读器
（源清单）      （Actions 里跑）      docs/feed-screen.xml                        ──►  墨水屏设备
```

同一份数据出两个版本：**完整版**标题原样；**墨水屏版**标题裁到 22 个全角字符、只留 12 条，
专门给 296×152 这类小屏（如 Dot Quote/0）用。

## 订阅地址

| 用途 | 地址 |
|---|---|
| 主地址（推荐） | `https://cheney-hub.github.io/rss-hub/feed.xml` |
| 墨水屏版 | `https://cheney-hub.github.io/rss-hub/feed-screen.xml` |
| 说明页 | https://cheney-hub.github.io/rss-hub/ |
| 墨水屏预览 | https://cheney-hub.github.io/rss-hub/screen.html |
| 镜像 1（raw） | `https://raw.githubusercontent.com/cheney-hub/rss-hub/main/docs/feed.xml` |
| 镜像 2（jsDelivr） | `https://cdn.jsdelivr.net/gh/cheney-hub/rss-hub@main/docs/feed.xml` |

把主地址粘进 FreshRSS / Feedly / Inoreader / NetNewsWire / 阅读 等任意 RSS 阅读器即可。
小屏设备（墨水屏、电子价签、桌面小电视）用墨水屏版，标题不会被切成半截。

## 当前实况

| 项目 | 数值 |
|---|---|
| 订阅源 | 43 个（中文综合、国际媒体中文版、财经快讯、科技、门户、港澳、新加坡中文） |
| 每源抓取 | 取最新 1 条，跨源去重后约 42～43 条 |
| 抓取频率 | 白天（北京 06:00–23:59）**每分钟一轮**；深夜（00:00–06:00）每小时一轮 |
| 发布频率 | 白天每分钟一版，深夜整点一版（自定义 Actions 部署，不受 Pages 10 次/小时限制） |
| 单轮耗时 | 3～5 秒（43 源并发 12 路） |
| 单次作业 | 白天 55 分钟 / 55 轮；单轮约 5 秒抓取 + 约 20 秒部署 |
| 墨水屏版 | 标题 ≤ 22 全角字符（约一行 22 字），只保留最新 12 条，一屏 4 条分 3 页 |

## 快速开始

| 你想做的事 | 看哪里 |
|---|---|
| 直接订阅 | 上面的主地址，无需任何操作 |
| 加减订阅源 | 改 `feeds.txt` 提交即可，Actions 自动重建（[OPERATIONS.md](OPERATIONS.md#加删订阅源)） |
| 本地跑一遍看结果 | `python3 build_feed.py`，产物在 `docs/feed.xml`（[OPERATIONS.md](OPERATIONS.md#本地调试)） |
| 换成自己的仓库 | Fork 后改三处：`FEED_URL`、Pages 设置、`docs/index.html` 里的地址（[OPERATIONS.md](OPERATIONS.md#换到自己的仓库)） |
| 搬到树莓派 / VPS | [pi/README.md](pi/README.md) / [OPERATIONS.md](OPERATIONS.md#迁移到树莓派或-vps) |
| 出问题了 | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

## 目录结构

```
.
├── feeds.txt                  # 订阅源清单：注释行 = 中文源名，下一行 = 地址
├── build_feed.py              # 【主链路】采集 + 去重 + 生成 RSS，Actions 调用
├── docs/                      # ← GitHub Pages 发布的目录（站点根）
│   ├── feed.xml               #   完整版订阅文件（每 10 轮备份提交一次）
│   ├── feed-screen.xml        #   墨水屏版：标题裁到一行、只留 12 条
│   ├── index.html             #   订阅说明页（前端读取 feed.xml 渲染）
│   └── screen.html            #   墨水屏预览：按 296×152 像素 1:1 模拟
├── .github/workflows/
│   ├── minute.yml             #   分钟级发布：白天每小时 55 轮循环，每分钟部署一次
│   ├── nightly.yml            #   夜间整点发布：北京 00:00–06:00 每小时一次
│   ├── rss.yml                #   改源即时发布：改 feeds.txt 立刻重建一次
│   └── guard.yml              #   凭据扫描：推送时检查有无混入 token
├── aggregate_rss.py           # 【备选】本地/树莓派版：抓完直接用 Contents API 推仓库
├── pi/                        # 【备选】树莓派 systemd 自启 + 定时 + 自恢复
│   ├── rss-hub.env.example    #   凭证模板（值为空，真实文件放 /etc/rss-hub/）
│   └── README.md              #   树莓派部署与排障
├── tools/check_secrets.py     # 仓库凭据扫描脚本（本地与 CI 共用）
├── tools/gen_minute_workflow.py # 生成 minute.yml（改轮数就是改这里）
├── server.js                  # 【归档】早期自托管版：Node 常驻服务，自带状态页
├── package.json               # server.js 的启动脚本
├── ARCHITECTURE.md            # 架构、算法规则、参数与取舍
├── OPERATIONS.md              # 运维手册：改源、凭证、监控、迁移
└── TROUBLESHOOTING.md         # 故障排查
```

三条通道并存，但只有第一条（Actions + Pages）是当前在用的；另外两条保留为可迁移的备选，代码都能直接跑。

## 工作方式

1. `.github/workflows/minute.yml` 在**北京 06:00–23:59** 每小时 :07 触发，作业内连跑 55 分钟；
   `.github/workflows/nightly.yml` 在**北京 00:00–06:00** 每小时整点后跑一轮（深夜新闻源更新稀少，不必每分钟抓）；
2. 每分钟一轮：抓 43 源（约 5 秒）→ 打包 `docs/` → 部署到 Pages（约 20 秒）→ 对齐到下一整分；
3. 每源只取**最新 1 条**，跨源按「链接去参数」+「标题归一化」双重去重；
4. 某源这轮抓不到，就用上一份 `feed.xml` 里该源的条目顶上，不会整条消失；
5. 每 10 轮把 `docs/feed.xml` 备份提交一次，仓库历史不会因高频发布而膨胀。

细节规则见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 小屏 / 墨水屏适配

参考 [Dot Quote/0](https://dot.mindreset.tech/docs/quote_0) 的规格（296 × 152 px、2.66 英寸、125 PPI、黑白），
同一轮采集顺带生成 `docs/feed-screen.xml`，规则：

| 项目 | 完整版 | 墨水屏版 |
|---|---|---|
| 标题长度 | 原样 | 最多 **22 个全角字符**（一行 280 px @ 11 px 字号留余量） |
| 条目数 | 最多 60 条 | 最新 **12 条**（一屏 4 条 × 3 页） |
| 截断方式 | — | 优先在 `，。：｜` 等标点断句；书名号/引号没闭合就退到其前面；补 `…` 收尾 |
| 直接可看 | 任意阅读器 | 打开 <https://cheney-hub.github.io/rss-hub/screen.html> 看 1:1 版面 |

想调宽度或条数，改运行参数即可（不用改代码）：

```bash
SCREEN_TITLE_WIDTH=18 SCREEN_ITEMS=8 python3 build_feed.py
```

对应环境变量：`SCREEN_TITLE_WIDTH`（全角字符数）、`SCREEN_ITEMS`（条数）、`SCREEN_OUT`（输出路径）。
`docs/screen.html` 是纯前端模拟，读取 `feed-screen.xml` 后按 4 条一页渲染，可翻页/放大/暂停轮播。

## 加一个源

```text
# 财联社·电报滚动
https://feed.quanwenrss.com/feed/cailianshe-roll.xml
```

注释行会成为条目里的 `<source>` 源名，建议写成「媒体名·栏目」。改完 push 就会自动重建。
想批量验证新源能否抓通，本地跑一遍，只看「[失败]」行即可：

```bash
python3 build_feed.py
```

## 已知限制

- **GitHub 的 cron 最短 5 分钟，还会延迟和丢档**（实测新仓库 50 分钟内 0 次触发）——所以分钟级不靠 cron，靠「小时级 cron + 作业内自循环」；
- **Pages 传统构建有 10 次/小时软限制**，每分钟一次会被限流；本仓库改用自定义 Actions 部署（`build_type: workflow`），官方文档明确该限制不适用于这种构建方式，实测每分钟一版可稳定发布；
- **runner 占用约 17 小时/天**（白天 18 小时 × 55 分钟 + 夜间 6 次）：公开仓库分钟数不计量，但属高频用法；若遇平台降速，把 `tools/gen_minute_workflow.py` 的轮数改小重新生成即可（如 29 轮 = 每 2 分钟一版）；
- 每分钟档期间，每小时最后约 5 分钟是收尾空档，feed 停在最后一次部署的版本；深夜则是整点之间的一小时空档；
- `github.io` 在部分网络环境访问偏慢，可用上面两个镜像（镜像跟随仓库备份，约 10 分钟一次）。

## 凭证与安全

**仓库里不含任何 token、口令或私钥**，克隆下来即可直接跑，不需要填任何东西（`python3 build_feed.py` 只要网络）。

- `.gitignore` 屏蔽 `*.env` / `.env*` / `*.pem` / `*.key` / `id_rsa*` 等；
- `tools/check_secrets.py` 可随时自查：`python3 tools/check_secrets.py`；
- `.github/workflows/guard.yml` 在每次推送时自动跑同一套规则，混进真 token 会让这次构建失败；
- 需要凭证的场景（树莓派推送、外部触发器）一律放在**仓库之外**：本机 `/etc/rss-hub/rss-hub.env`（`600`）或 GitHub 仓库 Secret。详见 [OPERATIONS.md#凭证管理](OPERATIONS.md#凭证管理)。

## 监控

- Actions 运行历史与日志：https://github.com/cheney-hub/rss-hub/actions
- 线上自检：`curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep lastBuildDate`
- 本地 WorkBuddy 侧配了一个每 3 小时的巡检：条目数 < 30 或 `lastBuildDate` 超过 45 分钟才汇报，正常静默。

## 说明

`feeds.txt` 里的源都是各媒体公开提供的 RSS 地址。本项目只做「合并 + 排序 + 去重」，不缓存正文、不转载内容，条目一律链接回原站。
