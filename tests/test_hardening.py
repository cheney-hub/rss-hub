"""加固点回归：一批「改坏了不报错、只是结果悄悄变错」的地方（零依赖、不联网）。

单独成文件是因为它们跨模块（build_feed / render / rotate / feeds_sync 都有份），
塞进 test_core.py 会把那个文件顶过 750 行的拆分提醒线。

共同点：出错时不抛异常、日志里也算成功，只有输出内容是错的 —— 所以每条用例断言的
都是**产物**（解出来的字符串、取到的链接、写出来的 XML），而不是「有没有抛」。

跑法：
    python -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_feed as B   # noqa: E402
import render as R       # noqa: E402
import rotate as RO      # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestEnvVarsNeverCrashAtImport(unittest.TestCase):
    """环境变量被写成空串 / 非数字时，模块必须照样能 import 并回落默认值。

    这些取值都在模块级，异常发生在 import 期：`main()` 根本进不去、`--version`
    也打不出来，CI 上只剩一行 traceback 和一份没更新的 feed。而 systemd 与
    Actions 里把变量声明成空值是常见操作，所以不能指望「配错了是你活该」。
    """
    CASES = (
        ("build_feed", ("REFRESH_SECONDS", "FETCH_TIMEOUT", "CONCURRENCY",
                        "SOURCE_TZ_OFFSET", "PER_SOURCE", "ITEM_LIMIT")),
        ("rotate", ("ROTATE_SECONDS", "ROTATE_POOL_MAX")),
        ("render", ("SCREEN_LINE_WIDTH", "SCREEN_BODY_MAX", "FEED_BODY_MAX")),
        ("feeds_sync", ("FEEDS_SYNC_TIMEOUT", "FEEDS_SYNC_MIN_SOURCES")),
    )

    @staticmethod
    def _try_import(module, **env):
        full = dict(os.environ)
        full.update(env)
        p = subprocess.run([sys.executable, "-c", "import %s" % module],
                           capture_output=True, text=True, env=full, cwd=ROOT)
        return p.returncode, (p.stderr or "")[-300:]

    def _for_each(self, value):
        for module, names in self.CASES:
            for name in names:
                with self.subTest(module=module, var=name):
                    rc, err = self._try_import(module, **{name: value})
                    self.assertEqual(
                        rc, 0,
                        "%s 在 %s=%r 时崩在 import 期（默认值的回落没写对）：\n%s"
                        % (module, name, value, err))

    def test_empty_string_falls_back_to_default(self):
        self._for_each("")

    def test_garbage_falls_back_to_default(self):
        self._for_each("abc")


class TestHardening(unittest.TestCase):
    """一批「改坏了不报错、只是结果悄悄变错」的加固点。

    共同点是：出错时不抛异常、日志里也算成功，只有输出内容是错的 —— 所以每条用例
    断言的都是**产物**，而不是「有没有抛」。
    """

    def test_charset_that_decodes_anything_is_not_trusted(self):
        # iso-8859-1 / cp1252 能解码任意字节序列、永不抛 UnicodeDecodeError。
        # 把它排进候选就等于关掉 utf-8 / gb18030 整条兜底链，中文源静默变乱码。
        gb = "中文标题测试内容".encode("gb18030")
        self.assertEqual(B.decode_body(gb, "iso-8859-1"), "中文标题测试内容")
        self.assertEqual(B.decode_body(gb, "windows-1252"), "中文标题测试内容")
        self.assertEqual(B.decode_body(gb, "utf-8"), "中文标题测试内容")
        self.assertEqual(B.decode_body("中文".encode("utf-8"), "big5"), "中文",
                         "声明错了但要能兜回来（big5 解 utf-8 字节会抛错）")

    def test_doctype_with_bracket_in_entity_is_fully_stripped(self):
        # 实体值里带 `]` 时按第一个 `]` 收尾会留下孤零零的 `]>`，ET 随即 ParseError，
        # 一个长得合法的 DOCTYPE 就把整源打进丢字段的正则兜底通路。
        xml = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "b]c">]>'
               '<rss><channel><item><title>T</title></item></channel></rss>')
        clean = B._sanitize(xml)
        self.assertNotIn("]", clean.split("<rss")[0], "DOCTYPE 没剥干净")
        self.assertEqual(len(B._item_nodes(clean)), 1, "应当走正常通路解析出 1 条")

    def test_single_quoted_href_is_still_a_link(self):
        # 只认双引号时 link 是空串 → 不打 <link>、guid 退化成「标题|源」，
        # 阅读器里这条打不开原文，而日志上看不出异常。
        raw = "<item><title>T</title><link href='http://x/1'/></item>"
        self.assertEqual(B._entry_of({"__raw__": raw})["link"], "http://x/1")

    def test_link_element_text_does_not_swallow_the_next_tag(self):
        raw = "<item><title>T</title><link>http://x/1</link><guid>g</guid></item>"
        self.assertEqual(B._entry_of({"__raw__": raw})["link"], "http://x/1")

    def test_prehistoric_timestamp_clamps_instead_of_raising(self):
        # .NET 模板把 default(DateTime) 序列化成 0001-01-01，epoch 是 -6.2e10，
        # 下游 fromtimestamp 在负区间上抛 OSError —— 那一抛就是整份产物不落盘。
        # 钳到 0 而不是返回 None：两者都会沉到池底，但 0 是数字，
        # 排序键 -(ts or 0) 拿它不会退化成「没有时间」的另一条分支。
        self.assertEqual(B._parse_date("0001-01-01T00:00:00Z"), 0.0)
        self.assertIsNone(B._parse_date(""))

    def test_missing_or_undecodable_feeds_file_does_not_raise(self):
        self.assertEqual(B.read_feed_entries("/nonexistent/feeds.txt"), [],
                         "清单读不到要退化成「没有源」，而不是把 traceback 甩出 main()")
        d = tempfile.mkdtemp()
        self.addCleanup(os.remove, os.path.join(d, "feeds.txt"))
        p = os.path.join(d, "feeds.txt")
        with open(p, "wb") as f:
            f.write("中文源名\nhttp://x/rss\n".encode("gb18030"))
        urls = [e["url"] for e in B.read_feed_entries(p)]
        self.assertEqual(urls, ["http://x/rss"],
                         "编码不对时坏字节退化成乱码源名，地址照旧能用")

    def test_extreme_timestamp_never_breaks_the_output(self):
        self.assertEqual(RO._fmt_at({"ts": -62135596800}), "发布时间未知")
        self.assertEqual(RO._fmt_at({"ts": 0}), "发布时间未知")
        self.assertEqual(RO._fmt_at({"ts": None}), "发布时间未知")
        # 整份产物也得写得出来：坏时间最多让这条不带 <pubDate>，不能赔上整份 feed
        it = {"title": "T", "link": "http://x/1", "src": "s", "ts": -62135596800}
        out = "\n".join(R._item_lines(it, screen=False))
        self.assertIn("<title>T</title>", out)
        # 不发 <pubDate> 而不是碰运气：fromtimestamp 在 Linux 上认负时间戳
        # （写出 0001-01-01 这种假日期，产物看着正常、实则是错的时间），
        # 在 Windows 上抛 OSError。这条断言因此必须是平台无关的。
        self.assertNotIn("pubDate", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
