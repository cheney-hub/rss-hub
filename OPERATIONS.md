# 运维手册

## 日常操作

### 加/删订阅源

编辑 `feeds.txt`：注释行（源中文名）在上，地址行在下，空行随便加。

```text
# ---------- 财经 ----------

# 财联社·电报滚动
https://feed.quanwenrss.com/feed/cailianshe-roll.xml
```

删源直接删掉那两行；想临时停用就在地址行前加 `#`（整行注释掉，不会被解析）。
改完 `git push` 到 `main`，`rss.yml` 会立刻重建并发布一次（约 30 秒出结果），无需等下一个整点。

**先验证再提交**（避免把 404 源带上线）：本地跑一遍，只关心 `[失败]` 与 `[回退]` 行。

```bash
python3 build_feed.py
# [失败] 某某源  https://...  (HTTP 404)
# [第 1 轮] 成功 42 / 回退 0 / 失败 1 → 条目 42（去重 1）耗时 4.2s
```

### 改源名

改地址上方那行注释即可。注释会写成 `<source>`，括号里的备注会被自动剥掉，别把重要信息放括号里。

### 改抓取参数

在 `.github/workflows/rss.yml` 的 `env:` 段里改：

| 变量 | 现值 | 调整建议 |
|---|---|---|
| 轮数（`minute.yml` 里的 55 组 step） | `55` | 由 `tools/gen_minute_workflow.py 55` 生成；改小可降 runner 占用（如 29 轮 = 每 2 分钟一版、每小时跑半小时） |
| `CONCURRENCY` | `12` | 源增多到 60+ 可提到 16；被源站限流时降到 8 |
| `FETCH_TIMEOUT` | `20` | 慢源多可提到 `25`；注意 `timeout-minutes: 12` 是总闸 |

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
python3 build_feed.py                       # 输出到 docs/feed.xml（默认）
OUT_FILE=/tmp/t.xml REFRESH_SECONDS=0 python3 build_feed.py   # 换个路径、只跑一轮
```

无第三方依赖，Python 3.8+ 即可。Windows、macOS、树莓派都一样。

### 回滚某次发布

`docs/feed.xml` 是生成物，直接回退那一次提交即可：

```bash
git log --oneline -- docs/feed.xml | head
git checkout <commit> -- docs/feed.xml && git commit -m "revert feed" && git push
```

下一次定时任务会基于回退后的内容重新生成（回退只影响「失败回退」的素材，不影响正常抓取）。

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
2. 改三处地址：
   - `.github/workflows/rss.yml` 里 `FEED_URL`；
   - `build_feed.py` 里 `FEED_URL` 默认值；
   - `docs/index.html` 里输入框的 `value` 与「源码仓库」链接；
3. 仓库 **Settings → Pages** → Source 选 **GitHub Actions**（不是 Deploy from a branch），保存；
4. 等 1～2 分钟，访问 `https://<用户名>.github.io/<仓库名>/feed.xml`；
5. 仓库 **Settings → Actions → General** → Workflow permissions 选 **Read and write permissions**（否则作业无法提交 feed）。

## 凭证管理

**这个仓库的硬约定：任何文件里都不出现 token、口令、私钥。** 三条保护：

| 保护 | 位置 | 作用 |
|---|---|---|
| `.gitignore` 屏蔽 | `*.env` / `.env*` / `*.pem` / `*.key` / `id_rsa*` 等 | 凭证文件即使放在项目里也不会被 add 进来 |
| 提交前扫描 | `tools/check_secrets.py` | `python3 tools/check_secrets.py`，命中即退出码 1 |
| CI 强制扫描 | `.github/workflows/guard.yml` | 每次推送自动跑，混进真 token 直接失败 |

凭证该放哪：

