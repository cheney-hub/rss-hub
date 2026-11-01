# 树莓派 + Cloudflare Tunnel 接入指引

目标：让树莓派上产出的 `feed.xml` / `feed-wrap.xml` / `rotate.json`
通过 `https://moreaix.com/rss/...` 对外稳定可拉取，**不经过 GitHub**。

## 架构

```
 各 RSS 源 ──抓取──> 树莓派 rss-build.timer (build_feed.py) ──写──> docs/feed.xml
                                                                        │
                                  rss-rotate.timer (rotate_screen.py) ──┤ 只读 feed.xml
                                                                        ▼
                                                        docs/feed-wrap.xml + docs/rotate.json
                                                                        │
                                                    serve.py 读 docs/ (:8080)
                                                                        │
                                                   cloudflared tunnel (出站连接)
                                                                        │
                                          Cloudflare 边缘  https://moreaix.com/rss/feed.xml
                                                                        │
                                                              客户端 / 墨水屏设备
```

要点：
- 树莓派 **只发起出站连接**，不需要公网 IP、不用开端口、不用路由器映射。
- Pi 是 `feed-wrap.xml` / `rotate.json` 的唯一写者与唯一服务器；`feed.xml` 有两个来源
  （GitHub Pages 那份给阅读器订阅，Pi 这份供 `moreaix.com/rss/` 与设备用），
  两边各自独立推进，不会互相覆盖 —— 但也别指望它们内容一致。
- 抓取与轮播分属两个 timer：`rss-build.timer`（白天 5 分钟 / 夜间整点）只管池子，
  `rss-rotate.timer`（全天 5 分钟）管屏幕播哪条。别把两者合并。
- 代价：**Pi 必须 7×24 在线**，断电/断网客户端就拉不到。

## 前置条件（已完成）

- 域名 `moreaix.com` 已在 Cloudflare Registrar 注册；NS 为
  `JONAH.NS.CLOUDFLARE.COM` / `OLLIE.NS.CLOUDFLARE.COM`（zone 已就绪）。

## 第 1 步：在 Cloudflare 后台创建 Tunnel

1. 登录 <https://dash.cloudflare.com> → 左侧 **Zero Trust**
2. **Networks → Tunnels → Create a tunnel**
3. 选 **Cloudflared** → 名字填 `rss-pi` → **Save tunnel**
4. 进入「Install connector」页，复制那串 **token**（或整条
   `cloudflared service install <TOKEN>` 命令）。留在这个页面别关。

> 这里必须由你在自己账号里点，我无法代登。若你愿意给一个
> 仅含 `Account: Cloudflare Tunnel:Edit` + `Zone: DNS:Edit` 权限的 API Token，
> 我可以脚本化建 tunnel 和 DNS 记录，再把 token 交给你。

## 第 2 步：配置公开主机名

仍在 tunnel 页面 → **Public Hostname** 标签 → **Add a public hostname**：

| 字段 | 填 |
|---|---|
| Subdomain | 留空（根域） |
| Domain | `moreaix.com` |
| Path | `/rss` |
| Service · Type | `HTTP` |
| Service · URL | `localhost:8080` |

保存后，Cloudflare 会自动建一条打通的记录：
`moreaix.com → <tunnel-id>.cfargotunnel.com`

- 客户端地址即：`https://moreaix.com/rss/feed.xml` 和 `/rss/feed-wrap.xml`
- `serve.py` 的前缀是**可选**的（见 `_target()`）：带 `/rss` 和不带都能找到文件。
  不是因为 Cloudflare 会剥前缀 —— 实测它不会（下面 6.1 有证据）。

## 第 3 步：在树莓派安装 cloudflared

SSH 到 Pi，执行（用第 1 步复制的命令）：

```bash
sudo cloudflared service install <TOKEN>
```

它会自动装成 systemd 服务并开机自启。检查：

```bash
sudo systemctl status cloudflared
```

## 第 4 步：让 serve.py 常驻

本轮已把单元按实际路径写好（`User=user`、`/home/user/pi-pipeline/serve.py`），
直接跑 `Pi/deploy.sh` 最省事；手工装则是：

```bash
sudo install -m 644 Pi/services/rss-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rss-serve
sudo systemctl status rss-serve
```

（换用户名/换目录时不要手改单元，改 `deploy.sh` 的 `PI_USER` —— 它在安装前统一
把 `User=user` 与 `/home/user` 替换掉，避免单元与脚本各指一处。）

