"""Pi/rotate_screen.py 的行为测试。

这个脚本是「显示时间线」的执行者：它每天被 timer 拉起 288 次，所以踩过的坑都在这三处：

1. **写盘次数**：池子没换血时算出来的正文逐字节相同，就不该落盘（每天 288 次写 SD 卡，
   而且 feed-wrap.xml 的 mtime 会被下游当「有新内容」看）。比较时必须摘掉
   `<lastBuildDate>`——`build_rss` 每次都把它改成「现在」，直接比全文永远不会相等。
2. **状态坏了要能自愈**：rotate.json 写坏了宁可重开一轮重复一条，也不能整条时间线停住。
3. **地址不写死**：设备版地址由完整版的 `<link>` 推导，写死就会两处漂移。

不联网，全部落在临时目录里。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "Pi"))

import render                                            # noqa: E402
import rotate                                            # noqa: E402
import rotate_screen as RS                               # noqa: E402

# 一份够用的完整版：两条带 <source>、一条缺 participants 字段、一条时间坏掉
SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>RSS 头条</title>
<link>https://FEED_HOST/rss/feed.xml</link>
<description>x</description>
<item>
  <title>甲国通过新的预算法案</title>
  <link>http://host-a/1</link>
  <description>摘要甲</description>
  <pubDate>Sat, 31 Oct 2026 01:05:00 GMT</pubDate>
  <source url="http://host-a/feed">源甲</source>
</item>
<item>
  <title>乙公司发布季度财报</title>
  <link>http://host-b/2</link>
  <description>摘要乙</description>
  <pubDate>Sat, 31 Oct 2026 00:30:00 GMT</pubDate>
  <source url="http://host-b/feed">源乙</source>
</item>
<item>
  <title>没有 source 节点的条目</title>
  <link>http://host-c/3</link>
  <description/>
  <pubDate>不是个日期</pubDate>
</item>
</channel></rss>
"""


