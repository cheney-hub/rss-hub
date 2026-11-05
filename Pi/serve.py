#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/ 下的 feed.xml / feed-wrap.xml / rotate.json 供 Cloudflare Tunnel 拉取。

零依赖，只用标准库。树莓派上由 systemd 拉起：
    python3 serve.py --root /home/user/rss-hub/docs --port 8080 --prefix /rss

对外地址形如 https://moreaix.com/rss/feed.xml
（Tunnel 的 path 前缀无论是否被剥掉，serve.py 都能正确找到文件）

同一份代码还兼了两个静态站：8081 根域主页（--root /home/user/site）与
现在 Pi 上**只跑一份**：moreaix-site 用 `--root /home/user/site` 加两个别名
（`/rss` → feed 目录、`/kindle` → 看板图），一台出三样东西。
曾经的 8080（只出 feed）与 8082（只出看板图、绑 0.0.0.0）都已下线：
同一份 serve.py 换参数跑三遍没有任何收益，而 Tunnel 只为它多担了两条 path 规则
（顺序匹配 + 非锚定正则两个坑，见 TUNNEL.md 6.2）。

设计要点：
- 只读、只服务静态文件，防目录穿越（root 用 realpath，符号链接也穿不出去）。
- RSS/JSON/HTML 给正确 Content-Type。
- 强制 Cache-Control: no-cache —— 此前 Pages CDN 缓存 600 秒踩过坑，
  这里让 Cloudflare 边缘不缓存，保证客户端拿到的总是最新一版。
- 带 CORS *，方便网页阅读器跨域拉取（docs/screen.html 就跨域读 feed-wrap.xml）。
  这条头对**所有**响应都发（含 404），否则前端看到的是跨域错误而不是 404。
- 带 Last-Modified + If-Modified-Since → 304：Kindle 每 5 分钟来拉一次看板 PNG，
  图没变就只回一个 304 头，别每次把几十 KB 重传一遍（省流量也省电）。
- 可选：给 HTML 响应注入 Cloudflare Web Analytics 的 beacon。token 为空就完全不注入，
  只有 moreaix-site 这一台配了 token；docs/index.html 与 GitHub Pages 是同一份源文件，
  但改写只发生在响应里，磁盘文件和 Pages 上的页面都不受影响。