先本机验证服务活着（`/feed.xml` 与 `/rss/feed.xml` 都该是 200，前缀剥没剥都认）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/rss/feed.xml   # 期望 200
```

## 第 5 步：公网验证

从外网（手机 4G 也行）：

```bash
curl -sI https://moreaix.com/rss/feed.xml
curl -s  https://moreaix.com/rss/feed.xml | head -n 20
```

应该看到 `HTTP/2 200`、`content-type: application/rss+xml`，以及 `<rss ...>`。

## 关键：关掉边缘缓存

`serve.py` 已发 `Cache-Control: no-cache`，但 Cloudflare 的默认缓存规则可能仍会
拦截静态文件。为避免「改了 feed 客户端还是旧内容」（此前 Pages 缓存 600 秒踩过），
在 **Caching → Cache Rules** 加一条：

- 匹配：`URI Path` `starts_with "/rss"`
- 动作：**Bypass cache**

## 第 6 步（可选）：把 Kindle 看板也放到公网

8082 原本只打算在局域网里用，走到了这一步要把它交给隧道：`moreaix.com` 的隧道是
Pi **主动出站**连到 Cloudflare 的，不需要任何入站规则 —— 所以它恰好绕开了 Pi 那层
让我们试不通的防火墙。

对外地址是 **`https://moreaix.com/kindle/rss-pw2.png`**（根域 + 路径，不再单独开
`board` 子域），网页版则是 **`https://moreaix.com/kindle/`**。

### 6.1 Pi 侧：给 8082 加一层 `--prefix=/kindle`

Cloudflare 的路径转发会把 Public Hostname 里那层 path **原样**带给后端，不会替你剥掉。
证据就在现有那条路由上：那边配的是 `/rss`，所以请求 `moreaix.com/rss/feed.xml` 是 200，
而 `moreaix.com/rss/rss/feed.xml` 是 404 —— 剥掉的话后者该命中才对。

所以前端挂了 `/kindle`，后端就得认这层前缀，这正是 `serve.py` 的 `--prefix` 存在的理由。
新版单元里已经写好，重装即可：

```bash
sudo install -m 644 Pi/services/kindle-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart kindle-serve
sudo systemctl is-active kindle-serve
```

手工改的话只有 ExecStart 末尾那个参数：`--prefix=` → `--prefix=/kindle`。

这层前缀是**可选**的（`_target()` 里剥不剥都认），所以局域网里的 Kindle 直接取
`/rss-pw2.png`（不带前缀）照样是 200 —— 公网、局域网两套口径并存，不用迁就任何一边。

### 6.2 让 `moreaix.com/kindle/*` 出图

**结论先说：不用再加任何 Public Hostname 了。** 看板图挂在已有的两台后端上
（8080 与 8081 都带了 `--alias /kindle=/home/user/kindle`），公网走的是根域那条
兜底路由，与 Tunnel 的路由顺序无关。2026-11-01 实测：

```
https://moreaix.com/kindle/rss-pw2.png  -> 200 image/png  73,004B
https://moreaix.com/kindle/             -> 200 text/html  80 条
```

下面是踩过的两个坑，留着当你以后再往根域挂 path 时的前车之鉴。

**坑一：ingress 是顺序匹配，兜底会吃掉一切。**
cloudflared 自上而下逐条求值、**首个命中即停**，不是「最长路径优先」。根域上本来就有一条
**不带 path** 的 `hostname: moreaix.com`（指到 8081 根域主页），它匹配这个 hostname 的
**所有**路径；新加的 `/kindle` 是**追加在列表末尾**的，于是永远轮不到。表现是「Pi 本地
200、公网 404」，而且那个 404 还是我们自己的 serve.py 吐的（带 CORS 头），很容易误判成
「服务端目录不对」。

**坑二：path 字段是非锚定正则，不是前缀匹配。**
`/rss` 这条规则会命中**任何包含 `/rss` 的路径**，位置不限。看板图叫 `rss-pw2.png`，路径
`/kindle/rss-pw2.png` 里正好有 `/rss`（来自文件名），于是被这条规则截到了 8080 ——
同一目录下 `/kindle/` 是 200、`/kindle/rss-pw2.png` 却是 404，就是这么来的。实测对照：

| 请求 | 落到 |
|---|---|
| `/kindle/rsschk12.png`（含 `/rss`） | 8080 |
| `/kindle/xrsschk2238.png`（只有 `-rss`） | 8081 |

所以「给 8080 也加同一个别名」是最省事的解法：落到哪台都能出图。想从根上治，就把那条
path 改成锚定的写法 `^/rss(/|$)`，再顺手把 `/kindle` 挪到兜底之前——但要动 Dashboard，
且收益仅限「路由表更干净」，功能上已经不需要了。

**看实际下发的配置**（比在浏览器里猜 404 强得多）：

```bash
sudo journalctl -u cloudflared | grep "Updated to new configuration" | tail -1
```

> 这一节早期版本写的是「别挂成 `moreaix.com/kindle` 路径」，理由是当时 8082 用的是
> `--prefix=`（无前缀）。现在前缀已经在后端补上，那条限制作废；反过来，谁把
> `--prefix` 改回空字符串，8082 那台就会重新变成一排 404（8080/8081 有别名，不受影响）。

### 6.3 边缘缓存也要 bypass

Cloudflare 默认会替静态扩展名（含 `.png`）在边缘存一份，优先级高过源站发的
`Cache-Control`，结果是「图早就重画了、浏览器拿到的还是旧的」。沿用第 5 步那条按路径
匹配的写法即可，不必改成按 hostname —— 本站的 cache rule 一直是路径口径，保持一致更好找：

- **Caching → Cache Rules** 新建规则
- 匹配：`URI Path` `starts_with "/kindle"`
- 动作：**Bypass cache**

