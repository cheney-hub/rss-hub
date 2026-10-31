#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/ 下的 feed.xml / feed-wrap.xml / rotate.json 供 Cloudflare Tunnel 拉取。

零依赖，只用标准库。树莓派上由 systemd 拉起：
    python3 serve.py --root /home/user/rss-hub/docs --port 8080 --prefix /rss

对外地址形如 https://moreaix.com/rss/feed.xml
（Tunnel 的 path 前缀无论是否被剥掉，serve.py 都能正确找到文件）

同一份代码还兼了两个静态站：8081 根域主页（--root /home/user/site）与
8082 Kindle 看板图（--root /home/user/kindle，唯一绑 0.0.0.0 的实例）。

设计要点：
- 只读、只服务静态文件，防目录穿越（root 用 realpath，符号链接也穿不出去）。
- RSS/JSON/HTML 给正确 Content-Type。
- 强制 Cache-Control: no-cache —— 此前 Pages CDN 缓存 600 秒踩过坑，
  这里让 Cloudflare 边缘不缓存，保证客户端拿到的总是最新一版。
- 带 CORS *，方便网页阅读器跨域拉取（docs/screen.html 就跨域读 feed-wrap.xml）。
  这条头对**所有**响应都发（含 404），否则前端看到的是跨域错误而不是 404。
- 带 Last-Modified + If-Modified-Since → 304：Kindle 每 5 分钟来拉一次看板 PNG，
  图没变就只回一个 304 头，别每次把几十 KB 重传一遍（省流量也省电）。
"""
import argparse
import email.utils
import os
import sys
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


class _StaticHandler(BaseHTTPRequestHandler):
    """只做 GET/HEAD 的静态文件处理器；没有列目录、没有写操作。

    root 与 prefix 由 make_handler() 通过子类属性注入（一个服务实例一份），
    不放在模块全局里：一个进程同时服务多个目录时不会互相串。
    """

    root = ""
    prefix = ""
    server_version = "rss-serve/1.0"
    # 默认会把 "Python/3.13.x" 拼进 Server 头。8080 是公网（走 Tunnel），
    # 没必要告诉扫描器这台机器跑的是哪个小版本。
    sys_version = ""

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
        rel = rel or "index.html"
        # realpath 而不是 abspath：root 里放一个指向外部的符号链接，abspath 判定
        # 会放行、realpath 会把链接解开后再比，穿不出去。
        target = os.path.realpath(os.path.join(self.root, rel))
        if target != self.root and not target.startswith(self.root + os.sep):
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
        except (TypeError, ValueError):
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:                   # 极少数格式不带时区，按 HTTP 惯例当 UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    def _send(self, head_only):
        """取文件 → 能 304 就 304，否则 200 + 全文（head_only 时只回头）。"""
        target = self._target()
        if target is None or not os.path.isfile(target):
            self.send_error(404, "Not Found")
            return
        try:
            st = os.stat(target)
        except OSError:
            self.send_error(500, "Stat Error")
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
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(500, "Read Error")
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
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
        super().end_headers()

    def do_GET(self):
        """GET：正常取文件。"""
        self._send(False)

    def do_HEAD(self):
        """HEAD：只回头和 Content-Length，客户端据此判断要不要真下载。"""
        self._send(True)

    def log_message(self, fmt, *args):
        """访问日志：一行一条打到 stderr（systemd 收进 journal）。"""
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))


def make_handler(root, prefix=""):
    """造一个绑定了 root/prefix 的 Handler 类。

    每个服务实例各造一个子类，而不是把目录写死进 _StaticHandler：同一个进程将来
    同时服务多个目录时（比如 8080 的 feed 和 8082 的图合并），两边互不影响。
    prefix 是「Tunnel 上那层路径前缀」，传空串表示不要前缀（8081/8082 用）。
    """
    class Handler(_StaticHandler):
        """某个 --root 实例专用的处理器。"""

    Handler.root = os.path.realpath(root)
    Handler.prefix = prefix
    return Handler


def main():
    """解析参数 → 起 ThreadingHTTPServer → 前台跑（systemd 负责重启）。"""
    ap = argparse.ArgumentParser(description="静态 RSS 服务（给 Cloudflare Tunnel 用）")
    ap.add_argument("--root", default="docs", help="静态文件根目录（默认 docs）")
    ap.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="监听地址（Tunnel 用 127.0.0.1 即可；局域网调试用 0.0.0.0）")
    ap.add_argument("--prefix", default="/rss",
                    help="URL 前缀（Tunnel path 用 /rss；传空字符串则不要前缀）")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        # 直接退出而不是自己建目录：目录该由部署/渲染任务准备好，
        # 这里静默建一个空目录只会让 404 更难查。
        sys.exit("根目录不存在: %s" % args.root)

    httpd = ThreadingHTTPServer(
        (args.bind, args.port), make_handler(args.root, args.prefix.strip("/"))
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
