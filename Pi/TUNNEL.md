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
                                       serve.py (:8081) 一份实例服务三处：
                                        /        → site/        （根域主页）
                                        /rss/*   → docs/        （feed 三个文件）
                                        /kindle/*→ kindle/      （看板图 + 网页）
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
| Path | **留空** |
| Service · Type | `HTTP` |
| Service · URL | `localhost:8081` |

保存后，Cloudflare 会自动建一条打通的记录：
`moreaix.com → <tunnel-id>.cfargotunnel.com`

- 客户端地址即：`https://moreaix.com/rss/feed.xml` 和 `/rss/feed-wrap.xml`
- **只配这一条就够了**：`/rss` 与 `/kindle` 都由 serve.py 的 `--alias` 在后端分流，
  不需要额外的 path 规则。这一点很重要——path 规则会带来两个实测过的坑（顺序匹配、
  非锚定正则），没有它就没有这些麻烦，细节见 6.2。
- 顺带把 `www.moreaix.com` 也指到 `localhost:8081` 即可（同一条规则换个 hostname）。

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
sudo install -m 644 Pi/services/moreaix-site.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now moreaix-site
sudo systemctl status moreaix-site
```

> 2026-11-01 之前这里是两台：`rss-serve`（8080，只出 feed）+ `moreaix-site`（8081，只出主页）。
> 它们是同一个 `serve.py`，合并后 8081 用 `--alias /rss=… --alias /kindle=…` 一台全包，
> 8080 已下线。两个坑的来龙去脉见 6.2。

（换用户名/换目录时不要手改单元，改 `deploy.sh` 的 `PI_USER` —— 它在安装前统一
把 `User=user` 与 `/home/user` 替换掉，避免单元与脚本各指一处。）

先本机验证服务活着：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/rss/feed.xml   # 期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/               # 期望 200
```

## 第 5 步：公网验证

从外网（手机 4G 也行）：

```bash
curl -sI https://moreaix.com/rss/feed.xml
curl -s  https://moreaix.com/rss/feed.xml | head -n 20
```

应该看到 `HTTP/2 200`、`content-type: application/rss+xml`，以及 `<rss ...>`。

## 关键：给 `/rss` 开「短边缘缓存」，但 `/kindle` 保持 Bypass

`serve.py` 现在按资源发**差异化** `Cache-Control`（见源码 `_cache_policy_for`）：

- `feed.xml` / `feed-wrap.xml` / `rotate.json` / `ai-accum.json` →
  `public, max-age=120, s-maxage=120, stale-while-revalidate=600`
- `ai-digest.html` / `sitemap.xml` → 更长 TTL；`index.html` / `reader.html` / `screen.html` → `max-age=60`
- **看板 PNG → `no-cache`**（保留 304，避免 Kindle 每次被迫下整张图）

但 Cloudflare **默认不缓存 `.xml` / `.html`**（不在默认可缓存扩展名里），所以光靠源站
头没用，必须在边缘显式开一条规则，让上面的 `s-maxage` 真正生效。本站扛高并发靠的就是
「访客几乎全被边缘吃掉、源站（Pi）只接缓存未命中」这一招——feed 本就每 5 分钟才重建，
边缘缓存 2 分钟最坏也只晚 2 分钟，新闻阅读器完全无感，但源站请求量能掉一两个数量级。

在 **Caching → Cache Rules** 加/改两条：

1. `/rss` 路径（feed、页面、JSON 全部缓存）：
   - 匹配：`URI Path` `starts_with "/rss"`
   - 动作：**Cache everything** → 同时把 **Edge Cache TTL** 设成 `120` 秒（与源站
     `s-maxage` 对齐；不设也行，源站头会兜底）。不要选 Bypass。
2. `/kindle` 路径（看板图，必须保 304）：
   - 匹配：`URI Path` `starts_with "/kindle"`
   - 动作：**Bypass cache**（保持原样，别动）。

> 为什么 `/rss` 用 "Cache everything" 而不是 "Bypass"：早期为了「客户端永远拿最新」
> 一刀切 Bypass，结果每个访客、reader.html 每 5 分钟轮询都穿透到 Pi，feed.xml 有 286KB，
> 高并发下就是放大器。现在短 TTL 既新鲜又抗压。注意 `s-maxage` 只对共享缓存（CDN）生效，
> 浏览器本地仍按 `max-age=120` 缓存，体验一致。

## 第 6 步（可选）：把 Kindle 看板也放到公网

看板图原本只打算在局域网里用（8082 那台，现已连同单元文件删除），走到了这一步就把它并进公网那条：
`moreaix.com` 的隧道是 Pi **主动出站**连到 Cloudflare 的，不需要任何入站规则 ——
所以它恰好绕开了 Pi 那层让我们试不通的防火墙。

对外地址是 **`https://moreaix.com/kindle/rss-pw2.png`**（根域 + 路径，不再单独开
`board` 子域），网页版则是 **`https://moreaix.com/kindle/`**。

### 6.1 怎么做：一条根域规则 + 服务端两条别名

看板图和 feed 都不需要单独的 Public Hostname，因为它们**和主页是同一个 `serve.py`**：

```
serve.py --root /home/user/site \
         --alias /rss=/home/user/rss-hub/docs \
         --alias /kindle=/home/user/kindle --port 8081
```

Tunnel 只要把整个 `moreaix.com` 指向 `localhost:8081`，三条路径各归各的目录。
2026-11-01 实测（这条链路现在就在跑）：

```
https://moreaix.com/kindle/rss-pw2.png  -> 200 image/png
https://moreaix.com/kindle/             -> 200 text/html   80 条
https://moreaix.com/rss/feed.xml        -> 200 application/rss+xml
https://moreaix.com/                    -> 200 text/html
```

`--alias` 的越界检查比的是别名生效后的真实目录，`/kindle/../` 这类穿越照样 404。

### 6.2 为什么不要给根域加 path 规则（两个实测过的坑）

早期版本确实给 `/rss` 和 `/kindle` 各配过一条 path 规则，踩了两个坑，最后靠「不用 path 规则」
一次性绕开。留着这段是因为**你以后很可能又想加一条**，先看清楚代价：

**坑一：ingress 是顺序匹配，兜底会吃掉一切。**
cloudflared 自上而下逐条求值、**首个命中即停**，不是「最长路径优先」。根域上那条
**不带 path** 的规则会匹配该 hostname 的**所有**路径，所以新加的 `/kindle` 只要追加在它
后面，就永远轮不到。表现是「Pi 本地 200、公网 404」，而且那个 404 还是我们自己的 serve.py
吐的（带 CORS 头），很容易误判成「服务端目录不对」。

**坑二：path 字段是非锚定正则，不是前缀匹配。**
`/rss` 这条规则会命中**任何包含 `/rss` 的路径**，位置不限。看板图叫 `rss-pw2.png`，路径
`/kindle/rss-pw2.png` 里正好有 `/rss`（来自文件名），于是被这条规则截走了 —— 同一目录下
`/kindle/` 是 200、`/kindle/rss-pw2.png` 却是 404。实测对照：

| 请求 | 落到哪台（当时的 8080 / 8081） |
|---|---|
| `/kindle/rsschk12.png`（含 `/rss`） | 8080 |
| `/kindle/xrsschk2238.png`（只有 `-rss`） | 8081 |

**结论**：能不加 path 规则就别加。真要加，两个前提缺一不可 —— 它必须排在**不带 path 的
那条之前**，并且 path 写成锚定的正则（如 `^/rss(/|$)`），否则 `rss-pw2.png` 这种文件名
随时会再次中招。

**看实际下发的配置**（比在浏览器里猜 404 强得多）：

```bash
sudo journalctl -u cloudflared | grep "Updated to new configuration" | tail -1
```

### 6.3 看板图（/kindle）必须保持 Bypass

看板图 `rss-pw2.png` 是 `board.sh` 每 5 分钟拉一次、靠 `If-Modified-Since` 拿 **304**
省流量的。一旦边缘把 PNG 也缓存了，设备就再也收不到 304、每次都被迫下整张图——对最弱势
的客户端（墨水屏、可能走计费/慢网）是回退，不能要。所以 `/kindle` 这条规则**永远保持
Bypass cache**（见第 5 步第 2 条），不要跟着 `/rss` 一起改成 Cache everything。

Cloudflare 默认会替静态扩展名（含 `.png`）在边缘存一份，优先级高过源站发的
`Cache-Control`——这正是为什么要显式 Bypass：否则即便源站发 `no-cache`，边缘也可能
把图缓存成「旧的」。沿用第 5 步那条按路径匹配的写法即可，不必改成按 hostname——
本站的 cache rule 一直是路径口径，保持一致更好找。

### 6.4 Pi 侧先自证健康（隧道转发到 localhost，与外网无关）

```bash
sudo systemctl status moreaix-site.service
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/kindle/rss-pw2.png  # 期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/kindle/             # 期望 200
```

第二条是 `kindle_page.py` 写的网页看板（见下面 6.5）。它是 `kindle-screen.service`
的第二条 ExecStart，跟着 feed.xml 的重建一起跑 —— 那个单元与那两个渲染器都在
`../kindle/` 下，归 `../kindle/deploy.sh` 装，本目录不负责。

### 6.5 两个地址，各管一摊

| 地址 | 给谁用 |
|---|---|
| `https://moreaix.com/kindle/` | 网页看板：能滚动、能点开原文，条目远远多于一屏 |
| `https://moreaix.com/kindle/rss-pw2.png` | 单张 PNG：给 `board.sh` + `eips` 画屏那条路 |

两者读同一份 `feed.xml`，互不依赖：删掉网页那一步，eips 那条路照常工作。
内容产出归 [../kindle/README.md](../kindle/README.md) 管，本文件只讲**怎么走到网外**。

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
| 502 | cloudflared 到了 Pi，但 `localhost:8081` 没人听：`systemctl status moreaix-site` |
| 404 | serve.py 的 `--root` 指错了目录，或 `docs/` 里还没生成 feed.xml（等 rss-build.timer 跑一轮） |
| `/kindle/*` 或 `/rss/*` 在 Pi 本地 200、公网 404 | 现在只有一条根域规则，正常不会再出现。若你**加过 path 规则**，看 6.2 的两个坑。定位：请求一个不存在的文件名，再看 `journalctl -u moreaix-site`（现在只有这一台静态服务） |
| 配好了还是旧 404 | 边缘把 404 也缓存了（`.png` 默认 4 小时）。配完 Bypass cache 仍不生效就手动清一次：Caching → Configuration → Custom Purge，填那个 URL |
| 拿到的总是旧内容 | 边缘缓存：feed/页面给了 120s/60s 边缘 TTL，最坏晚 2 分钟属正常；若怀疑缓存过头，按第 5 步把 `/rss` 的 Edge Cache TTL 调小或临时改 Bypass 验证 |
| 客户端跨域被拦 | serve.py 对所有响应（含 404）都带 `Access-Control-Allow-Origin: *`，若仍不行检查是否有别的代理层 |
| 夜里 feed 不更新 | 夜间节拍是每小时整点（`rss-build.timer`），不是故障；先 `journalctl -u rss-build` 看有没有跑 |

## 注意

- `rotate.json` 是**显示游标**，由 `rss-rotate.timer` 每 5 分钟推进并写在本机
  `docs/rotate.json`；`rss-build.service` 那份被指到 `/tmp`（它不参与轮播）。
  游标只在 Pi 上有一份真源，别去同步 GitHub 那份 —— 两边窗口号会互相打架。
- 端口 `8081` 可改，改了要同步改第 2 步的 `Service · URL` 和 `moreaix-site.service`。
- 时区：`rss-build.timer` 的日夜两条 `OnCalendar` 按系统时区解释，
  Pi 必须是 `Asia/Shanghai`（见 `README.md`「时区」一节）。