### 6.4 Pi 侧先自证健康（隧道转发到 localhost，与外网无关）

```bash
sudo systemctl status kindle-serve.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/kindle/rss-pw2.png  # 期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8082/kindle/             # 期望 200
```

第二条是 `kindle_page.py` 写的网页看板（见下面 6.5）。它是 `kindle-screen.service`
的第二条 ExecStart，跟着 feed.xml 的重建一起跑，不用单独装任何东西。

### 6.5 两个地址，各管一摊

| 地址 | 给谁用 |
|---|---|
| `https://moreaix.com/kindle/` | 网页看板：能滚动、能点开原文，条目远远多于一屏 |
| `https://moreaix.com/kindle/rss-pw2.png` | 单张 PNG：给 `board.sh` + `eips` 画屏那条路 |

两者读同一份 `feed.xml`，互不依赖：删掉网页那一步，eips 那条路照常工作。

文件名 `rss-pw2.png` 由 `kindle_screen.py` 的 `BOARD_PNG` 决定（= 第一个尺寸的副本）；
每个尺寸另有一份独立的 `kindle-<W>x<H>.png`。改名要同步三处：那个常量、`board.sh`
里的 URL、`kindle_page.py` 页脚那个「看板图」链接。

### 6.6 Kindle 老浏览器可能不认 Cloudflare 的证书

KPW2 的实验性浏览器自带的根证书比较旧，遇到现代证书链有可能直接报证书错误
（而不是「打不开」）。三条解决办法，按顺序试：

1. 先试 `http://moreaix.com/kindle/` —— Cloudflare 默认会重定向到 https，
   先不用管，看浏览器能不能过；
2. 报证书错的：SSL/TLS → Edge Certificates → 关掉 **Always Use HTTPS**
   （可以只对这一个子域配 Configuration Rule，别全局关）；
3. 还不行就让 `board.sh` 走非加密地址：
   `BOARD_URL="http://moreaix.com/kindle/rss-pw2.png" sh board.sh`

> 这一节我没能在真机上验过（这台机器够不到 Kindle），是预案不是结论。
> 试出来哪一条可用，请回来把这一段改成实际结果。

### 6.7 board.sh 要不要改走公网

默认仍是局域网直连（最快，也不依赖隧道和外网）：

```bash
sh board.sh                                                    # 局域网
BOARD_URL="https://moreaix.com/kindle/rss-pw2.png" sh board.sh  # 走公网
```

只有在「ping 通但所有端口一律超时」时才需要第二种 —— 代价是每 5 分钟的取图都要
绕 Cloudflare 一圈。注意这是**进程级**环境变量：KUAL 菜单里的「启动常驻刷新」
用的是默认地址，要长期走公网得改 `start.sh`（或在该脚本里 export）。

## 常见故障

| 现象 | 原因 / 处理 |
|---|---|
| 530 / 1033 | tunnel 没连上：`systemctl status cloudflared`，看 token 是否正确、Pi 出网是否正常 |
| 502 | cloudflared 到了 Pi，但 `localhost:8080` 没人听：`systemctl status rss-serve` |
| 404 | serve.py 的 `--root` 指错了目录，或 `docs/` 里还没生成 feed.xml（等 rss-build.timer 跑一轮） |
| `/kindle/*` 在 Pi 本地 200、公网一直 404 | 多半是 Tunnel 的两个坑之一：① 配的 `/kindle` 规则排在根域兜底之后（顺序匹配、首个命中即停）；② 路径里含 `/rss` 被那条非锚定正则截到 8080。现在 8080/8081 都带了 `--alias /kindle=…`，两台都能出图，正常情况下不会再遇到。定位落到哪台：请求一个不存在的文件名，再看 `journalctl -u kindle-serve`、`-u moreaix-site`、`-u rss-serve` 谁的访问日志里有它 |
| 配好了还是旧 404 | 边缘把 404 也缓存了（`.png` 默认 4 小时）。配完 Bypass cache 仍不生效就手动清一次：Caching → Configuration → Custom Purge，填那个 URL |
| 拿到的总是旧内容 | 边缘缓存：见上面「关掉边缘缓存」 |
| 客户端跨域被拦 | serve.py 对所有响应（含 404）都带 `Access-Control-Allow-Origin: *`，若仍不行检查是否有别的代理层 |
| 夜里 feed 不更新 | 夜间节拍是每小时整点（`rss-build.timer`），不是故障；先 `journalctl -u rss-build` 看有没有跑 |

## 注意

- `rotate.json` 是**显示游标**，由 `rss-rotate.timer` 每 5 分钟推进并写在本机
  `docs/rotate.json`；`rss-build.service` 那份被指到 `/tmp`（它不参与轮播）。
  游标只在 Pi 上有一份真源，别去同步 GitHub 那份 —— 两边窗口号会互相打架。
- 端口 `8080` 可改，改了要同步改第 2 步的 `Service · URL` 和 `rss-serve.service`。
- 时区：`rss-build.timer` 的日夜两条 `OnCalendar` 按系统时区解释，
  Pi 必须是 `Asia/Shanghai`（见 `README.md`「时区」一节）。