"""
import argparse
import collections
import email.utils
import os
import stat
import string
import sys
import threading
import time
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MIME = {
    ".xml": "application/rss+xml; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}

# ---- 对外暴露面 ------------------------------------------------------------
# 只有这些扩展名会被发出去。站点根目录里还躺着 analytics.env（统计 token），
# 构建任务写的 *.tmp/*.new 半成品也在被服务的目录里；「存在就发」等于把配置挂公网。
ALLOWED_EXT = frozenset(MIME)
MAX_PATH = 2048
# Tunnel 转发时 Host 就是对外 hostname；挡掉其它 Host 可防 DNS rebinding 与
# 用伪造 Host 探测内网。localhost/回环留给本机健康检查与测试。
ALLOWED_HOSTS = frozenset(("moreaix.com", "www.moreaix.com",
                           "localhost", "127.0.0.1", "::1"))
SOCKET_TIMEOUT = 8                # Slowloris：慢慢发头的连接 8 秒就断
RATE_WINDOW = 60.0
RATE_PER_IP = 600                 # 每访客每分钟；正常订阅/看板远不到这个量级
RATE_GLOBAL = 2400                # 全局兜底：伪造不同 CF-Connecting-IP 也挡得住
CACHE_MAX_ENTRIES = 32
CACHE_MAX_BYTES = 2 * 1024 * 1024

# beacon 脚本地址写成 os.environ.get 的默认值：它本来就该可配置，也顺带符合门禁
# 「源码里不许出现带真实域名的裸地址」。Cloudflare 控制台给的手动 snippet 就是这个地址。
BEACON_JS = os.environ.get("CF_BEACON_JS", "https://static.cloudflareinsights.com/beacon.min.js")
# token 由 Pi 上 /home/user/site/analytics.env 提供（不进仓库）。空 = 不注入，
# 于是「先发版、后给 token」两段解耦：没配 token 时服务行为与改动前逐字节一致。
CF_BEACON_TOKEN = os.environ.get("CF_BEACON_TOKEN", "").strip()
# 显式 ASCII 白名单：str.isalnum() 会把全角字母、阿拉伯数字等 Unicode 字符也算
# 「字母数字」，而 token 要被拼进 HTML 属性，这里不给自己留模糊地带。
_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + "-_")


def beacon_tag(token):
    """生成 beacon 的 <script> 标签；token 没配或不像 token 就返回空字节。

    只放行字母数字与 -_：token 要被拼进 HTML 属性，万一配置里混进引号或尖括号，
    宁可不统计，也不能让一个配置写错把整页 HTML 弄坏。
    """
    token = (token or "").strip()
    js = BEACON_JS
    # BEACON_JS 也要当外部输入看：它同样被拼进 HTML 属性。配置写错宁可不统计。
    if not js.startswith("https://") or any(
            ch in js for ch in ('"', "'", "<", ">", "\\", " ", "\r", "\n", "\t")):
        return b""
    if not token or not all(ch in _TOKEN_CHARS for ch in token):
        return b""
    return ('<script type="module" src="%s" '
            'data-cf-beacon=\'{"token": "%s"}\'></script>\n'
            % (js, token)).encode("utf-8")


def inject_beacon(body, ext, token):
    """把 beacon 插在 </body> 前；任何不满足条件都把 body 原样退回。

    只改 HTML、只插一次、找不到插入点就不动。静态服务器宁可少统计一个页面，
    也不能把 feed.xml 或看板 PNG 这些非 HTML 产物写坏。注入在算 Content-Length
    之前完成，所以 GET 与 HEAD 报的长度都跟实际发出去的字节一致。
    """
    tag = beacon_tag(token)
    if not tag or ext.lower() not in (".html", ".htm"):
        return body
    if b"data-cf-beacon" in body or b"cloudflareinsights" in body:
        return body                     # 已经有人插过了，别插第二次
    low = body.lower()
    pos = low.rfind(b"</body>")
    if pos < 0:
        pos = low.rfind(b"</html>")
    if pos < 0:
        return body
    return body[:pos] + tag + body[pos:]


class _Runtime:
    """一个服务实例的内存态：响应缓存 + 限流 + 被拒请求汇总，全都不落盘。

    挂在 make_handler 造出的子类上而不是模块全局：同一进程起多个实例时互不串，
    测试也能各起一份干净的状态。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = collections.OrderedDict()
        self.global_hits = collections.deque()
        self.ip_hits = {}
        self.sweeps = 0
        self.err = {"codes": {}, "since": 0.0}

    def allow(self, ip):
        """滑动窗口限流；超限返回 False，由调用方回 429。

        两条与内存有关的硬约束：
          * 全局额度先判、判满就直接返回 —— 被拒的请求**不**为伪造 IP 建条目。
            原来的写法是 setdefault 在前，攻击者用一万个不同的 CF-Connecting-IP
            就能把字典撑到一万项（哪怕这些请求全被 429 拒绝）。
          * 每 256 次放行扫一遍过期条目。原来只删「空 deque」，而 deque 只在
            该 IP 再次请求时才清空，一次性灌进来的条目永远不会变空 —— 清理形同虚设。
        """
        now = time.monotonic()
        cutoff = now - RATE_WINDOW
        with self.lock:
            g = self.global_hits
            while g and g[0] <= cutoff:
                g.popleft()
            if len(g) >= RATE_GLOBAL:
                return False
            q = self.ip_hits.get(ip)
            if q is None:
                q = collections.deque()
                self.ip_hits[ip] = q
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= RATE_PER_IP:
                return False
            g.append(now)
            q.append(now)
            self.sweeps += 1
            if self.sweeps >= 256:
                self.sweeps = 0
                for k, dq in list(self.ip_hits.items()):
                    while dq and dq[0] <= cutoff:
                        dq.popleft()
                    if not dq:
                        del self.ip_hits[k]
            return True

    def body(self, path, key, ext, token):
        """命中缓存就直接给内存里的响应字节，一次 open 都不做。

        key 是 stat 五元组：文件被 os.replace 换 inode、或被原地改写（ctime 必变）
        都会让 key 失配并重读，所以缓存不会把新内容压成旧内容。缓存的是**注入后**
        的字节，连 beacon 拼接也一起省了。
        """
        ck = (key, ext, token)
        with self.lock:
            hit = self.cache.get(path)
            if hit and hit[0] == ck:
                self.cache.move_to_end(path)
                return hit[1]
        with open(path, "rb") as fh:
            raw = fh.read()
        out = inject_beacon(raw, ext, token)
        with self.lock:
            self.cache[path] = (ck, out)
            self.cache.move_to_end(path)
            total = sum(len(v[1]) for v in self.cache.values())
            while len(self.cache) > CACHE_MAX_ENTRIES or total > CACHE_MAX_BYTES:
                _, dropped = self.cache.popitem(last=False)
                total -= len(dropped[1])
        return out

    def log_summary(self, code):
        """所有状态码按分钟汇总成一条；窗口内返回 None（一行都不写）。

        只聚合 4xx 是不够的：攻击者用**正常 GET** 一样能指挥 journald 写 SD 卡。
        统一按分钟汇总后每分钟最多一行，代价是丢掉逐条访问明细（IP 与路径）。
        """
        now = time.monotonic()
        with self.lock:
            e = self.err
            e["codes"][code] = e["codes"].get(code, 0) + 1
            if now - e["since"] < RATE_WINDOW:
                return None
            codes, e["codes"] = e["codes"], {}
            e["since"] = now
        return "最近 %.0fs 响应 %d 次：%s" % (
            RATE_WINDOW, sum(codes.values()),
            ", ".join("%s×%d" % kv for kv in sorted(codes.items())))


