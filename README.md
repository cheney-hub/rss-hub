# rss-hub · RSS 聚合头条

> **本项目专为 [Dot (Quote/0)](https://dot.mindreset.tech/docs/quote_0) 墨水屏设备开发**——
> 一款基于 ESP32-C3 的 2.13 寸黑白墨水屏桌面设备。输出针对它的显示链路做了专门适配：
> 标题转纯文本（摘除 emoji/符号，避免墨水屏豆腐块）、按全角宽度优雅截断、字段极简化，
> 并按「Dot 云端抓取渲染、设备只拉图」的真实机制设计（详见[小屏适配](#小屏--墨水屏适配dot-quote0)）。
> 完整版 `feed.xml` 仍兼容任意标准 RSS 阅读器。

把几十个 RSS 源的**最新一条**抓下来，跨源去重、按发布时间倒序，合成一个新的 RSS，
由 **GitHub Actions** 定时重建、**GitHub Pages** 对外发布。全程免费、无需服务器、无需本机常开。

```
feeds.txt  ──►  build_feed.py  ──►  docs/feed.xml          ──►  GitHub Pages  ──►  RSS 阅读器
（源清单）      （Actions 里跑）      docs/feed-screen.xml                        ──►  墨水屏设备（单行）
                                     docs/feed-wrap.xml                          ──►  墨水屏设备（折行）
```

同一份数据出三份：**完整版**标题原样，给电脑/手机上的 RSS 阅读器；**墨水屏版**标题转纯文本并优雅截断
（13 个全角字符内，实测一行容量）；**折行版**先把第一行当标题主体，放不下的自动续到下一行（默认最多 3 行）。
条目都给足 60 条，填进 Dot Quote/0 的循环内容列表（显示样式由 Dot 云端渲染）。
全程只用 RSS 只读通路，不调用任何需要密钥的 API。

## 订阅地址

| 用途 | 地址 |
|---|---|
| 主地址（推荐） | `https://cheney-hub.github.io/rss-hub/feed.xml` |
| 墨水屏版 · 单行 | `https://cheney-hub.github.io/rss-hub/feed-screen.xml` |
| 墨水屏版 · 折行 | `https://cheney-hub.github.io/rss-hub/feed-wrap.xml` |
| 折行探针（测设备） | https://cheney-hub.github.io/rss-hub/wrap/ |
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
| 墨水屏版 | 单行版标题 ≤ 13 全角字符；折行版第一行作标题、超出续行（默认 3 行上限）；全程无密钥 |

## 快速开始

| 你想做的事 | 看哪里 |
|---|---|
| 直接订阅 | 上面的主地址，无需任何操作 |
| 加减订阅源 | 改 `feeds.txt` 提交即可，Actions 自动重建（[OPERATIONS.md](OPERATIONS.md#加删订阅源)） |
| 本地跑一遍看结果 | `python3 build_feed.py`，产物在 `docs/feed.xml`（[OPERATIONS.md](OPERATIONS.md#本地调试)） |
| 换成自己的仓库 | Fork 后改三处：`FEED_URL`、Pages 设置、`docs/index.html` 里的地址（[OPERATIONS.md](OPERATIONS.md#换到自己的仓库)） |
| 出问题了 | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |

## 目录结构

```
.
├── feeds.txt                  # 订阅源清单：注释行 = 中文源名，下一行 = 地址
├── build_feed.py              # 【主链路】采集 + 去重 + 生成三份 RSS，Actions 调用
├── docs/                      # ← GitHub Pages 发布的目录（站点根）
│   ├── feed.xml               #   完整版订阅文件（每 10 轮备份提交一次）
│   ├── feed-screen.xml        #   墨水屏版：纯文本标题 + 优雅截断（单行）+ 字段极简
│   ├── feed-wrap.xml          #   墨水屏版 · 折行：第一行标题，超出续行
│   ├── index.html             #   订阅说明页（前端读取 feed.xml 渲染）
│   ├── screen.html            #   小屏内容示意：单行 vs 折行对比
│   └── wrap/                  #   折行探针：t1~t4 四个测试源 + 说明页
├── .github/workflows/
│   ├── minute.yml             #   分钟级发布：白天每小时 55 轮循环，每分钟部署一次
│   ├── nightly.yml            #   夜间整点发布：北京 00:00–06:00 每小时一次
│   ├── rss.yml                #   改源即时发布：改 feeds.txt 立刻重建一次
│   └── guard.yml              #   凭据扫描：推送时检查有无混入 token
├── tools/check_secrets.py     # 仓库凭据扫描脚本（本地与 CI 共用）
├── tools/gen_minute_workflow.py # 生成 minute.yml（改轮数就是改这里）
├── tools/gen_wrap_probe.py    # 生成折行探针源（测设备认哪种换行符）
├── ARCHITECTURE.md            # 架构、算法规则、参数与取舍
├── OPERATIONS.md              # 运维手册：改源、凭证、监控、迁移
└── TROUBLESHOOTING.md         # 故障排查
```

## 工作方式

1. `.github/workflows/minute.yml` 在**北京 06:00–23:59** 每小时 :07 触发，作业内连跑 55 分钟；
   `.github/workflows/nightly.yml` 在**北京 00:00–06:00** 每小时整点后跑一轮（深夜新闻源更新稀少，不必每分钟抓）；
2. 每分钟一轮：抓 43 源（约 5 秒）→ 打包 `docs/` → 部署到 Pages（约 20 秒）→ 对齐到下一整分；
3. 每源只取**最新 1 条**，跨源按「链接去参数」+「标题归一化」双重去重；
4. 某源这轮抓不到，就用上一份 `feed.xml` 里该源的条目顶上，不会整条消失；
5. 每 10 轮把 `docs/feed.xml` 备份提交一次，仓库历史不会因高频发布而膨胀。

细节规则见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 小屏 / 墨水屏适配（Dot Quote/0）

**先说清机制**（官方文档：[内容模式](https://dot.mindreset.tech/docs/quote_0/start/content_mode)、[时间机制](https://dot.mindreset.tech/docs/quote_0/faq/refresh_timing)、[RSS](https://dot.mindreset.tech/docs/service/studio/rss_feed)）：
Dot Quote/0 的设备端（ESP32-C3，400 KB 内存）**不解析 RSS XML**。在 Dot App 里把 RSS 地址加进
「循环内容」列表后，由 **Dot 云端**抓取解析、按自家模板渲染成位图，设备按刷新间隔（App 里 1 分钟～12 小时可调）
从云端拉图显示。**屏幕上显示几条、字号多大、怎么排版，都由 Dot 云端决定**，订阅方控制不了；
云端多久抓一次本源也由 Dot 调度，但本源每分钟都是新鲜的，它随时来抓都是最新数据。

因此小屏版不做「猜测排版」，只做云端渲染照顾不到的事：

**实测结论（2026-10-29，296×152 屏）**：RSS 卡片在设备上就是**一行大字号标题**，约 13～14 个全角字就满宽，
超出的部分由云端从右边硬切（并补它自己的 `…`）。既然宽度抢不回来，就往下要空间：
**把标题折成多行**（见下节），第一行作标题主体、放不下的自动续行。

| 项目 | 完整版 feed.xml | 墨水屏版 feed-screen.xml |
|---|---|---|
| 标题字符 | 原样（可含 emoji） | **纯文本**：emoji、箭头、几何图形、零宽字符、变体选择符全部摘除（黑白墨水屏上它们是豆腐块或占位） |
| 标题长度 | 原样 | 最多 **13 个全角字符**（实测一行容量）；超宽时优先在 `，、；！？` 处收尾（但必须保住 80% 宽度，宁可硬切也不砍成半句）、书名号/引号不切开、最后补 `…`——让我们的截断先发生，云端就不会再切第二刀 |
| 条目数 | 最多 60 条 | **60 条**（云端取「最新 N 条」，N 由 Dot 决定，给满随它挑） |
| 每条字段 | title / link / pubDate / guid / source | 同左，但 **guid 用 12 位短哈希**、channel 去掉 `atom:link`、`generator`、`ttl` |
| 用途 | 电脑/手机上的 RSS 阅读器 | 填进 Dot App 的循环内容列表 |

在 Dot App 里订阅时填这个地址：

```
https://cheney-hub.github.io/rss-hub/feed-screen.xml
```

> 判断是否用对了源：设备页首小字标题是 **「RSS 头条 · 墨水屏」** 才说明订阅的是墨水屏版；
> 如果是「RSS 聚合头条」，那是完整版，标题没有任何预处理，被切得更难看。

## 标题折行：第一行作标题，超出续行

单行 13 字实在不够读。宽度抢不回来，就往下要空间——规则很简单：
**第一行放标题主体（在标点处收尾），放不下的自动续到下一行，最多 3 行**，末行还装不下才补 `…`。

能否真的显示成多行，取决于一个我们控制不了的点：**Dot 云端抓取时会不会保留标题里的换行符**。
所以除了单行版，另外输出一份**折行版** `feed-wrap.xml`：

| 源 | 标题实际长什么样 | 一屏能读到 |
|---|---|---|
| `feed-screen.xml` | `花旗：将微软(MSFT.O…` | 13 字 |
| `feed-wrap.xml` | `花旗：将微软(MSFT.O)` / `目标价从636美元上调至680美` / `元。` | 约 39 字 |

**先用探针确认设备认哪种换行**（4 个源，同一句话，只有换行表示方式不同）：

https://cheney-hub.github.io/rss-hub/wrap/

| 探针 | 换行怎么表示 | 判定 |
|---|---|---|
| `t1.xml` | 真实换行符（`&#10;`） | 屏上 3 行 → 生效，用 `feed-wrap.xml` 默认配置 |
| `t2.xml` | `<br>`（CDATA 包裹） | 屏上 3 行 → 说明云端按 HTML 解析，设 `SCREEN_WRAP_SEP=br` |
| `t3.xml` | U+2028 行分隔符 | 屏上 3 行 → 设 `SCREEN_WRAP_SEP=u2028` |
| `t4.xml` | 同一句话拆成 3 条 item | 屏上段落会变 → 云端会轮播同源多条，可走「拆分多条」支路 |

全都只显示 1 行、后面被切 → 换行被云端吃掉了，折行此路不通，只能回到单行版
（`SCREEN_TITLE_WIDTH` 调小一点，至少让断句位置好看些）。

**折行版失效也不会更差**：第一行前 13 字与单行版内容一致，最坏情况就是退回「一屏一行」，
所以可以放心加进 App 对比着看。调参不用改代码：

```bash
SCREEN_WRAP_LINES=4 SCREEN_WRAP_SEP=lf python3 build_feed.py   # 折 4 行
```

探针源由 `tools/gen_wrap_probe.py` 生成（同一句话、行宽与正式源一致，避免结论失真）。

## 为什么不用 Dot 文本 API

Dot 还有一个文本 API（`POST /api/authV2/open/device/{id}/text`），版面固定是「标题 1 行 + 正文 3 行 + 落款」，
字号行高可调，理论上能一屏 4 条。早期版本接入过（`push_text.py`），之后**主动下线**，原因只有一个：它要求

- 在 Dot App 里新建 API 密钥（`dot_app_…` 开头），并把**设备序列号**一起交给调用方；
- 每一轮采集都带着这个密钥向设备发起一次写请求。

这个密钥等同于设备写权限。它一旦落到仓库、CI 日志或第三方 runner 上，就等于把设备控制权交了出去；
而本项目的立身之本是「**零凭证**」——多出一屏几条的收益，不值得长期背一个可直接控制设备的口令。

所以现在只走 RSS 这一条只读通路：不发起任何写请求，仓库里没有任何密钥。
`tools/check_secrets.py` 把 `dot_app_…` 形态也列进了拦截规则，CI 每次推送都会扫。

> 确实需要一屏多条时，建议把本仓库 Fork 出去单独改造，别在零凭证仓库里开这个口子。

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
- 需要凭证的场景（外部触发器）一律放在**仓库之外**：GitHub 仓库 Secret。详见 [OPERATIONS.md#凭证管理](OPERATIONS.md#凭证管理)。

## 监控

- Actions 运行历史与日志：https://github.com/cheney-hub/rss-hub/actions
- 线上自检：`curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep lastBuildDate`
- 本地 WorkBuddy 侧配了一个每 3 小时的巡检：条目数 < 30 或 `lastBuildDate` 超过 45 分钟才汇报，正常静默。

## 说明

`feeds.txt` 里的源都是各媒体公开提供的 RSS 地址。本项目只做「合并 + 排序 + 去重」，不缓存正文、不转载内容，条目一律链接回原站。
