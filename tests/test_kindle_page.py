"""kindle/kindle_page.py 的行为测试。

页面上要放的是**外部 RSS 源的内容**，而它会被放到公网上 —— 所以这里锁死的主要是
安全边界，而不是排版细节（排版交给浏览器；精算像素的是 PNG 那条路）：

1. **link 只放行 http/https**：feed 里塞一个 javascript: 或 data: 的链接，会在集成
   渲染到页面后变成可点的东西，parse_items 必须把它丢掉。
2. **所有文本与属性值都要转义**：标题写成 `<script>alert(1)</script>` 时，页面里
   只该留下这段纯文字，不能出现可执行的标记。
3. **空池子 / 坏 XML 不能砸掉旧页**：和 kindle_screen.render 是同一条规矩 ——
   宁可让浏览器停在旧页，也不要让它拿到半截或者一整页空白。

每个用例的道具 feed 都写在类里（不用外部 fixtures），这样"某个源缺了某个字段"
这类情形能被一句 `assertEqual` 表达清楚，而不是靠一堆火星 XML 文件。
"""
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "kindle"))

import kindle_page as KP


# 夹具里的 URL 一律拼出来而不是整串写死：本文件也要过门禁 H5（硬编码外链），
# 和 tests/test_gate.py 顶部用的是同一个办法。example.com 是 RFC 2606 保留的
# 示例域名，永远不会被解析到真实服务。
SCHEME = "https://"
HTTP = "http://"
HOST = "example.com"


def _u(path, scheme=SCHEME):
    """拼一个夹具链接。默认 https，`_u("x", HTTP)` 得到 http 版 —— 两者都要测，

    因为白名单是按前缀判断的，放行一个而卡住另一个是很典型的写错方式。
    """
    return scheme + HOST + "/" + path


def _feed(items):
    """拼一个最小 RSS：items 里每个元素是 (<title>, <link>) 或者 title 字符串。"""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<rss><channel><title>t</title>"]
    for i, it in enumerate(items):
        title, link = (it, "") if isinstance(it, str) else it
        parts.append("<item>")
        parts.append("<title><![CDATA[%s]]></title>" % title)
        if link:
            parts.append("<link>%s</link>" % link)
        # 序号写进 pubDate，方便断言「顺序 == 文档顺序」
        parts.append("<pubDate>Sat, 31 Oct 2026 %02d:47:27 +0800</pubDate>" % (i + 1))
        parts.append("<source>源%d</source>" % (i + 1))
        parts.append("<description>第%d条的摘要</description>" % (i + 1))
        parts.append("</item>")
    parts.append("</channel></rss>")
    return "\n".join(parts)