| 场景 | 存放位置 | 说明 |
|---|---|---|
| Actions 定时提交 feed | 无需你提供 | 仓库内置 `GITHUB_TOKEN`（`permissions: contents: write`），GitHub 签发、作业结束即失效 |
| 手动 / 外部触发工作流 | GitHub 仓库 Secret（`GH_PAT`）或本地 shell 环境变量 | 需要 `workflow` 权限；Secret 只在工作流运行时可见 |
| 通道 B（树莓派推送） | `/etc/rss-hub/rss-hub.env`（`600`）+ unit 的 `EnvironmentFile=` | 见 [pi/README.md](pi/README.md#2-生成凭证文件留在仓库之外) |

当前仓库与 `origin` 都是无凭证状态，历史里也没有提交过任何真实 token。

轮换顺序：GitHub → Settings → Developer settings → Personal access tokens → 吊销旧的、生成新的 → 更新 Secret / `/etc/rss-hub/rss-hub.env` → `sudo systemctl restart rss-aggregator.timer`。

## 监控

- **Actions 页面**：https://github.com/cheney-hub/rss-hub/actions —— 每次运行的耗时、日志、失败轮次都在这里；
- **线上自检**（无需鉴权）：

```bash
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep -E "lastBuildDate"
curl -s https://cheney-hub.github.io/rss-hub/feed.xml | grep -c "<item>"
```

- **WorkBuddy 侧巡检**：每 3 小时跑一次，判据是「条目 ≥ 30 且 `lastBuildDate` 距今 ≤ 45 分钟」，异常才汇报，正常不打扰；
- 想接第三方监控（UptimeRobot / 自建 Cronitor）：监控 `feed.xml` 的 200 状态即可，注意别用「内容关键字」判据（新闻标题天天变）。

## 四档刷新方案

## 刷新节奏怎么调

当前是 **1 分钟一版**，代价是 runner 占用约 22 小时/天。想降载就改轮数，改完重新生成工作流即可：

| 目标节奏 | 怎么改 | runner 占用 |
|---|---|---|
| 白天 1 分钟 + 深夜 1 小时（现状） | `python tools/gen_minute_workflow.py 55`；两个工作流的 cron 按 UTC 小时互补（`22,23,0-15` / `16-21`） | 约 17 小时/天 |
| 全天 1 分钟 | 把 `minute.yml` 的 cron 改回 `"7 * * * *"`，删掉 `nightly.yml` | 约 22 小时/天 |
| 2 分钟 | 生成脚本里把每轮 step 的 `sleep` 改成对齐 120 秒，再 `gen_minute_workflow.py 29` | 约 11 小时/天 |
| 5 分钟 | 同上，周期 300 秒、轮数 11 | 约 5 小时/天 |
| 30 分钟 | 停用 `minute.yml` 与 `nightly.yml`，给 `rss.yml` 加回 cron（如 `3,13,23,33,43,53 * * * *`） | 忽略不计 |

两个绕不开的约束，也是这套设计的原因：

- **cron 不能写 `*/1`**：Actions 的 schedule 最短 5 分钟，且官方承认高负载会延迟、丢档，实测新仓库 50 分钟内 0 次触发。分钟级只能靠「小时级 cron + 作业内循环」；
- **不能靠 commit 发布**：Pages 传统构建有 10 次/小时软限制，每分钟一次会被限流；改用自定义 Actions 部署后不受该限制（官方文档明示），所以流程是「打包 artifact → `deploy-pages`」。

其他可选通道：

| 通道 | 实际刷新 | 成本 | 何时选它 |
|---|---|---|---|
| GitHub Actions（现状） | 1 分钟 | 0 | 默认 |
| 树莓派 / VPS + systemd | 1 分钟，且不占 GitHub runner | ¥0～¥38/年 | 嫌高频占用，或想彻底自主 |
| 外部定时器打 `workflow_dispatch` | 5～10 分钟 | 0 | 只补 cron 丢档，不想长跑作业 |

外部定时器补档（备用，未启用）：

```yaml
# 在任意工作流末尾追加一步，用仓库 Secret 里的 PAT 拉起下一轮
      - name: 预约下一次
        env:
          PAT: ${{ secrets.GH_PAT }}
        run: |
          curl -s -X POST -H "Authorization: Bearer $PAT" \
            -H "Accept: application/vnd.github+json" \
            https://api.github.com/repos/${{ github.repository }}/actions/workflows/minute.yml/dispatches \
            -d '{"ref":"main"}'
```

## 迁移到树莓派或 VPS

1. 把整个目录拷到目标机（或 `git clone`），装好 `python3`；
2. 生成 PAT（classic，勾 `repo`）；
3. 执行 `bash pi/install.sh` —— 它会创建 `/etc/rss-hub/rss-hub.env`（`600`）并按实际路径/用户改写 unit；凭证只填进那个文件，不进仓库：

```bash
GITHUB_TOKEN=你的PAT GITHUB_REPO=你的用户名/rss-hub bash pi/install.sh
# 或先安装、再编辑：sudo nano /etc/rss-hub/rss-hub.env
```

4. 验证：`sudo systemctl start rss-aggregator.service && journalctl -u rss-aggregator.service -f`。

详细步骤与排障见 [pi/README.md](pi/README.md)。VPS 同理，路径与用户由 `install.sh` 自动适配（也可用 `RUN_USER=` / `PYTHON_BIN=` 指定）。

迁移后建议**保留** GitHub Actions（两条独立通道，谁挂了都不至于断更）；若确认只用自托管，把 workflow 里的 `schedule` 段删掉即可。

## 成本

| 项目 | 费用 |
|---|---|
| 仓库 + Actions（public repo） | 0 |
| Pages 托管与流量 | 0 |
| PAT | 0 |
| 自托管（可选） | 树莓派 ¥0（已有）／腾讯云轻量 ¥38～68/年／RackNerd $21.99/年／Oracle Cloud 永久免费（需信用卡） |

## 备份与恢复

仓库本身就是完整备份：源清单、脚本、工作流、生成物、历史全在里面。恢复只需 `git clone`，然后在 Settings → Pages 里重开一次发布。

建议额外留一份：`feeds.txt` 单独存一份到笔记或另一个仓库——它是唯一无法自动重建的资产。
