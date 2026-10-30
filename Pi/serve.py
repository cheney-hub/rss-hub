#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 docs/ 下的 feed.xml / feed-wrap.xml / rotate.json 供 Cloudflare Tunnel 拉取。

零依赖，只用标准库。树莓派上由 systemd 拉起：
    python3 serve.py --root /home/pi/rss-hub/docs --port 8080 --prefix /rss

对外地址形如 https://moreaix.com/rss/feed.xml
（Tunnel 的 path 前缀无论是否被剥掉，serve.py 都能正确找到文件）

设计要点：
- 只读、只服务静态文件，防目录穿越。
- RSS/JSON/HTML 给正确 Content-Type。
- 强制 Cache-Control: no-cache —— 此前 Pages CDN 缓存 600 秒踩过坑，
  这里让 Cloudflare 边缘不缓存，保证客户端拿到的总是最新一版。
- 带 CORS *，方便网页阅读器跨域拉取。
"""
import argparse
import os
import sys
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


def make_handler(root, prefix=""):
    root = os.path.abspath(root)

    class Handler(BaseHTTPRequestHandler):
        server_version = "rss-serve/1.0"

        def _target(self):
            rel = self.path.split("?", 1)[0].lstrip("/")
            # 兼容 Tunnel 的 path 前缀：/rss/feed.xml 与 /feed.xml 都指到同一文件
            if prefix and (rel == prefix or rel.startswith(prefix + "/")):
                rel = rel[len(prefix):].lstrip("/")
            rel = rel or "index.html"
            target = os.path.abspath(os.path.join(root, rel))
            if target != root and not target.startswith(root + os.sep):
                return None
            return target

        def _send(self, head_only):
            target = self._target()
            if target is None or not os.path.isfile(target):
                self.send_error(404, "Not Found")
                return
            ext = os.path.splitext(target)[1].lower()
            try:
                with open(target, "rb") as fh:
                    body = fh.read()
            except OSError:
                self.send_error(500, "Read Error")
                return
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def do_GET(self):
            self._send(False)

        def do_HEAD(self):
            self._send(True)

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))

    return Handler


def main():
    ap = argparse.ArgumentParser(description="静态 RSS 服务（给 Cloudflare Tunnel 用）")
    ap.add_argument("--root", default="docs", help="静态文件根目录（默认 docs）")
    ap.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    ap.add_argument("--bind", default="127.0.0.1",
                    help="监听地址（Tunnel 用 127.0.0.1 即可；局域网调试用 0.0.0.0）")
    ap.add_argument("--prefix", default="/rss",
                    help="URL 前缀（Tunnel path 用 /rss；传空字符串则不要前缀）")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
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
