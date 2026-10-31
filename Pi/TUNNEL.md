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
- `serve.py` 已做前缀兼容：即使 Cloudflare 把 `/rss` 剥掉再转发，也能找到文件。

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

## 常见故障

| 现象 | 原因 / 处理 |
|---|---|
| 530 / 1033 | tunnel 没连上：`systemctl status cloudflared`，看 token 是否正确、Pi 出网是否正常 |
| 502 | cloudflared 到了 Pi，但 `localhost:8080` 没人听：`systemctl status rss-serve` |
| 404 | serve.py 的 `--root` 指错了目录，或 `docs/` 里还没生成 feed.xml（等 rss-build.timer 跑一轮） |
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
