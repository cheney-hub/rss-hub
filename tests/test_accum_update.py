"""Pi/accum_update.py 的离线测试：验证「每 5 分钟把 feed 并入当日滚动池」的编排。

不联网、不调 CF；用临时 feed.xml + 临时 ai-accum.json 路径，断言：
  * 首次合并：池子装上 feed 的全部条目（去重后）
  * 幂等：再跑一次不增不减（无重复）
  * 上限：feed 超过 AI_ACCUM_CAP 时池子被截到上限
  * 零点重置：存储日期不是今天时，旧池被丢弃、只留当天 feed
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Pi"))
import accum_update as AU  # noqa: E402
import ai_digest as AD  # noqa: E402


def _feed_xml(items):
    out = ['<?xml version="1.0"?><rss><channel>']
    for it in items:
        out.append(
            "<item><title>%s</title><link>%s</link><description>%s</description>"
            "<pubDate>%s</pubDate><source>%s</source></item>"
            % (it["title"], it["link"], it.get("desc", ""), it.get("pub", ""),
               it.get("src", "S"))
        )
    out.append("</channel></rss>")
    return "".join(out)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestAccumUpdate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.feed = os.path.join(self.tmp, "feed.xml")
        self.accum = os.path.join(self.tmp, "ai-accum.json")
        self.patcher = mock.patch.dict(os.environ, {
            "FEED_XML": self.feed,
            "AI_ACCUM_PATH": self.accum,
        })
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _items(self, n, prefix="t"):
        return [{"title": "%s%d" % (prefix, i), "link": "http://x/%s%d" % (prefix, i),
                 "desc": "d%d" % i, "pub": "Sat, 7 Nov 2026 10:0%d:00 +0800" % (i % 10),
                 "src": "S"} for i in range(n)]

    def _read(self):
        with open(self.accum, encoding="utf-8") as f:
            return json.load(f)

    def test_fresh_merge_loads_all(self):
        _write(self.feed, _feed_xml(self._items(7)))
        self.assertEqual(AU.main(), 0)
        d = self._read()
        self.assertEqual(d["date"], AD._today_local())
        self.assertEqual(len(d["items"]), 7)

    def test_idempotent_no_duplicates(self):
        _write(self.feed, _feed_xml(self._items(9)))
        AU.main()
        first = self._read()
        AU.main()  # 再跑一次，应幂等
        second = self._read()
        self.assertEqual(len(first["items"]), 9)
        self.assertEqual(len(second["items"]), 9)
        links = [it["link"] for it in second["items"]]
        self.assertEqual(len(links), len(set(links)), "出现重复条目")

    def test_cap_respected(self):
        # 临时把上限压到 5，喂 12 条，池子应被截到 5
        with mock.patch.object(AD, "ACCUM_CAP", 5):
            _write(self.feed, _feed_xml(self._items(12)))
            AU.main()
        d = self._read()
        self.assertLessEqual(len(d["items"]), 5)

    def test_midnight_reset_drops_old_pool(self):
        # 预置一份「昨天」的池子（含 3 条），再合并今天的 feed（4 条），
        # 因为存储日期不是今天，_load_accum 返回空 —— 池子重攒为今天的 4 条。
        old = {"date": "2000-01-01", "items": [
            {"title": "old%d" % i, "link": "http://old/%d" % i,
             "source": "S", "summary": "", "date_raw": ""} for i in range(3)]}
        with open(self.accum, "w", encoding="utf-8") as f:
            json.dump(old, f)
        _write(self.feed, _feed_xml(self._items(4)))
        AU.main()
        d = self._read()
        self.assertEqual(d["date"], AD._today_local())
        self.assertEqual(len(d["items"]), 4)
        self.assertTrue(all("old" not in it["title"] for it in d["items"]))

    def test_empty_feed_is_noop(self):
        # feed 不存在时不应写文件、不应崩
        os.remove(self.feed) if os.path.exists(self.feed) else None
        self.assertEqual(AU.main(), 0)
        self.assertFalse(os.path.exists(self.accum))

    def test_cap_zero_means_unlimited(self):
        # cap=0（默认）时不截断：喂 30 条全部保留，而非被截到某个上限
        with mock.patch.object(AD, "ACCUM_CAP", 0):
            _write(self.feed, _feed_xml(self._items(30)))
            AU.main()
        d = self._read()
        self.assertEqual(len(d["items"]), 30)

    def test_cap_zero_keeps_growing_all_day(self):
        # 模拟跨多个节拍累积（不限时 union 持续增长，不被丢最旧）
        with mock.patch.object(AD, "ACCUM_CAP", 0):
            for batch in range(3):
                _write(self.feed, _feed_xml(self._items(10, prefix="b%d" % batch)))
                AU.main()
        d = self._read()
        self.assertEqual(len(d["items"]), 30)  # 3×10 全留，无截断


if __name__ == "__main__":
    unittest.main()
