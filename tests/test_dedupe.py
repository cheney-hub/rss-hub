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
        「一直在显示」就是这个。修法是**保留数字** —— 门槛不用动：归一化后 9 字，
        8 字门槛照样拦得住（门槛试过降到 6，实测零收益，见 `DEDUP_TITLE_MIN` 的注释）。
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
        """门槛上下界各留一条：**8 字判重 / 7 字不判重**（现 `DEDUP_TITLE_MIN = 8`）。

        为什么两条都得在：只留「够长 → 判重」那半边，把门槛偷偷调到 1（一律判重）
        测试照样绿；只留「够短 → 放行」那半边，把门槛调到 99（永不判重）也绿。
        本项目栽过两次（`ROTATE_MAX_LAG`、`sim --seed`），都是参数改了没人能证明。
        所以下面两条不写死数字，而是断言这组语料**正好卡在门槛两侧**：门槛一改，
        这里立刻红，逼着改的人连同语料一起改。
        """
        # 上界：8 字（= 门槛）→ 参与判重
        long_a = mk("A股迎来开门红啦", link="http://x/1", src="a")
        long_b = mk("A股迎来开门红啦", link="http://x/2", src="b")
        self.assertEqual(len(R.norm_title(long_a["title"])), R.DEDUP_TITLE_MIN,
                         "这条语料要正好等于门槛长度，否则边界就名不副实")
        out, dropped = R.dedupe([long_a, long_b])
        self.assertEqual(len(out), 1, "够到门槛的同题标题要并成一条")
        self.assertEqual(dropped, 1)

        # 下界：7 字（门槛 - 1）→ 放行。挡的是「门槛被调低」：
        # 降到 6 实测一条都没多丢，但会把「主力资金流向」「融资融券余额」这类
        # 栏目型短标题（归一化 6~7 字）在不同事件撞名时并成一条。
        short_a = mk("A股迎来开门红", link="http://y/1", src="c")
        short_b = mk("A股迎来开门红", link="http://y/2", src="d")
        self.assertEqual(len(R.norm_title(short_a["title"])), R.DEDUP_TITLE_MIN - 1,
                         "这条语料要正好比门槛短一个字")
        out2, dropped2 = R.dedupe([short_a, short_b])
        self.assertEqual(len(out2), 2, "够不着门槛的短标题不能互相误杀")
        self.assertEqual(dropped2, 0)

    def test_long_titles_are_compared_beyond_first_20_chars(self):
        """长标题的分歧落在第 21 字之后时，仍需分成两条（键取前 `NORM_TITLE_MAX` 字）。

        只比前 20 字的话，「…增仓…升至 5.2%」和「…降至 4.2%」会被当成同一条丢掉一条。
        键加长是纯收紧方向：只会让撞键更难，不会新增误并。
        """
        a = mk("北向资金本周增持宁德时代持股比例由4.1%升至5.2%创近三个月新高",
               link="http://x/1", src="a")
        b = mk("北向资金本周增持宁德时代持股比例由4.1%降至4.2%创近三个月新高",
               link="http://x/2", src="b")
        out, dropped = R.dedupe([a, b])
        self.assertEqual(len(out), 2, "分歧在后半句的两条长标题不能合并")
        self.assertEqual(dropped, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
