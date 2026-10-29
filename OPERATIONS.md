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
改完 `git push` 到 `main`，Actions 会自动重建一次（约 5 分钟出结果）。

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
| `REFRESH_SECONDS` | `240` | 想少用 runner 时间就调小（如 `120`）；调大会拉长单次作业 |
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
| Pages 源 | `main` 分支的 `/docs` 目录（legacy 构建） |
| 访问地址 | `https://cheney-hub.github.io/rss-hub/` |

### 换到自己的仓库

1. Fork 或新建空仓库，把代码推上去；
2. 改三处地址：
   - `.github/workflows/rss.yml` 里 `FEED_URL`；
   - `build_feed.py` 里 `FEED_URL` 默认值；
   - `docs/index.html` 里输入框的 `value` 与「源码仓库」链接；
3. 仓库 **Settings → Pages** → Source 选 `Deploy from a branch`，分支 `main`，目录 `/docs`，保存；
4. 等 1～2 分钟，访问 `https://<用户名>.github.io/<仓库名>/feed.xml`；
5. 仓库 **Settings → Actions → General** → Workflow permissions 选 **Read and write permissions**（否则作业无法提交 feed）。

## 凭证管理

| 场景 | 用什么 | 是否需要你保管 |
|---|---|---|
| Actions 定时提交 feed | 仓库内置 `GITHUB_TOKEN`（`permissions: contents: write`） | 不需要，GitHub 自动签发、作业结束即失效 |
| 手动/外部触发工作流 | PAT（classic 勾 `repo` + `workflow`，或 fine-grained 勾 Actions: write） | 需要，只放本地环境变量 |
| 通道 B（树莓派推送） | PAT 写进 systemd unit 或环境文件 | 需要 |

**当前仓库里没有任何 token**，`origin` 也是无凭证的普通地址。若日后启用「自调度链」或把 PAT 交给外部定时器，才需要写进仓库 Secret：

```bash
# Settings → Secrets and variables → Actions → New repository secret
# 名称建议 GH_PAT；工作流里用 ${{ secrets.GH_PAT }} 引用
```

轮换 token 时：GitHub → Settings → Developer settings → Personal access tokens → 删除旧的、生成新的，然后更新 Secret / 树莓派 unit，重启对应任务。

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

GitHub cron 是「尽力而为」调度，实测新仓库首次触发可延迟近一小时。想要更稳更快，四档可选：

| 档位 | 做法 | 实际刷新 | 成本 | 风险 |
|---|---|---|---|---|
| ① 现状 | 只靠 GitHub cron | 10～25 分钟，偶发丢档 | 0 | 无 |
| ② 自调度链 | 作业末尾用仓库 Secret 里的 PAT 调 `workflow_dispatch` 拉起下一次 | 稳定 10 分钟 | 0（公开仓库分钟数不限） | runner 用量约 11 小时/天，属高频用法，可能被平台降速 |
| ③ 外部定时器 | cron-job.org / Cloudflare Worker 定时打 `workflow_dispatch` | 稳定 5～10 分钟 | 0 | token 需交给对方，建议用仅限本仓库的 fine-grained token |
| ④ 自托管 | 树莓派或 VPS 跑 `aggregate_rss.py` + cron/systemd | 1 分钟 | ¥0（已有设备）～¥38/年 | 需一台常开设备 |

②的做法（未启用，要用再加）：

```yaml
# .github/workflows/rss.yml 末尾追加一步
      - name: 预约下一次
        env:
          PAT: ${{ secrets.GH_PAT }}
        run: |
          curl -s -X POST -H "Authorization: Bearer $PAT" \
            -H "Accept: application/vnd.github+json" \
            https://api.github.com/repos/${{ github.repository }}/actions/workflows/rss.yml/dispatches \
            -d '{"ref":"main"}'
```

## 迁移到树莓派或 VPS

1. 把整个目录拷到目标机（或 `git clone`），装好 `python3`；
2. 生成 PAT（classic，勾 `repo`），只写进 systemd unit，不要提交；
3. 填 `pi/rss-aggregator.service` 里的 `GITHUB_TOKEN` / `GITHUB_REPO`；
4. 执行 `bash pi/install.sh`，注册为开机自启 + 每 10 分钟一轮的 systemd 定时任务；
5. 验证：`sudo systemctl start rss-aggregator.service && journalctl -u rss-aggregator.service -f`。

详细步骤与排障见 [pi/README.md](pi/README.md)。VPS 同理，只是路径与用户不同（记得同步改 unit 里的 `User` 与 `WorkingDirectory`）。

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
