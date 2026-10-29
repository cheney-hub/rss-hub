# 架构说明

## 一、三条通道

| 通道 | 代码 | 常驻要求 | 刷新下限 | 现状 |
|---|---|---|---|---|
| **A. GitHub Actions + Pages** | `build_feed.py` + `minute.yml` / `nightly.yml` | 无 | 白天 **1 分钟**，深夜 **1 小时** | **当前线上** |
| B. 本地 / 树莓派推送 | `aggregate_rss.py` + `pi/` | 需一台常开的机器 | 取决于 timer 周期（可 1 分钟） | 备选 |
| C. 自托管服务 | `server.js` | 需常驻进程与公网入口 | 60 秒 | 归档 |

三者的抓取与去重规则完全一致，切换只换执行环境，`feeds.txt` 不用改。

## 二、通道 A 的数据流

```
              ┌───────────────── GitHub ─────────────────┐
feeds.txt ──► │ Actions 作业（白天每小时触发，内跑 55 轮） │
              │   ├─ 并发 12 路抓 43 个源                │
              │   ├─ 每源解析出「最新 1 条」              │
              │   ├─ 失败源 → 用上一份 feed 的同源条目顶上 │
              │   ├─ 跨源去重 + 按时间倒序                │
              │   └─ 生成 docs/feed.xml                  │
              │           ↓ 打包为 artifact               │
              │           ↓ deploy-pages 部署             │
              └───────────────────┬──────────────────────┘
                                  ↓
                    Pages（自定义 Actions 部署产物）
                                  ↓
                   https://cheney-hub.github.io/rss-hub/feed.xml
```

一次作业的大致时序：

| 时间点 | 动作 |
|---|---|
| 触发 | `minute.yml` 北京 06:00–23:59 每整点 :07；`nightly.yml` 北京 00:00–06:00 每整点 :07（只 1 轮） |
| T+0s | 检出仓库、装 Python 3.12（约 15 秒，仅一次） |
| T+15s | 第 1 轮：并发抓 43 源（3～5 秒）→ 打包 → 部署（约 20 秒） |
| T+60s | 第 2 轮（sleep 到整分后再抓） |
| … | 每小时 55 轮，直到 T+55min 作业结束 |
| 每 10 轮 | 把 `docs/feed.xml` 备份提交一次（每小时 5～6 次） |

每轮都会真实部署一次，所以订阅端看到的节奏就是 1 分钟。作业内循环而不是靠 cron，是因为 GitHub 的 schedule 最短 5 分钟且会丢档；用 artifact 部署而不是 git commit，是因为 Pages 传统构建有 10 次/小时软限制。两个限制叠加起来，只有这套组合能做到稳定的分钟级。

## 三、`build_feed.py` 模块分解

| 环节 | 函数 | 说明 |
|---|---|---|
| 读源 | `read_feed_entries()` | 注释行存为待用源名，遇到地址行配对；`#` 行尾注释会被截断；`====` 分组标题不算源名 |
| 抓取 | `http_get()` | 20 秒超时，gzip，伪装常规浏览器 UA + Accept |
| 解码 | `decode_body()` | 先 UTF-8，失败退 GB18030（老中文站常见），再退 replacement |
| 修补 | `_sanitize()` | 补 `&nbsp;` 等未定义 HTML 实体、删控制字符、去掉 XML 声明里的 `encoding`（否则 ElementTree 对 str 报错或按错编码解码） |
| 解析 | `parse_latest()` | 支持 RSS 2.0 / RSS 1.0(RDF) / Atom；`ParseError` 时按 `<item>`/`<entry>` 块正则兜底 |
| 取最新 | `parse_latest()` 内部 | `pubDate` → `published` → `updated` → `date` 依次尝试；`email.utils.parsedate_to_datetime` 与 ISO8601 双解析；解析不到记 0 分 |
| 去重 | `dedupe()` / `norm_title()` | 见第四节 |
| 回退 | `load_prev_items()` | 从上一份 `feed.xml` 抽出「源地址 → 条目」映射 |
| 生成 | `build_rss()` | RSS 2.0 + `atom:link self`，`ttl=5`，条目上限 60 |

## 四、关键规则

### 1. 源名怎么来

`feeds.txt` 里紧挨在地址上面的那条注释就是源名，写入 `<source url="...">源名</source>`。
括号备注会被剥掉（`# 新华社·国际（备用）` → `新华社·国际`），`====` / `----` 这类分组分隔行不会被当成源名。

### 2. 为什么每源只取 1 条