class _StaticHandler(BaseHTTPRequestHandler):
    """只做 GET/HEAD 的静态文件处理器；没有列目录、没有写操作。

    root 与 prefix 由 make_handler() 通过子类属性注入（一个服务实例一份），
    不放在模块全局里：一个进程同时服务多个目录时不会互相串。
    """

    root = ""
    prefix = ""
    aliases = ()
    beacon_token = ""
    runtime = None
    _code = 0
    server_version = "rss-serve/1.0"
    # 默认会把 "Python/3.13.x" 拼进 Server 头。8080 是公网（走 Tunnel），
    # 没必要告诉扫描器这台机器跑的是哪个小版本。
    sys_version = ""

    def setup(self):
        """给每条连接设读超时：慢速发头（Slowloris）不能一直占着一个线程。"""
        super().setup()
        self.connection.settimeout(SOCKET_TIMEOUT)

    def handle(self):
        """超时的连接安静关掉，不把栈回溯刷进 journal。"""
        try:
            super().handle()
        except (TimeoutError, OSError):
            pass

    def send_response(self, code, message=None):
        """记下状态码：log_message 据此决定逐条记还是按分钟汇总。"""
        self._code = code
        super().send_response(code, message)

    def _client_ip(self):
        """取访客 IP：流量全从本机 Tunnel 来，client_address 恒是 127.0.0.1。

        Cloudflare 会覆盖（不是透传）CF-Connecting-IP 为真实访客地址；能被本机
        直连伪造的只有 8081 这个回环口，而那已经需要本机权限了。
        """
        ip = (self.headers.get("CF-Connecting-IP") or "").strip().split(",")[0].strip()
        return ip[:64] or self.client_address[0]

    @staticmethod
    def _host_ok(value):
        """Host 白名单；HTTP/1.0 不带 Host 时放行（老式客户端，也构不成 rebinding）。"""
        host = (value or "").strip().lower()
        if not host:
            return True
        if host.startswith("["):                # [::1]:8081
            host = host[1:].split("]", 1)[0]
        else:
            host = host.split(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _precheck(self):
        """请求进来先过四关：Host、URL 长度/控制字符、限流。返回 False 已自行回响应。"""
        hosts = self.headers.get_all("Host") or []
        # 重复 Host 要全查：只取第一个值时，伪造头能绕过白名单（实测返回 200）。
        if hosts and not all(self._host_ok(h) for h in hosts):
            self.send_error(400, "Bad Host")
            return False
        # 不带 Host 的 HTTP/1.0 由基类兼容处理；HTTP/1.1 缺 Host 基类自己会回 400，
        # 这里不重复判一次（重复检查写不出能把它弄红的反例）。
        raw = self.path
        if len(raw) > MAX_PATH:
            self.send_error(414, "URI Too Long")
            return False
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in raw):
            # 含 NUL 的路径会让 realpath 抛 ValueError，未兜住就是整条连接 500
            self.send_error(400, "Bad Request")
            return False
        if not self.runtime.allow(self._client_ip()):
            self.send_response(429, "Too Many Requests")
            self.send_header("Retry-After", str(int(RATE_WINDOW)))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _target(self):
        """把请求路径映射到磁盘路径；越界返回 None。

        刻意的取舍：**不做 URL 解码**。解码后 "%2e%2e%2f" 才会变成 "../"，
        不解码它就只是个普通文件名的前缀，天然穿不出去（实测 %2e%2e%2f 一律 404）。
        代价是带百分号编码的文件名取不到 —— 本站所有产物都是 ASCII 名，够用。
        """
        rel = self.path.split("?", 1)[0].lstrip("/")
        # 兼容 Tunnel 的 path 前缀：/rss/feed.xml 与 /feed.xml 都指到同一文件
        if self.prefix and (rel == self.prefix or rel.startswith(self.prefix + "/")):
            rel = rel[len(self.prefix):].lstrip("/")
        # 别名：同一台实例顺带服务另一个目录（根域那台用它出 /kindle/*，
        # 绕开 Tunnel 里「兜底规则排在前面、path 规则永远轮不到」的坑）。
        root = self.root
        for apath, aroot in self.aliases:
            if rel == apath or rel.startswith(apath + "/"):
                rel = rel[len(apath):].lstrip("/")
                root = aroot
                break
        rel = rel or "index.html"
        # realpath 而不是 abspath：root 里放一个指向外部的符号链接，abspath 判定
        # 会放行、realpath 会把链接解开后再比，穿不出去。
        # 注意这里比的是**实际生效的** root（可能是别名目录），不是 self.root。
        target = os.path.realpath(os.path.join(root, rel))
        if target != root and not target.startswith(root + os.sep):
            return None
        # 白名单 + 隐藏文件：analytics.env、*.tmp/*.new 半成品、.git 一律取不到。
        # 放在 realpath 之后，越界判定仍是第一道，符号链接测试不受影响。
        if os.path.splitext(target)[1].lower() not in ALLOWED_EXT:
            return None
        if any(seg.startswith(".") for seg in rel.split("/")):
            return None
        return target

    def _ims_since(self):
        """If-Modified-Since 解析成 epoch 秒；没带或解析不出来返回 None。

        客户端回送的可能是任意垃圾（老版本 wget、中间代理改写过的头），
        解析失败一律当「没带条件」：多传一次文件，总比一个坏头把所有人 304 掉好。
        """
        raw = self.headers.get("If-Modified-Since")
        if not raw:
            return None
        try:
            dt = email.utils.parsedate_to_datetime(raw)
        except Exception:
            # 不只 TypeError/ValueError：parsedate_to_datetime 对畸形输入还能抛
            # OverflowError 之类，而这里是请求处理链上唯一没有兜底的一处 ——
            # 漏一种异常就是整个请求 500（看板拉图失败），比多发一次文件贵得多。
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:                   # 极少数格式不带时区，按 HTTP 惯例当 UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    def _send(self, head_only):
        """过闸 → 一次 stat → 能 304 就 304，否则从内存缓存取全文（head_only 只回头）。

        以前每个请求 isfile + stat + open/read 三次系统调用，feed.xml 近 400KB 每次
        都从盘上读一遍；现在只 stat 一次，命中缓存连 open 都没有。304 直接由 stat
        回答，连缓存都不用查。
        """
        if not self._precheck():
            return
        target = self._target()
        if target is None:
            self.send_error(404, "Not Found")
            return
        try:
            st = os.stat(target)
        except OSError:
            self.send_error(404, "Not Found")   # 与「不存在」同码，不泄露文件状态
            return
        if not stat.S_ISREG(st.st_mode):        # 目录、设备、FIFO 一律不给
            self.send_error(404, "Not Found")
            return
        ext = os.path.splitext(target)[1].lower()
        last_mod = self.date_time_string(st.st_mtime)
        since = self._ims_since()
        # 秒级比较：Last-Modified 只有秒精度，用 int() 向下取整才不会同一秒里反复 200
        if since is not None and int(st.st_mtime) <= since:
            self.send_response(304)
            self.send_header("Last-Modified", last_mod)
            self.end_headers()
            return
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        try:
            body = self.runtime.body(target, key, ext, self.beacon_token)
        except OSError:
            self.send_error(500, "Read Error")
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME[ext])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Last-Modified", last_mod)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def end_headers(self):
        """统一出口：CORS 与缓存头在这里发，200/304/404/500 一个不漏。

        以前这两条只在 200 里发，跨域读 404 时会变成「CORS 报错」而不是 404，
        排查时多绕一圈。
        """
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        # 低风险安全头：不改页面内容，也不挡现有的内联脚本与跨域读 feed。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        # 只放不破坏页面内联 style/script 的指令。严格 CSP 要先外置脚本，
        # 贸然加 default-src 会把首页 canvas 动画与 CF 统计一起弄坏。
        self.send_header("Content-Security-Policy",
                         "frame-ancestors 'self'; object-src 'none'; base-uri 'self'")
        super().end_headers()

    def do_GET(self):
        """GET：正常取文件。"""
        self._send(False)

    def do_HEAD(self):
        """HEAD：只回头和 Content-Length，客户端据此判断要不要真下载。"""
        self._send(True)

    def _reject_method(self):
        """非 GET/HEAD 一律 405，且必须先过同一道预检。

        基类对未定义的 do_* 直接回 501，那条路径不经 _precheck()：Host 白名单、
        URL 校验、限流全被绕过（实测「伪造 Host 的 POST」照样 501）。让任意
        方法都落到这道闸，顺带不再把请求方法名原样回显到状态行与响应体。
        """
        if not self._precheck():
            return
        self.send_response(405, "Method Not Allowed")
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def __getattr__(self, name):
        """基类用 hasattr(self, "do_" + command) 找方法：兜住任意自定义方法。

        只显式列 POST/PUT/... 会漏掉 BREW 之类，那些又掉回不受限流的 501。
        """
        if name.startswith("do_"):
            return self._reject_method
        raise AttributeError(name)

    def log_error(self, fmt, *args):
        """send_error 先调本方法、再调 send_response，此时 _code 还是旧值。

        若在这里逐条写，4xx 就绕过了下面的汇总（正是开着日志实测抓到的）。
        交给 send_response 触发的那次 log_request 统一记一次即可。
        """
        return

    def log_message(self, fmt, *args):
        """所有状态码按分钟汇总一条：正常 GET 也能指挥 journal 写盘。"""
        line = self.runtime.log_summary(self._code)
        if line:
            sys.stderr.write("%s\n" % line)


