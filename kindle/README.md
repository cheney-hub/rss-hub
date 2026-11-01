# Kindle 看板（e-ink RSS board）

把 `feed.xml` 渲染成灰度 PNG，Kindle 定时拉取并画到屏幕上，做成一个会自己更新的头条看板。

> **本项目与 `Pi/` 相互独立。** 两个目录各自有自己的 `deploy.sh` 与 `services/`，脚本之间
> 互不引用、互不安装对方的单元。它们只是**恰好跑在同一台树莓派上**（共用 `/home/user/pi-pipeline`
> 这个部署目录和同一个公网出口），任何一方删掉，另一方照常工作。
>
> 分工一句话：**谁的设备谁的代码**。同为本项目库的成员，文件按「跑在哪台机器上」分成两摊：
>
> | 跑在哪 | 文件 | 怎么装 |
> |---|---|---|
> | 树莓派（渲染端） | `kindle_screen.py` / `kindle_page.py` / `services/*.{path,service}` | `sh kindle/deploy.sh` |
> | Kindle 本身（设备端） | `board.sh` / `extension/rssboard/`（KUAL 菜单） | `sh kindle/build-extension-zip.sh` 打包后拷进去 |

## 目录结构

```
kindle/
├── README.md               # 本文件：看板链路、部署、上屏步骤、排障
├── JAILBREAK.md            # KPW2（90D4 / 5.12.2.2）完整越狱流程（WatchThis）—— 不越狱上不了屏
├── kindle_screen.py        # 【跑在树莓派】feed.xml → 758×1024 灰度 PNG（依赖 Pillow）
├── kindle_page.py          # 【跑在树莓派】feed.xml → index.html 网页看板（零第三方依赖）
├── services/
│   ├── kindle-screen.path      # 监视 feed.xml 的 PathChanged，变了就拉起下面那个单元
│   └── kindle-screen.service   # oneshot：上面那两个 .py 各跑一次，出 PNG 与网页
├── deploy.sh               # 只装渲染端（两个 .py + 两个单元），不碰 Pi/ 的东西
├── board.sh                # 【跑在 Kindle 上】定时 wget 看图 → eips 画屏
├── build-extension-zip.sh  # 打 KUAL 扩展安装包：「解压到 Kindle 根目录即可」的 zip（日常用这个）
├── build-mrpackage.sh      # 打成 MRPI 的 .bin（要先有 kindletool，重装整机时才值）
└── extension/rssboard/     # KUAL 扩展本体
    ├── config.xml          #   扩展元信息；少了它 KUAL 只是**静静地不显示菜单**
    ├── menu.json           #   菜单项。action 一律写 "sh <绝对路径>"
    └── bin/
        ├── common.sh       #     六个菜单脚本共用的路径与三个小函数
        ├── start.sh / stop.sh / refresh.sh    # 启停循环 / 立即画一帧
        ├── status.sh       #     状态摘要（写屏 + 写文件）
        ├── doctor.sh       #     自检：四段挨个验，结果写成文件
        ├── restore.sh      #     把屏幕还给书库
        └── uninstall.sh    #     删掉扩展目录本身
```

## 链路

```
rss-build.service（每 5 分 / 夜间每小时，根目录引擎）
        │  写 docs/feed.xml
        ▼
kindle-screen.path ──▶ kindle-screen.service
        │  只在 feed.xml 变化时触发，不轮询
        ▼
/home/user/kindle/kindle-<W>x<H>.png  ──复制──▶ rss-pw2.png（默认图，BOARD_PNG）
        │
moreaix-site.service（8081，--alias /kindle=…，Pi 上唯一一份 serve.py）
        │  一台出主页 + feed + 看板图，Tunnel 只需一条根域规则
        ▼
公网取 moreaix.com/kindle/rss-pw2.png
        ▼
Kindle: wget → eips -g / fbink -g
```

要点：

- **公网那一跳不由本项目负责**：8081 是 `Pi/` 的静态服务，Tunnel 的规则写在
  [`../Pi/TUNNEL.md`](../Pi/TUNNEL.md) 里。本目录只负责产出 `/home/user/kindle/` 下的文件。
- 出图用 **systemd path 单元**而不是 timer：不按固定节拍空跑。监视的是 `feed.xml`
  的 `PathChanged`（不是 `PathModified`，因为写方用的是「tmp + `os.replace`」，被换掉的
  inode 上不会产生 `IN_MODIFY`）。代价是：每轮构建都会原子替换一次 `feed.xml`，
  于是每轮构建都会重画一次 —— 「内容没变」也照样重画，最多白天 288 次/天。
