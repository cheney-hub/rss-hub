"""设备屏排版的两组行为测试：宽度折算 + 标题切分。

这两件事<｜hy_place▁holder▁no▁813｜>错了不会报错，只会「屏幕上难看一点」：标题少几个字、数字被切成两半、
末尾留个逗号。设备端一行只有 13 个全角宽，切点由 `render.split_title()` 决定，
所以这里锁的是「切点规则」，不是具体某条新闻长什么样。

从 tests/test_core.py 拆出来：那边塞了太多主题，单一文件 763 行不好定位。

跑法：
    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import render as R  # noqa: E402  （被测对象是渲染层的纯函数）


class TestWidth(unittest.TestCase):
    def test_fullwidth(self):
        # 真机实测：13 个全角字顶格，第 14 个字被云端切掉
        self.assertEqual(R.text_width("卡特彼勒第三季度电力与能源"), 13.0)

    def test_halfwidth(self):
        self.assertEqual(R.text_width("abcd"), 2.0)
        self.assertEqual(R.text_width("20.15"), 2.5)


class TestSplitTitle(unittest.TestCase):
    def test_head_never_exceeds_screen(self):
        t = "微软启动2017年以来首次美国高评级债券发行，规模预计超过百亿美元"
        head, tail = R.split_title(t)
        self.assertLessEqual(R.text_width(head), 13.0)
        self.assertTrue(head)
        self.assertTrue(tail)

    def test_does_not_cut_number(self):
        # 曾经把「20|15亿元」切成两半，小数点被剥掉后只剩「15亿元」
        head, tail = R.split_title("净亏损20.15亿元，同比由盈转亏")
        for s in (head, tail):
            self.assertNotRegex(s, r"\d\.$|^\.\d")

    def test_head_has_no_trailing_comma(self):
        head, _ = R.split_title("万事达(MA.N)盘前上涨1.8%，第三季度业绩超出预期。")
        self.assertFalse(head.endswith(("，", "、", "；")))

    def test_tail_has_no_leading_punctuation(self):
        _, tail = R.split_title("瑞士政府已作出评估，即如果瑞银将总部迁走会怎样")
        self.assertFalse(tail.startswith(("，", "、", "；", "。")))

    def test_does_not_orphan_number_unit(self):
        """数字与量词不能被切成两行：「…超3」+「亿条」读起来像断了。"""
        head, tail = R.split_title("日本今年前10月外泄信息超3亿条 或系AI无差别攻击")
        self.assertFalse(head.endswith(("3", "1", "2")))
        self.assertTrue(tail.startswith("3亿条"), tail)

    def test_does_not_split_unit_from_number(self):
        """「…亏损2.59亿」+「元」同样要退：「亿元」是一个整体。"""
        head, tail = R.split_title("大家养老前三季度亏损2.59亿元，累计净资产收益率-8.30%")
        self.assertFalse(head.endswith(("亿", "万")))
        self.assertTrue(tail.startswith("2.59亿元"), tail)
        self.assertFalse(head.endswith("-"), "减号不该留在行尾")

    def test_short_title_kept_whole(self):
        head, tail = R.split_title("港股收盘")
        self.assertEqual(head, "港股收盘")
        self.assertEqual(tail, "")

    def test_source_name_tail_still_stripped(self):
        """「标题 - 来源名」照旧剥掉：来源已经由 <source> 显示，重复占宽度。"""
        head, tail = R.split_title("卡特彼勒第三季度电力与能源业务 - 财联社")
        self.assertNotIn("财联社", head + tail)
        self.assertTrue(head.startswith("卡特彼勒"))

    def test_number_tail_is_not_a_source_name(self):
        """尾巴带数字时不能剥：那是标题的一部分（年份 / 编号），不是来源名。

        原先的正则把「苹果发布会-2024」的「-2024」当来源名吃掉，
        屏幕上直接少一个年份，且没有任何报错。
        """
        for title, keep in (("苹果发布会-2024", "2024"),
                            ("财报速递 | 2024", "2024"),
                            ("某公司公告_123", "123")):
            head, tail = R.split_title(title)
            self.assertIn(keep, head + tail, "%s 里的 %s 被剥掉了" % (title, keep))


if __name__ == "__main__":
    unittest.main(verbosity=2)