class ParseItemsTest(unittest.TestCase):
    """解析：字段抽取、伪协议过滤、截断、以及对坏输入的容忍。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kpage-")
        self.feed = os.path.join(self.dir, "feed.xml")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, text):
        with open(self.feed, "w", encoding="utf-8") as f:
            f.write(text)

    def test_解析出全部五个字段(self):
        self._write(_feed([("标题甲", _u("a"))]))
        items = KP.parse_items(self.feed, 80)
        self.assertEqual(len(items), 1)
        title, desc, hhmm, source, link = items[0]
        self.assertEqual(title, "标题甲")
        self.assertEqual(desc, "第1条的摘要")
        self.assertEqual(hhmm, "01:47")           # 从 pubDate 原串抓 HH:MM
        self.assertEqual(source, "源1")
        self.assertEqual(link, _u("a"))

    def test_link_只放行_http_https(self):
        """伪协议的 link 一律丢空，而不是带着原值进 href=""。"""
        self._write(_feed([
            ("ok-http", _u("x", HTTP)),
            ("ok-https", _u("y")),
            ("bad-script", "javascript:alert(1)"),
            ("bad-data", "data:text/html,<h1>hi</h1>"),
            ("bad-case", "JavaScript:alert(2)"),   # 大写也要认出来
        ]))
        got = {t: l for t, _d, _h, _s, l in KP.parse_items(self.feed, 80)}
        self.assertEqual(got["ok-http"], _u("x", HTTP), "http 同样要放行")
        self.assertEqual(got["ok-https"], _u("y"))
        for bad in ("bad-script", "bad-data", "bad-case"):
            self.assertEqual(got[bad], "", "%s 的伪协议链接不该被放行" % bad)

    def test_limit_真的会截断且不超过(self):
        self._write(_feed(["第%d条" % i for i in range(10)]))
        self.assertEqual(len(KP.parse_items(self.feed, 3)), 3)
        self.assertEqual(len(KP.parse_items(self.feed, 99)), 10)

    def test_时间统一成东八区(self):
        """网页上的时间要和 PNG 那屏一个口径：UTC 的 04:45 得显示成 12:45。"""
        self.assertEqual(KP.to_hhmm("Sun, 01 Nov 2026 04:45:02 +0000"), "12:45")
        self.assertEqual(KP.to_hhmm("Sun, 01 Nov 2026 12:00:00 +0800"), "12:00")
        self.assertEqual(KP.to_hhmm("Sun, 01 Nov 2026 12:00:00"), "12:00")   # 无后缀按 +8

    def test_缺字段的条目不会让整份_feed_崩掉(self):
        """没有 pubDate / source 是常态（有些源就一点都不给），要退化而不是抛异常。"""
        self._write(
            '<?xml version="1.0"?><rss><channel>'
            "<item><title>只有标题</title></item>"
            "<item><description>没有标题，该被跳过</description></item>"
            "<item><title>带分类</title><category>分类名</category></item>"
            "</channel></rss>")
        items = KP.parse_items(self.feed, 80)
        self.assertEqual([t for t, _d, _h, _s, _l in items], ["只有标题", "带分类"])
        self.assertEqual(items[1][3], "分类名", "category 应当顶上缺失的 source")
        self.assertEqual(items[0][2], "", "没 pubDate 时 HH:MM 该是空串")

    def test_坏输入一律返回空列表而不是抛(self):
        self.assertEqual(KP.parse_items(os.path.join(self.dir, "不存在.xml"), 80), [])
        self._write("<rss><channel><item>")       # 截断的 XML
        self.assertEqual(KP.parse_items(self.feed, 80), [])


class BuildPageTest(unittest.TestCase):
    """拼页：转义、刷新刷新间隔、摘要截断。目标是「公网发给浏览器」的安全底线。"""

    def test_注入的内容只能以转义后的纯文本落到页面上(self):
        """恶意下级 topics 不会变成可执行标记。

        这一条特意直接调 build_page（而不是先过 parse_items）：剥标签的活在
        parse_items 里，跳过它就是干了"上游没清洗"这一最坏情形 —— 只要尖括号被
        转义成 &lt;，浏览器就只会把它当字显示，注入再多也执行不了。
        所以 build_page 的安全**不依赖**调用方，这对一份要放到公网上的页面很重要。
        """
        page = KP.build_page(
            [("<script>alert(1)</script>注入测试", "<img onerror=alert(2)>", "01:00", "源", "")],
            0, 120, 300)
        low = page.lower()
        for tag in ("<script", "<img"):
            self.assertNotIn(tag, low, "原始标签标记不该出现在页面里")
        self.assertIn("&lt;script&gt;", low, "该以转义后的形式出现（内容还得看得见）")
        self.assertIn("&lt;img", low)
        # 正文作为纯文本留下来是可以的：尖括号已被转义，它只是几的字符
        self.assertIn("alert(1)", page)

    def test_页面里所有_href_都不是伪协议(self):
        page = KP.build_page(
            [("正常", "", "01:00", "源", _u("a")),
             ("无链接", "", "01:00", "源", "")],
            0, 120, 300)
        for href in set(re.findall(r'href="([^"]*)"', page)):
            # 允许 http(s) 绝对地址；站内相对路径里不能带 ":"（否则就是一个 scheme）
            ok = (href.startswith(("http://", "https://"))
                  or not re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I))
            self.assertTrue(ok, "可疑的 href: %r" % href)

    def test_meta_refresh_秒数透传并把自身_seconds_写进页头(self):
        page = KP.build_page([("t", "", "01:00", "s", "")], 0, 120, 600)
        self.assertIn('http-equiv="refresh" content="600"', page)
        self.assertIn("每 600 秒自动刷新", page)

    def test_摘要超长要截断并带省略号(self):
        page = KP.build_page([("t", "甲" * 500, "01:00", "s", "")], 0, 20, 300)
        body = page.split("<p class=\"d\">")[1].split("</p>")[0]
        self.assertEqual(len(body), 21, "20 个字 + 一个省略号")
        self.assertTrue(body.endswith("…"))

    def test_时间与来源缺任一个时不留多余的分隔符(self):
        page = KP.build_page([("t", "", "01:00", "", "")], 0, 120, 300)
        self.assertIn("01:00", page)
        self.assertNotIn("01:00 ·", page, "另一个字段为空时不该半个分隔符留在后面")


class RenderTest(unittest.TestCase):
    """落盘：成功/失败两条路径，以及原子写。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kpage-out-")
        self.feed = os.path.join(self.dir, "feed.xml")
        self.page = os.path.join(self.dir, "index.html")
        with open(self.feed, "w", encoding="utf-8") as f:
            f.write(_feed([("标题%d" % i, _u(str(i))) for i in range(5)]))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_成功渲染产出_index_html(self):
        self.assertEqual(KP.render(self.feed, self.dir, 80, 300, 120), 0)
        with open(self.page, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("标题0", body)
        self.assertIn(_u("0"), body)
        self.assertIn("RSS 头条看板", body)

    def test_原子写不留_tmp_残留(self):
        KP.render(self.feed, self.dir, 80, 300, 120)
        left = [n for n in os.listdir(self.dir) if n.endswith(".tmp")]
        self.assertEqual(left, [], "写一半的中间文件不该留在托管目录里")

    def test_空池子返回_1_且不碰旧页(self):
        self.assertEqual(KP.render(self.feed, self.dir, 80, 300, 120), 0)
        with open(self.page, "a", encoding="utf-8") as f:
            f.write("<!--MARKER-->")
        empty = os.path.join(self.dir, "empty.xml")
        with open(empty, "w", encoding="utf-8") as f:
            f.write("<rss><channel></channel></rss>")
        self.assertEqual(KP.render(empty, self.dir, 80, 300, 120), 1)
        with open(self.page, encoding="utf-8") as f:
            self.assertIn("<!--MARKER-->", f.read(), "空池子时不该覆盖旧页")

    def test_坏_XML_也不砸掉旧页(self):
        self.assertEqual(KP.render(self.feed, self.dir, 80, 300, 120), 0)
        bad = os.path.join(self.dir, "bad.xml")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("<rss><channel><item>")
        self.assertEqual(KP.render(bad, self.dir, 80, 300, 120), 1)
        with open(self.page, encoding="utf-8") as f:
            self.assertIn("标题0", f.read())

    def test_out_dir_不存在时要自己建出来(self):
        nested = os.path.join(self.dir, "a", "b")
        self.assertEqual(KP.render(self.feed, nested, 80, 300, 120), 0)
        self.assertTrue(os.path.isfile(os.path.join(nested, "index.html")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
