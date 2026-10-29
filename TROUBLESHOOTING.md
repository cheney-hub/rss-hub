# 故障排查

先做三步定位，90% 的问题能当场分清是「抓取」「提交」还是「发布」：

```bash
# 1. 线上 feed 最新构建时间与条目数
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep lastBuildDate
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep -c "<item>"

# 2. 仓库里最新一次提交（Actions 有没有成功提交）
curl -s https://api.github.com/repos/cheney-hub/rss-hub/commits?per_page=1 | grep -m1 '"message"'

# 3. 最近几次运行状态
curl -s https://api.github.com/repos/cheney-hub/rss-hub/actions/runs?per_page=5 | grep -E '"name"|"status"|"conclusion"'
```

| 现象 | 多半是 | 去哪一节 |
|---|---|---|
| feed 打不开 / 404 | Pages 没开或目录选错 | [1](#1-feed-打不开返回-404) |
| feed 能开但 0 条 | 源全挂，或文件被写成空 | [2](#2-feed-能打开但一条都没有) |
| `lastBuildDate` 长期不动 | 定时任务没触发，或作业失败 | [3](#3-feed-长时间不更新) [4](#4-actions-作业失败) |
| 运行记录里有 cancelled | 整点抢占，正常调度 | [13](#13-运行记录里出现-cancelled) |
| 报 `in progress deployment` | Pages 部署撞锁 | [14](#14-deployment-request-failed--due-to-in-progress-deployment) |
| 作业成功但仓库没新提交 | 权限不足（只读 token） | [5](#5-作业成功但提交被拒) |
| 某个源反复失败 | 源站 404 / 反爬 / 已停更 | [6](#6-单个源一直失败) |
| 同一新闻出现两次 | 去重阈值或链接带参数 | [7](#7-条目重复) |
| 标题出现乱码 | 站点编码是 GBK 且声明缺失 | [8](#8-标题乱码) |
| 说明页空白 | 浏览器 CORS 或 feed 内容异常 | [9](#9-说明页空白或载入失败) |
| 墨水屏上标题被截断 / 放不下 / 出现方块 | 用了完整版，或宽度阈值偏大，或标题带 emoji | [15](#15-墨水屏上标题被截断难看--一行放不下) |
| 推送后「凭据扫描」失败 | 文件里混进了 token / 口令 | [12](#12-凭据扫描失败) |

---

## 1. feed 打不开、返回 404

依次确认：

1. 仓库 **Settings → Pages** 是否显示 “Your site is live at …”，Source 是否为 `main` + `/docs`；
2. `docs/feed.xml` 是否真的在 `main` 分支上（`git ls-tree main docs/`）；
3. 路径大小写：必须是 `docs/feed.xml`；
4. Pages 构建历史里有没有红色 ✗（仓库 → Actions → pages build and deployment）；
5. 刚开启 Pages 时需要 1～2 分钟生效，别急。

## 2. feed 能打开但一条都没有

`build_feed.py` 有一条硬保护：**本轮一个条目都没抓到就不覆盖旧文件**。所以「空 feed」通常只出现在首次部署时。

- 本地跑一次看失败原因：`python3 build_feed.py`，看 `[失败]` 行的错误码；
- 全部 `HTTP 403` → 换 UA 或改用镜像源（`feed.quanwenrss.com` 系列）；
- 全部超时 → 网络出口问题（某些地区访问不了境外源），换执行环境试试。

## 3. feed 长时间不更新

按这个顺序排除：

1. **作业有没有在跑**：Actions 页面最近运行时间。若长时间空白，见第 10 节；
2. **作业跑了但没提交**：日志最后一步是否打印「内容无变化，跳过提交」——如果每轮抓到的都是同一条，确实没有变化，属正常；
3. **提交了但 Pages 没重建**：看 pages build 是否排队/失败，或触发一次手动 Run workflow；
4. **缓存**：`github.io` 有 CDN 缓存，加时间戳绕开 `curl -s "https://.../feed.xml?t=$(date +%s)"`。

## 4. Actions 作业失败

打开失败的那次运行，看是哪一步：

| 失败步骤 | 常见原因 | 处置 |
|---|---|---|
| 抓取并生成 feed | 某个源把作业拖到超时（`timeout-minutes: 12`） | 调小 `CONCURRENCY` 或 `FETCH_TIMEOUT`；把慢源从 `feeds.txt` 拿掉 |
| 提交更新 | `git push` 被拒（远端有新提交） | 工作流里已带 `git pull --rebase --autostash`，仍失败就手动 `Run workflow` 重跑 |
| 提交更新 | `Resource not accessible by integration` | 仓库 Settings → Actions → General → Workflow permissions 改成 **Read and write** |

重跑：Actions → 选那次运行 → 右上 **Re-run jobs**。

## 5. 作业成功但提交被拒

99% 是权限问题。`permissions: contents: write` 已在 workflow 里声明，但如果仓库级设置是「Read repository contents」，声明再高也没用：

**Settings → Actions → General → Workflow permissions → Read and write permissions → Save**。

## 6. 单个源一直失败

日志形如 `[失败] 某某源 https://... (HTTP 404)`。逐个判断：

- `404` → 源地址已失效，删掉或换新地址（可去 `quanwenrss` 镜像找同名源）；
- `403` → 反爬或需要 UA/Referer，先试镜像；镜像也不行就删；
- `超时` → 源站慢或墙外不可达；
- `未解析到条目` / `XML 解析失败` → 返回的是 HTML 拦截页或非 feed 内容；
- `回退` 频繁出现 → 该源长期不稳定，建议替换。

批量体检（一次性列出所有失败源）：

```bash
python3 build_feed.py 2>&1 | grep -E "^\[(失败|回退)\]"
```

## 7. 条目重复

去重规则见 [ARCHITECTURE.md 第四节](ARCHITECTURE.md#3-去重规则)。检查点：

- 两条的**链接**去掉 `#?` 参数后是否相同？
- 两条**标题**去标点后是否 ≥ 8 个汉字且前 20 字相同？

短标题（`港股收盘`）不参与标题去重，这是故意的——否则会误杀。若确实需要更严格，改 `build_feed.py` 里 `len(tt) >= 8` 这个阈值，但要接受误杀变多。

## 8. 标题乱码

成因是源站声明与实际编码不一致。当前处理是「UTF-8 失败退 GB18030」，仍乱码说明是别的编码（如 Big5）或声明错误。

快速定位：

```bash
curl -sI "源地址" | grep -i content-type
curl -s "源地址" | head -c 200
```

看 XML 声明里的 `encoding=`。若是 `big5`、`gb2312` 之外的值，可在 `decode_body()` 的候选列表里加上对应编码。

## 9. 说明页空白或载入失败

`docs/index.html` 用 `fetch('./feed.xml')` 渲染，失败时页面底部会显示「feed 载入失败」。

- 直接访问 `.../feed.xml` 是否正常？不正常 → 回到第 1、2 节；
- 刚提交完 → Pages 还没重建好，等 1 分钟；
- 反向代理/浏览器插件拦截 → 换无痕窗口试。

## 10. 定时任务完全不触发

症状：Actions 页面只有 `push` / `workflow_dispatch` 记录，`event=schedule` 一条都没有。

这是 GitHub 的已知特性，不是配置错误：定时任务跑在共享调度队列上，**新仓库 + 高频 cron 优先级最低**，首次触发可能延迟数小时，忙时静默丢档。

自查三件事：

1. workflow 文件在**默认分支** `main` 上（`schedule` 只在默认分支生效）；
2. Actions 页面该工作流是 Enabled（不是灰色 Disabled）；
3. 仓库 60 天内有过提交（长期无活动会被自动停用）。

都正常就只能等，或换更可靠的触发方式（[OPERATIONS.md 四档刷新方案](OPERATIONS.md#四档刷新方案)）。

## 11. 想确认某个源「是不是实时更新的」

订阅源质量决定一切。验一个源是否活跃：

```bash
curl -s "源地址" | grep -E "<pubDate>|<updated>" | head -5
```

对比当前时间，最近条目在 1 小时内 → 实时；只有几天前的 → 慢源，适合放但别指望它带节奏。

## 12. 凭据扫描失败

推送后 `凭据扫描 / scan` 变红，说明被 `tools/check_secrets.py` 命中。本地复现并定位：

```bash
python3 tools/check_secrets.py          # 只看 tracked 文件
python3 tools/check_secrets.py --all    # 连未跟踪文件一起扫
```

输出会给出 `文件:行号 [类型] 片段`。处置：

1. 把真实值移到仓库之外（GitHub Secret），文件里只留变量名；
2. 若该值**已经推送上去**，光删文件没用——历史里仍在：
   - 先把那个 token **吊销重发**（这是唯一可靠的止血）；
   - 再决定是否清理历史（`git filter-repo` 后强推，会改写所有 SHA，协作仓库慎用）。
3. 文档里要举例时用明显假的占位（如 `你的PAT`），不要写成「平台前缀 + 一长串字符」的形态，规则会当成真凭据。

## 13. 运行记录里出现 cancelled

正常现象，不是故障。`minute.yml` 的 `concurrency` 用了 `cancel-in-progress: true`：整点触发时若上一轮还在跑（比如被排过队、拖过了整点），新的一轮直接接管，保证每次触发都从第 1 轮重新开始、时刻不会逐小时后移累积。

判断有没有问题只看线上：`lastBuildDate` 在 10 分钟内就是正常。

## 14. `Deployment request failed ... due to in progress deployment`

Pages 同一时刻只允许一个部署，撞上就会报这个 400。三个工作流都会部署，处理方式不同：

- `minute.yml` 与 `nightly.yml` 用同一个 `concurrency` 组，撞上时**排队**而不是报错；
- `rss.yml` 独立成组（改源要立刻生效，不能排 55 分钟），失败时等 35 秒重新打包再部署一次。

如果这个错误还出现，说明两次部署间隔小于约 25 秒，把 `rss.yml` 里的 `sleep 35` 调大。

## 15. 墨水屏上标题被截断难看 / 一屏只看到一条

先分清是**版面极限**还是**喂料问题**。RSS 卡片在 296×152 屏上就是**一行大字号**，
实测约 13～14 个全角字满宽 —— 这是版面决定的，feed 里塞再多也只显示第一条。

**a. 用了完整版 `feed.xml`** —— 标题原样输出（最长 60 字），必被云端硬切，还会显示成
「RSS 聚合头条」这个标题。改用墨水屏版（页首应显示「RSS 头条 · 墨水屏」）：

```
https://cheney-hub.github.io/rss-hub/feed-screen.xml
```

**b. 换成墨水屏版后仍被二次截断** —— 说明 `SCREEN_TITLE_WIDTH` 大于设备实际容量，
我们的截断还没发生，云端就先动刀了。默认已是 13，确认工作流里没有被改回 24：

```bash
grep -n "SCREEN_TITLE_WIDTH" .github/workflows/*.yml | head
# 本地试：调到 12 看看断句位置是否更舒服
SCREEN_TITLE_WIDTH=12 python3 build_feed.py
```

**c. 想一屏看多条** —— RSS 卡片做不到，换文本 API：配好 `DOT_API_KEY` / `DOT_DEVICE_ID` 后
`push_text.py` 会推「标题 1 行 + 正文 3 行」，一屏 4 条（见 [README](README.md#一屏-4-条文本-api推荐)）。
配了密钥但设备没变，按顺序查：

```bash
# 1) 密钥与设备号是否真的传进作业了（不该打印出真值）
gh secret list        # 或 Settings → Secrets → Actions
# 2) 本地直推，看服务端返回（会打印 HTTP 状态与响应体）
DOT_API_KEY=dot_app_xxx DOT_DEVICE_ID=设备号 python3 push_text.py
```

常见返回：`401` 密钥无效或已删除；`404` 设备号不对，**或 App 里没把「文本 API」内容加进循环列表**；
`200` 但屏幕不动 —— 设备没联网 / 在睡眠，等下一次唤醒；`refreshNow` 关掉时本来就只存云端不立即显示。

**d. 标题里出现方块、空白、半截符号** —— 源站标题带了 emoji 或装饰字符，设备字库没有对应字形。
墨水屏版已用 `plain_text()` 统一摘除（emoji、箭头、几何图形、零宽字符、变体选择符），
若还有残留，先确认用的确实是 `feed-screen.xml` 而不是 `feed.xml`：

```bash
curl -s https://cheney-hub.github.io/rss-hub/feed-screen.xml | grep "<title>" | head -3
```

改工作流的 `env:` 才能长期生效（见 [OPERATIONS.md](OPERATIONS.md#调墨水屏版)）。先看内容示意：
<https://cheney-hub.github.io/rss-hub/screen.html>。

另外几个已知的排版细节：标题只在 `，、；！？` 处收尾，且断点必须保住 80% 宽度（冒号不算断点——
「雷普索尔首席执行官：展望未来」在冒号处断只剩主体，谓语全丢）；`《》“”（）` 没闭合会退到符号前，不会切成半截；
截断处统一补 `…`；末尾「- 来源名」这类尾巴会先被去掉（来源已单独用 `<source>` 给）。
