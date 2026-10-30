# Kindle 看板（e-ink RSS board）

把 `feed.xml` 渲染成灰度 PNG，Kindle 定时拉取并画到屏幕上，做成一个会自己更新的头条看板。

## 链路

```
rss-build.service（每 5 分 / 夜间每小时）
        │  写 docs/feed.xml
        ▼
kindle-screen.path ──▶ kindle-screen.service
        │  只在 feed.xml 变化时触发，不轮询
        ▼
/home/user/kindle/kindle-<W>x<H>.png  （8bit 灰度，白底黑字）
        │
kindle-serve.service（8082，绑 0.0.0.0，仅局域网）
        ▼
Kindle: wget → eips -g / fbink -g
```

要点：

- **8082 是唯一绑 `0.0.0.0` 的服务**。8080/8081 仍只监听 `127.0.0.1` 给 Cloudflare Tunnel 用，
  看板这一套不进公网、不走 Tunnel，和原有两套系统完全隔离。
- 出图用 **systemd path 单元**而不是 timer：feed 没变就一次都不跑，改一次画一次。
- PNG 走 `tmp + os.replace` 原子替换，Kindle 不会拉到半张图。
- 目标机已确认：**Kindle Paperwhite 2（第 6 代，序列号前缀 `90D4`）**，固定 `1024x758`，
  同时复制成 `kindle.png`。换机型只改 `kindle-screen.service` 的 `--sizes` 一行。

## 服务端（已完成，无需再动）

```bash
systemctl is-active kindle-screen.path kindle-serve.service
curl -o /tmp/k.png http://127.0.0.1:8082/kindle.png   # 应为 200 / image/png
python3 /home/user/pi-pipeline/kindle_screen.py --help
```

## 客户端（要越狱才能做）

**不越狱做不到实时更新。** 原生系统既不允许自定义屏保，实验版浏览器也不能定时刷新、
且一休眠画面就没了。所以这一步的前提是越狱。

完整越狱流程见 **[`JAILBREAK.md`](JAILBREAK.md)**（方案、每一步、卡住的补救、关 OTA 的方法都在里面）。
这里只记三条最容易搞错的：

- **只能用 WatchThis，不能用 LanguageBreak** —— MobileRead 兼容表把 KPW2 @ 5.12.2.2
  标成「未确认/有报告问题」，WatchThis 则明确支持 PW2 + 5.12.2.2。
- locale 必须选 **English (United Kingdom)**，别的语言不触发。
- **KPW2 是点「Store（商店）」按钮**进下一步，不是搜 `;dsts`（那是别的机型）。

越狱并装好 USBNetwork 之后，上屏就三步：

1. `scp kindle/board.sh root@<kindle-ip>:/mnt/us/board.sh`
2. `sh /mnt/us/board.sh` 前台试跑，确认出图
3. `nohup sh /mnt/us/board.sh >/dev/null 2>&1 &` 常驻（Kindle 没有 systemd，重启要重拉）

刷新策略：5 分钟局部刷一次，每 10 次用 `eips -c` 整屏闪一次清残影。
e-ink 刷太勤既费电也留残影。

## 渲染器

`kindle_screen.py`（零第三方依赖以外的 Pillow）：

- 字体自动挑：扫 Noto CJK → 文泉驿 → Arphic，按 index 试到能出「中」字为止，缺字不会画成豆腐块。
- 折行按「汉字逐字、英数单词整体」切分，行首不出现收尾标点。
- 版式：页眉（更新时间）→ 头条大字 + 摘要 → 分隔线 → 时间 + 标题列表 → 页脚（条数）。
  列表条数按剩余高度自动塞，横竖屏通用（以短边为基准缩放）。
- 灰阶：正文用 70 而不是常见的 #888 —— e-ink 只有 16 级灰，浅灰在屏上会糊。
