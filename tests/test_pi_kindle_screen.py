"""Pi/kindle_screen.py 的行为测试。

这台机器上可能没装 Pillow（kindle_screen 依赖它，Pi 上有），没装就整文件跳过；
下面的大部分用例用的是**自己写的假 draw**（只实现 `textlength`），所以不依赖字体。

重点锁的是三个曾经真的出过事的地方：

1. **`draw_board()` 只画一遍列表**：27f28f1 删 `_draw_lead()` 时漏删下面那行
   `_draw_list(...items[1:])`，两次都从同一个 y 起笔 → 除最后一行外每行双字叠印、
   整版不可读。线下面一条用例会把这条永久钉住。
2. **`wrap()` 的超宽原子要硬切**：一个 140 字符的 URL 在 1024px 版面上量出 1960px，
   原子切不开就会整段画到画布外面。
3. **孤字行 / 行首标点回挂会把两行并回一行并顶出边界**，所以 `_fit_title()` 会退成
   「一行 + 省略号」。
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "Pi"))

try:
    import kindle_screen as KS
    HAVE_PIL = True
    _IMPORT_ERR = ""
except ImportError as exc:                       # 本机没装 Pillow，整文件跳过
    KS = None
    HAVE_PIL = False
    _IMPORT_ERR = str(exc)


class _FakeDraw:
    """按字符数算宽的量宽器（每字 10px），让折行结果与真实字体无关。

    生产代码只用到 draw.textlength()，所以画图的地方拿它替换 PIL 的 ImageDraw
    是安全的，而且把「折行」这件纯几何的事从字体度量里剥出来 —— 换个字体、
    换台机器都不该改变行数怎么算。
    """

    def textlength(self, text, font=None):
        return len(text) * 10.0


# -*- coding: utf-8 -*-
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>头条</title><link>https://HOST/feed.xml</link>
<item>
  <title>【源甲】甲国通过新的预算法案</title>
  <description>&lt;p&gt;这是&lt;b&gt;摘要&lt;/b&gt;&lt;/p&gt;</description>
  <pubDate>Sat, 31 Oct 2026 01:05:00 GMT</pubDate>
  <source url="http://host-a/1">源甲</source>
</item>
<item>
  <title></title>
  <description>没有标题的条目应当被丢掉</description>
  <pubDate>Sat, 31 Oct 2026 00:30:00 GMT</pubDate>
</item>
<item>
  <title>乙公司发布季度财报</title>
  <description>摘要乙</description>
  <pubDate>Fri, 30 Oct 2026 23:59:59 GMT</pubDate>
  <category>源乙</category>
</item>
</channel></rss>
"""


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestCleanText(unittest.TestCase):
    def test_strips_tags_and_entities(self):
        KS_clean = KS.clean_text
        self.assertEqual(KS_clean("<p>甲 &amp; 乙</p>"), "甲 & 乙")
        self.assertEqual(KS_clean("<b>粗</b>体"), "粗 体")

    def test_collapses_whitespace(self):
        self.assertEqual(KS.clean_text("甲\n\n 乙\t丙"), "甲 乙 丙")

    def test_empty(self):
        self.assertEqual(KS.clean_text(""), "")
        self.assertEqual(KS.clean_text(None), "")


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestAtomize(unittest.TestCase):
    def test_words_stay_whole(self):
        self.assertEqual(KS.atomize("abc def"), ["abc", " ", "def"])

    def test_cjk_is_per_char(self):
        self.assertEqual(KS.atomize("甲乙"), ["甲", "乙"])

    def test_mixed_keeps_number_and_unit_together(self):
        self.assertEqual(KS.atomize("20.15亿元"), ["20.15", "亿", "元"])


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestParseFeed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rss-kindle-")
        self.feed = os.path.join(self.tmp, "feed.xml")
        with open(self.feed, "w", encoding="utf-8") as f:
            f.write(FEED)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skips_items_without_title(self):
        items = KS.parse_feed(self.feed)
        self.assertEqual(len(items), 2, "没标题的条目不该上屏")

    def test_strips_source_bracket_prefix(self):
        items = KS.parse_feed(self.feed)
        self.assertEqual(items[0][0], "甲国通过新的预算法案")

    def test_hhmm_is_cut_from_pubdate_verbatim(self):
        """时间列直接抓 pubDate 原串里的 HH:MM（不转时区，混源时时间序可能非单调）。"""
        items = KS.parse_feed(self.feed)
        self.assertEqual(items[0][2], "01:05")
        self.assertEqual(items[1][2], "23:59")

    def test_source_falls_back_to_category(self):
        items = KS.parse_feed(self.feed)
        self.assertEqual(items[1][3], "源乙")

    def test_broken_xml_returns_empty_instead_of_raising(self):
        """feed 写坏了就 0 条 → render() 返回 1 且保留旧图，不把屏幕刷白。"""
        bad = os.path.join(self.tmp, "bad.xml")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("<rss><channel>")
        self.assertEqual(KS.parse_feed(bad), [])


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestParseSizes(unittest.TestCase):
    def test_multiple_sizes(self):
        self.assertEqual(KS.parse_sizes("1024x758,800x600"), [(1024, 758), (800, 600)])

    def test_tolerates_spaces_and_case(self):
        self.assertEqual(KS.parse_sizes(" 1024 X 758 "), [(1024, 758)])

    def test_bad_format_exits(self):
        """尺寸是 systemd 单元里写死的参数，写错就该启动那一下失败并被 journal 记下来。"""
        for bad in ("1024", "1024*758", "abcx758", ""):
            with self.assertRaises(SystemExit, msg=bad):
                KS.parse_sizes(bad)


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestFontScale(unittest.TestCase):
    def test_never_smaller_than_floor(self):
        """/758 的比例缩到很小的版面时字号要有下限，否则字会缩成一团墨点。"""
        self.assertGreaterEqual(KS._fs(758 / 2000, 26), 10)
        self.assertEqual(KS._fs(1.0, 22), 22)


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestWrap(unittest.TestCase):
    """折行：每次 Matterhorn 都是量宽的几何问题，用 _FakeDraw 把字体因素排除掉。"""

    def setUp(self):
        self.d = _FakeDraw()

    def test_short_text_stays_one_line(self):
        self.assertEqual(KS.wrap(self.d, "甲乙丙", None, 100, 2), ["甲乙丙"])

    def test_word_boundary(self):
        """英文单词不被拆一半；宽度按 10px/字算，'abc'(30) 放不进第二行就让位。"""
        lines = KS.wrap(self.d, "abc def", None, 50, 3)
        self.assertEqual(lines, ["abc", "def"])

    def test_content_is_preserved(self):
        text = "甲乙丙丁戊己庚辛壬癸的子丑寅卯"
        joined = "".join(KS.wrap(self.d, text, None, 50, 5))
        self.assertEqual(joined, text)

    def test_overwide_atom_is_hard_cut(self):
        """单个原子比一整行还宽时必须按字符切（长 URL 否则会画到画布外面）。"""
        lines = KS.wrap(self.d, "a" * 40, None, 100, 3)
        self.assertTrue(lines)
        for ln in lines:
            # 「…」也算一个字宽
            self.assertLessEqual(self.d.textlength(ln), 100 + 10)

    def test_max_lines_is_respected(self):
        """行数超限要出省略号，不能一直往下排把页脚顶出去。"""
        # 40 个互不相同的字：每行 10 字，放完 2 行还剩一半
        text = "".join(chr(0x4E00 + i) for i in range(40))
        lines = KS.wrap(self.d, text, None, 100, 2)
        self.assertEqual(len(lines), 2, "max_lines 是硬上限：调用方按行数预留高度")
        self.assertTrue(lines[-1].endswith("…"), lines)

    def test_cut_to_width_always_advances(self):
        """版面窄到连一个字都放不下时也要前进 1 个字符，否则调用方会死循环。"""
        head, rest = KS._cut_to_width(self.d, "甲乙丙", None, 1)
        self.assertTrue(head)
        self.assertEqual(head + rest, "甲乙丙")


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestFitTitle(unittest.TestCase):
    def setUp(self):
        self.d = _FakeDraw()

    def test_lonely_second_line_falls_back_to_one_line(self):
        """第二行只剩一个字（宽度 < 可用宽度 30%）时退成一行加省略号，宁可截一个字。"""
        lines = KS._fit_title(self.d, None, "甲乙丙丁戊己庚辛壬癸子", 100)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith("…"), lines)

    def test_short_title_unwrapped(self):
        self.assertEqual(KS._fit_title(self.d, None, "甲乙", 100), ["甲乙"])


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestBoard(unittest.TestCase):
    """画面本身：8bit 灰度 + 只画一遍列表。"""

    def setUp(self):
        self.items = [("甲国通过新的预算法案", "摘要甲", "01:05", "源甲"),
                      ("乙公司发布季度财报", "摘要乙", "23:59", "源乙")]
        self.addCleanup(setattr, KS, "_draw_list", KS._draw_list)

    def test_board_is_grayscale_and_sized(self):
        img = KS.draw_board(1024, 758, self.items, 1700000000)
        self.assertEqual(img.mode, "L", "e-ink 要 8bit 灰度，RGB 上去会偏色")
        self.assertEqual(img.size, (1024, 758))

    def test_draw_list_called_exactly_once(self):
        """这是本，文件存在的意义之一：曾经被调用两次（第二次传 items[1:]），
        两次都从同一个 y 起笔 → 除最后一行外每行双字叠印、整版不可读。"""
        calls = []
        original = KS._draw_list

        def spy(*a, **kw):
            calls.append(a)
            return original(*a, **kw)

        KS._draw_list = spy
        KS.draw_board(1024, 758, self.items, 1700000000)
        self.assertEqual(len(calls), 1, "_draw_list 被画了 %d 次" % len(calls))
        passed_items = [a for a in calls[0] if isinstance(a, list)]
        self.assertEqual(passed_items, [self.items], "应当一次画完整份，不能切片再画一遍")


