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
/home/user/kindle/kindle-<W>x<H>.png  ──复制──▶ rss-pw2.png（默认图，BOARD_PNG）
        │
moreaix-site.service（8081，--alias /kindle=… --alias /rss=…，Pi 上唯一一份 serve.py）
        │  一份实例出三样：/ 主页、/rss feed、/kindle 看板；Tunnel 只需一条根域规则，
        │  不再有 path 规则，也就没有顺序匹配/非锚定正则那些坑
        ▼
公网取 moreaix.com/kindle/rss-pw2.png
        ▼
Kindle: wget → eips -g / fbink -g
```

要点：

- **8082 那台服务已经删除**（不是停用，单元文件都没了）。它原本让局域网里的 Kindle 就近取图，
  但 Pi 把 wlan0 入站全 DROP，实测 24 小时零外部访问；现在看板图由 8081 的 `--alias /kindle`
  出（见 [`TUNNEL.md`](TUNNEL.md) 6.1）。整台机器**没有任何监听 `0.0.0.0` 的服务**。
- 出图用 **systemd path 单元**而不是 timer：不按固定节拍空跑。监视的是 `feed.xml`
  的 `PathChanged`（不是 `PathModified`，因为写方用的是「tmp + `os.replace`」，被换掉的
  inode 上不会产生 `IN_MODIFY`）。代价是：每轮构建都会原子替换一次 `feed.xml`，
  于是每轮构建都会重画一次 —— 「内容没变」也照样重画，最多白天 288 次/天。
- 条目左侧那个时间是**东八区**：`pubDate` 先按 RFC 822 解析再换算过来。feed 里各源的
  时区是混的（`+0800` / `+0000` / `GMT` / `+0900` 都有，实测 UTC 那类约占四成），
  直接抄原串会出现 `04:45` 夹在一排 `12:45` 中间——那是格林威治时间，不是乱序。
  不带时区的按 `+8` 解释，和 `build_feed` 的 `SOURCE_TZ_OFFSET` 同一套口径。
- PNG 走 `tmp + os.replace` 原子替换，Kindle 不会拉到半张图。
- 目标机已确认：**Kindle Paperwhite 2（第 6 代，序列号前缀 `90D4`）**，固定 `758x1024`
  （竖屏帧缓冲；面板参数写 1024×758 指的是横过来看，别拿它当 `--sizes`），
  同时复制成默认图 `rss-pw2.png`（`kindle_screen.py` 的 `BOARD_PNG`）。
  换机型只改 `kindle-screen.service` 的 `--sizes` 一行。

## 服务端（已完成，无需再动）

```bash
systemctl is-active kindle-screen.path moreaix-site.service
curl -o /tmp/k.png http://127.0.0.1:8081/kindle/rss-pw2.png   # 应为 200 / image/png
                                                              # 不带 /kindle 前缀也一样通
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/kindle/   # 网页版，也应为 200
python3 /home/user/pi-pipeline/kindle_screen.py --help
```

同一个 `kindle-screen.path` 现在会跑两条：`kindle_screen.py` 出 PNG、`kindle_page.py`
出 `index.html`（网页看板）。后者能滚动、能点开原文，条目数远多于一屏；不需要的话
删掉 `kindle-screen.service` 里第二条 `ExecStart`，PNG 那条路不受影响。

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

**想用 KUAL 点着用**（推荐），先打一个安装包：

```bash
sh Pi/kindle/build-extension-zip.sh      # 产出 Pi/kindle/dist/rssboard-extension.zip
```

把那个 zip 解压到 **Kindle 根目录**（不要多套一层文件夹），得到 `board.sh` 与
`extensions/rssboard/`。拔线后在书库打开 **KUAL**，会多出一组 **RSS Board**：
立即刷新一次 / 启动常驻刷新 / 停止刷新 / 查看状态（写屏）/ **自检** / 移除扩展。
**怎么退出看板**（两级，别混）：

- **临时回去**：轻点一下屏幕。系统收到触摸就会重绘书库，看板被盖掉；循环还在跑，
  5 分钟后又会盖回来。
- **真退出**：KUAL → RSS Board → 「恢复书库界面（停刷新）」。它同时做三件事：
  停循环、把 `preventScreenSaver` 释放（否则设备再也不休眠、一直耗电）、让书库重画。

排查「点了菜单一点反应都没有」时有个通用办法：临时加一个**只写文件、不做别的事**的
菜单项（写完 `E://probe.txt` 就退出）。文件出现了＝KUAL 能执行、问题在脚本内部；
没出现＝动作压根没跑（这招 2026-11-01 用来把「CRLF 解析失败」和「/bin/sh 解析不到」
两个原因分开）。排查完记得把那一项删掉。