class _Server(ThreadingHTTPServer):
    """线程随主进程走、半开连接的 backlog 有上限（超时由 Handler.setup 兜）。"""
    daemon_threads = True
    request_queue_size = 64


def make_handler(root, prefix="", aliases=None, beacon_token=None):
    """造一个绑定了 root/prefix 的 Handler 类。

    每个服务实例各造一个子类，而不是把目录写死进 _StaticHandler：同一个进程将来
    同时服务多个目录时（用 aliases 把 /rss、/kindle 挂到同一台），两边互不影响。
    prefix 是「Tunnel 上那层路径前缀」，传空串表示不要前缀（8081 用）。
    prefix 与 alias 的区别：prefix 只在一台实例上剥一层（历史用法），alias 可以挂多组
    「路径 → 目录」，更适合一台实例服务多个目录。

    aliases 是 [(路径前缀, 目录)] 的列表，用于「一台实例顺带服务另一个目录」。
    按路径长度降序匹配，所以 /kindle 与 /kindle/live 这种嵌套关系不会互相盖住。
    beacon_token 传 None 时取环境变量 CF_BEACON_TOKEN；显式传值是为了让测试
    不用改进程环境就能起一个带注入的服务。
    """
    class Handler(_StaticHandler):
        """某个 --root 实例专用的处理器。"""

    Handler.root = os.path.realpath(root)
    Handler.prefix = prefix
    Handler.beacon_token = CF_BEACON_TOKEN if beacon_token is None else beacon_token
    Handler.runtime = _Runtime()       # 每个实例一份缓存/限流状态，测试互不污染
    pairs = [(_norm_slash(p), os.path.realpath(d)) for p, d in (aliases or [])]
    Handler.aliases = tuple(sorted(pairs, key=lambda x: len(x[0]), reverse=True))
    return Handler