- 条目左侧那个时间是**东八区**：`pubDate` 先按 RFC 822 解析再换算过来。feed 里各源的
  时区是混的（`+0800` / `+0000` / `GMT` / `+0900` 都有，实测 UTC 那类约占四成），
  直接抄原串会出现 `04:45` 夹在一排 `12:45` 中间——那是格林威治时间，不是乱序。
  不带时区的按 `+8` 解释，和根目录 `build_feed` 的 `SOURCE_TZ_OFFSET` 同一套口径。
- PNG 走 `tmp + os.replace` 原子替换，Kindle 不会拉到半张图。
- 目标机已确认：**Kindle Paperwhite 2（第 6 代，序列号前缀 `90D4`）**，固定 `758x1024`
  （竖屏帧缓冲；面板参数写 1024×758 指的是横过来看，别拿它当 `--sizes`），
  同时复制成默认图 `rss-pw2.png`（`kindle_screen.py` 的 `BOARD_PNG`）。
  换机型只改 `kindle-screen.service` 的 `--sizes` 一行。

## 渲染端部署（树莓派）

```bash
sh kindle/deploy.sh     # 用法见脚本头：PI_USER=user PI_HOST=100.88.176.80 ./deploy.sh
```

它只做三件事：传两个渲染器到 `pi-pipeline/`、装两个单元、`try-restart` 那个 `.path`。
**跑 `Pi/deploy.sh` 不会捎带做这里的事，也不需要** —— 反过来也一样。

装完自查：

```bash
systemctl is-active kindle-screen.path
curl -o /tmp/k.png http://127.0.0.1:8081/kindle/rss-pw2.png   # 应为 200 / image/png
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/kindle/   # 网页版，也应为 200
python3 /home/user/pi-pipeline/kindle_screen.py --help
```

同一个 `kindle-screen.path` 现在会跑两条：`kindle_screen.py` 出 PNG、`kindle_page.py`
出 `index.html`（网页看板）。后者能滚动、能点开原文，条目数远多于一屏；不需要的话
删掉 `kindle-screen.service` 里第二条 `ExecStart`，PNG 那条路不受影响。

### 渲染器细节

`kindle_screen.py`（除 Pillow 外零第三方依赖）：

- 字体自动挑：扫 Noto CJK → 文泉驿 → Arphic，按 index 试到能出「中」字为止，缺字不会画成豆腐块。
- 折行按「汉字逐字、英数单词整体」切分，行首不出现收尾标点。
- 版式：页眉（「RSS 头条」+ 右上角更新时间）→ 一条分隔线 → 时间 + 标题列表 → 页脚（条数）。
  列表里**所有条目同一规格，第一条不放大**，也没有摘要区；标题最多 2 行，放不下就
  「一行 + 省略号」。条数按剩余高度自动塞，横竖屏通用（以短边为基准缩放）。
- 灰阶：正文纯黑，次要文字（时间戳 / 页眉时间 / 页脚）用 140（≈#8C）——
  e-ink 只有 16 级灰，再浅一档就开始发虚。

## 设备端上屏（要越狱）

**不越狱做不到实时更新。** 原生系统既不允许自定义屏保，实验版浏览器也不能定时刷新、
且一休眠画面就没了。所以这一步的前提是越狱。

完整越狱流程见 **[`JAILBREAK.md`](JAILBREAK.md)**（方案、每一步、卡住的补救、关 OTA 的方法都在里面）。
这里只记三条最容易搞错的：

- **只能用 WatchThis，不能用 LanguageBreak** —— MobileRead 兼容表把 KPW2 @ 5.12.2.2
  标成「未确认/有报告问题」，WatchThis 则明确支持 PW2 + 5.12.2.2。
- locale 必须选 **English (United Kingdom)**，别的语言不触发。
- **KPW2 是点「Store（商店）」按钮**进下一步，不是搜 `;dsts`（那是别的机型）。

### 用法一：命令行（最简）

越狱并装好 USBNetwork 之后，上屏就三步：

1. `scp kindle/board.sh root@<kindle-ip>:/mnt/us/board.sh`
2. `sh /mnt/us/board.sh` 前台试跑，确认出图
3. `nohup sh /mnt/us/board.sh >/dev/null 2>&1 &` 常驻（Kindle 没有 systemd，重启要重拉）

### 用法二：KUAL 菜单（推荐）

先打一个安装包：

