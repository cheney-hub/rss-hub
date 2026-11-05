"""Pi/serve.py 的行为测试：真起一个 127.0.0.1 的 HTTP 服务，用 urllib 打进去。

为什么要真起服务、而不是给 Handler 打个桩：下面三条规则全都在**HTTP 头层面**成立，
mock 一个子类会把它们绕过去，测了等于没测：

1. **防目录穿越**：root 用 realpath 比、并且刻意不对 URL 做解码（`%2e%2e%2f` 因此天然穿不出去）。
2. **CORS 与 Cache-Control 走统一出口 `end_headers()`**，404 也必须带上——否则跨域读一个
   不存在的文件时，前端看到的是跨域错误而不是 404，排查会绕一大圈。
3. **`If-Modified-Since` → 304**：Kindle 每 5 分钟拉一次看板图，图没变只回一个头，省电省流量。

网络上只走本地回环（127.0.0.1 + 端口 0 随机端口），不碰外网，CI 里也能跑。
"""
import builtins
import collections
import email.utils
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "Pi"))

import serve                                            # noqa: E402


def ask(url, method="GET", headers=None):
    """发一个请求并返回响应对象；4xx/3xx 一律以 HTTPError 形式抛出（urllib 的默认行为）。"""
    req = urllib.request.Request(url, method=method, headers=headers or {})
    return urllib.request.urlopen(req)