def _norm_slash(s):
    """把 "/kindle/" 这类写法压成 "kindle"，与 _target 里已去掉前导斜杠的 rel 对齐。"""
    return s.strip("/")


def parse_alias(text):
    """解析 --alias 参数：只接受 "PREFIX=DIR" 这种形如 /kindle=/home/user/kindle 的写法。

    写错就直接退出：这是 systemd 单元里的固定参数，含糊地跳过只会让 404 更难查。
    """
    left, sep, right = text.partition("=")
    if not sep or not _norm_slash(left) or not right:
        sys.exit("--alias 格式不对: %r（应形如 /kindle=/home/user/kindle）" % text)
    return _norm_slash(left), right


def main():
    """解析参数 → 起 ThreadingHTTPServer → 前台跑（systemd 负责重启）。"""
    ap = argparse.ArgumentParser(description="静态 RSS 服务（给 Cloudflare Tunnel 用）")
    ap.add_argument("--root", default="docs", help="静态文件根目录（默认 docs）")
    ap.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="监听地址（Tunnel 用 127.0.0.1 即可；局域网调试用 0.0.0.0）")
    ap.add_argument("--prefix", default="/rss",
                    help="URL 前缀（Tunnel path 用 /rss；传空字符串则不要前缀）")
    ap.add_argument("--alias", action="append", default=[], metavar="PATH=DIR",
                    help="额外把某个 URL 前缀映射到另一个目录，可重复；如 /kindle=/home/user/kindle")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        # 直接退出而不是自己建目录：目录该由部署/渲染任务准备好，
        # 这里静默建一个空目录只会让 404 更难查。
        sys.exit("根目录不存在: %s" % args.root)

    httpd = _Server(
        (args.bind, args.port),
        make_handler(args.root, args.prefix.strip("/"),
                     [parse_alias(a) for a in args.alias])
    )
    root_show = os.path.abspath(args.root)
    print("serving %s -> http://%s:%d" % (root_show, args.bind, args.port), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
