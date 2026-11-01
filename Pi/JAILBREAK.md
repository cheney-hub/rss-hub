# KPW2（90D4 / 固件 5.12.2.2）越狱完整流程

**目标机**：Kindle Paperwhite 2，第 6 代，序列号前缀 `90D4`，固件 `5.12.2.2`，屏幕 6 吋，**竖屏 758×1024**（面板规格常写成横屏 1024×758）。
**方案**：**WatchThis**（demo 模式 payload 漏洞）。

> ⚠️ 先读三遍再动手
> - **会清空设备**：商店书还在亚马逊云端，但本地书/笔记/阅读进度会没，先用 USB 把 `documents/` 整个拷走。
> - **有变砖风险**，会失去官方保修。搞不定就停在原地，别硬来。
> - **不能用 LanguageBreak**：MobileRead 的兼容表把 **KPW2 @ 5.12.2.2 标为「未确认 / 有报告问题」**，
>   而 WatchThis 明确列出 **KT2 / PW2 → 5.12.2.2**。这条别选错。
> - 全程**飞行模式 / 断网**，根目录**先删掉任何 `.bin` 和 `update.bin.tmp.partial`**，
>   否则机器可能在你操作途中自己把新固件装上，漏洞就没了。

资料来源：MobileRead 原帖 [WatchThis – Software Jailbreak for any Kindle](https://www.mobileread.com/forums/showthread.php?t=346037)、
[kindlemodding.org WatchThis 页](https://kindlemodding.org/jailbreaking/Legacy/WatchThis/)、
MobileRead Wiki 的 WatchThis 条目。

---

## 0. 准备

1. USB 拷走 `documents/` 备份。
2. 删根目录所有 `.bin` 和 `update.bin.tmp.partial`。
3. 去掉设备密码锁（忘了就在密码框输 `111222777` 重置，会清数据）。
4. 下载 `watchthis-jailbreak-r03.zip`。官方直链在 MobileRead 一楼的 **Download Link**：
   <https://mega.nz/file/2ahlQKZS#jXyYLEp9rvRQCOzv7LNYBF-9fOfPhpigaLZMHZkN7fg>
   （`#` 后面那段是解密密钥，必须连着一起复制；mega 下不动就改用网盘镜像，但**务必用下面的校验值验一遍**）。
   **解压到电脑上，不要解压到 Kindle**。解压出 `watchthis-release/`，里面先按机型分子目录、
   再按「机型-固件」分子目录，我们要的是 **`watchthis-release/PW2/PW2-5.12.2.2/`**，需要三个文件：
   - `PW2-5.12.2.2.zip`（**不要解压**）
   - `demo.json`
   - `Update_hotfix_watchthis_custom.bin`（第 3 步用；**注意它在 `watchthis-release/` 根目录**，
     不在 PW2 子目录里）

   校验值（2026-10 实测）：整包 646247 字节，SHA256 `38ed5dadd0f98b1ac846967fbde14aa0543fc824207ee1889159993c87a2bfa3`；
   `PW2-5.12.2.2.zip` 的 MD5 是 `4e6052f30ada437c5924a6e416e186c8`，**与 `demo.json` 里
   `files[0].checksum` 是同一个值**——这个字段本来就是给机器验的，对不上说明包坏了或被换过，别往机器上放。
5. 开飞行模式。

---

## 1. 进 Demo 模式

1. 设置 → 设备选项 → **重置**（恢复出厂）。
2. 语言选 **English (United Kingdom) / en_GB** —— 这一步必须选对，别的 locale 不触发。
3. WiFi 页：**随便点一个网络再退出来**，跳过注册。
4. 搜索框输 `;enter_demo` 回车 → 重启 → 进 demo 模式。
5. demo 里依次：跳过 WiFi → 商店注册**填任意假值**（只有 Store Name 必填）→
   「Fetching available demo types」点 **Skip** → demo 类型选 **standard** →
   「Demo Mode: Add Content」按 **Done**（**这一步先别放越狱文件**）。
6. 屏幕进屏保，点一下会弹 "CONFIGURE DEVICE" 错误 →
   **秘密手势**：**右下角双指点击后抬起，再单指向左滑**（可能要试几次）。
   看不懂的话去 kindlemodding 那个页面找手势演示视频。

---

## 2. 装越狱

1. 搜索框输 `;demo` 回车 → 选 **Sideload Content**。
2. USB 连电脑，在 Kindle **根目录**建文件夹 `.demo`（注意前面的点）。
3. 拷进去：`PW2-5.12.2.2.zip`（**保持压缩状态，别解压**）+ `demo.json`。
4. 在 `.demo/` 里**再建一个空目录 `goodreads`**（必须空着，一个文件都别放）。
   ```
   Kindle 根目录
   └── .demo
       ├── PW2-5.12.2.2.zip
       ├── demo.json
       └── goodreads/      ← 空
   ```
5. **安全弹出** → 按 **Done** → 静默安装。
6. 退出 demo 菜单。
7. **KPW2 / Kindle 7（我们是这个）：点「商店小车」图标。**
8. 其他机型（KPW3/4/5、KO1/2/3、Kindle 8/10、KV）才是：搜 `;dsts` 进设置 →
   **Help & User Guides → Get started**。

   ⚠️ **第 7 和第 8 是二选一，不是先后两步。** 原笔记把它们串行写了，照着做会在点完商店
   之后去找「Help」，而 **KPW2 上根本没有这个入口**（2026-10 现场确认）。MobileRead 原文
   第 10 条就是 "If jailbreaking KT2 or PW2, select the store button *instead*"。
9. 触发之后**设备自动重启**，越狱脚本在这次启动过程中跑完。

**成功标志**：USB 连电脑，根目录出现 **`mkk`** 文件夹。

**卡住了怎么办**
- 弹 Application Error：长按电源 **15~40 秒**硬重启 → `;demo` → Sideload Content → Done → Exit，
  **不要再拷一遍文件**。
- **点完商店没反应 / 找不到「Help & User Guides」**：KPW2 走的是第 7 条（商店小车），
  第 8 条是别的机型的走法，**PW2 上没有这个入口**。判定标准是「设备有没有自己重启」——
  重启了就算触发成功，直接看根目录有没有 `mkk`，有就进第 3 步。没重启就再点一次商店图标，
  还不行就按上面那条硬重启重来一遍（同样别重拷文件）。
- `;enter_demo` 进不去：根目录建一个空文件 `DONT_CHECK_BATTERY`，再输 `;demo` 试。
- demo 里要重置：搜 `;uzb` 开 USB 存储 → 根目录建空文件 `DO_FACTORY_RESTORE` → 重启。

---

## 3. 装 WatchThis 专用热修复（必须先做）

不装这个，后面 MRPI / KUAL 全都装不上，而且重启可能出问题。

1. 搜索框输 `;uzb` 回车（开 USB 存储）。
2. 把 `Update_hotfix_watchthis_custom.bin` 拷到 **根目录**。
3. 弹出 → `;dsts`（或下拉进设置）→ **Update Your Kindle**。
4. 装完会**自动脱离 demo 模式**、重建应用注册表、清掉越狱残留文件。

---

## 4. 装 MRPI + KUAL（拿到"能装东西"和"能点东西"的能力）

下载页 <https://kindlemodding.org/jailbreaking/Legacy/post-jailbreak/installing-kual-mrpi>，
2026-11 实测可用的四个直链：

| 组件 | 适用 | 直链 |
|---|---|---|
| MRPI (modern) | **KPW2 @ 5.12.2.2 用这个**（固件 5.x 的都用它） | <https://kindlemodding.org/jailbreaking/Legacy/post-jailbreak/installing-kual-mrpi/kual-mrinstaller-khf.zip> |
| MRPI (legacy - pre-K5) | K4 / K3 / DX（固件 4.x 及更早） | <https://kindlemodding.org/jailbreaking/Legacy/post-jailbreak/installing-kual-mrpi/kual-mrinstaller-1.7.N-r19303.zip> |
| PEKI（KUAL） | K5 及更新，**我们用这个** | <https://github.com/KindleTweaks/PEKI/releases/latest/download/PEKI.zip> |
| KUAL-KDK-1.0.azw2 | K4 及更早，放 `documents/` | <https://kindlemodding.org/jailbreaking/Legacy/post-jailbreak/KUAL-KDK-1.0.azw2> |

两个 MRPI 按钮**按机型二选一**，别都装：modern 版是 Marek 的 `kual-mrinstaller-khf.zip`
（作者原话「normal mrpi 支持的机器它都支持」），legacy 版 `kual-mrinstaller-1.7.N-r19303.zip`
只给 pre-K5 的机器。

1. 解压 MRPI 包（**用 7-Zip 或 `tar`，别用 WinZip**——它解 `.xz` 会坏文件，之后 `;log mrpi`
   只会回一句 "MRPI is not installed"），把里面的 **`extensions`** 和 **`mrpackages`**
   两个目录拷到 Kindle 根目录。
2. KUAL 两条路，二选一：
   - **PEKI（页面现在推荐这个）**：解压 `PEKI.zip`，把 `KUAL.sh` 和 `KUAL.jar` 拷进 **`documents/`**，
     回书库直接点 KUAL 即可，不需要 `;log mrpi`。
   - **老办法**：`Update_KUALBooklet_*_install.bin` 拷进 `mrpackages/`，再走下面第 3、4 步装上。
3. 弹出 → 搜索框输 **`;log mrpi`** 回车（或 KUAL → Helper → Install MR Packages）。
4. 看到 "Hush, little baby" 后 UI 重启，书库里多一本 KUAL 的书。
   期间弹 Application Error 是正常的，关掉即可。

⚠️ 装之前确认有 **约 220 MB** 空余空间；**先开飞行模式再腾空间**，否则 Kindle 可能拿这块空地
去后台下 OTA 更新，漏洞就白做了。

**`;log mrpi` 完全没反应**（PW2 上有人遇到过）：先装
[`Update_hotfix_universal.bin`](https://github.com/KindleModding/Hotfix/releases)，再重试 `;log mrpi`。

---

## 5. 装 USBNetwork（拿 SSH，后面才好调试）

看板脚本要在 shell 里跑，有个 SSH 会省很多事。

1. 把 `Update_usbnet_0.xx.N_install_pw2_and_up.bin` 拷进 `mrpackages/` → `;log mrpi` → 装完重启。
2. 根目录出现 `usbnet/`：
   - `DISABLED_auto` 改名 **`auto`**（开机自启）
   - `usbnet/etc/config` 里把 `USE_WIFI="true"`、`USE_WIFI_SSHD_ONLY="true"`
   - 想免密就把自己的公钥放进 `usbnet/etc/authorized_keys`
3. 搜 `;711` 看设备 IP；`ssh root@<那个IP>`，默认密码一般是 `mario`。
4. ⚠️ **装了 USBNetwork 后 USB 磁盘模式会失效**，之后传文件走 `scp` / sftp。
5. 系统根目录 `/` 是只读的，**只有 `/mnt/us/` 能永久写** —— 别去动系统分区。

---

## 6. 关 OTA（这一步不做，一联网就被推新固件）

看板必须要联网，所以**不能靠一直开飞行模式**躲更新，得真的把 OTA 堵掉。

固件 5.11 及以上用 **renameotabin** 扩展（原理是把 `/usr/bin/otaupd`、`/usr/bin/otav3` 改名）：

1. 下载解压 renameotabin，把 `renameotabin` 文件夹拷进 `extensions/`
   （有的包里套了一层同名目录，拷**最里面**那层）。
2. 确认根目录没有 `.bin` / `update.bin.tmp.partial`。
3. 弹出 → 打开 KUAL → **Rename OTA binaries → Rename** → 自动重启。
4. 现在才能关掉飞行模式连 WiFi。
5. **验证**：设置里的 "Update Your Kindle" 按钮应该是**灰色点不动**的。
6. 以后要升降级或恢复出厂：KUAL → Rename OTA binaries → **Restore**，否则装不上固件。

---

## 7. 上我们的看板

两条路，**装一次就行，选一条**：

**A. scp（最省事，改脚本方便）**

1. `scp Pi/kindle/board.sh root@<kindle-ip>:/mnt/us/board.sh`
2. `ssh root@<kindle-ip> 'chmod +x /mnt/us/board.sh'`
3. 先**前台试跑**：`ssh root@<kindle-ip> 'sh /mnt/us/board.sh'`，看屏幕有没有出图。
   不出图就先确认图床是好的：`curl -o /tmp/k.png https://moreaix.com/kindle/rss-pw2.png`
   （`board.sh` 的默认地址就是它；局域网那条 8082 已经彻底删除，别再照旧笔记去试。）
4. 确认 OK 后常驻：
   `ssh root@<kindle-ip> 'nohup sh /mnt/us/board.sh >/dev/null 2>&1 &'`

**B. KUAL 扩展（之后能在机器上点着用）**

先跑 `sh Pi/kindle/build-extension-zip.sh` 打出一个安装包
（`Pi/kindle/dist/rssboard-extension.zip`），把它的内容解压到 Kindle **根目录**：
根上多一个 `board.sh`、`extensions/` 下多一个 `rssboard/`。
之后打开 KUAL，会多出一组 **RSS Board** 菜单：立即刷新一次 / 启动常驻刷新 / 停止刷新 /
查看状态（写屏）/ 自检 / 移除扩展。

> 拷脚本时留意**换行**：`.sh` 必须是 LF。CRLF 会让 Kindle 的 ash 报 `missing ]` /
> `syntax error`，而且一行都跑不起来、屏上毫无提示。用 `build-extension-zip.sh`
> 打的包已经强制转成 LF 了，手工拷的话自己确认一下。出问题先点「自检」，它把结果写成 `E://rssboard-diag.txt`
（board.sh 在不在、wget、实际取图、循环 pid 逐项列出来），拔线插电脑直接看。菜单不出现就先查 `config.xml` 和 `menu.json` 有没有到位
——少一个 KUAL 只是静静地不显示，没有任何报错。

**C. 打成 MRPI 安装包（要重装机器时才值得费这个事）**

`Pi/kindle/build-mrpackage.sh` 会把 `board.sh` + 扩展一起包成
`Update_rssboard_install.bin`，丢进根目录 `mrpackages/` 后 `;log mrpi` 即可安装。
它需要一台能编 `kindletool` 的 Linux（树莓派就行），详见脚本头注释。

无论哪条路：**Kindle 没有 systemd，重启后都要重新点一次「启动常驻刷新」**（或重新 nohup）。

刷新策略（按 `board.sh` 实际行为）：每 **5 分钟拉一次图**，内容变了才 `eips -g` 局部刷屏；
`FULL_EVERY=10` 是**按真正刷屏的次数**计数，不是墙钟 50 分钟——图长期不变时它会一直等着，
不会自己整屏闪。墨水屏刷太勤既费电也留残影，别把 `INTERVAL` 调小。

---

## 出问题时的检查顺序

1. 图床：`curl -o /tmp/k.png https://moreaix.com/kindle/rss-pw2.png` → 200 + PNG
2. Pi 侧渲染：`systemctl status kindle-screen.path`、`journalctl -u kindle-screen.service`
3. Kindle 能拿到图吗：`ssh` 进去 `wget -O /mnt/us/t.png https://moreaix.com/kindle/rss-pw2.png`
4. 能画到屏吗：`eips -g /mnt/us/t.png`
