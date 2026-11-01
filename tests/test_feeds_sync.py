"""清单同步的行为测试（stdlib unittest，零第三方依赖，不联网）。

覆盖的是「宁可用旧清单，也不能因为同步失败让构建停摆」这条底线：网络错误、
404、WAF 页、内容太少、写不进磁盘，每一种都必须退回 kept 且不动本地文件。

跑法：
    python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feeds_sync as S  # noqa: E402

UA = "test-agent"          # 由调用方传（线上是 build_feed.UA），这里随便给一个


def _list_text(n, tag="new"):
    """造一份 n 个地址的清单：**注释名 + 地址两行一组**，与 feeds.txt 同形。"""
    return "".join("# 源%s%d\nhttps://src/%s%d.xml\n" % (tag, i, tag, i)
                   for i in range(n))


def _sources(path):
    """文件里真能被当清单读出来的地址数。"""
    with open(path, encoding="utf-8") as f:
        return sum(1 for ln in f if ln.strip().startswith(("http://", "https://")))


class TestFeedsSync(unittest.TestCase):
    """清单同步：宁可用旧清单，也不能因为同步失败让这一轮构建停摆。

    两端各踩过一次坑才有这个类：① 仓库改了清单却没推到 Pi，两边各出一版 feed，
    屏幕上完全看不出来；② 反过来若同步失败就让构建失败，表现是 feed 直接停更、
    且没有任何报错线索。所以每条用例都同时断言「返回值」和「本地文件没被碰过」。
    """
    # 本地那份要够「像清单」（地址数达到门槛），否则校验会先把它挡掉，测不到后面几条
    OLD = _list_text(12, "old").encode("utf-8")

    def _path(self, content=OLD):
        p = os.path.join(tempfile.mkdtemp(), "feeds.txt")
        self.addCleanup(shutil.rmtree, os.path.dirname(p), True)
        if content is not None:
            with open(p, "wb") as f:
                f.write(content)
        return p

    def _patch(self, body=b"", status=200, exc=None):
        """把 urlopen 换成假实现；返回记录到每次调用的 [(Request, timeout)]。"""
        calls = []

        class _Resp:
            def __init__(self):
                self.status = status

            def read(self, _n=-1):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def fake(req, timeout=None):
            calls.append((req, timeout))
            if exc is not None:
                raise exc
            return _Resp()

        orig = S.urllib.request.urlopen
        S.urllib.request.urlopen = fake
        self.addCleanup(setattr, S.urllib.request, "urlopen", orig)
        return calls

    def test_no_url_does_not_touch_network(self):
        # 没配同步地址时（CI 就是这种）一次网都不该发，否则等于给每轮构建加依赖
        calls = self._patch(body=self.OLD, exc=AssertionError("不该联网"))
        p = self._path()
        self.assertEqual(S.sync(p, UA, url=""), "kept")
        self.assertEqual(calls, [], "没配地址却发了请求")
        self.assertEqual(open(p, "rb").read(), self.OLD)

    def test_newer_list_replaces_local(self):
        n = S.FEEDS_SYNC_MIN_SOURCES + 2
        new = _list_text(n).encode("utf-8")
        self._patch(body=new)
        p = self._path()
        self.assertEqual(S.sync(p, UA, url="http://x/feeds.txt"), "updated")
        self.assertEqual(open(p, "rb").read(), new)
        # 写进去的必须真是能用的清单，而不是一堆字节
        self.assertEqual(_sources(p), n)

    def test_identical_content_is_same_and_not_rewritten(self):
        self._patch(body=self.OLD)
        p = self._path()
        before = os.stat(p).st_mtime_ns
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "same")
        self.assertEqual(os.stat(p).st_mtime_ns, before, "内容没变不该重写文件")
        self.assertFalse(os.path.exists(p + ".new"), "留下了临时文件")

    def test_network_error_keeps_local(self):
        for exc in (urllib.error.URLError("down"), TimeoutError(), OSError("reset")):
            with self.subTest(exc=type(exc).__name__):
                self._patch(exc=exc)
                p = self._path()
                self.assertEqual(S.sync(p, UA, url="http://x/f"), "kept")
                self.assertEqual(open(p, "rb").read(), self.OLD)

    def test_html_page_kept_even_with_many_links(self):
        # WAF 拦截页、仓库 404 页都可能带着一堆链接，所以「像不像 HTML」要单独判，
        # 不能只数地址条数
        self._patch(body=("<html><body>Sign in</body></html>\n" + _list_text(20)).encode())
        p = self._path()
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "kept")
        self.assertEqual(open(p, "rb").read(), self.OLD)

    def test_too_few_sources_kept(self):
        self._patch(body=_list_text(S.FEEDS_SYNC_MIN_SOURCES - 1).encode())
        p = self._path()
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "kept")
        self.assertEqual(open(p, "rb").read(), self.OLD)

    def test_min_sources_boundary_updates(self):
        self._patch(body=_list_text(S.FEEDS_SYNC_MIN_SOURCES).encode())
        p = self._path()
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "updated")

    def test_non_utf8_body_kept(self):
        self._patch(body=b"\xff\xfe\x00\x01" + b"https://a/%d\n" % 1 * 20)
        p = self._path()
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "kept")
        self.assertEqual(open(p, "rb").read(), self.OLD)

    def test_non_200_kept(self):
        self._patch(body=_list_text(20).encode(), status=404)
        p = self._path()
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "kept")
        self.assertEqual(open(p, "rb").read(), self.OLD)

    def test_missing_local_file_is_created(self):
        new = _list_text(S.FEEDS_SYNC_MIN_SOURCES + 1).encode()
        self._patch(body=new)
        p = self._path(content=None)
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "updated")
        self.assertEqual(open(p, "rb").read(), new)

    def test_write_failure_keeps_build_alive(self):
        # 目标写不进去时（这里用「路径是个目录」逼出 OSError）必须返回 kept 而非抛异常，
        # 否则一轮都跑不出来；临时文件也要收干净
        self._patch(body=_list_text(20).encode())
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "feeds.txt")
        os.mkdir(p)
        self.assertEqual(S.sync(p, UA, url="http://x/f"), "kept")
        self.assertFalse(os.path.exists(p + ".new"), "失败后留下了临时文件")

    def test_request_asks_for_fresh_copy(self):
        # 走 CDN 时如果拿到缓存副本，「改完马上生效」就失效了 —— 这正是这个功能的全部意义
        calls = self._patch(body=_list_text(S.FEEDS_SYNC_MIN_SOURCES + 1).encode())
        p = self._path()
        S.sync(p, UA, url="http://x/f")
        req, timeout = calls[0]
        self.assertEqual(timeout, S.FEEDS_SYNC_TIMEOUT)
        self.assertEqual(req.get_header("Cache-control"), "no-cache")


if __name__ == "__main__":
    unittest.main(verbosity=2)