class TestStaticServer(unittest.TestCase):
    """起一个真服务，把它当 HTTP 服务测（包括 304/404 这些状态码本身）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rss-serve-")
        cls.root = os.path.join(cls.tmp, "root")        # 被服务的目录
        os.makedirs(cls.root)
        cls.outside = os.path.join(cls.tmp, "secret.txt")   # root 外面的文件，穿越测试用
        # Unicode 之外的字节也要能出：PNG 是二进制，走 rb 读
        cls.files = {
            "feed.xml": "<rss><channel><item><title>甲</title></item></channel></rss>",
            "rotate.json": '{"window": 1}',
            "index.html": "<html><body>ok</body></html>",
            "sitemap.xml": '<?xml version="1.0" encoding="UTF-8"?>'
                           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>',
            "a.bin": "\x00\x01\x02binary",
        }
        for name, body in cls.files.items():
            with open(os.path.join(cls.root, name), "w", encoding="utf-8") as f:
                f.write(body)
        with open(cls.outside, "w", encoding="utf-8") as f:
            f.write("不该被读到")

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        serve.make_handler(cls.root, "rss"))
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        t.start()
        cls.pump = t

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 基本输出 ----

    def test_content_type(self):
        """RSS / JSON / HTML / sitemap 要给对 Content-Type，读屏器据此判断是否当 feed 解析。"""
        for name, want in (("feed.xml", "application/rss+xml; charset=utf-8"),
                           ("rotate.json", "application/json; charset=utf-8"),
                           ("index.html", "text/html; charset=utf-8"),
                           ("sitemap.xml", "application/xml; charset=utf-8")):
            with ask("%s/rss/%s" % (self.base, name)) as r:
                self.assertEqual(r.status, 200, name)
                self.assertEqual(r.headers.get("Content-Type"), want, name)
                self.assertEqual(r.read().decode("utf-8"), self.files[name], name)

    def test_unknown_ext_is_not_served(self):
        """白名单外一律 404：站点根目录里的 .env / .tmp 不能靠「猜类型」发出去。"""
        with self.assertRaises(urllib.error.HTTPError) as cm:
            ask("%s/rss/a.bin" % self.base)
        self.assertEqual(cm.exception.code, 404)

    def test_head_has_length_but_no_body(self):
        """HEAD 只回头：Kindle 端先探一次 Content-Length 再决定要不要拉。"""
        with ask("%s/rss/feed.xml" % self.base, method="HEAD") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(int(r.headers.get("Content-Length")),
                             len(self.files["feed.xml"].encode("utf-8")))
            self.assertEqual(r.read(), b"")

    # ---- 路径 → 文件的映射 ----

    def test_prefix_and_bare_path_are_the_same_file(self):
        """Tunnel 可能把 /rss 前缀剥掉也可能不剥，两种都要指向同一个文件。"""
        a = ask("%s/rss/feed.xml" % self.base).read()
        b = ask("%s/feed.xml" % self.base).read()
        self.assertEqual(a, b)
        self.assertEqual(a.decode("utf-8"), self.files["feed.xml"])

    def test_directory_url_falls_back_to_index(self):
        """/rss 这种打到目录上的请求要落到 index.html，而不是 404 或列目录。"""
        with ask("%s/rss" % self.base) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"ok", r.read())

    # ---- 安全 ----

    def test_dotdot_cannot_escape_root(self):
        """../ 一律穿不出去（realpath 解开后再比前缀）。"""
        for path in ("/../secret.txt", "/rss/../secret.txt",
                     "/..%2fsecret.txt", "/%2e%2e%2fsecret.txt"):
            with self.assertRaises(urllib.error.HTTPError, msg=path) as cm:
                ask(self.base + path)
            self.assertEqual(cm.exception.code, 404, path)

    def test_percent_encoding_is_not_decoded(self):
        """刻意不做 URL 解码：解码才会把 %2e%2e%2f 还原成 ../。
        这条以「取不到就算了」换「绝对穿不出去」，用例锁的是后者。
        """
        with self.assertRaises(urllib.error.HTTPError) as cm:
            ask("%s/rss/%%2e%%2e%%2fsecret.txt" % self.base)
        self.assertEqual(cm.exception.code, 404)

    def test_outside_file_is_never_served(self):
        """兜底检查：穿越失败还不够，内容也绝不能出现在任何响应里。"""
        try:
            with ask("%s/rss/../../secret.txt" % self.base) as r:
                body = r.read()
        except urllib.error.HTTPError:
            return
        self.assertNotIn("不该被读到".encode("utf-8"), body)

    def test_no_python_version_in_server_header(self):
        """8080 经 Tunnel 暴露在公网，没必要告诉扫描器这台机器跑哪个小版本。"""
        with ask("%s/rss/feed.xml" % self.base) as r:
            server = r.headers.get("Server", "")
        self.assertNotIn("Python", server, "Server 头泄漏了 Python 版本：%r" % server)
        self.assertEqual(serve._StaticHandler.sys_version, "")

    # ---- 缓存与头 ----

    def test_cache_and_cors_on_200(self):
        """no-cache 是吃 Pages CDN 600 秒缓存的亏之后加的；CORS 给 docs/screen.html 跨域读。"""
        with ask("%s/rss/feed.xml" % self.base) as r:
            self.assertIn("no-cache", r.headers.get("Cache-Control", ""))
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_cors_also_on_404(self):
        """404 也必须带 CORS：否则跨域场景下前端拿到的是跨域报错，看不出是真 404。"""
        with self.assertRaises(urllib.error.HTTPError) as cm:
            ask("%s/rss/nope.xml" % self.base)
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(cm.exception.headers.get("Access-Control-Allow-Origin"), "*")

    def test_not_modified_returns_304(self):
        """时间戳没变就只回一个头：Kindle 每 5 分钟拉一次图，这条直接决定流量与耗电。"""
        path = os.path.join(self.root, "feed.xml")
        past = 1600000000
        os.utime(path, (past, past))
        since = email.utils.formatdate(past, usegmt=True)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            ask("%s/rss/feed.xml" % self.base, headers={"If-Modified-Since": since})
        self.assertEqual(cm.exception.code, 304)
        self.assertTrue(cm.exception.headers.get("Last-Modified"))

    def test_stale_modified_since_gets_full_body(self):
        """客户端的时间戳比文件旧 → 正常回 200 + 全文，不能被永久 304 掉。"""
        path = os.path.join(self.root, "feed.xml")
        os.utime(path, (1600000000, 1600000000))
        with ask("%s/rss/feed.xml" % self.base,
                 headers={"If-Modified-Since":
                          email.utils.formatdate(1500000000, usegmt=True)}) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read().decode("utf-8"), self.files["feed.xml"])

    def test_garbage_modified_since_is_treated_as_absent(self):
        """坏的条件头当「没带」处理：多传一次文件，总比一个坏头把所有人都 304 掉强。"""
        with ask("%s/rss/feed.xml" % self.base,
                 headers={"If-Modified-Since": "not-a-date"}) as r:
            self.assertEqual(r.status, 200)


class TestTargetMapping(unittest.TestCase):
    """不必起服务也能验的映射逻辑（realpath 判定 + 前缀剥离）。"""

    class _Dummy:
        aliases = ()            # 没有别名，走最朴素的单根映射

        def __init__(self, root, prefix, path):
            self.root, self.prefix, self.path = root, prefix, path

        def _target(self):
            return serve._StaticHandler._target(self)

    def test_realpath_blocks_symlink_escape(self):
        """root 里放一个指向外部的符号链接，realpath 会解开它 → 判出越界。"""
        with tempfile.TemporaryDirectory() as tmp:
            real_root = os.path.join(tmp, "root")
            inside = os.path.join(tmp, "inside")
            os.makedirs(real_root)
            os.makedirs(inside)
            try:
                os.symlink(inside, os.path.join(real_root, "link"))
            except (OSError, NotImplementedError):
                self.skipTest("这台机器不支持符号链接")
            root = serve.make_handler(real_root, "").root
            t = self._Dummy(root, "", "/link")
            self.assertIsNone(t._target(), "符号链接穿出去了")


class TestAlias(unittest.TestCase):
    """一台实例顺带服务第二个目录（--alias /kindle=…）。

    这条能力是为了绕开 Tunnel 的路由顺序：根域那条不带 path 的兜底规则会吃掉
    所有路径，另配的 /kindle Public Hostname 排在它后面永远轮不到。把看板图挂到
    兜底那台身上之后，公网怎么排都不影响结果。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rss-alias-")
        cls.site = os.path.join(cls.tmp, "site")
        cls.board = os.path.join(cls.tmp, "kindle")
        os.makedirs(cls.site)
        os.makedirs(cls.board)
        for d, name, body in ((cls.site, "index.html", "<h1>HOME</h1>"),
                              (cls.site, "logo.svg", "<svg/>"),
                              (cls.board, "index.html", "<h1>BOARD</h1>")):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(body)
        # PNG 必须按二进制写：用文本模式会把 \x89 编成 UTF-8 的两个字节，
        # 后面断言魔数就对不上了（这个坑当场踩过一次）
        with open(os.path.join(cls.board, "rss-pw2.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nIMG")

        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            serve.make_handler(cls.site, "", [("/kindle/", cls.board)]))
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_alias_dir_url_falls_back_to_its_own_index(self):
        """/kindle 与 /kindle/ 要落到**别名目录**的 index.html，不是根目录那份。"""
        for path in ("/kindle", "/kindle/"):
            with ask(self.base + path) as r:
                self.assertEqual(r.status, 200, path)
                self.assertIn(b"BOARD", r.read(), path)

    def test_alias_serves_files_under_that_prefix(self):
        with ask(self.base + "/kindle/rss-pw2.png") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers.get("Content-Type"), "image/png")
            self.assertEqual(r.read(), b"\x89PNG\r\n\x1a\nIMG")

    def test_root_still_serves_its_own_files(self):
        """加了别名不能把根目录挤掉。"""
        with ask(self.base + "/") as r:
            self.assertIn(b"HOME", r.read())
        with ask(self.base + "/logo.svg") as r:
            self.assertEqual(r.status, 200)

    def test_alias_does_not_leak_into_other_prefixes(self):
        """别名只在它自己那层前缀下生效：/kindle/logo.svg 不该拿到根目录的文件。"""
        with self.assertRaises(urllib.error.HTTPError) as c:
            ask(self.base + "/kindle/logo.svg")
        self.assertEqual(c.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as c:
            ask(self.base + "/other/rss-pw2.png")
        self.assertEqual(c.exception.code, 404)

    def test_alias_still_blocks_traversal(self):
        """切到别名目录之后，越界检查比的必须是别名那个根。"""
        with self.assertRaises(urllib.error.HTTPError) as c:
            ask(self.base + "/kindle/../index.html")
        self.assertEqual(c.exception.code, 404)


class TestBeaconInjection(unittest.TestCase):
    """Cloudflare Web Analytics beacon 注入：只改 HTML、只插一次、token 为空就完全不动。

    分两层测：纯函数管「什么情况下改、改在哪」；真起服务管 HTTP 头层面
    （Content-Length / HEAD / 304 / 别名页面）—— 这些打桩是测不出来的。
    断言里不写死脚本域名，一律引用 serve.BEACON_JS：门禁不许源码出现裸外链。
    """

    TOKEN = "0123456789abcdef0123456789abcdef"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rss-beacon-")
        cls.root = os.path.join(cls.tmp, "root")
        cls.feed = os.path.join(cls.tmp, "feed")
        cls.board = os.path.join(cls.tmp, "board")
        for d in (cls.root, cls.feed, cls.board):
            os.makedirs(d)
        cls.files = {
            cls.root: {
                "index.html": b"<html><body><p>hi</p></body></html>",
                "feed.xml": b"<rss><channel></channel></rss>",
                # 二进制产物里故意混一段 <body>，用来抓「忘了判扩展名」
                "shot.png": b"\x89PNG\r\n\x1a\n<body>fake</body>",
            },
            cls.feed: {"index.html": b"<body>subscribe</body>"},
            cls.board: {"index.html": b"<body>board</body>"},
        }
        for d, files in cls.files.items():
            for name, body in files.items():
                with open(os.path.join(d, name), "wb") as f:
                    f.write(body)
        # 与线上 moreaix-site 同构：一个 root + 两个 alias，一次注入覆盖三页
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            serve.make_handler(cls.root, "",
                               aliases=[("rss", cls.feed), ("kindle", cls.board)],
                               beacon_token=cls.TOKEN))
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- 纯函数层：改不改、改在哪 ----

    def test_tag_contains_token_and_module_type(self):
        tag = serve.beacon_tag(self.TOKEN)
        self.assertIn(b'type="module"', tag)
        self.assertIn(self.TOKEN.encode(), tag)
        self.assertIn(serve.BEACON_JS.encode(), tag)

    def test_injected_right_before_body_close(self):
        out = serve.inject_beacon(b"<html><body>abc</body></html>", ".html", self.TOKEN)
        self.assertLess(out.index(b"data-cf-beacon"), out.index(b"</body>"))
        self.assertTrue(out.startswith(b"<html><body>abc"))
        self.assertTrue(out.endswith(b"</body></html>"))

    def test_htm_extension_and_uppercase_close(self):
        out = serve.inject_beacon(b"<BODY>x</BODY>", ".HTM", self.TOKEN)
        self.assertIn(b"data-cf-beacon", out)

    def test_html_close_is_fallback_insert_point(self):
        out = serve.inject_beacon(b"plain</html>", ".html", self.TOKEN)
        self.assertLess(out.index(b"data-cf-beacon"), out.index(b"</html>"))

    def test_non_html_never_touched(self):
        raw = b"<body>x</body>"
        for ext in (".xml", ".png", ".json", ".js", ""):
            self.assertEqual(serve.inject_beacon(raw, ext, self.TOKEN), raw)

    def test_no_insert_point_leaves_body_alone(self):
        raw = b"just text, no closing tag"
        self.assertEqual(serve.inject_beacon(raw, ".html", self.TOKEN), raw)

    def test_already_injected_is_not_duplicated(self):
        once = serve.inject_beacon(b"<body>x</body>", ".html", self.TOKEN)
        self.assertEqual(serve.inject_beacon(once, ".html", self.TOKEN), once)

    def test_empty_token_disables_injection(self):
        raw = b"<body>x</body>"
        self.assertEqual(serve.inject_beacon(raw, ".html", ""), raw)
        self.assertEqual(serve.beacon_tag(None), b"")

    def test_malformed_token_is_rejected(self):
        raw = b"<body>x</body>"
        for bad in ('abc"onload="evil', "<script>", "a b", "a/b"):
            self.assertEqual(serve.beacon_tag(bad), b"")
            self.assertEqual(serve.inject_beacon(raw, ".html", bad), raw)

    def test_unicode_token_is_rejected(self):
        """isalnum() 会放行全角字母与阿拉伯数字；属性里只认 ASCII。"""
        for bad in ("ｂ３８ｄ", "abc١٢٣", "tok\U0001f600"):
            self.assertEqual(serve.beacon_tag(bad), b"", repr(bad))

    def test_unsafe_beacon_js_disables_injection(self):
        """BEACON_JS 也拼进 HTML 属性：配错不能变成注入点。"""
        raw = b"<body>x</body>"
        with mock.patch.object(serve, "BEACON_JS", 'https://x/"onload="evil'):
            self.assertEqual(serve.beacon_tag(self.TOKEN), b"")
            self.assertEqual(serve.inject_beacon(raw, ".html", self.TOKEN), raw)
        with mock.patch.object(serve, "BEACON_JS", "http://x/x.js"):
            self.assertEqual(serve.beacon_tag(self.TOKEN), b"")

    # ---- HTTP 层：头与真实字节 ----

    def test_get_content_length_matches_injected_body(self):
        with ask(self.base + "/index.html") as r:
            body = r.read()
            self.assertIn(b"data-cf-beacon", body)
            self.assertEqual(int(r.headers["Content-Length"]), len(body))

    def test_head_length_matches_get_and_has_no_body(self):
        with ask(self.base + "/index.html") as g:
            get_len = int(g.headers["Content-Length"])
        with ask(self.base + "/index.html", "HEAD") as h:
            self.assertEqual(int(h.headers["Content-Length"]), get_len)
            self.assertEqual(h.read(), b"")

    def test_alias_pages_are_also_injected(self):
        for path in ("/rss/", "/kindle/"):
            with ask(self.base + path) as r:
                self.assertIn(b"data-cf-beacon", r.read())

    def test_non_html_response_is_byte_identical(self):
        for name in ("feed.xml", "shot.png"):
            with ask(self.base + "/" + name) as r:
                body = r.read()
            self.assertNotIn(b"data-cf-beacon", body)
            with open(os.path.join(self.root, name), "rb") as f:
                self.assertEqual(body, f.read())

    def test_304_still_has_no_body_after_injection(self):
        with ask(self.base + "/index.html") as r:
            last = r.headers["Last-Modified"]
            r.read()
        with self.assertRaises(urllib.error.HTTPError) as c:
            ask(self.base + "/index.html", headers={"If-Modified-Since": last})
        self.assertEqual(c.exception.code, 304)