```bash
sh kindle/build-extension-zip.sh      # 产出 kindle/dist/rssboard-extension.zip
```

把那个 zip 解压到 **Kindle 根目录**（不要多套一层文件夹），得到 `board.sh` 与
`extensions/rssboard/`。拔线后在书库打开 **KUAL**，会多出一组 **RSS Board**：
立即刷新一次 / 启动常驻刷新 / 停止刷新 / 查看状态（写屏）/ **自检** / 恢复书库界面 / 移除扩展。

> zip 里的 `board.sh` 是**当前工作区**那份，改过地址或文件名记得重打一次，别拿旧包去覆盖。

**怎么退出看板**（两级，别混）：

- **临时回去**：轻点一下屏幕。系统收到触摸就会重绘书库，看板被盖掉；循环还在跑，
  5 分钟后又会盖回来。
- **真退出**：KUAL → RSS Board → 「恢复书库界面（停刷新）」。它同时做三件事：
  停循环、把 `preventScreenSaver` 释放（否则设备再也不休眠、一直耗电）、让书库重画。

### 三条硬约束（都是实测踩出来的）

- **拷到 Kindle 的 `.sh` 必须是 LF 换行**。CRLF 会让 Kindle 的 ash 把行尾 `\r` 当成
  命令的一部分，症状是 `sh: missing ]`、`syntax error: unexpected end of file`，
  **脚本一行都不执行，屏上却什么提示都没有**（2026-11-01 就因为这条白排查了一轮）。
  `build-extension-zip.sh` 打包时已经强制转 LF，并在产物里逐文件打印 `CRLF=0`；
  手工拷的话，务必确认编辑器没给你换成 CRLF。
- `menu.json` 的 `internal` **必须**是 `"status <文字>"` 或 `"breadcrumb <文字>"`；
  直接塞一句说明文字会让 KUAL 当成未知内部命令，动作跑完照样弹「执行出错」。
- eips 写屏只能用 ASCII：Kindle 自带点阵字体没有汉字，中文会变成空白/方块。
  所以本目录所有写屏文案一律英文。

装完不出现菜单，先看 `config.xml`、`menu.json` 有没有真的到位——少一个文件 KUAL
只是静静地不显示，没有任何报错。

### 刷新策略

5 分钟拉一次图，**内容没变就不刷屏**（`cmp` 比对）；每真正刷新 10 次用 `eips -c`
整屏闪一次清残影。e-ink 刷太勤既费电也留残影。计数按的是**真正刷屏的次数**，
不是墙钟轮次 —— 图没变的那轮不计入。

## 排障

**排查入口永远是「自检」菜单**（`bin/doctor.sh`）：结果写成 **`E://rssboard-diag.txt`**
（新报告在最前、旧内容接在后面），拔线插电脑直接打开，**不用 SSH、不用看屏**。屏上只留
一行结论。所有菜单动作与 `board.sh` 的 stderr 也都追加进同一份文件，所以哪怕只是点了
「启动常驻刷新」没生效，也能从文件里看出走到哪一步。

自检逐项列出：board.sh 在不在、wget 路径、能不能真取到那张图（会实际下载一次，
顺带验 DNS / TLS / CDN 三层）、循环 pid、当前图的字节数与时间、扩展目录内容；
wget 失败时还会带上它的报错原文。

排查「点了菜单一点反应都没有」时有个通用办法：临时加一个**只写文件、不做别的事**的
菜单项（写完 `E://probe.txt` 就退出）。文件出现了＝KUAL 能执行、问题在脚本内部；
没出现＝动作压根没跑（这招 2026-11-01 用来把「CRLF 解析失败」和「/bin/sh 解析不到」
两个原因分开）。排查完记得把那一项删掉。

常见问题：

| 现象 | 先看什么 |
|---|---|
| 屏幕停在旧画面不动 | 局域网 IP 已彻底作废（Pi 把 wlan0 入站全 DROP），图只能走公网 `moreaix.com` |
| wget 报证书错误 | KPW2 那套根证书较旧；把 `board.sh` 的 URL 换成 `http://`（实测不跳转、直接 200） |
| 点菜单毫无反应 | 先看 `.sh` 是不是 CRLF；再看 `rssboard-diag.txt` 有没有新增一行 |
| 拔掉 USB 后不再刷新 | Kindle 没有 systemd，重启后要重拉；或改用 KUAL 的「启动常驻刷新」 |
| 设备不休眠、一直在耗电 | 用 KUAL 的「恢复书库界面」或 `stop.sh` 释放 `preventScreenSaver` |
