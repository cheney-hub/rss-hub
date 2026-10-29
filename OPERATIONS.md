# 运维手册

## 日常操作

### 加减订阅源

编辑 `feeds.txt`：注释行（源中文名）在上，地址行在下，空行随便加。

```text
# ---------- 财经 ----------

# 财联社·电报滚动
https://feed.quanwenrss.com/feed/cailianshe-roll.xml
```

删源直接删那两行；想临时停用就在地址行前加 `#`。
改完 `git push` 到 `main`，`rss.yml` 会立刻重建并发布一次（约 30 秒），不用等整点。

**先验证再提交**，避免把 404 源带上线：

```bash
python3 build_feed.py 2>&1 | grep -E "^\[(失败|回退)\]"
```

### 改源名

改地址上方那行注释即可。注释会写成 `<source>`，括号里的备注会被自动剥掉，别把重要信息放括号里。

### 调参

抓取与产出参数都在工作流的 `env:` 段里（`minute.yml` 是脚本生成的，改完要重新生成）：

```bash
# 改 tools/gen_minute_workflow.py 的模板 → 重新生成
python3 tools/gen_minute_workflow.py 55
```

| 变量 | 现值 | 调整建议 |
|---|---|---|
| 轮数 | `55` | `gen_minute_workflow.py 55` 的最后那个数字；改小可降 runner 占用（29 轮 = 每 2 分钟一版） |
| `CONCURRENCY` | `12` | 源增到 60+ 可提到 16；被源站限流时降到 8 |
| `FETCH_TIMEOUT` | `20` | 慢源多可提到 25；注意 `timeout-minutes` 是总闸 |
| `SCREEN_LINE_WIDTH` | `13` | 标题行宽度（全角字符数）。296×152 屏真机实测 13 字顶格，多出来的字云端直接切；调小会让标题更短、正文更长 |
| `SCREEN_BODY_MAX` | `60` | 正文（`<description>`）最多保留多少全角字，约合 3 行。给 0 表示不限 |

设备版还自动做了三件事，不用配：标题转纯文本（emoji / 箭头 / 零宽字符全摘掉）、
标题超宽时把余下原文搬进 `<description>`（一个字都不丢）、
字段极简（guid 短哈希、去掉 `atom:link`/`generator`/`ttl`，同条数下体积约 −22%，仍完全合法）。

调完看 <https://cheney-hub.github.io/rss-hub/screen.html> 预览效果，以设备实际显示为准。

### 手动触发一次

- 网页：仓库 → **Actions** → 左侧「RSS 聚合」→ **Run workflow** → 选 `main`；
- 命令行（需 PAT）：

```bash
curl -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/cheney-hub/rss-hub/actions/workflows/rss.yml/dispatches \
  -d '{"ref":"main"}'
```

### 本地调试

```bash
python3 build_feed.py                                        # 生成 docs/feed.xml + docs/feed-wrap.xml
OUT_FILE=/tmp/t.xml REFRESH_SECONDS=0 python3 build_feed.py   # 换路径、只跑一轮
```

零第三方依赖，Python 3.8+，Windows / macOS / Linux 都一样。

### 回滚某次发布

`docs/` 下都是生成物，直接回退那一次提交即可：

```bash
git log --oneline -- docs/feed.xml | head
git checkout <commit> -- docs/feed.xml docs/feed-wrap.xml
git commit -m "revert feed" && git push
```

下一轮定时任务会重新生成（回退只影响「失败回退」的素材，不影响正常抓取）。

## 仓库与 Pages 设置

当前配置（已生效，无需重做）：

| 项目 | 值 |
|---|---|
| 仓库 | `cheney-hub/rss-hub`（public） |
| 默认分支 | `main` |
| Pages 源 | 自定义 Actions 部署（`build_type: workflow`），产物来自 `docs/` |
| 访问地址 | `https://cheney-hub.github.io/rss-hub/` |

### 换到自己的仓库

1. Fork 或新建空仓库，把代码推上去；
2. 改地址（三处）：`.github/workflows/*.yml` 里的 `FEED_URL`、`build_feed.py` 的 `FEED_URL` 默认值、
   `docs/index.html` 里输入框的 `value` 与「源码仓库」链接；
