# rss-hub · RSS 聚合头条

> **专为 [Dot (Quote/0)](https://dot.mindreset.tech/docs/quote_0) 墨水屏设备开发**——
> 一款基于 ESP32-C3 的 2.13 寸黑白墨水屏桌面设备。输出按它的真实显示链路做过专门适配：
> 标题转纯文本（摘掉 emoji / 装饰符号，避免墨水屏豆腐块）、按 13 全角折行（第一行作标题、超出续行）、
> 字段极简化。完整版 `feed.xml` 同时兼容任意标准 RSS 阅读器。

把几十个 RSS 源的**最新一条**抓下来，跨源去重、按发布时间倒序，合成一个新的 RSS，
由 **GitHub Actions** 定时重建、**GitHub Pages** 对外发布。全程免费、无需服务器、无需本机常开，
**不调用任何需要密钥的接口**。

```
feeds.txt ──► build_feed.py ──► docs/feed.xml       ──► GitHub Pages ──► RSS 阅读器
（源清单）    （Actions 里跑）   docs/feed-wrap.xml                    ──► Dot 设备（折行）
```

## 订阅地址

| 用途 | 地址 |
|---|---|
| **设备版**（Dot App 里填这个） | `https://cheney-hub.github.io/rss-hub/feed-wrap.xml` |
| 完整版（电脑 / 手机阅读器） | `https://cheney-hub.github.io/rss-hub/feed.xml` |
| 说明页 | https://cheney-hub.github.io/rss-hub/ |
| 屏幕效果预览 | https://cheney-hub.github.io/rss-hub/screen.html |
| 镜像 1（raw） | `https://raw.githubusercontent.com/cheney-hub/rss-hub/main/docs/feed.xml` |
| 镜像 2（jsDelivr） | `https://cdn.jsdelivr.net/gh/cheney-hub/rss-hub@main/docs/feed.xml` |

完整版粘进 FreshRSS / Feedly / Inoreader / NetNewsWire / 阅读 等任意阅读器即可。
设备版填进 Dot App 的「循环内容」列表——**页首小字显示「RSS 头条 · 折行」才说明用对了源**。

## 当前实况

| 项目 | 数值 |
|---|---|
| 订阅源 | 43 个（中文综合、国际媒体中文版、财经快讯、科技、门户、港澳、新加坡中文） |
| 每源抓取 | 取最新 1 条，跨源去重后约 42～43 条，上限 60 条 |
| 抓取频率 | 白天（北京 06:00–23:59）**每分钟一轮**；深夜（00:00–06:00）每小时一轮 |
| 发布频率 | 白天每分钟一版，深夜整点一版（自定义 Actions 部署，不受 Pages 10 次/小时限制） |
| 单轮耗时 | 3～5 秒（43 源并发 12 路） |
| 设备版标题 | 每行 13 全角、最多 3 行，第一行作标题主体，放不下的续行 |

## 快速开始

| 你想做的事 | 看哪里 |
|---|---|
| 直接订阅 | 上面的地址，无需任何操作 |
| 加减订阅源 | 改 `feeds.txt` 提交即可，Actions 自动重建（[OPERATIONS.md](OPERATIONS.md#加减订阅源)） |
| 本地跑一遍 | `python3 build_feed.py`（零依赖，Python 3.8+） |
| 调刷新节奏 / 折行参数 | [OPERATIONS.md](OPERATIONS.md#调参) |
| 出问题了 | [OPERATIONS.md 故障排查](OPERATIONS.md#故障排查) |

## 目录结构

```
.
├── feeds.txt                # 订阅源清单：注释行 = 中文源名，下一行 = 地址
├── build_feed.py            # 主程序：采集 + 去重 + 生成两份 RSS
├── docs/                    # ← GitHub Pages 发布的目录（站点根）
│   ├── feed.xml             #   完整版（每 10 轮备份提交一次）
│   ├── feed-wrap.xml        #   设备版：纯文本标题 + 折行 + 字段极简
│   ├── index.html           #   订阅说明页
│   └── screen.html          #   屏幕效果预览（读 feed-wrap.xml）
├── .github/workflows/
│   ├── minute.yml           #   白天：每小时 55 轮，每分钟部署一次（脚本生成）
│   ├── nightly.yml          #   夜间：北京 00:00–06:00 每小时一次
│   ├── rss.yml              #   改源即时发布
│   └── guard.yml            #   推送时扫描凭据
├── tools/gen_minute_workflow.py  # 生成 minute.yml（改轮数就是改这里）
├── tools/check_secrets.py        # 凭据扫描（本地与 CI 共用）
├── README.md                # 本文
└── OPERATIONS.md            # 运维手册 + 故障排查
```

## 工作方式

1. `.github/workflows/minute.yml` 在**北京 06:00–23:59** 每小时 :07 触发，作业内连跑 55 分钟；
   `nightly.yml` 在**北京 00:00–06:00** 每小时跑一轮（深夜新闻源更新稀少，不必每分钟抓）。
2. 每分钟一轮：并发抓 43 源（约 5 秒）→ 生成两份 feed → 打包 `docs/` → 部署到 Pages（约 20 秒）→ 对齐到下一整分。
3. 每源只取**最新 1 条**——需求就是「头条合并」，43 源 × 1 条刚好一屏扫完；取多会让高频快讯源刷屏。
4. 跨源两道去重，命中任一条即丢弃：
   - **链接**：去掉 `#` 片段与 `?` 参数再比，规避 `?utm_source=` 造成的伪重复；
   - **标题**：去标点空白、只留汉字取前 20 字，且**须 ≥ 8 字**才参与比较（否则「港股收盘」这类短标题会被误杀）。
5. 某源这轮抓不到：立即重试一次 → 仍失败就用上一份 `feed.xml` 里该源的条目顶上（保留原 `pubDate`，
   排序位置不变），单源抖动不会让整份 feed 少一条。
6. 每 10 轮把 `docs/feed.xml` 备份提交一次，仓库历史不会因高频发布而膨胀。

`guid` 用「文章链接|源地址」：同一篇文章被两个源同时收录时 guid 仍不同，阅读器不会吞条目。
设备版则把 guid 换成该串的 12 位 md5，体积降到十几字节。

## 设备适配（Dot Quote/0）

**先看机制**（官方文档：[内容模式](https://dot.mindreset.tech/docs/quote_0/start/content_mode)、
[刷新时间](https://dot.mindreset.tech/docs/quote_0/faq/refresh_timing)、
[RSS](https://dot.mindreset.tech/docs/service/studio/rss_feed)）：

设备端（ESP32-C3，400 KB 内存）**不解析 RSS XML**。在 Dot App 里把 RSS 地址加进「循环内容」后，
由 **Dot 云端**抓取解析、按自家模板渲染成位图，设备按刷新间隔（App 里 1 分钟～12 小时可调）拉图显示。
**显示几条、字号多大、怎么排版全由云端决定**，订阅方控制不了；云端多久抓一次本源也由它调度，
但本源每分钟都是新鲜的，随时来抓都是最新。

我们唯一能控制的，是「喂给云端的原料」：

| 项目 | 完整版 `feed.xml` | 设备版 `feed-wrap.xml` |
|---|---|---|
| 标题字符 | 原样（可含 emoji） | **纯文本**：emoji、箭头、几何图形、零宽字符、变体选择符全部摘除（黑白屏上是豆腐块或占位） |
| 标题排版 | 原样 | **折行**：每行 13 全角，第一行作标题主体，放不下的续到下一行，最多 3 行 |
| 条目数 | 上限 60 条 | 60 条（云端取「最新 N 条」，N 由它决定，给满随它挑） |
| 字段 | title / link / pubDate / guid / source，含 `atom:link`、`generator`、`ttl` | 同样字段，但 guid 用 12 位短哈希，channel 去掉上述三项 |

**实测依据（2026-10-29，296×152 屏）**：RSS 卡片是一行大字号，约 13～14 个全角字就满宽，
超出部分由云端从右边硬切并补它自己的省略号。宽度抢不回来，就往下要空间——折行：

```
花旗：将微软(MSFT.O)          ← 第一行：标题主体（13 全角满载，标点处收尾）
目标价从636美元上调至680美      ← 续行：放不下的自动接上
元。
```

排版细节：行尾优先落在 `，、；！？` 等标点；断点必须保住 80% 宽度，宁可硬切也不砍成半句；
`《》“”（）` 没闭合会退到符号前，不会切成半截；末行只剩一两个字时从上一行尾部挪字过来补，
但**撞到标点就停**（避免出现「，同比增长…」这种行首标点）；只有末行还装不下才补 `…`。

**断句不认冒号**是有原因的：「雷普索尔首席执行官：展望未来」若在冒号处收尾只剩主体、谓语全丢。

**折行不生效怎么办**：RSS 卡片能否真显示成多行，取决于云端是否保留标题里的换行符——这一点无法预知，
所以做了配置项。屏上仍是一行，依次试：

```bash
SCREEN_WRAP_SEP=br python3 build_feed.py       # 云端按 HTML 解析
SCREEN_WRAP_SEP=u2028 python3 build_feed.py    # 老派行分隔符
```

三种都不行就是云端吃掉了换行，折行此路不通，退回单行（把 `SCREEN_LINE_WIDTH` 调小一点，
至少让断句位置好看些）。**折行失效也不会更差**：第一行前 13 字就是单行版的内容。

改工作流 `env:` 才能长期生效，参数说明见 [OPERATIONS.md#调参](OPERATIONS.md#调参)。

## 为什么不用 Dot 文本 API

Dot 还有一个文本 API（`POST /api/authV2/open/device/{id}/text`），版面固定「标题 1 行 + 正文 3 行 + 落款」，
字号行高可调，理论上能一屏 4 条。早期版本接入过，之后**主动下线**，原因只有一个：

- 它要求在 Dot App 里新建 API 密钥（`dot_app_…` 开头），并把**设备序列号**一起交给调用方；
- 之后每一轮采集都要带着这个密钥向设备发起一次写请求。

这个密钥等同于设备写权限。一旦落到仓库、CI 日志或第三方 runner 上，就等于把设备控制权交了出去。
本项目的立身之本是**零凭证**，多一屏几条的收益不值得长期背一个能直接控制设备的口令。
现在只走 RSS 这一条只读通路：不发起任何写请求，仓库里没有任何密钥；
`tools/check_secrets.py` 把 `dot_app_…` 形态也列进了拦截规则。

## 加一个源

```text
# 财联社·电报滚动
https://feed.quanwenrss.com/feed/cailianshe-roll.xml
```

注释行成为条目里的 `<source>` 源名，建议写成「媒体名·栏目」；括号里的备注会被剥掉。
改完 push 就自动重建。批量验证新源能否抓通，本地跑一遍只看 `[失败]` 行：

```bash
python3 build_feed.py
```

## 已知限制

- **GitHub 的 cron 最短 5 分钟，还会延迟和丢档**（实测新仓库 50 分钟内 0 次触发）——所以分钟级不靠 cron，
  靠「小时级 cron + 作业内自循环」；
- **Pages 传统构建有 10 次/小时软限制**，每分钟一次会被限流；本项目改用自定义 Actions 部署
  （`build_type: workflow`），官方文档明确该限制不适用于这种构建方式；
- **runner 占用约 17 小时/天**（白天 18 小时 × 55 分钟 + 夜间 6 次）：公开仓库分钟数不计量，但属高频用法；
  想降载就改轮数（见 [OPERATIONS.md#刷新节奏](OPERATIONS.md#刷新节奏)）；
- 每分钟档期间，每小时最后约 5 分钟是收尾空档；深夜则是整点之间的一小时空档；
- `github.io` 在部分网络环境访问偏慢，可用上面两个镜像（跟随仓库备份，约 10 分钟一次）；
- 折行能否生效取决于 Dot 云端，不受本项目控制（见上节）。

## 凭证与安全

**仓库里不含任何 token、口令或私钥**，克隆下来直接跑，不需要填任何东西。

- `.gitignore` 屏蔽 `*.env` / `.env*` / `*.pem` / `*.key` / `id_rsa*` 等；
- `tools/check_secrets.py` 可随时自查：`python3 tools/check_secrets.py`；
- `.github/workflows/guard.yml` 每次推送自动跑同一套规则，混进真 token 会让构建失败。

## 监控

- Actions 运行历史：https://github.com/cheney-hub/rss-hub/actions
- 线上自检：`curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep lastBuildDate`

## 说明

`feeds.txt` 里的源都是各媒体公开提供的 RSS 地址。本项目只做「合并 + 排序 + 去重」，
不缓存正文、不转载内容，条目一律链接回原站。