class TestParseFeedXml(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rss-rotate-")
        self.feed = os.path.join(self.tmp, "feed.xml")
        with open(self.feed, "w", encoding="utf-8") as f:
            f.write(SAMPLE)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_items_and_channel_link(self):
        items, feed_url = RS.parse_feed_xml(self.feed)
        self.assertEqual(len(items), 3)
        self.assertEqual(feed_url, "https://FEED_HOST/rss/feed.xml")
        self.assertEqual(items[0]["title"], "甲国通过新的预算法案")
        self.assertEqual(items[0]["src_name"], "源甲")
        self.assertEqual(items[0]["src"], "http://host-a/feed")
        self.assertEqual(items[0]["summary"], "摘要甲")

    def test_missing_source_does_not_crash(self):
        """<source> 缺了要给空串，不能报 KeyError / AttributeError——一个字段缺失不该让整份 feed 解析失败。"""
        items, _ = RS.parse_feed_xml(self.feed)
        third = items[2]
        self.assertEqual(third["src"], "")
        self.assertEqual(third["src_name"], "")
        self.assertEqual(third["summary"], "")

    def test_bad_pubdate_falls_to_zero(self):
        """时间解析不出来就 ts=0（落到池子最末），不能让整份 feed 解析失败。"""
        items, _ = RS.parse_feed_xml(self.feed)
        self.assertEqual(items[2]["ts"], 0)
        self.assertGreater(items[0]["ts"], items[1]["ts"])


class TestWrapUrl(unittest.TestCase):
    def test_sibling_of_feed(self):
        self.assertEqual(
            RS.wrap_url_of("https://FEED_HOST/rss/feed.xml"),
            "https://FEED_HOST/rss/feed-wrap.xml")

    def test_empty_in_empty_out(self):
        """拿不到 <link> 时返回空串，让调用方自己判断，别凭空造一个 rss URL。"""
        self.assertEqual(RS.wrap_url_of(""), "")

    def test_no_double_slash_when_at_root(self):
        self.assertEqual(RS.wrap_url_of("https://HOST/feed.xml"),
                         "https://HOST/feed-wrap.xml")


class TestWriteIfChanged(unittest.TestCase):
    """正文一样就不落盘——这条直接决定 SD 卡的擦写次数与下游对「有新内容」的判断。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rss-write-")
        self.path = os.path.join(self.tmp, "feed-wrap.xml")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_write_happens(self):
        self.assertTrue(RS._write_if_changed(self.path, "<rss/>"))

    def test_same_content_is_not_written_again(self):
        RS._write_if_changed(self.path, "<rss/>")
        self.assertFalse(RS._write_if_changed(self.path, "<rss/>"))

    def test_only_timestamp_changed_counts_as_unchanged(self):
        """build_rss 每次都重写 lastBuildDate；只变了它不该算「内容变了」。

        不摘这个字段的话，跳过写入的优化一次都不会命中（等于没有），
        而且 mtime 会一直往前跑。
        """
        a = "<rss><channel><lastBuildDate>D1</lastBuildDate></channel></rss>"
        b = "<rss><channel><lastBuildDate>D2</lastBuildDate></channel></rss>"
        self.assertTrue(RS._write_if_changed(self.path, a))
        self.assertFalse(RS._write_if_changed(self.path, b))

    def test_real_content_change_is_written(self):
        RS._write_if_changed(self.path, "<rss>A</rss>")
        self.assertTrue(RS._write_if_changed(self.path, "<rss>B</rss>"))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "<rss>B</rss>")

    def test_write_is_atomic(self):
        """写 .tmp 再 os.replace：读到一半的 XML 会让设备端解析失败、屏幕停在上一版。"""
        RS._write_if_changed(self.path, "<rss/>")
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_unreadable_file_still_writes(self):
        """文件不存在是常态（第一次跑），不能因为读不到就不写。"""
        missing = os.path.join(self.tmp, "nope", "x.xml")
        os.makedirs(os.path.dirname(missing))
        self.assertTrue(RS._write_if_changed(missing, "<rss/>"))


class TestLoadState(unittest.TestCase):
    """rotate.json 是发布出去的产物，被人手改过 / 写坏过都要能撑住。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rss-state-")
        self.state = os.path.join(self.tmp, "rotate.json")
        self.addCleanup(setattr, RS, "STATE", RS.STATE)
        RS.STATE = self.state

    def _w(self, text):
        with open(self.state, "w", encoding="utf-8") as f:
            f.write(text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_dict(self):
        self._w('{"window": 5, "shown": ["a"]}')
        self.assertEqual(RS.load_state()["window"], 5)

    def test_broken_json_is_empty_state(self):
        self._w("{not json")
        self.assertEqual(RS.load_state(), {})

    def test_non_dict_is_empty_state(self):
        """JSON 里存了列表（写坏的典型形态）也要退化成空状态，而不是后面 AttributeError。"""
        self._w('[1, 2, 3]')
        self.assertEqual(RS.load_state(), {})

    def test_missing_file_is_empty_state(self):
        self.assertEqual(RS.load_state(), {})


class TestWrapDescription(unittest.TestCase):
    def test_delegates_to_render(self):
        """这段文案以前在 build_feed 与本脚本各写一遍、算法还不一样。现在只有一个真源。"""
        for n in (0, 1, 41):
            self.assertEqual(RS.wrap_description(n), render.device_description(n))


class TestMainRound(unittest.TestCase):
    """跑一轮真正的 main()：产一份单条 feed-wrap.xml + 一份 rotate.json。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rss-main-")
        with open(os.path.join(self.tmp, "feed.xml"), "w", encoding="utf-8") as f:
            f.write(SAMPLE)
        self.addCleanup(setattr, RS, "FEED", RS.FEED)
        self.addCleanup(setattr, RS, "WRAP_OUT", RS.WRAP_OUT)
        self.addCleanup(setattr, RS, "STATE", RS.STATE)
        self.addCleanup(setattr, rotate, "time", rotate.time)
        RS.FEED = os.path.join(self.tmp, "feed.xml")
        RS.WRAP_OUT = os.path.join(self.tmp, "feed-wrap.xml")
        RS.STATE = os.path.join(self.tmp, "rotate.json")
        # 冻结时钟：裁窗边界上抖起来的问题最难查，用例要给确定答案
        # 取样本里最新一条之后的时间：这样三条都能进候选，选中的应当是「甲」
        now = int(datetime(2026, 10, 31, 1, 10, tzinfo=timezone.utc).timestamp())
        Now = type("Now", (), {"time": staticmethod(lambda: now)})
        rotate.time = Now()
        self.now = now

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_exactly_one_item(self):
        import xml.etree.ElementTree as ET
        self.assertEqual(RS.main(), 0)
        root = ET.parse(RS.WRAP_OUT).getroot()
        self.assertEqual(len(root.findall("./channel/item")), 1)
        self.assertEqual(root.findtext("./channel/title"), RS.CHANNEL)
        self.assertEqual(root.findtext("./channel/item/title"), "甲国通过新的预算法案",
                         "这一窗应当上屏池子里最新的一条")
        self.assertTrue(os.path.isfile(RS.STATE))

    def test_same_window_does_not_advance(self):
        """同一窗重复跑不推进：这是屏幕上「5 分钟换一条」而不是「每轮都换」的关键。"""
        RS.main()
        with open(RS.WRAP_OUT, encoding="utf-8") as f:
            first = f.read()
        RS.main()
        with open(RS.WRAP_OUT, encoding="utf-8") as f:
            self.assertEqual(first, f.read())

    def test_empty_pool_keeps_previous_version(self):
        """池子空时保持上一版不动（退出码 1），不能把屏幕刷成空白。"""
        with open(RS.FEED, "w", encoding="utf-8") as f:
            f.write("<rss><channel><title>t</title></channel></rss>")
        self.assertEqual(RS.main(), 1)

    def test_state_is_valid_json_with_cursor(self):
        RS.main()
        with open(RS.STATE, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["window"], int(self.now // 300))
        self.assertTrue(state["shown"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