「自检」的结果**写成文件**放在 Kindle 根目录：拔线插电脑打开
`E://rssboard-diag.txt` 就能看，屏上只有一行结论（eips 只能写 ASCII，塞不下细节）。
报告里逐项列出：board.sh 在不在、wget 路径、能不能真取到图（会实际下载一次）、
循环 pid、当前图的字节数与时间、扩展目录内容；wget 失败时还会带上它的报错原文。
文件是「新报告在最前、旧内容接在后面」，跑几次都留档。

其余菜单动作也会往同一个文件追加一行事件（`refresh: rc=0`、`start: pid 1234` 之类），
所以哪怕只是点了「启动常驻刷新」没生效，也能从文件里看出走到哪一步。

三条硬约束（都是实测踩出来的）：

- **拷到 Kindle 的 `.sh` 必须是 LF 换行**。CRLF 会让 Kindle 的 ash 把行尾 `\r` 当成
  命令的一部分，症状是 `sh: missing ]`、`syntax error: unexpected end of file`，
  **脚本一行都不执行，屏上却什么提示都没有**（2026-11-01 就因为这条白排查了一轮）。
  `build-extension-zip.sh` 打包时已经强制转 LF，并在产物里逐文件打印 `CRLF=0`；
  手工拷的话，务必确认编辑器没给你换成 CRLF。
- `menu.json` 的 `internal` **必须**是 `"status <文字>"` 或 `"breadcrumb <文字>"`；
  直接塞一句说明文字会让 KUAL 当成未知内部命令，动作跑完照样弹「执行出错」。
- eips 写屏只能用 ASCII：Kindle 自带点阵字体没有汉字，中文会变成空白/方块。

装完不出现菜单，先看 `config.xml`、`menu.json` 有没有真的到位——少一个文件 KUAL
只是静静地不显示，没有任何报错。zip 里的 `board.sh` 是**当前工作区**那份，改过
地址或文件名记得重打一次，别拿旧包去覆盖。

刷新策略：5 分钟拉一次图，**内容没变就不刷屏**（`cmp` 比对）；每真正刷新 10 次用
`eips -c` 整屏闪一次清残影。e-ink 刷太勤既费电也留残影。

## 渲染器

`kindle_screen.py`（零第三方依赖以外的 Pillow）：

- 字体自动挑：扫 Noto CJK → 文泉驿 → Arphic，按 index 试到能出「中」字为止，缺字不会画成豆腐块。
- 折行按「汉字逐字、英数单词整体」切分，行首不出现收尾标点。
- 版式：页眉（「RSS 头条」+ 右上角更新时间）→ 一条分隔线 → 时间 + 标题列表 → 页脚（条数）。
  列表里**所有条目同一规格，第一条不放大**，也没有摘要区；标题最多 2 行，放不下就
  「一行 + 省略号」。条数按剩余高度自动塞，横竖屏通用（以短边为基准缩放）。
- 灰阶：正文纯黑，次要文字（时间戳 / 页眉时间 / 页脚）用 140（≈#8C）——
  e-ink 只有 16 级灰，再浅一档就开始发虚。
