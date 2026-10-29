# rss-hub · RSS 聚合头条

把 `feeds.txt` 里几十个 RSS 源的**最新一条**抓下来，跨源去重、按发布时间倒序合并成一个新的 RSS，
由 **GitHub Actions 每 5 分钟自动刷新**，通过 **GitHub Pages** 对外提供订阅。

不依赖任何本机、树莓派或第三方服务器。

## 订阅地址

```
https://cheney-hub.github.io/rss-hub/feed.xml
```

网页版（可复制地址、看最新条目）：<https://cheney-hub.github.io/rss-hub/>

备用镜像（主域名访问不畅时用）：

```
https://raw.githubusercontent.com/cheney-hub/rss-hub/main/docs/feed.xml
https://cdn.jsdelivr.net/gh/cheney-hub/rss-hub@main/docs/feed.xml
```

## 工作方式

```
feeds.txt  ──►  build_feed.py  ──►  docs/feed.xml  ──►  GitHub Pages
（源清单）      （Actions 里跑）     （提交回仓库）        （对外订阅）
```

1. `.github/workflows/rss.yml` 每 5 分钟触发一次；
2. 作业内连着刷 4 轮（每轮间隔 60 秒），所以触发后的 4 分钟内 feed 保持分钟级新鲜；
3. 每源只取**最新 1 条**，跨源按「链接」和「标题归一化」双重去重；
4. 某个源临时抓不到时，自动回退到上一份 `feed.xml` 里该源的条目，不会整条消失；
5. 有变化才提交，没有变化不产生空提交。

## 加/减订阅源

只改 `feeds.txt`，格式是「注释行（源中文名）+ 下一行地址」，空行分组：

```
# ===== 财经 =====
# 财联社·电报滚动
https://feed.quanwenrss.com/feed/cailianshe-roll.xml
```

提交后 Actions 会自动重建一次。**注释行会成为条目里的 `<source>` 源名**，所以注释尽量写「媒体名·栏目」。

## 手动触发

仓库 → Actions → 「RSS 聚合」→ Run workflow。

## 文件说明

| 文件 | 用途 |
|---|---|
| `feeds.txt` | 订阅源清单（注释行 + 地址行） |
| `build_feed.py` | 采集 + 去重 + 生成 RSS（零依赖，Actions 用） |
| `docs/feed.xml` | 生成出来的订阅文件（Pages 发布目录） |
| `docs/index.html` | 订阅说明页 |
| `.github/workflows/rss.yml` | 定时任务 |
| `aggregate_rss.py` | 旧版：本地/树莓派跑，用 GitHub API 推送（可选保留） |
| `pi/` | 树莓派 systemd 自启 + 自恢复（可选，不用可忽略） |
| `server.js` | 早期 WorkBuddy 云沙箱自托管版（已不是主链路） |

## 本地调试

```bash
OUT_FILE=./docs/feed.xml python3 build_feed.py     # 只生成，不推送
```

## 已知限制

- GitHub 定时任务有排队延迟，实际刷新间隔通常在 5～15 分钟；
- 仓库连续 60 天无提交时 GitHub 会暂停定时任务（本仓库每次刷新都会提交，不会触发）；
- `github.io` 域名在部分网络环境下访问偏慢，可用上面的 raw / jsDelivr 镜像。
