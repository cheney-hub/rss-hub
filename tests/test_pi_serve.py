"""Pi/serve.py 的行为测试：真起一个 127.0.0.1 的 HTTP 服务，用 urllib 打进去。

为什么要真起服务、而不是给 Handler 打个桩：下面三条规则全都在**HTTP 头层面**成立，
mock 一个子类会把它们绕过去，测了等于没测：

1. **防目录穿越**：root 用 realpath 比、并且刻意不对 URL 做解码（`%2e%2e%2f` 因此天然穿不出去）。
2. **CORS 与 Cache-Control 走统一出口 `end_headers()`**，404 也必须带上——否则跨域读一个
   不存在的文件时，前端看到的是跨域错误而不是 404，排查会绕一大圈。
3. **`If-Modified-Since` → 304**：Kindle 每 5 分钟拉一次看板图，图没变只回一个头，省电省流量。

网络上只走本地回环（127.0.0.1 + 端口 0 随机端口），不碰外网，CI 里也能跑。
"""
import email.utils
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

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

    # ---- 基本 Cow出力 ----

    def test_content_type(self):
        """RSS / JSON / HTML 要给对 Content-Type，读屏器据此判断是否当 feed 解析。"""
        for name, want in (("feed.xml", "application/rss+xml; charset=utf-8"),
                           ("rotate.json", "application/json; charset=utf-8"),
                           ("index.html", "text/html; charset=utf-8")):
            with ask("%s/rss/%s" % (self.base, name)) as r:
                self.assertEqual(r.status, 200, name)
                self.assertEqual(r.headers.get("Content-Type"), want, name)
                self.assertEqual(r.read().decode("utf-8"), self.files[name], name)

    def test_unknown_ext_is_octet_stream(self):
        """不认识的扩展名别猜：application/octet-stream 让它走下载而不是被执行。"""
        with ask("%s/rss/a.bin" % self.base) as r:
            self.assertEqual(r.headers.get("Content-Type"), "application/octet-stream")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
