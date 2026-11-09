"""时间戳失真源的排序修正（stdlib unittest，零依赖、不联网）。

要防的错是两类，都很难在现场看出来：
  ① 把正常源误判成失真源 —— 条目凭空变旧，可能从池子里消失；
  ② 改了排序用的时间戳却没改写进产物那个字符串 —— 下一轮读回来又是源标称的时钟，
     记忆链断掉，等于白改（`_round_trip` 那几条就是盯这个的）。

跑法：
    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arrival as A  # noqa: E402
import build_feed as B  # noqa: E402


def mk(title, src="https://src/%d.xml", ts=1000):
    """一条最小可用条目。src 默认按标题不同而不同，需要同源时显式传同一个值。"""
    url = src % hash(title) if "%d" in src else src
    return {"title": title, "link": "https://x/" + title, "ts": ts,
            "date_raw": "n/a", "src": url, "src_name": "测试源"}


def group(src, n, ts):
    return [mk("t%d" % i, src=src, ts=ts) for i in range(n)]


class TestClockStampedSources(unittest.TestCase):
    """哪些源算「时间戳失真」：整份条目共用一个时间戳，且条目数够得上判断。"""

    def test_shared_timestamp_is_flagged(self):
        self.assertEqual(A.clock_stamped_sources(group("https://nk/rss", 5, 900)),
                         {"https://nk/rss"})

    def test_distinct_timestamps_are_untouched(self):
        items = [mk("t%d" % i, src="https://good/rss", ts=900 + i) for i in range(5)]
        self.assertEqual(A.clock_stamped_sources(items), set(),
                         "时间戳逐条不同的源被误判成失真源")

    def test_one_second_apart_is_still_normal(self):
        # 差一秒就说明源在逐条标注发布时间，不能因为「这批凑巧很集中」就归成失真
        items = [mk("t%d" % i, src="https://burst/rss", ts=900 + i) for i in range(5)]
        self.assertEqual(A.clock_stamped_sources(items), set())

    def test_too_few_items_to_judge(self):
        # 只有 1~2 条时本来就可能同秒，证据不足不动它
        for n in (1, 2):
            with self.subTest(n=n):
                self.assertEqual(
                    A.clock_stamped_sources(group("https://tiny/rss", n, 900)), set())
        self.assertEqual(A.clock_stamped_sources(group("https://tiny/rss", A.MIN_ITEMS, 900)),
                         {"https://tiny/rss"})


class TestSettle(unittest.TestCase):
    """改成「首次见到时间」的那一步。"""

    def prev(self, src, titles, ts):
        return {src: [{"title": t, "link": "https://x/" + t, "ts": ts,
                       "src": src, "date_raw": "n/a"} for t in titles]}

    def test_keeps_earlier_first_seen(self):
        # 失真源的判据是「整批条目共用一个时间戳」，所以整个源也共用一个认定时间：
        # 上一轮没见过的那两条（新稿）同样拿到上一轮那个时钟，而不是本轮标称的 2000。
        # 只按条目记的话，源每轮换稿、新稿就拿到最新时刻 → 每轮都是它霸榜，
        # 那正是这个模块要治的现象（取舍见 prev_source_index 的注释）。
        items = group("https://nk/rss", 5, 2000)
        prev = self.prev("https://nk/rss", ["t0", "t1", "t2"], 1000)
        self.assertEqual(A.settle(items, prev), (5, 0))
        self.assertEqual([it["ts"] for it in items], [1000] * 5,
                         "整批都应回到上一轮认定的时间，而不是本轮标称")

    def test_item_that_fell_out_of_the_window_stays_old(self):
        """掉出产物窗口后重新入池的条目不能变年轻。

        产物只留 ITEM_LIMIT 条，条目一旦被裁出去，它的「首见时间」就随产物消失了。
        只有条目级记忆时，它下一轮带着源刷新的新时钟回来、又查不到旧记录，
        于是把新时钟当成首见时间永久锁定 —— 条目真的变年轻、排到全池最前，
        「怎么老是这条」隔几小时复发一次就是这么来的（用 ITEM_LIMIT=3 复现过）。
        """
        src = "https://nk/rss"
        # 上一轮产物里这个源只剩**另一条**（老条目本身已被裁掉）
        prev = {src: [{"title": "t9", "link": "https://x/t9", "ts": 1000,
                       "src": src, "date_raw": "n/a"}]}
        items = group(src, 3, 5000)          # 本轮源把整批刷成 5000
        self.assertEqual(A.settle(items, prev), (3, 0))
        self.assertTrue(all(it["ts"] == 1000 for it in items),
                        "老条目掉出窗口后重新入池，不能拿本轮的新时钟当首见时间")

    def test_entry_level_memory_wins_over_source_level(self):
        # 源级记忆只是兜底：条目自己有记录时必须用条目自己的（更精确的那个），
        # 不能被源里更早的另一条盖掉。
        src = "https://nk/rss"
        prev = {src: [
            {"title": "t0", "link": "https://x/t0", "ts": 3000, "src": src, "date_raw": "n/a"},
            {"title": "t1", "link": "https://x/t1", "ts": 1000, "src": src, "date_raw": "n/a"},
            {"title": "t2", "link": "https://x/t2", "ts": 1000, "src": src, "date_raw": "n/a"},
        ]}
        items = group(src, 3, 9000)
        A.settle(items, prev)
        by_title = {it["title"]: it["ts"] for it in items}
        self.assertEqual(by_title["t0"], 3000, "条目级记录优先于源级兜底")
        self.assertEqual(by_title["t1"], 1000)

    def test_rewrites_date_raw_so_next_round_can_remember(self):
        # 只改 ts 不改写出去的字符串，下一轮从产物读回来还是源标称的时钟 —— 记忆链断了
        items = group("https://nk/rss", 3, 2000)
        A.settle(items, self.prev("https://nk/rss", ["t0", "t1", "t2"], 1000))
        self.assertNotEqual(items[0]["date_raw"], "n/a")
        self.assertEqual(B._parse_date(items[0]["date_raw"]), 1000,
                         "改写后的 date_raw 必须能被自己解析回同一个时间戳")

    def test_never_makes_an_item_younger(self):
        # 源补发旧稿时，上一轮的时间比本轮标称更晚，取小值 —— 年龄只增不减
        items = group("https://nk/rss", 3, 1000)
        A.settle(items, self.prev("https://nk/rss", ["t0", "t1", "t2"], 2000))
        self.assertTrue(all(it["ts"] == 1000 for it in items))

    def test_normal_source_untouched(self):
        items = [mk("t%d" % i, src="https://good/rss", ts=2000 + i) for i in range(5)]
        prev = self.prev("https://good/rss", ["t0"], 1000)
        self.assertEqual(A.settle(items, prev), (0, 0))
        self.assertEqual([it["ts"] for it in items], [2000, 2001, 2002, 2003, 2004])

    def test_first_run_without_prev_is_noop(self):
        items = group("https://nk/rss", 5, 2000)
        self.assertEqual(A.settle(items, {}), (0, 0))
        self.assertTrue(all(it["ts"] == 2000 for it in items))

    def test_only_flagged_source_is_adjusted(self):
        items = group("https://nk/rss", 3, 5000) + \
            [mk("g%d" % i, src="https://good/rss", ts=3000 + i) for i in range(3)]
        prev = self.prev("https://nk/rss", ["t0", "t1", "t2"], 1000)
        prev.update(self.prev("https://good/rss", ["g0", "g1", "g2"], 1000))
        self.assertEqual(A.settle(items, prev), (3, 0))
        good = [it for it in items if it["src"] == "https://good/rss"]
        self.assertEqual([it["ts"] for it in good], [3000, 3001, 3002])

    def test_same_title_from_other_source_is_not_confused(self):
        # 身份是「链接 + 源」，换了个源的同名条目不能拿来当首见记录
        items = group("https://nk/rss", 3, 5000)
        prev = {"https://other/rss": [{"title": "t0", "link": "https://x/t0",
                                       "ts": 1000, "src": "https://other/rss"}]}
        self.assertEqual(A.settle(items, prev), (0, 0))

    def test_round_trip_two_rounds_in_a_row(self):
        """连跑两轮：时间必须稳定在第一轮那个值，不能跟着源一路往上漂。"""
        prev = {}
        for claimed in (1000, 7000, 9000):
            items = group("https://nk/rss", 3, claimed)
            A.settle(items, prev)
            prev = {"https://nk/rss": [dict(it) for it in items]}
        self.assertEqual(items[0]["ts"], 1000, "源每轮刷新时间戳，时间应停在首次见到")


class TestSettleNoSourceTime(unittest.TestCase):
    """情况 B：源根本没给时间（pubDate 缺失 / 解析失败，ts==0）。

    第一次见到就以抓取时刻落定并打标记；之后再见到同一条沿用首次时间，
    不跟着每轮刷新往前漂；源一旦补上真实时间就以源时间为准。
    """

    def prev_from(self, items):
        return {"https://src/rss": [dict(it) for it in items]}

    def test_first_seen_uses_now_and_is_marked(self):
        # 源无时间、也没有历史 → 以传入的 now 为准（真实运行里是本轮抓取时刻）
        items = [mk("t%d" % i, src="https://src/rss", ts=0) for i in range(3)]
        settled, no_time = A.settle(items, {}, now=1000)
        self.assertEqual((settled, no_time), (0, 3))
        self.assertTrue(all(it["ts"] == 1000 for it in items))
        self.assertTrue(all(it.get("no_source_time") for it in items))
        # 改写后的 date_raw 必须能被自己解析回同一时间戳
        self.assertEqual(B._parse_date(items[0]["date_raw"]), 1000)

    def test_reappearance_keeps_first_seen(self):
        # 第一轮落定 1000；第二轮源仍无时间（ts==0）、但 now 已变成 2000，
        # 应沿用首次 1000，而不是跟着 now 往前漂成 2000。
        items = [mk("t%d" % i, src="https://src/rss", ts=0) for i in range(3)]
        A.settle(items, {}, now=1000)
        prev = self.prev_from(items)
        items2 = [mk("t%d" % i, src="https://src/rss", ts=0) for i in range(3)]
        settled, no_time = A.settle(items2, prev, now=2000)
        self.assertEqual((settled, no_time), (0, 3))
        self.assertTrue(all(it["ts"] == 1000 for it in items2),
                        "同一条再次出现应沿用首次获取时间，不能跟着 now 漂")

    def test_no_time_does_not_touch_timed_items(self):
        # 同一批里有的有真实时间、有的没有：有时间的原样保留，没时间的按 now 落定
        mixed = ([mk("a%d" % i, src="https://has/rss", ts=5000 + i) for i in range(2)]
                 + [mk("b%d" % i, src="https://none/rss", ts=0) for i in range(2)])
        settled, no_time = A.settle(mixed, {}, now=9000)
        self.assertEqual((settled, no_time), (0, 2))
        self.assertEqual([it["ts"] for it in mixed[:2]], [5000, 5001])
        self.assertEqual([it["ts"] for it in mixed[2:]], [9000, 9000])

    def test_source_time_wins_once_present(self):
        # 首轮无时间落定 1000 并打标记；次轮源突然给了真实时间 7000 → 以源时间为准
        items = [mk("t0", src="https://src/rss", ts=0)]
        A.settle(items, {}, now=1000)
        prev = self.prev_from(items)
        items2 = [mk("t0", src="https://src/rss", ts=7000)]   # 这次源给了真实时间
        settled, no_time = A.settle(items2, prev, now=2000)
        self.assertEqual((settled, no_time), (0, 0))
        self.assertEqual(items2[0]["ts"], 7000,
                        "源一旦提供真实时间，就必须以源时间为准、覆盖首抓时间")

    def test_round_trip_stays_at_first_seen(self):
        """连跑两轮：无时间条目必须停在首次获取时间，不能跟着 now 一路往上漂。"""
        prev = {}
        for now in (1000, 5000, 8000):
            items = [mk("t%d" % i, src="https://src/rss", ts=0) for i in range(3)]
            A.settle(items, prev, now=now)
            prev = self.prev_from(items)
        self.assertEqual(items[0]["ts"], 1000, "源一直无时间，时间应停在首次获取")


if __name__ == "__main__":
    unittest.main(verbosity=2)