需求就是「头条合并」：43 个源 × 1 条 ≈ 43 条，刚好是一屏能扫完的体量；取多会让高频快讯源（金十、雪球、财联社）刷屏，把慢节奏的深度源挤掉。

### 3. 去重规则

两道关，命中任一条即丢弃：

- **链接去重**：去掉 `#` 之后的片段和 `?` 之后的参数再比，规避 `?utm_source=` 这类跟踪参数造成的伪重复；
- **标题去重**：标题去掉标点/空白/英文数字，只留汉字，取前 20 字；**长度 ≥ 8 个汉字**才参与比较。

阈值取 8 是有原因的：早期用 4 时，`港股开盘`、`A股收盘` 这类短标题会被误杀。短标题只做链接去重，不做标题去重。

### 4. `guid` 的设计

```xml
<guid isPermaLink="false">文章链接|源地址</guid>
```

带上源地址，是为了让「同一篇文章被两个源同时收录」时两条 guid 仍不同——阅读器不会因为 guid 冲突把其中一条吞掉（重复本身已在上一步去掉了，这里只是保险）。

### 5. 失败回退

某个源这轮 403/超时/解析不出条目：

1. 立即重试一次（间隔 1 秒）；
2. 仍失败 → 从上一份 `feed.xml` 里取该源上一条，原样带上（`stale` 计数 +1）；
3. 若该源历史上从没成功过 → 才真正缺一条，并在日志打 `[失败]`。

这样单源抖动不会让整份 feed 少一条，也不会让某条旧闻一直卡在顶部——它保留的是原本的 `pubDate`，排序位置不变。

### 6. 时间与排序

全部换算成 Unix 时间戳排序，无日期的记 0 分沉底；输出时优先沿用原始 `pubDate` 字符串（保留原时区），没有才用时间戳格式化。

## 五、参数表

### `build_feed.py`

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FEEDS_FILE` | `./feeds.txt` | 源清单 |
| `OUT_FILE` | `./docs/feed.xml` | 输出文件 |
| `PREV_FILE` | 同 `OUT_FILE` | 回退用的上一份 feed |
| `FEED_URL` | `https://cheney-hub.github.io/rss-hub/feed.xml` | 频道 `<link>` |
| `REFRESH_SECONDS` | `0` | 循环刷到第几秒收工，0 = 只跑一轮 |
| `FETCH_TIMEOUT` | `20` | 单源超时（秒） |
| `CONCURRENCY` | `12` | 并发抓取数 |
| `MAX_ITEMS`（代码常量） | `60` | 输出条目上限 |

### `aggregate_rss.py`（通道 B）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GITHUB_TOKEN` | 空 | PAT，需 `repo` 写权限；不设则只本地生成 |
| `GITHUB_REPO` | 空 | `owner/repo` |
| `GITHUB_BRANCH` | `main` | 推送分支 |
| `FEED_PATH` | `docs/feed.xml` | 仓库内路径 |
| `FEED_URL` | 按 owner/repo 推算 | 频道 `<link>`，会自动去掉 `docs/` 前缀 |
| `DRY_RUN` | 空 | `=1` 只生成不推送 |

### `server.js`（通道 C）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `3000` | 监听端口 |
| `CACHE_TTL` | `60` | 后台刷新周期（秒） |
| `FETCH_TIMEOUT` | `20000` | 单源超时（毫秒） |
| `FETCH_CONCURRENCY` | `12` | 并发数 |

接口：`/feed.xml`、`/rss.xml`（同内容）、`/status.json`（`ok`/`items`/`sources`/`lastBuiltAt`/`refreshing`）、`/` 状态页。
注意 `/healthz` 被部分托管平台占用，所以自检接口叫 `/status.json`。

## 六、设计取舍

**为什么不用第三方 rss 库？** 目标环境是「GitHub 上零配置跑起来」，`feedparser`/`lxml` 都要装依赖；源站 XML 的坑（实体、编码、声明）用一个 `_sanitize()` + 正则兜底就能覆盖大部分，避免为 43 个源引入整套依赖树。

**为什么发布用 Pages 而不是实时接口？** Real-time 需要常驻进程和公网入口，那就要服务器；静态文件 + CDN 才是真正免维护。代价是刷新延迟，用户已知。

**为什么不缓存正文？** 只做标题聚合，正文留在原站，既省流量也避免版权问题。

**为什么 `docs/` 当站点根？** Pages 支持「分支 + 目录」发布，把生成物和源码分开，改代码不会误发布源码，且仓库根目录保持干净。
