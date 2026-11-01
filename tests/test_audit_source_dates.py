"""源时间戳体检的判据测试（stdlib unittest，零第三方依赖，不联网）。

测的只是「怎么下结论」这部分（纯函数），抓取留给真机。每条用例都自带反例：
把注入的数据改动一两处，结论必须跟着变 —— 否则这条检查形同虚设。

跑法：
    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import arrival  # noqa: E402  （共用「几条以上才算」这个门槛）
import audit_source_dates as A  # noqa: E402

NOW = 1_700_000_000
MIN = arrival.MIN_ITEMS


def batch(n, ts=NOW - 600, key=lambda i: "k%d" % i):
    """造一批时间戳相同的条目；每条身份不同。"""
    return [(key(i), ts) for i in range(n)]


def varied(n, base=NOW - 600):
    return [("k%d" % i, base - i * 60) for i in range(n)]


class TestSharedStamp(unittest.TestCase):
    def test_all_same_is_shared(self):
        self.assertTrue(A.shared_stamp(batch(5)))

    def test_varied_is_not_shared(self):
        self.assertFalse(A.shared_stamp(varied(5)))

    def test_too_few_items_to_say(self):
        # 一两条同秒是常事，证据不足不能判「共用」
        self.assertFalse(A.shared_stamp(batch(MIN - 1)))
        self.assertTrue(A.shared_stamp(batch(MIN)))


class TestClassify(unittest.TestCase):
    def test_normal_source(self):
        verdict, notes = A.classify([varied(5)], now=NOW)
        self.assertEqual(verdict, "正常")
        self.assertEqual(notes, [])

    def test_shared_stamp_source(self):
        verdict, notes = A.classify([batch(5)], now=NOW)
        self.assertEqual(verdict, "共用时间戳")
        self.assertTrue(any("共用一个时间戳" in n for n in notes))

    def test_restamped_across_samples(self):
        # 同一条在两次采样之间被改了时间 —— 只有两次采样才看得出来
        a = varied(5)
        b = [("k0", NOW - 60)] + varied(5)[1:]
        verdict, notes = A.classify([a, b], now=NOW)
        self.assertEqual(verdict, "逐条改时间")
        self.assertTrue(any("时间戳在两次采样之间变了" in n for n in notes))

    def test_stable_across_samples_is_normal(self):
        self.assertEqual(A.classify([varied(5), varied(5)], now=NOW)[0], "正常")

    def test_future_dated_source(self):
        future = [(("k%d" % i), NOW + 3600 * (5 - i)) for i in range(5)]
        verdict, notes = A.classify([future], now=NOW)
        self.assertEqual(verdict, "未来时间")
        self.assertTrue(any("顶到未来" in n for n in notes))

    def test_future_wins_over_shared_stamp(self):
        # 既共用时间戳又在未来的，报更危险的那个
        self.assertEqual(A.classify([batch(5, ts=NOW + 7200)], now=NOW)[0], "未来时间")

    def test_shared_stamp_wins_over_restamped(self):
        a = batch(5, ts=NOW - 600)
        b = batch(5, ts=NOW - 60)
        verdict, notes = A.classify([a, b], now=NOW)
        self.assertEqual(verdict, "共用时间戳")
        self.assertTrue(any("重盖章" in n for n in notes), "第二个问题要留在备注里")

    def test_no_pubdate_at_all(self):
        self.assertEqual(A.classify([batch(5, ts=0)], now=NOW)[0], "无时间")

    def test_small_clock_skew_is_tolerated(self):
        # 源站与本机差几十秒很常见，不该报未来时间
        skewed = [("k%d" % i, NOW + 60 - i) for i in range(5)]
        self.assertEqual(A.classify([skewed], now=NOW)[0], "正常")

    def test_unreachable_source(self):
        self.assertEqual(A.classify([None, None], now=NOW)[0], "取不到")

    def test_one_good_sample_is_enough(self):
        # 有一轮抓到了就够下结论，不能因为另一轮失败就判「取不到」
        self.assertEqual(A.classify([None, varied(5)], now=NOW)[0], "正常")


if __name__ == "__main__":
    unittest.main(verbosity=2)
