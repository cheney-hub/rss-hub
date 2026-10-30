"""跨源「同题去重」的用例（stdlib unittest，零第三方依赖，不联网）。

为什么单独一个文件：这三条是 1.7.1 修线上缺陷时加的（Pi 线上 120 条里 12 条
「屏上前 13 字」撞车，真因是 `norm_title()` 删数字 + 8 字门槛放过了三源同稿），
加进 `test_core.py` 之后它越过 750 行的拆分提醒线；而它们守的是一整块独立口径
（标题归一化判重），跟 test_core 里那些按链接身份判重的用例不是一回事，
拆出来之后两边都能单独读。

和 test_core.py 的 `TestDedupe` 的分工：
  * test_core.py  —— 链接身份（`_link_key`）：锚点、追踪参数、有意义的参数、参数顺序；
  * 本文件        —— 标题身份（`norm_title`）：同题三源、数字是否参与身份、字数门槛上下界。

跑法：
    python -m unittest tests.test_dedupe -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import render as R      # noqa: E402  （dedupe / norm_title 住在渲染层）


def mk(title, link=None, src="s", ts=1000):
    """造一条条目。link 不传时用标题兜底，所以「同标题不同源」必须显式给 link。"""
    return {"title": title, "link": link or "http://x/" + title, "src": src, "ts": ts}


class TestTitleDedupe(unittest.TestCase):
    """标题归一化判重：同一条新闻被几家源转发时，屏上只该出现一次。"""

    def test_same_headline_three_sources(self):
        """同一条快讯被三个源转发（标题相同、链接不同）→ 只留最新那条，dropped=2。

        2026-10-31 从 Pi 线上那份 120 条里实测出来的：`norm_title()` 当时会把数字删掉，
        「国际油价30日上涨」归一化后只剩「国际油价日上涨」= 7 字，够不着当时的 8 字门槛，
        于是雪球 / 财联社 / 金十三家的同一条新闻**同时上了屏** —— 用户看到的
        「一直在显示」就是这个。修法是保留数字（`DEDUP_TITLE_MIN` 同时降到 6）。
        """
        items = [
            mk("国际油价30日上涨", link="http://a/1", src="a", ts=300),
            mk("国际油价30日上涨", link="http://b/2", src="b", ts=200),
            mk("国际油价30日上涨", link="http://c/3", src="c", ts=100),
        ]
        out, dropped = R.dedupe(items)
        self.assertEqual(len(out), 1, "三源转发同一条新闻只该留一条")
        self.assertEqual(dropped, 2)
        self.assertEqual(out[0]["src"], "a", "保留先到的那条（items 已按时间倒序）")

    def test_date_in_headline_distinguishes_stories(self):
        """30 日 vs 31 日必须分成两条 ——「保留数字」这条护栏的反向用例。

        口径类改动必须有「不同取值 → 不同结果」的那一条：只锁「去重成功」的话，
        把 `norm_title` 粗暴改成「一律判重」也能通过。本项目栽过两次
        （`ROTATE_MAX_LAG`、`sim --seed` 都是没有反向断言的假旋钮 / 被忽略的参数）。
        """
        out, dropped = R.dedupe([
            mk("国际油价30日上涨", link="http://a/1", src="a"),
            mk("国际油价31日上涨", link="http://b/2", src="b"),
        ])
        self.assertEqual(len(out), 2, "不同日期是两条不同的新闻")
        self.assertEqual(dropped, 0)
        self.assertIn("30", R.norm_title("国际油价30日上涨"), "数字必须留在去重键里")

    def test_title_threshold_boundary(self):
        """门槛降到 6 字之后的上下边界（常量 `DEDUP_TITLE_MIN`，别再写死 8）。"""
        long_a = mk("A股迎来开门红", link="http://x/1", src="a")      # 归一化 7 字：参与判重
        long_b = mk("A股迎来开门红", link="http://x/2", src="b")
        out, dropped = R.dedupe([long_a, long_b])
        self.assertEqual(len(out), 1, "≥6 字的同题标题要并成一条")
        self.assertEqual(dropped, 1)

        short_a = mk("港股收盘", link="http://y/1", src="c")          # 4 字：不参与判重
        short_b = mk("港股收盘", link="http://y/2", src="d")
        out2, dropped2 = R.dedupe([short_a, short_b])
        self.assertEqual(len(out2), 2, "极短标题不能互相误杀")
        self.assertEqual(dropped2, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