@unittest.skipUnless(HAVE_PIL, "需要 Pillow：%s" % _IMPORT_ERR)
class TestRender(unittest.TestCase):
    """落盘流程：原子替换 kindle.png，池子空时保留旧图。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rss-render-")
        self.feed = os.path.join(self.tmp, "feed.xml")
        self.out = os.path.join(self.tmp, "kindle")
        with open(self.feed, "w", encoding="utf-8") as f:
            f.write(FEED)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_first_size_as_default_png(self):
        self.assertEqual(KS.render(self.feed, self.out, [(400, 300), (200, 150)]), 0)
        for name in ("kindle-400x300.png", "kindle-200x150.png", "kindle.png"):
            self.assertTrue(os.path.isfile(os.path.join(self.out, name)), name)
        self.assertFalse(os.path.exists(os.path.join(self.out, "kindle.png.tmp")),
                         "临时文件没 replace 干净")

    def test_png_magic(self):
        """设备端 wget 到半张图会直接画失败，所以必须是完整 PNG。"""
        KS.render(self.feed, self.out, [(400, 300)])
        with open(os.path.join(self.out, "kindle.png"), "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_empty_pool_keeps_previous_png(self):
        old = os.path.join(self.out, "kindle.png")
        os.makedirs(self.out)
        KS.render(self.feed, self.out, [(400, 300)])
        with open(old, "rb") as f:
            before = f.read()
        with open(self.feed, "w", encoding="utf-8") as f:
            f.write("<rss><channel><title>t</title></channel></rss>")
        self.assertEqual(KS.render(self.feed, self.out, [(400, 300)]), 1)
        with open(old, "rb") as f:
            self.assertEqual(f.read(), before, "池子空时不该覆盖旧图")


if __name__ == "__main__":
    unittest.main(verbosity=2)
