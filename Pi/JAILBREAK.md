# KPW2（90D4 / 固件 5.12.2.2）越狱完整流程

**目标机**：Kindle Paperwhite 2，第 6 代，序列号前缀 `90D4`，固件 `5.12.2.2`，屏幕 **1024×758**。
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
4. 下载 `watchthis-jailbreak-r03.zip`（上面 MobileRead 帖子里有；网盘镜像也行）。
   **解压到电脑上，不要解压到 Kindle**。里面按机型/固件分目录，我们要的是 **`PW2/5.12.2.2/`**，
   需要三个文件：
   - `PW2-5.12.2.2.zip`（**不要解压**）
   - `demo.json`
   - `Update_hotfix_watchthis_custom.bin`（后面第 3 步用）
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
7. **PW2 专属：点「Store（商店）」按钮**。（其他机型才是搜 `;dsts`，KPW2 别走那条。）
8. 进 **Help & User Guides → Get started**。
9. 自动重启，越狱脚本在启动过程中执行完。

**成功标志**：USB 连电脑，根目录出现 **`mkk`** 文件夹。

**卡住了怎么办**
- 弹 Application Error：长按电源 **15~40 秒**硬重启 → `;demo` → Sideload Content → Done → Exit，
  **不要再拷一遍文件**。
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

1. 解压 MRPI 包，把里面的 **`extensions`** 和 **`mrpackages`** 两个目录拷到 Kindle 根目录。
2. 解压 KUAL 包，把 `Update_KUALBooklet_*_install.bin` 拷进 `mrpackages/`。
3. 弹出 → 搜索框输 **`;log mrpi`** 回车（或 KUAL → Helper → Install MR Packages）。
4. 看到 "Hush, little baby" 后 UI 重启，书库里多一本 KUAL 的书。
   期间弹 Application Error 是正常的，关掉即可。

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

1. `scp Pi/kindle/board.sh root@<kindle-ip>:/mnt/us/board.sh`
2. `ssh root@<kindle-ip> 'chmod +x /mnt/us/board.sh'`
3. 先**前台试跑**：`ssh root@<kindle-ip> 'sh /mnt/us/board.sh'`，看屏幕有没有出图。
   不出图就先在本机 curl `http://192.168.6.6:8082/kindle.png` 确认图床是好的，再排 Kindle 侧。
4. 确认 OK 后常驻：
   `ssh root@<kindle-ip> 'nohup sh /mnt/us/board.sh >/dev/null 2>&1 &'`
5. Kindle 没有 systemd，**重启后要重新拉一次**。要真正开机自启就把它挂到 KUAL 的启动项里。

刷新策略：5 分钟局部刷一次，每 10 次（约 50 分钟）用 `eips -c` 整屏闪一次清残影。
墨水屏刷太勤既费电也留残影，别把 `INTERVAL` 调太小。

---

## 出问题时的检查顺序

1. 图床：`curl -o /tmp/k.png http://192.168.6.6:8082/kindle.png` → 200 + PNG
2. Pi 侧渲染：`systemctl status kindle-screen.path`、`journalctl -u kindle-screen.service`
3. Kindle 能拿到图吗：`ssh` 进去 `wget -O /mnt/us/t.png http://192.168.6.6:8082/kindle.png`
4. 能画到屏吗：`eips -g /mnt/us/t.png`
