# 树莓派 + Cloudflare Tunnel 接入指引

目标：让树莓派上产出的 `feed.xml` / `feed-wrap.xml` / `rotate.json`
通过 `https://feed.moreaix.com/...` 对外稳定可拉取，**不经过 GitHub**。

## 架构

```
 各 RSS 源 ──抓取──> 树莓派 (build_feed.py) ──写文件──> docs/feed.xml
                                                        docs/feed-wrap.xml
                                                        docs/rotate.json
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
- Pi 是唯一写者 + 唯一服务器，不存在 GitHub 那套双写冲突。
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

把 `serve.py` 和 `rss-serve.service` 放到 Pi 上（假设 `/home/pi/pi-pipeline/`），
并按实际路径改 `rss-serve.service` 里的 `User` / `WorkingDirectory` / `ExecStart`：

```bash
sudo cp /home/pi/pi-pipeline/rss-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rss-serve
sudo systemctl status rss-serve
```

先本机验证服务活着：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/feed.xml   # 期望 200
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
| 404 | serve.py 的 `--root` 指错了目录，或 `docs/` 里还没生成 feed.xml |
| 拿到的总是旧内容 | 边缘缓存：见上面「关掉边缘缓存」 |
| 客户端跨域被拦 | serve.py 已带 `Access-Control-Allow-Origin: *`，若仍不行检查是否有别的代理层 |

## 注意

- `rotate.json` 是轮播游标，Pi 上应由 build_feed.py 在本地写；
  若沿用云端那份，会与 GitHub 那套互相覆盖。
- 端口 `8080` 可改，改了要同步改第 2 步的 `Service · URL` 和 systemd 单元。
