import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_feed as B
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 抓取测试用无域名夹具（项目惯例：http://x/1 这种不触发「硬编码外链」门禁），
# 靠片段匹配决定 mock 成功/失败，与真实域名无关。
PRIMARY = "https://primary/163/today"
BACKUP = "https://backup/163/today"


class TestFallbackParse(unittest.TestCase):
    def test_four_sources_have_fallbacks(self):
        es = B.read_feed_entries(os.path.join(ROOT, "feeds.txt"))
        by_name = {e["name"]: e for e in es}
        # feeds.txt 里手写了 4 个公共源带备用实例（央视要闻/国际、一财要闻、网易今日关注）
        self.assertEqual(len([e for e in es if e.get("fallbacks")]), 4)
        netease = by_name["网易新闻·今日关注"]
        self.assertEqual(len(netease["fallbacks"]), 1)
        # 备用地址是附属字段，不应被当成独立源计入条目
        urls = [e["url"] for e in es]
        self.assertNotIn(netease["fallbacks"][0], urls)

    def test_every_entry_has_fallbacks_key(self):
        es = B.read_feed_entries(os.path.join(ROOT, "feeds.txt"))
        for e in es:
            self.assertIn("fallbacks", e, "每条目都应有 fallbacks 键（可为空列表）")


def _fake_get(side):
    """side: {url 片段: 是否成功}；未命中的地址默认成功。"""
    def fake(url):
        for frag, ok in side.items():
            if frag in url:
                if ok:
                    xml = (b'<rss><channel><item><title>t</title><link>l</link>'
                           b'<pubDate>2026-11-10T21:00:00+08:00</pubDate>'
                           b'</item></channel></rss>')
                    return (200, xml, None)
                raise RuntimeError("boom")
        return (200, b'<rss><channel></channel></rss>', None)
    return fake


class TestFallbackFetch(unittest.TestCase):
    def test_primary_ok_uses_primary(self):
        e = {"name": "n", "url": PRIMARY, "fallbacks": [BACKUP]}
        with patch.object(B, "http_get", _fake_get({"primary": True})):
            items, counts = B._collect([e], {})
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(len(items), 1)
        # 无论实际从哪取，回退键 / 展示都统一用主地址
        self.assertEqual(items[0]["src"], PRIMARY)

    def test_primary_fails_fallback_used(self):
        e = {"name": "网易", "url": PRIMARY, "fallbacks": [BACKUP]}
        with patch.object(B, "http_get",
                          _fake_get({"primary": False, "backup": True})):
            items, counts = B._collect([e], {})
        self.assertEqual(counts["ok"], 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["src"], PRIMARY)

    def test_all_fail_returns_none(self):
        e = {"name": "网易", "url": PRIMARY, "fallbacks": [BACKUP]}
        with patch.object(B, "http_get",
                          _fake_get({"primary": False, "backup": False})):
            items, counts = B._collect([e], {})
        self.assertEqual(counts["ok"], 0)
        self.assertEqual(counts["fail"], 1)
        self.assertEqual(len(items), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
