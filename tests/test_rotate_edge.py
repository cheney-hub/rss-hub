"""轮播机制的边界与异常用例（stdlib unittest，零第三方依赖，不联网）。

和 test_core.py 的分工：
  * test_core.py 守「稳态该怎么做」—— 逐片推进、同窗不跳条、播完才回头；
  * 本文件守「出错时会发生什么」—— 参数取极值、时钟错乱、池子被挤兑、
    上屏条目在窗口内被挤出池子。这些路径平时不走到，一旦走到就是静默故障，
    所以这里每条用例都写明「期望行为」和「现在的行为」。

只测公开入口 rotate_pick()，不直接调 _pick_bucket / _hold_current：
那两个是内部台阶，签名随时会改（本轮就改过 _bucketize 的参数和
_hold_current 的返回值），锁死它们等于给重构上镣铐。

跑法：
    python -m unittest tests.test_rotate_edge -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_feed as B          # noqa: E402
from render import item_guid    # noqa: E402

DWELL = B.ROTATE_SECONDS


def sort_key(it):
    """与 run_once 的排序键一致：时间倒序，同一时间戳按 guid 定序。"""
    return (-(it.get("ts") or 0), item_guid(it))


def mk(title, ts):
    """造一条条目。link + src 一起决定 guid，所以标题不同 = guid 不同。"""
    return {"title": title, "link": "http://x/" + title,
            "src": "s/" + title, "ts": ts}


def at(bucket, title=None):
    """造一条落在第 bucket 片里的条目（+1 秒保证片号就是 bucket）。"""
    return mk(title or ("B%d" % bucket), bucket * DWELL + 1)


class RotateEdgeBase(unittest.TestCase):
    def setUp(self):
        self.dwell = DWELL

    def pick(self, pool, state, w):
        return B.rotate_pick(pool, state, now=w * self.dwell)


# ---------------------------------------------------------------- 参数极值
class TestPickRule(RotateEdgeBase):
    """机制的唯一规则：**每窗播池子里最新的、还没播过的那一片；都播过就重开一轮。**

    历史：这里曾经写着「三级台阶」——第一级取「最近 N 片里最新的未播片」，第二级取
    「全池最新的未播片」，靠一个 `ROTATE_MAX_LAG` 参数控制带宽。但第二级是更大的集合、
    且同样取最新的那一个：只要第一级非空，两级必然挑中同一条。所以第一级、
    以及它依赖的那个参数，从未改变过任何结果。
    证据：穷举 50,292 组（w=3..11 × 任意片集合 × 部分 shown × 参数 0/1/2/6）零反例；
    端到端 288 窗、参数从 0 调到 1000，上屏序列逐条相同，连 lag 都一样。

    所以参数被删掉了（见 `TestMaxLagRemoved`），下面的用例锁的是**真正的规则**。
    """

    def test_pick_is_always_the_newest_unshown(self):
        """逐窗断言：上屏的必须是「池子里最新的、还没播过的那一片」。

        这是整个轮播唯一的选片规则。它成立，机制就成立；它不成立，屏上就会出现
        「明明有更新的没播，却先播了旧的」。
        """
        rnd = random.Random(11)
        pool, st, uid = [], {}, 0
        for w in range(1, 60):
            now = w * self.dwell
            for _ in range(rnd.randint(0, 3)):
                uid += 1
                pool.append(mk("T%d" % uid, now - rnd.randint(0, 2 * self.dwell)))
            pool.sort(key=sort_key)
            del pool[120:]
            buckets, _keys = B._bucketize(pool, self.dwell)
            shown_before = {int(b) for b in (st.get("shown") or [])}
            it, st, _ = self.pick(pool, st, w)
            unshown = [b for b in sorted(buckets, reverse=True)
                       if b not in shown_before and b <= w]
            if unshown:                             # 还有没播过的片：必须拿最新的那片
                self.assertEqual(st["bucket"], unshown[0],
                                 "第 %d 窗没有选「全池最新未播片」" % w)

    def test_old_items_are_reached_only_when_recent_ones_are_done(self):
        """最近的片都播过之后，才会轮到更旧的片 —— 顺序是先新后旧，不跳。"""
        pool = sorted([at(b, "B%d" % b) for b in range(90, 100)], key=sort_key)
        st, seq = {}, []
        for w in range(100, 106):
            it, st, _ = self.pick(pool, st, w)
            seq.append(st["bucket"])
        self.assertEqual(seq, [99, 98, 97, 96, 95, 94], "应当严格从新到旧")


class TestMaxLagRemoved(RotateEdgeBase):
    """`ROTATE_MAX_LAG` 已被删除 —— 它从来没生效过，留着只会误导调参。

    README / OPERATIONS 曾经承诺「调大 = 旧条目也能轮到，代价是屏上内容更旧」，
    实测调大什么都不变。排障时按文档去调这个参数，会一直调不到效果。
    这两条用例守「别把它加回来」：常量不许复活，环境变量也不许再造出这个旋钮。
    """

    def test_max_lag_knob_is_gone(self):
        self.assertFalse(hasattr(B, "ROTATE_MAX_LAG"),
                         "ROTATE_MAX_LAG 已被删除（它不影响选片结果），不要加回来")

    def test_env_var_does_not_resurrect_the_knob(self):
        """半删更糟：常量删了但还留着 `os.environ.get("ROTATE_MAX_LAG")`，
        下次有人「顺手」接上就复活了一个假旋钮。这里只禁「读取」，不禁文档里提历史。"""
        with open(B.__file__, encoding="utf-8") as f:
            src = f.read()
        patterns = ('environ.get("ROTATE_MAX_LAG"',
                    "environ.get('ROTATE_MAX_LAG'",
                    'environ["ROTATE_MAX_LAG"]',
                    "environ['ROTATE_MAX_LAG']")
        found = [p for p in patterns if p in src]
        self.assertEqual(found, [],
                         "别再读这个环境变量了：它不影响任何选片结果，接上去就是一个假旋钮")


class TestNeverBlanks(RotateEdgeBase):
    """永不空屏：只要池子里还有一条没播过的，就必须有内容上屏。

    这是产品裁决的前提 —— 墨水屏上没有发布时间，「停住不动」和「设备坏了」无法区分，
    有内容变化才说明设备还活着。所以宁可播旧的也不返回 None。
    """

    def test_does_not_blank_screen(self):
        """本窗没有片时不能空屏：必须退回全池补播，而不是返回 None。"""
        pool = sorted([at(100), at(101)], key=sort_key)
        it, st, _ = self.pick(pool, {}, 110)
        self.assertIsNotNone(it, "池子里有 2 片，不该返回 None（空屏）")
        self.assertEqual(it["title"], "B101")
        self.assertEqual(st["bucket"], 101)

    def test_takes_current_window_when_available(self):
        """池里有本窗的片时，选中的必须是它（lag=0），不能越过它去选更旧的。"""
        now = 120 * self.dwell
        cur = mk("本窗", now + 10)              # 片号 = 120
        stale = at(80, "两小时前的")
        pool = sorted([cur, stale], key=sort_key)
        it, st, _ = self.pick(pool, {}, 120)
        self.assertEqual(it["title"], "本窗")
        self.assertEqual(st["lag"], 0)

    def test_picks_previous_window_when_present(self):
        """稳态 lag=1（池里只有上一窗的片）：照样播它，不空屏。"""
        pool = [at(119, "上一窗的")]
        it, st, _ = self.pick(pool, {}, 120)
        self.assertIsNotNone(it, "MAX_LAG=0 不是空屏开关")
        self.assertEqual(it["title"], "上一窗的")
        self.assertEqual(st["lag"], 1)

    def test_backfills_from_whole_pool(self):
        """池里只剩 20 片以前的旧片：照样补播，lag 21 片 —— 补播不设年龄门槛。"""
        pool = sorted([at(b) for b in range(80, 100)], key=sort_key)
        it, st, _ = self.pick(pool, {}, 120)
        self.assertIsNotNone(it)
        self.assertEqual(it["title"], "B99", "全池未播里最新的一片")
        self.assertEqual(st["lag"], 21)


class TestBacktrackUncappedByDesign(RotateEdgeBase):
    """唯一的那条规则不设年龄上限 —— **这是刻意的产品取舍，不是缺陷**。

    理由（产品裁决）：墨水屏上没有发布时间，「停住不动」和「设备坏了」无法区分，
    有内容变化才说明设备活着，所以「宁可播旧的也不空屏」。
    代价是极端停更 / 低产能时上屏内容可能很旧（实测最差 22.9 小时，见 README），
    这个代价是明确接受的。本类用例锁住这个决定，避免以后有人「顺手」加上限。
    """

    def test_relaxed_pick_can_be_arbitrarily_old(self):
        """池里只剩一片很旧的片 → 照样上屏，lag 到 90 片（7.5 小时）。

        这是取舍的边界：只要池子里还有一条没播过的，就不会返回 None。
        """
        pool = [at(10, "很久以前")]
        it, st, _ = self.pick(pool, {}, 100)
        self.assertIsNotNone(it, "宁可播旧的也不空屏（产品裁决）")
        self.assertEqual(it["title"], "很久以前")
        self.assertEqual(st["lag"], 90)
        self.assertEqual(st["lag"] * self.dwell / 60, 450.0, "屏上这条是 7.5 小时前的")

    def test_uncapped_backtrack_survives_a_two_hour_outage(self):
        """停更 2 小时（24 窗）的实际形状：lag 单调上涨，屏上内容一路退回两小时前。

        这是「不设上限」这个决定在真实故障下的样子，写死它，让以后读代码的人
        一眼看到代价具体长什么样，而不是只看一句「刻意取舍」。
        """
        pool = sorted([at(b, "B%d" % b) for b in range(10, 22)], key=sort_key)
        st, lags = {}, []
        for w in range(22, 34):
            it, st, _ = self.pick(pool, st, w)
            self.assertIsNotNone(it, "停更期间也不空屏")
            lags.append(st["lag"])
        self.assertGreaterEqual(len(set(lags)), 5, "lag 应随停更推进持续上涨")
        self.assertGreater(lags[-1], 12,
                           "停更 12 窗后 lag 已远超当年的 6 片「上限」—— 刻意不封顶")



class TestSingleBucket(RotateEdgeBase):
    """池子只有 1 片：屏幕会被同一条占住，直到有新内容把它顶掉。"""

    def test_same_item_five_windows_and_cycle_increments(self):
        """连跑 5 窗 → 同一片上屏 5 次，cycle 每窗 +1。

        这是「池子比消费还小」的退化态：无新内容可播，机制只能重播。
        写死这个行为，是因为 cycle 在这里的含义已经不是「第几轮播完」而是
        「重播了几次」，排障时看到 cycle 一路上涨要能对上这个原因。
        """
        pool = [at(10, "唯一一条")]
        st, titles, cycles = {}, [], []
        for w in range(11, 16):
            it, st, _ = self.pick(pool, st, w)
            titles.append(it["title"])
            cycles.append(st["cycle"])
        self.assertEqual(titles, ["唯一一条"] * 5)
        self.assertEqual(cycles, [1, 2, 3, 4, 5], "每窗都算「播完一轮重开」")
        self.assertEqual(len(st["shown"]), 1, "shown 恒定只留这一片，不会膨胀")


class TestEmptyPool(RotateEdgeBase):
    def test_returns_none_and_keeps_state(self):
        """空池必须返回 (None, state, ...)，不抛异常，且不改状态。

        main() 靠 pick is None 决定「不写设备版、不落盘状态」，
        这里如果抛异常，整轮构建就红了；如果改了状态，就会凭空推进一窗。
        """
        before = {"version": B.ROTATE_STATE_VERSION, "window": 500, "bucket": 499,
                  "lag": 1, "current": "abc", "shown": [499], "cycle": 3,
                  "pool": 1, "items": 1, "dwell": self.dwell}
        it, st, note = self.pick([], dict(before), 501)
        self.assertIsNone(it)
        self.assertEqual(st, before, "空池不该改动状态")
        self.assertIn("没有可播的片", note)

    def test_all_items_have_no_timestamp(self):
        """ts 全是 0 / None（源没写时间）：落到片 0，不该崩，也不该上屏到「1970」。"""
        pool = [{"title": "坏时间", "link": "http://x/a", "src": "s", "ts": None},
                {"title": "坏时间2", "link": "http://x/b", "src": "s", "ts": 0}]
        it, st, _ = self.pick(pool, {}, 100)
        self.assertIsNotNone(it, "有内容就该出一条，哪怕时间不可信")
        self.assertEqual(st["bucket"], 0)
        self.assertEqual(st["lag"], 100)


# ---------------------------------------------------------------- 时钟异常
class TestClockRollback(RotateEdgeBase):
    """now 比上次小（换机器 / NTP 回拨 / 手工补跑历史窗口）。"""

    def setUp(self):
        super().setUp()
        self.pool = sorted([at(b) for b in range(96, 100)], key=sort_key)
        _, self.st, _ = self.pick(self.pool, {}, 100)      # 先正常跑到 w=100
        self.assertEqual(self.st["window"], 100)

    def test_rollback_holds_previous_item(self):
        """回拨到 w=98：上屏那片仍在池内 → 不推进、不换条、window 不动。

        期望：屏幕停在原条目（5 分钟内换两条比晚一条更糟）。
        现状：满足 —— 片号命中，直接 hold。
        """
        b96 = at(96)
        state = {"version": B.ROTATE_STATE_VERSION, "window": 100, "bucket": 96,
                 "lag": 4, "current": item_guid(b96), "shown": [96], "cycle": 1,
                 "pool": 4, "items": 4, "dwell": self.dwell}
        it, st, note = self.pick(self.pool, state, 98)
        self.assertIsNotNone(it)
        self.assertEqual(it["title"], "B96")
        self.assertEqual(st["window"], 100, "回拨时不该把 window 改小")
        self.assertIn("同窗保持", note)

    def test_rollback_holds_even_when_bucket_looks_future(self):
        """回拨后上屏那片的片号会显得「在未来」，hold 仍必须认出它 → 屏幕不动。

        未来片只能在「选下一条」时排除（rotate_pick 里的 live 过滤），
        不能在 _bucketize 里剔除 —— 一旦在切片阶段就剔掉，时钟一回拨，
        屏上那一条就找不到人，于是走 skip：设备版不刷新、rotate.json 不落盘，
        白白空转一版。本用例钉住「hold 必须活过时钟回拨」。
        """
        it, st, note = self.pick(self.pool, self.st, 98)
        self.assertIsNotNone(it, "回拨时应当 hold 住原条目，而不是找不到人")
        self.assertEqual(it["title"], self.st["title"])
        self.assertEqual(st["window"], 100, "回拨时不该把 window 改小，也不该落盘新状态")
        self.assertIn("同窗保持", note)

    def test_rollback_recovers_once_clock_catches_up(self):
        """时钟走到 prev_w 之后必须自己恢复推进，不能永久卡住。"""
        _, st, _ = self.pick(self.pool, self.st, 95)
        it, st2, note = self.pick(self.pool, st, 101)
        self.assertEqual(st2["window"], 101)
        self.assertIn("新窗推进", note)
        self.assertIsNotNone(it)

    def test_rollback_with_item_gone_returns_none(self):
        """回拨 + 该条已离开池子 → 返回 skip(None)，调用方保留上一版设备版。"""
        other = sorted([at(b, "别的%d" % b) for b in range(50, 54)], key=sort_key)
        it, st, note = self.pick(other, self.st, 100)
        self.assertIsNone(it, "找不到原条目时宁可空一版，也不要 5 分钟内换两条")
        self.assertIn("保持上一版", note)
        self.assertEqual(st["window"], 100)

    def test_far_future_window_discards_state(self):
        """状态里的 window 跑到未来很多 → 整份丢掉重来，而不是永久 hold。

        load_rotate_state() 无条件信任线上那份 rotate.json（谁 window 大用谁）。
        一旦线上那份的 window 是个未来值（另一台机器时钟漂移、手工改过、
        仓库里被塞了测试状态），本机的 w 永远追不上 prev_w，
        每次构建都走「同窗保持」，rotate.json 再也不会被改写 —— 自愈路径不存在。
        所以这里要求：明显不合理的未来 window 必须被识别成坏状态。
        """
        poisoned = dict(self.st, window=10 ** 9, bucket=10 ** 9,
                        current="不存在的guid", shown=[10 ** 9])
        it, st, _ = self.pick(self.pool, poisoned, 200)
        self.assertIsNotNone(it, "window 明显不合理时应当丢弃状态重新起轮")
        self.assertEqual(st["window"], 200)

    def test_skew_within_tolerance_keeps_state(self):
        """回拨不超过 CLOCK_SKEW_WINDOWS 窗（NTP 校正常见的几十秒）→ 只冻结一版，不许丢状态。

        丢状态的代价是 shown 清空、cycle 归 1，下一窗很可能重播刚上屏那条；
        而墙钟往回跳几十秒是正常的，不该触发「整份重来」。
        """
        b96 = at(96)
        prev_w = 98 + B.CLOCK_SKEW_WINDOWS          # 恰好落在容忍边界上
        state = {"version": B.ROTATE_STATE_VERSION, "window": prev_w, "bucket": 96,
                 "lag": 1, "current": item_guid(b96), "shown": [96], "cycle": 3,
                 "pool": 4, "items": 4, "dwell": self.dwell}
        it, st, note = self.pick(self.pool, state, 98)
        self.assertIsNotNone(it)
        self.assertEqual(it["title"], "B96")
        self.assertIn("同窗保持", note)
        self.assertEqual(st["cycle"], 3, "没丢状态 → cycle 不该归 1")
        self.assertEqual(st["window"], prev_w)

    def test_skew_one_window_beyond_tolerance_discards_state(self):
        """超过容忍边界一窗就认定状态坏掉 —— 别把「长期卡死」当成「NTP 抖动」。"""
        b96 = at(96)
        state = {"version": B.ROTATE_STATE_VERSION,
                 "window": 98 + B.CLOCK_SKEW_WINDOWS + 1, "bucket": 96,
                 "lag": 1, "current": item_guid(b96), "shown": [96], "cycle": 3,
                 "pool": 4, "items": 4, "dwell": self.dwell}
        it, st, note = self.pick(self.pool, state, 98)
        self.assertNotIn("同窗保持", note)
        self.assertEqual(st["window"], 98, "坏状态被丢弃后按新窗重新起轮")


class TestFutureTimestamp(RotateEdgeBase):
    """源时钟错乱会把条目时间戳造到未来（时区写错、模板把年份拼成 2030）。"""

    def test_future_item_does_not_hijack_screen(self):
        """未来条目不参与轮播：三窗里一条都不该上屏，正常条照常轮。

        这是本轮修掉的缺陷：修之前未来片的片号恒最大，`keys[0]` 永远是它，
        每轮重开都把它顶上屏，其它内容被挤掉，lag 还会变成巨大的负数。
        """
        future = mk("2030年的消息", 1893456000)              # 2030-01-01
        real = [at(99, "真实A"), at(98, "真实B")]
        pool = sorted([future] + real, key=sort_key)
        st, aired = {}, []
        for w in (100, 101, 102):
            it, st, _ = self.pick(pool, st, w)
            if it is not None:
                aired.append(it["title"])
                self.assertGreaterEqual(st["lag"], 0, "lag 不许为负")
        self.assertNotIn("2030年的消息", aired,
                         "未来时间的条目不该上设备版（等它的时间真到了再说）")
        self.assertTrue(aired, "正常条目应该照常轮，不能因为它整池停摆")
        # pool 是「可播片数」，不含未来片；items 是原始条数，含未来那条。
        # 这两个数在排障时口径不同，锁死它免得以后被合并成一个。
        self.assertEqual(st["pool"], 2, "2 个真实片；2030 那条不算可播片")
        self.assertEqual(st["items"], 3, "items 数是原始条数，未来那条仍在")

    def test_slightly_ahead_item_still_airs(self):
        """只比现在早/晚几十秒的条目必须正常上屏 —— 钳制不能误伤源的小幅提前。"""
        now = 100 * self.dwell
        pool = [mk("刚发布", now + 60), mk("稍早", now - 60)]
        pool.sort(key=sort_key)
        it, st, _ = self.pick(pool, {}, 100)
        self.assertEqual(it["title"], "刚发布")
        self.assertEqual(st["lag"], 0)

    def test_only_future_items_does_not_crash(self):
        """整池都是未来条目 → 没有可播的片，返回 None 且不抛异常（设备版保持上一版）。"""
        pool = [mk("未来1", 1893456000), mk("未来2", 1893456001)]
        it, st, _ = self.pick(pool, {}, 100)
        self.assertIsNone(it)
        self.assertEqual(st, {})


# ---------------------------------------------------------------- 池子挤兑
class TestPoolChurn(RotateEdgeBase):
    """ITEM_LIMIT 很小 + 每窗大量新条目：每窗都有片被挤出去。"""

    def test_sixty_windows_of_churn(self):
        """连续 60 窗、每窗注入 3 条、池子截到 20 条。

        断言：不抛异常 / 不出现同一条连续两窗重播 / lag 非负 / 片数不超过条数。
        lag 的上界只要求「不为负」—— 机制没有年龄上限，挤兑时照样播池里最新的
        未播片（可能很旧），这一点由 TestBacktrackUncappedByDesign 单独钉住。
        """
        rnd = random.Random(2026)
        limit = 20
        pool, st, aired = [], {}, []
        for w in range(100, 160):
            now = w * self.dwell
            for k in range(3):
                pool.append(mk("T%d-%d" % (w, k), now - rnd.uniform(0, 4 * self.dwell)))
                pool[-1]["link"] = "http://x/%d-%d" % (w, k)
            pool.sort(key=sort_key)
            del pool[limit:]                       # 模拟 run_once 的 ITEM_LIMIT 截断
            it, st, _ = self.pick(pool, st, w)
            self.assertIsNotNone(it)
            self.assertGreaterEqual(st["lag"], 0)
            self.assertLessEqual(st["pool"], limit, "片数不可能超过条数")
            self.assertLessEqual(len(st["shown"]), st["pool"],
                                 "shown 只留池内片，不能超过池子")
            if aired:
                self.assertNotEqual(it["title"], aired[-1],
                                    "第 %d 窗与上一窗重复：%s" % (w, it["title"]))
            aired.append(it["title"])
        self.assertGreater(st["cycle"], 1, "60 窗里池子被换过多轮，cycle 应当递增")


class TestPickEvictedWithinWindow(RotateEdgeBase):
    """上屏条目在同一时间窗内被池子挤掉（ITEM_LIMIT 小 + 本窗涌入大量新条目）。"""

    def test_returns_none_and_keeps_state(self):
        """片号和 guid 都找不到 → skip，返回 None，状态原样返回。

        期望：不换条。现状：满足。
        代价（由调用方承担）：main() 不落盘 state，但完整版 feed.xml 已经重写，
        于是 feed-wrap.xml / rotate.json 停在旧版、feed.xml 是新的。
        """
        pool = sorted([at(b) for b in range(96, 100)], key=sort_key)
        _, st, _ = self.pick(pool, {}, 100)
        # 池子整体换血：旧条目全被挤掉
        fresh = sorted([mk("新-%d" % w, 100 * self.dwell + w) for w in range(30)],
                       key=sort_key)
        it, st2, note = self.pick(fresh, st, 100)
        self.assertIsNone(it)
        self.assertEqual(st2, st, "skip 不许改状态（否则会推进一窗）")
        self.assertIn("保持上一版", note)

    def test_state_lag_underreports_after_stall(self):
        """已知口径偏差：lag 是用「上一次落盘的 window」算的，停滞后会低估真实滞后。

        state 不推进时 window 冻在 100，lag 也冻住；但真实时间已经走到 103，
        屏上那条实际旧了 lag+3 片。index.html 直接拿 st.lag 显示「滞后 N 分」，
        在 skip/hold 连续发生的场景下会给出偏乐观的数字。
        """
        pool = sorted([at(b) for b in range(96, 100)], key=sort_key)
        _, st, _ = self.pick(pool, {}, 100)
        frozen_lag = st["lag"]
        self.assertEqual(st["window"], 100)
        # 连着一窗都不推进
        _, st2, _ = self.pick(pool, st, 100)
        self.assertEqual(st2["lag"], frozen_lag)
        self.assertEqual(st2["window"], 100, "state 不推进 → lag 不更新 → 显示偏乐观")


class TestHoldRepresentativeDrift(RotateEdgeBase):
    """同一窗内，片的代表条目被更晚到的条目顶掉（setdefault 取第一条）。"""

    def test_hold_switches_to_new_representative_and_updates_state(self):
        """片内代表换了 → 照播新代表，并把 guid 一起交回去改写状态。

        这是本轮修的第二个缺陷：修之前只按片号取条目、不回写 guid，
        于是 rotate.json 的 current/title 与屏上内容长期不一致。
        """
        old = at(99, "本片较早出的")
        _, st, _ = self.pick([old], {}, 100)
        self.assertEqual(st["current"], item_guid(old))

        newer = mk("本片后到的更新", 99 * self.dwell + 200)   # 同片、更晚发布
        pool = sorted([newer, old], key=sort_key)
        it, st2, note = self.pick(pool, st, 100)
        self.assertEqual(it["title"], "本片后到的更新")
        self.assertEqual(st2["current"], item_guid(newer),
                         "状态里的 guid 必须跟着屏上内容走")
        self.assertEqual(st2["title"], "本片后到的更新")
        self.assertIn("片内代表更新", note)
        self.assertEqual(st2["window"], 100, "同窗内不该推进 window")
        self.assertEqual(st2["bucket"], 99, "同窗内不该换片")


class TestNoRepeatAcrossWindows(RotateEdgeBase):
    """跨窗不重复是这套机制对用户最直观的承诺，单独做一遍扫描。"""

    def test_thirty_windows_no_consecutive_repeat(self):
        pool, st, aired = [], {}, []
        for w in range(100, 130):
            pool.append(at(w - 1, "T%d" % w))
            pool.sort(key=sort_key)
            it, st, _ = self.pick(pool, st, w)
            aired.append(it["title"])
        for i in range(1, len(aired)):
            self.assertNotEqual(aired[i], aired[i - 1],
                                "第 %d 窗重播了上一窗的条目" % (100 + i))
        self.assertEqual(len(set(aired)), len(aired), "30 窗应当 30 条不同内容")

    def test_cycle_restart_does_not_repeat_last_aired(self):
        """最小复现：全池播完重开一轮时，池子最新那片可能正是刚上屏那片 → 连播两窗。

        根因在 `_pick_bucket` 第三级台阶 `return keys[0], set(), cycle + 1`：
        清空 shown 后直接取「池子里最新的一片」，没有排除 `state["bucket"]`
        （刚上屏那一片）。只要重开的那一刻最新片 == 刚上屏片，就会连播两窗。

        触发条件（真实数据里存在）：本窗没有更新的片入池 + 其余片已全部播过。
        用 `ITEM_LIMIT` 默认 120、每窗 1~6 条的仿真里，400 窗（约 33 小时）
        出现 1~2 次；池子上限收到 20 时 400 窗出现 8~11 次。

        期望：重开一轮不许重播刚上屏那条（这是「同一条连显两次」的直接成因）。
        现状：不满足 —— 故本用例是红的。
        """
        a, b = at(10, "A"), at(11, "B")
        pool = sorted([a, b], key=sort_key)
        st = {"version": B.ROTATE_STATE_VERSION, "window": 12, "bucket": 10,
              "lag": 2, "current": item_guid(a), "shown": [10], "cycle": 1,
              "pool": 2, "items": 2, "dwell": self.dwell}
        first, st, _ = self.pick(pool, st, 13)
        self.assertEqual(first["title"], "B")          # 本窗唯一的「新片」是 11
        second, st, _ = self.pick(pool, st, 14)        # 全池播过 → 重开一轮
        self.assertNotEqual(second["title"], "B",
                            "第 14 窗把第 13 窗刚上屏的同一条又播了一遍")
        self.assertEqual(st["cycle"], 2, "确实走了「重开一轮」这条分支")


if __name__ == "__main__":
    unittest.main(verbosity=2)