class TestServeHardening(unittest.TestCase):
    """攻击面收敛：白名单 / Host / 限流 / 畸形 URL / 安全头，都走真 HTTP。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rss-guard-")
        cls.root = os.path.join(cls.tmp, "root")
        os.makedirs(cls.root)
        cls.files = {
            "index.html": b"<html><body>ok</body></html>",
            "feed.xml": b"<rss/>",
            "rotate.json": b"{}",
            "analytics.env": b"CF_BEACON_TOKEN=secret",
            "feed.xml.tmp": b"half written",
            "feed.xml.new": b"half written",
            "backup.bak": b"backup",
            "a.bin": b"\x00\x01",
            ".hidden": b"secret",
        }
        for name, body in cls.files.items():
            with open(os.path.join(cls.root, name), "wb") as f:
                f.write(body)
        cls.handler = serve.make_handler(cls.root, "")
        cls.httpd = serve._Server(("127.0.0.1", 0), cls.handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.pump = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.pump.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ask_code(self, path, headers=None):
        try:
            with ask(self.base + path, headers=headers) as r:
                r.read()
                return r.status, r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers

    def raw_code(self, target):
        """urllib/http.client 会先拦下畸形 URL，只能拿裸 socket 发。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(("GET %s HTTP/1.0\r\n\r\n" % target).encode("latin-1"))
            head = s.recv(256).split(b"\r\n", 1)[0]
            return int(head.split()[1])
        finally:
            s.close()

    @staticmethod
    def make_req(method="GET", path="/", host="localhost", extra=()):
        lines = ["%s %s HTTP/1.0" % (method, path)]
        if host is not None:
            lines.append("Host: " + host)
        lines.extend(extra)
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def raw_req(self, raw):
        """裸 socket 发任意请求，返回 (状态码, 小写的响应头字典, 已读到的 body)。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(raw)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 16384:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            code = int(lines[0].split()[1])
            hdr = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    hdr[k.strip().lower()] = v.strip()
            return code, hdr, rest
        finally:
            s.close()

    def test_secrets_and_half_written_artifacts_are_never_served(self):
        """配置、临时半成品、备份、隐藏文件、白名单外扩展名一律取不到。"""
        for p in ("/analytics.env", "/feed.xml.tmp", "/feed.xml.new", "/backup.bak",
                  "/a.bin", "/.hidden", "/.git/config", "/index.html.env"):
            self.assertEqual(self.ask_code(p)[0], 404, p)

    def test_whitelisted_files_still_served(self):
        for p in ("/index.html", "/feed.xml", "/rotate.json", "/"):
            self.assertEqual(self.ask_code(p)[0], 200, p)

    def test_host_outside_allowlist_is_rejected(self):
        self.assertEqual(self.ask_code("/index.html", {"Host": "evil.example"})[0], 400)
        self.assertEqual(self.ask_code("/index.html", {"Host": "127.0.0.1"})[0], 200)

    def test_host_normalization(self):
        ok = serve._StaticHandler._host_ok
        for h in ("moreaix.com", "moreaix.com:443", "127.0.0.1:8081", "", "[::1]:8081"):
            self.assertTrue(ok(h), h)
        for h in ("evil.example", "moreaix.com.evil.example", "0.0.0.0"):
            self.assertFalse(ok(h), h)

    def test_any_method_other_than_get_head_is_405(self):
        """基类的 501 绕过 _precheck：任意方法都落到同一道闸，且不回显方法名。"""
        for m in ("POST", "PUT", "DELETE", "OPTIONS", "TRACE", "PATCH", "BREW"):
            code, hdr, body = self.raw_req(self.make_req(m))
            self.assertEqual(code, 405, m)
            self.assertEqual(hdr.get("allow"), "GET, HEAD", m)
            self.assertEqual(hdr.get("content-length"), "0", m)
            self.assertNotIn(m.encode(), body, m)

    def test_rejected_method_still_checks_host(self):
        """伪造 Host 的 POST 以前回 501（白名单根本没跑），现在必须是 400。"""
        self.assertEqual(self.raw_req(self.make_req("POST", host="evil.example"))[0], 400)
        self.assertEqual(self.raw_req(self.make_req("OPTIONS", host="evil.example"))[0], 400)

    def test_rejected_method_is_rate_limited(self):
        """非 GET/HEAD 也要占限流额度，否则能无限打方法绕开闸门。"""
        old = serve.RATE_PER_IP
        serve.RATE_PER_IP = 2
        rt = self.handler.runtime
        try:
            rt.global_hits.clear()
            rt.ip_hits.clear()
            codes = [self.raw_req(self.make_req("POST"))[0] for _ in range(3)]
            self.assertEqual(codes, [405, 405, 429])
        finally:
            serve.RATE_PER_IP = old
            rt.global_hits.clear()
            rt.ip_hits.clear()

    def test_duplicate_host_headers_are_all_checked(self):
        """只取第一个 Host 时，第二个伪造值能白嫖白名单（实测曾返回 200）。"""
        self.assertEqual(self.raw_req(self.make_req(extra=["Host: evil.example"]))[0], 400)
        self.assertEqual(self.raw_req(self.make_req(extra=["Host: moreaix.com"]))[0], 200)

    def test_del_in_url_is_rejected(self):
        self.assertEqual(self.raw_code("/bad\x7fpath.html"), 400)

    def test_rejected_flood_does_not_grow_ip_table(self):
        """全局额度满后，被拒的伪造 IP 不该再往字典里塞条目。"""
        rt = serve._Runtime()
        for i in range(serve.RATE_GLOBAL + 500):
            rt.allow("10.%d.%d.%d" % (i // 65536 % 256, i // 256 % 256, i % 256))
        self.assertLessEqual(len(rt.ip_hits), serve.RATE_GLOBAL)

    def test_expired_ip_entries_are_swept(self):
        """原来的清理只删空 deque，一次性灌进来的条目永远变不空。"""
        rt = serve._Runtime()
        old = time.monotonic() - serve.RATE_WINDOW - 10
        for i in range(100):
            rt.ip_hits["198.51.100.%d" % i] = collections.deque([old])
        for i in range(300):
            rt.allow("203.0.113.%d" % (i % 250 + 1))
        self.assertLessEqual(len(rt.ip_hits), 250)

    def test_nul_byte_and_overlong_url_are_rejected(self):
        self.assertEqual(self.raw_code("/bad\x00path.html"), 400)
        self.assertEqual(self.raw_code("/" + "a" * 2100 + ".html"), 414)

    def test_security_headers_on_200_and_404(self):
        for path in ("/index.html", "/nope.html"):
            _, headers = self.ask_code(path)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(headers.get("X-Frame-Options"), "SAMEORIGIN", path)
            self.assertIn("strict-origin", headers.get("Referrer-Policy", ""), path)
            self.assertIn("geolocation=()", headers.get("Permissions-Policy", ""), path)
            csp = headers.get("Content-Security-Policy", "")
            self.assertIn("frame-ancestors", csp, path)
            self.assertNotIn("default-src", csp, path)  # 会掐掉首页内联脚本
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*", path)

    def test_rate_limit_returns_429_with_retry_after(self):
        old = serve.RATE_PER_IP
        serve.RATE_PER_IP = 3
        rt = self.handler.runtime
        try:
            rt.global_hits.clear()
            rt.ip_hits.clear()
            codes = [self.ask_code("/index.html",
                                   {"CF-Connecting-IP": "198.51.100.7"}) for _ in range(5)]
            self.assertEqual([c[0] for c in codes[:3]], [200, 200, 200])
            self.assertEqual(codes[3][0], 429)
            self.assertTrue(codes[3][1].get("Retry-After"))
        finally:
            serve.RATE_PER_IP = old
            rt.global_hits.clear()
            rt.ip_hits.clear()

    def test_limit_is_per_visitor_not_shared_loopback(self):
        old = serve.RATE_PER_IP
        serve.RATE_PER_IP = 2
        rt = self.handler.runtime
        try:
            rt.global_hits.clear()
            rt.ip_hits.clear()
            for ip in ("198.51.100.7", "198.51.100.8"):
                for _ in range(2):
                    self.assertEqual(
                        self.ask_code("/index.html", {"CF-Connecting-IP": ip})[0], 200)
        finally:
            serve.RATE_PER_IP = old
            rt.global_hits.clear()
            rt.ip_hits.clear()


class TestResponseCache(unittest.TestCase):
    """缓存命中不读盘、内容一变立刻失效 —— 这两条决定「零磁盘读」可信不可信。"""

    @staticmethod
    def key_of(path):
        st = os.stat(path)
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def test_second_read_never_opens_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.html")
            with open(p, "wb") as f:
                f.write(b"<body>x</body>")
            key = self.key_of(p)
            rt = serve._Runtime()
            with mock.patch("builtins.open", wraps=builtins.open) as m:
                first = rt.body(p, key, ".html", "")
                second = rt.body(p, key, ".html", "")
            self.assertEqual(first, second)
            reads = [c for c in m.call_args_list if c.args and c.args[0] == p]
            self.assertEqual(len(reads), 1)

    def test_changed_file_forces_reread(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.html")
            with open(p, "wb") as f:
                f.write(b"<body>A</body>")
            rt = serve._Runtime()
            self.assertIn(b"A", rt.body(p, self.key_of(p), ".html", ""))
            time.sleep(0.01)
            with open(p, "wb") as f:
                f.write(b"<body>B</body>")
            self.assertIn(b"B", rt.body(p, self.key_of(p), ".html", ""))

    def test_token_change_forces_reinject(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.html")
            with open(p, "wb") as f:
                f.write(b"<body>x</body>")
            rt = serve._Runtime()
            key = self.key_of(p)
            self.assertNotIn(b"data-cf-beacon", rt.body(p, key, ".html", ""))
            self.assertIn(b"data-cf-beacon", rt.body(p, key, ".html", "abc123"))

    def test_cache_stays_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = serve._Runtime()
            for i in range(serve.CACHE_MAX_ENTRIES + 10):
                p = os.path.join(tmp, "f%d.html" % i)
                with open(p, "wb") as f:
                    f.write(b"<body>x</body>")
                rt.body(p, self.key_of(p), ".html", "")
            self.assertLessEqual(len(rt.cache), serve.CACHE_MAX_ENTRIES)

    def test_log_summary_aggregates_within_window(self):
        rt = serve._Runtime()
        rt.err["since"] = time.monotonic() - 3600
        first = rt.log_summary(404)
        self.assertIn("404", first)
        self.assertIsNone(rt.log_summary(404))

    def test_success_log_is_also_aggregated(self):
        """200 逐条写 journal 时，正常 GET 一样能指挥 SD 卡写盘。"""
        handler = serve._StaticHandler.__new__(serve._StaticHandler)
        handler._code = 200
        handler.runtime = serve._Runtime()
        handler.runtime.err["since"] = time.monotonic() - 3600
        with mock.patch("sys.stderr") as err:
            handler.log_message('"%s" %s %s', "GET / HTTP/1.0", 200, 100)
            handler.log_message('"%s" %s %s', "GET / HTTP/1.0", 200, 100)
        self.assertEqual(err.write.call_count, 1)

    def test_log_error_is_silent_so_aggregation_is_not_bypassed(self):
        """send_error 先 log_error 再 send_response；这里逐条写就绕过了汇总。"""
        handler = serve._StaticHandler.__new__(serve._StaticHandler)
        with mock.patch("sys.stderr") as err:
            handler.log_error("code %d, message %s", 404, "Not Found")
        err.write.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