3. **Settings → Pages** → Source 选 **GitHub Actions**（不是 Deploy from a branch）；
4. **Settings → Actions → General → Workflow permissions** 选 **Read and write permissions**（否则作业无法提交 feed）；
5. 等 1～2 分钟，访问 `https://<用户名>.github.io/<仓库名>/feed.xml`。

## 刷新节奏

当前是 **5 分钟一版**（设备刷新周期也是 5 分钟），全天不间断，runner 占用约 13 轮 × 24 小时。
节奏与轮播绑定：`ROTATE_SECONDS` 就是「一条在屏上待多久」，改它等于改轮播速度。

| 目标节奏 | 怎么改 | 一轮走完（43 条） |
|---|---|---|
| **5 分钟（现状）** | `gen_minute_workflow.py 13`，周期 300 秒，cron `7 * * * *` | 约 3.6 小时 |
| 10 分钟（CDN 缓存吃条时改这个） | `tools/gen_minute_workflow.py` 里 `PERIOD=600`、轮数 6；yml 里 `ROTATE_SECONDS: "600"` | 约 7 小时 |
| 2 分钟 | `PERIOD=120`、轮数 30；不要动 `ROTATE_SECONDS`（否则设备跟不上） | 约 1.4 小时 |
| 省 runner | `gen_minute_workflow.py 1`（每小时只跑一轮），轮播自然变成 1 小时一条 | 约 1.8 天 |

两个绕不开的约束，也是这套设计的原因：

- **cron 不能写 `*/1`**：Actions 的 schedule 最短 5 分钟，且官方承认高负载会延迟、丢档；稳定节拍只能靠
  「小时级 cron + 作业内循环 + 对齐到整点」；
- **不能靠 commit 发布**：Pages 传统构建有 10 次/小时软限制；改用自定义 Actions 部署后不受限，
  所以流程是「打包 artifact → `deploy-pages`」。

轮播状态（`docs/rotate.json`）随 artifact 一起发布，不提交进仓库（`.gitignore` 已忽略）。

## 轮播调参

| 变量 | 默认 | 作用 |
|---|---|---|
| `ROTATE_SECONDS` | 300 | 一条在屏上待多久 = 设备刷新周期。**必须 ≥ 设备刷新间隔**，否则设备会跳过条目 |
| `ROTATE_POOL_MAX` | 0 | 轮播池上限，0 = 全部（去重后的所有条目，通常 120 条）。设为 12 就是「只轮最近 12 条」 |
| `PER_SOURCE` | 3 | 每源取几条进池 |
| `ITEM_LIMIT` | 120 | 去重后条目上限，超出的按时间砍最旧 |
| `SCREEN_BODY_MAX` | 90 | 正文最多多少全角 = 屏上正文区约 4 行 × 22 字 |
| `ROTATE_STATE_URL` | 自动 | 线上状态地址，换仓库时跟随 `FEED_URL` 自动推导 |

调参后**不需要清空状态**：池子变大变小、条目消失，`shown` 里对不上的 guid 会被自动清理。

## 凭证与安全

**硬约定：任何文件里都不出现 token、口令、私钥。** 三条保护：

| 保护 | 位置 | 作用 |
|---|---|---|
| `.gitignore` 屏蔽 | `*.env` / `.env*` / `*.pem` / `*.key` / `id_rsa*` | 凭证文件即使放项目里也不会被 add |
| 提交前扫描 | `tools/check_secrets.py` | `python3 tools/check_secrets.py`，命中即退出码 1 |
| CI 强制扫描 | `.github/workflows/guard.yml` | 每次推送自动跑，混进真 token 直接失败 |

| 场景 | 存放位置 | 说明 |
|---|---|---|
| Actions 定时提交 feed | 无需你提供 | 仓库内置 `GITHUB_TOKEN`（`permissions: contents: write`），作业结束即失效 |
| 手动 / 外部触发工作流 | 仓库 Secret（`GH_PAT`）或本地环境变量 | 需要 `workflow` 权限 |

轮换：GitHub → Settings → Developer settings → Personal access tokens → 吊销旧的、生成新的 → 更新 Secret。

## 监控

- **Actions**：https://github.com/cheney-hub/rss-hub/actions —— 耗时、日志、失败轮次都在这里；
- **线上自检**：

```bash
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep -E "lastBuildDate"
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep -c "<item>"
```

- 接第三方监控（UptimeRobot / Cronitor）只监控 200 状态，别用「内容关键字」判据（标题天天变）。

## 成本与备份

| 项目 | 费用 |
|---|---|
| 仓库 + Actions（public repo） | 0 |
| Pages 托管与流量 | 0 |

仓库本身即完整备份：源清单、脚本、工作流、生成物、历史全在里面，恢复只需 `git clone` 再重开一次 Pages。
`feeds.txt` 建议另存一份——它是唯一无法自动重建的资产。

## 故障排查

先做三步定位，90% 的问题能当场分清是「抓取」「提交」还是「发布」：

```bash
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep lastBuildDate   # 线上构建时间
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep -c "<item>"     # 条目数
```

| 现象 | 多半是 | 去哪一节 |
|---|---|---|
| feed 打不开 / 404 | Pages 没开或目录选错 | [1](#1-feed-打不开返回-404) |
| feed 能开但 0 条 | 源全挂，或文件被写成空 | [2](#2-feed-能打开但一条都没有) |
| `lastBuildDate` 长期不动 | 定时任务没触发，或作业失败 | [3](#3-feed-长时间不更新) [4](#4-actions-作业失败) |
| 运行记录里 cancelled | 整点抢占，正常调度 | [12](#12-运行记录里出现-cancelled) |
| 报 `in progress deployment` | Pages 部署撞锁 | [13](#13-deployment-request-failed--due-to-in-progress-deployment) |
| 作业成功但仓库没新提交 | 权限只有只读 | [5](#5-作业成功但提交被拒) |
| 某个源反复失败 | 源站 404 / 反爬 / 已停更 | [6](#6-单个源一直失败) |
| 同一新闻出现两次 | 去重阈值或链接带参数 | [7](#7-条目重复) |
| 标题乱码 | 站点编码是 GBK 且声明缺失 | [8](#8-标题乱码) |
| 说明页空白 | 浏览器 CORS 或 feed 异常 | [9](#9-说明页空白或载入失败) |
| 设备上标题被切 / 没有正文 / 出现方块 | 用了完整版，或标题宽度不对 | [14](#14-设备上标题被截断或只有一行) |
| 推送后「凭据扫描」失败 | 文件里混进了 token | [11](#11-凭据扫描失败) |

### 1. feed 打不开、返回 404

1. **Settings → Pages** 是否显示 “Your site is live at …”，Source 是否为 GitHub Actions；
2. `docs/feed.xml` 是否在 `main` 上（`git ls-tree main docs/`）；
3. 路径大小写必须是 `docs/feed.xml`；
4. Actions 里 `pages build and deployment` 有没有红色 ✗；
5. 刚开启 Pages 时需 1～2 分钟生效。

### 2. feed 能打开但一条都没有

`build_feed.py` 有硬保护：**本轮一条都没抓到就不覆盖旧文件**，所以「空 feed」通常只出现在首次部署。

- 本地跑一次看 `[失败]` 行的错误码；
- 全部 `HTTP 403` → 换 UA 或改用镜像源（`feed.quanwenrss.com` 系列）；
- 全部超时 → 网络出口问题，换执行环境试。

### 3. feed 长时间不更新

1. **作业有没有在跑**：Actions 最近运行时间，长时间空白见[第 10 节](#10-定时任务完全不触发)；
2. **跑了但没部署**：看日志里 deploy 那步是否报错，见[第 13 节](#13-deployment-request-failed--due-to-in-progress-deployment)；
3. **缓存**：`github.io` 有 CDN 缓存，加时间戳绕开 `curl -s "…/feed.xml?t=$(date +%s)"`。

### 4. Actions 作业失败

| 失败步骤 | 常见原因 | 处置 |
|---|---|---|
| 采集 | 某源把作业拖到超时（`timeout-minutes`） | 调小 `CONCURRENCY` / `FETCH_TIMEOUT`，或把慢源从 `feeds.txt` 拿掉 |
| 备份提交 | `git push` 被拒（远端有新提交） | 已带 `git pull --rebase --autostash`；仍失败就手动 Run workflow 重跑 |
| 备份提交 | `Resource not accessible by integration` | Workflow permissions 改成 **Read and write** |

重跑：Actions → 选那次运行 → 右上 **Re-run jobs**。

### 5. 作业成功但提交被拒

99% 是权限问题。`permissions: contents: write` 已在 workflow 里声明，但仓库级设置若为只读，声明再高也没用：
**Settings → Actions → General → Workflow permissions → Read and write permissions → Save**。

### 6. 单个源一直失败

日志形如 `[失败] 某某源 https://... (HTTP 404)`：

- `404` → 地址失效，删掉或换镜像；
- `403` → 反爬，先试镜像，不行就删；
- `超时` → 源站慢或墙外不可达；
- `未解析到条目` / `XML 解析失败` → 返回的是 HTML 拦截页；
- `[回退]` 频繁出现 → 该源长期不稳定，建议替换。

### 7. 条目重复

去重规则见 [README #工作方式](README.md#工作方式)。检查点：链接去掉 `#?` 后是否相同；
标题去标点后是否 ≥ 8 字且前 20 字相同。短标题不参与标题去重是故意的（否则「港股收盘」会被误杀），
真要更严格就改 `build_feed.py` 里 `len(tt) >= 8` 的阈值，代价是误杀变多。

### 8. 标题乱码

源站声明与实际编码不一致。现处理是「UTF-8 失败退 GB18030」，仍乱码说明是别的编码（如 Big5）：

```bash
curl -sI "源地址" | grep -i content-type
curl -s "源地址" | head -c 200      # 看 XML 声明里的 encoding=
```

在 `decode_body()` 的候选列表里加上对应编码即可。

### 9. 说明页空白或载入失败

`docs/index.html` 用 `fetch('./feed.xml')` 渲染，失败时页面底部会提示。

- 直接访问 `.../feed.xml` 是否正常？不正常回到第 1、2 节；
- 刚提交完 → Pages 还没重建好，等 1 分钟；
- 浏览器插件 / 反向代理拦截 → 换无痕窗口试。

### 10. 定时任务完全不触发

症状：Actions 里只有 `push` / `workflow_dispatch` 记录，`event=schedule` 一条都没有。
这是 GitHub 的已知特性——定时任务跑在共享队列上，**新仓库 + 高频 cron 优先级最低**。自查三件事：

1. workflow 在**默认分支** `main` 上（`schedule` 只在默认分支生效）；
2. 该工作流是 Enabled（不是灰色 Disabled）；
3. 仓库 60 天内有过提交（长期无活动会被自动停用）。

都正常就只能等，或手动 Run workflow 补水。

**本仓库实测（2026-10-29 建库当天）**：`event=schedule` 计数为 0，即建库 5 小时内一次都没触发过。
建库当天靠 cron 不可靠，先用下面的命令手动起一轮（跑满 1 小时，覆盖 13 个 5 分钟窗）：

```bash
TOKEN=$(printf "protocol=https\nhost=github.com\n\n" | git credential fill | sed -n 's/^password=//p')
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/cheney-hub/rss-hub/actions/workflows/minute.yml/dispatches \
  -d '{"ref":"main"}'      # 204 = 已排队
```

查是否真有 schedule 事件在跑：

```bash
curl -s "https://api.github.com/repos/cheney-hub/rss-hub/actions/runs?event=schedule&per_page=1" \
  | grep -o '"total_count":[0-9]*'
```

若建库满 24 小时后 `total_count` 仍是 0，说明这台仓库的 cron 队列长期不给配额，两条兜底路线选一：

| 方案 | 做法 | 代价 |
|---|---|---|
| 外部定时器（推荐） | [cron-job.org](https://cron-job.org) 免费建一个 5 分钟任务，POST 上面那个 dispatch 地址，Header 带 `Authorization: Bearer <fine-grained PAT>`（只给本仓库 Actions: write） | token 存在第三方站点，需单独建一个最小权限 PAT |
| 手动补水 | 每天点 4 次 Run workflow（每次覆盖 1 小时），或在手机 GitHub App 上点 | 人工 4 次/天 |

两者都不影响 feed 本身：轮播游标按时间窗走，晚启动一小时只会让节奏从启动时刻重新对齐，不会乱序。

### 11. 凭据扫描失败

`凭据扫描 / scan` 变红，说明被 `tools/check_secrets.py` 命中。本地复现：

```bash
python3 tools/check_secrets.py          # 只看 tracked 文件
python3 tools/check_secrets.py --all    # 连未跟踪文件一起扫
```

按 `文件:行号 [类型] 片段` 定位后：① 把真实值移到仓库之外，文件里只留变量名；
② 若已推送上去，光删文件没用——先**吊销重发**那个 token，再决定是否清理历史
（`git filter-repo` 后强推会改写所有 SHA，协作仓库慎用）；
③ 文档举例用明显假的占位（如 `你的PAT`），别写成「平台前缀 + 一长串字符」，规则会当成真凭据。

### 12. 运行记录里出现 cancelled

正常现象。`minute.yml` 用了 `concurrency: cancel-in-progress: true`：整点触发时若上一轮还在跑，
新一轮直接接管，保证每次触发都从第 1 轮开始、时刻不会逐小时后移累积。
判断有没有问题只看线上：`lastBuildDate` 在 10 分钟内就是正常。

### 13. `Deployment request failed ... due to in progress deployment`

Pages 同一时刻只允许一个部署，撞上就报这个 400。三个工作流的处理不同：

- `minute.yml` 独占 `rss-pages` 组，撞上时后触发的一方**接管**（保证整点重新对齐）；
- `rss.yml` 独立成组（改源要立刻生效，不能排一小时），失败时等 35 秒重新打包再部署一次。

仍出现说明两次部署间隔小于约 25 秒，把 `rss.yml` 里的 `sleep 35` 调大。

### 14. 设备上标题被截断或只有一行

先在设备页首确认订阅的是哪个源：**「RSS 头条 · 小屏」** = 设备版，
**「RSS 聚合头条」** = 完整版（标题没做任何预处理，必然被切得最难看）。

| 现象 | 原因 | 处置 |
|---|---|---|
| 标题 13 字后停住，后面没有正文 | 云端不渲染 `<description>` | 只能靠标题行，把 `SCREEN_LINE_WIDTH` 调到真机顶格数（多试 12 / 13 / 14） |
| 标题 13 字后停住，接着出现正文 | 正常，正文生效 | 想读更多就把 `SCREEN_BODY_MAX` 调大 |
| 每行不到 13 字就被切 | `SCREEN_LINE_WIDTH` 大于设备实际容量 | 调到 12 试，或直接按 13 保持 |
| 出现方块、空白、半截符号 | 标题里有 emoji 或装饰字符 | 确认用的是设备版（`plain_text()` 已统一摘除）；用 `curl …/feed-wrap.xml \| grep "<title>" \| head -3` 核对 |
| 正文以「，」开头 | 切点落在标点上 | 已在 `split_title()` 里剥掉（`HEAD_STRIP`），若仍有请反馈具体标题 |

只显示第一条是 RSS 卡片的版面极限（一屏一行大字号），不是 feed 的问题——
把余下原文搬进正文，是在这个极限里唯一能多塞字的方向。

### 15. 设备上的内容不换 / 重复 / 跳条

设备版每版只发一条，选谁由 `docs/rotate.json` 的游标决定。先看线上状态：

```bash
curl -s "https://cheney-hub.github.io/rss-hub/rotate.json?cb=$(date +%s)" | python3 -m json.tool
```

`window` = 当前时间窗（`now // ROTATE_SECONDS`），`shown` = 本轮已展示的 guid，
`pool` = 池子条数，`cycle` = 第几轮。

| 现象 | 原因 | 处置 |
|---|---|---|
| 内容一直不变 | 设备刷新间隔设得比 `ROTATE_SECONDS` 长 | Dot App 里把刷新间隔改成 **5 分钟** |
| 同一条连显两次、中间那条没看到 | Pages CDN 缓存 `max-age=600`，拿到 10 分钟前的版本 | `ROTATE_SECONDS` 改 `600`（永不漏，但 10 分钟才换） |
| 轮播卡死不动 | `rotate.json` 的 `dwell` 和当前 `ROTATE_SECONDS` 不一致（改过节拍） | 代码已自动识别并重新起轮；仍有问题就删掉线上 `rotate.json` |
| `pool` 比源数少 | 部分源本轮抓取失败用了回退条目，guid 去重后合并 | 看同一轮日志里的 `[回退]` 行 |
