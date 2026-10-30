"""轮播机制的边界与异常用例（stdlib unittest，零第三方依赖，不联网）。

和 test_core.py 的分工：
  * test_core.py 守「稳态该怎么做」—— 逐条推进、同窗不跳条、播完才回头；
  * 本文件守「出错时会发生什么」—— 参数取极值、时钟错乱、池子被挤兑、
    上屏条目在窗口内被挤出池子。这些路径平时不走到，一旦走到就是静默故障，
    所以这里每条用例都写明「期望行为」和「现在的行为」。

只测公开入口 rotate_pick()，不直接调 _pick_item / _hold_current：
那两个是内部台阶，签名随时会改（本轮就改过：选条单位从「时间片」换成了「条目」，
`_bucketize` 退役、`_hold_current` 不再回写 guid），锁死它们等于给重构上镣铐。

跑法：
    python -m unittest tests.test_rotate_edge -v
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rotate as R              # noqa: E402  ← 被测模块
from render import item_guid    # noqa: E402

DWELL = R.ROTATE_SECONDS


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


def eligible(item, w):
    """发布时间不晚于当前窗（与 rotate._pickable 同口径）。"""
    return int((item.get("ts") or 0) // DWELL) <= w


def guid_of(it):
    return item_guid(it)


class RotateEdgeBase(unittest.TestCase):
    def setUp(self):
        self.dwell = DWELL

    def pick(self, pool, state, w):
        return R.rotate_pick(pool, state, now=w * self.dwell)

    def air(self, pool, state, w):
        """跑一窗并返回 (条目, 状态, 说明)。pool 必须已按 sort_key 排序。"""
        return self.pick(pool, state, w)


# ---------------------------------------------------------------- 选条规则
class TestPickRule(RotateEdgeBase):
    """机制的唯一规则：**每窗播池子里最新的、还没播过的那一条；都播过就重开一轮。**

    1.7.0 之前是按「时间片」轮的：池子切成 dwell 秒一片、片内取最新一条当代表、
    shown 记片号。它有个致命缺陷 —— 同一片里后到的条目既当不上代表、也永远标不上
    已播，等于进池即弃：实测池子 120 条只切出 26~44 片，一天里大半条目从未获得
    上屏机会，与「防止老条目轮不到」的初衷正好相反。现在按条轮 + 用 guid 标注，
    每条都有一次机会，标记也不会因池子滚动而错位。

    `ROTATE_MAX_LAG` 仍不在（历史上那个「先看最近 N 片」的旋钮从未生效过，
    详见 `TestMaxLagRemoved`），下面的用例锁的是**真正的规则**。
    """

    def test_pick_is_always_the_newest_unshown(self):
        """逐窗断言：上屏的必须是「池子里最新的、还没播过的那一条」。

        这是整个轮播唯一的选条规则。它成立，机制就成立；它不成立，屏上就会出现
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
            shown_before = set(st.get("shown") or [])
            prev_current = st.get("current")
            it, st, _ = self.pick(pool, st, w)
            live = [x for x in pool if eligible(x, w)]
            idx = next((i for i, x in enumerate(live)
                        if guid_of(x) not in shown_before), None)
            if idx is not None:                     # 还有没播过的：必须拿最新的那条
                self.assertEqual(guid_of(it), guid_of(live[idx]),
                                 "第 %d 窗没有选「全池最新未播条」" % w)
                self.assertEqual(st["back"], idx)
            else:                                   # 全池播过：重开一轮
                restart = next(x for x in live if guid_of(x) != prev_current)
                self.assertEqual(guid_of(it), guid_of(restart),
                                 "重开一轮应从最新的一条起")
                self.assertEqual(st["cycle"], (st.get("cycle") or 1))

    def test_old_items_are_reached_only_when_recent_ones_are_done(self):
        """最近的几条都播过之后，才会轮到更旧的 —— 顺序是先新后旧，不跳。"""
        pool = sorted([at(b, "B%d" % b) for b in range(90, 100)], key=sort_key)
        st, seq, backs = {}, [], []
        for w in range(100, 106):
            it, st, _ = self.pick(pool, st, w)
            seq.append(st["back"])
            backs.append(it["title"])
        self.assertEqual(backs, ["B99", "B98", "B97", "B96", "B95", "B94"],
                         "应当严格从新到旧")
        self.assertEqual(seq, [0, 1, 2, 3, 4, 5], "back = 前面还压着几条更新的")


class TestPerItemFairness(RotateEdgeBase):
    """同一时间窗里到达的多条必须**各获得一次上屏机会** —— 1.7.0 修掉的缺陷。"""

    def test_items_in_one_span_all_get_aired(self):
        """同一个 5 分钟片里的 5 条：5 窗里要出 5 条不同的内容，不是只出 1 条代表。"""
        now = 100 * self.dwell
        pool = sorted([mk("同窗-%d" % k, now - k) for k in range(5)], key=sort_key)
        st, titles = {}, []
        for w in range(100, 105):
            it, st, _ = self.pick(pool, st, w)
            titles.append(it["title"])
        self.assertEqual(len(set(titles)), 5, "同窗 5 条一条都不能白抓：%s" % titles)

    def test_pool_counts_items_not_time_slices(self):
        """`pool` 是**可播条数**，不再是切片数：30 条落在同一两片里也必须算 30。"""
        base = 100 * self.dwell
        pool = sorted([mk("T%d" % k, base - k * 7) for k in range(30)], key=sort_key)
        it, st, _ = self.pick(pool, {}, 100)
        self.assertEqual(st["pool"], 30, "按片算会只剩 1~2 片，绝大部分条目被吞掉")
        self.assertEqual(st["items"], 30)

    def test_shown_holds_guids_not_indexes(self):
        """shown 存的是条目的 guid：池子滚动（头部进新条目）后标记不会错位到别人身上。"""
        pool = sorted([at(b, "B%d" % b) for b in range(96, 100)], key=sort_key)
        _, st, _ = self.pick(pool, {}, 100)
        aired = guid_of(pool[0])
        self.assertIn(aired, st["shown"], "标记应当是条目的 guid")
        self.assertEqual(len(st["shown"]), 1)
        # 头部塞进 5 条更新的：若按序号记「第 0 条播过」，标记会落到新来的那条身上
        fresh = sorted([mk("新-%d" % k, 100 * self.dwell + 10 - k) for k in range(5)],
                       key=sort_key)
        pool2 = sorted(fresh + pool, key=sort_key)
        it, st2, _ = self.pick(pool2, st, 101)
        self.assertIn("新-0", it["title"], "轮换到的应当是刚来的那条")
        self.assertIn(aired, st2["shown"], "旧标记不能因为池子滚动而丢失")


class TestMaxLagRemoved(RotateEdgeBase):
    """`ROTATE_MAX_LAG` 已被删除 —— 它从来没生效过，留着只会误导调参。

    README / OPERATIONS 曾经承诺「调大 = 旧条目也能轮到，代价是屏上内容更旧」，
    实测调大什么都不变。排障时按文档去调这个参数，会一直调不到效果。
    这两条用例守「别把它加回来」：常量不许复活，环境变量也不许再造出这个旋钮。
    """

    def test_max_lag_knob_is_gone(self):
        self.assertFalse(hasattr(R, "ROTATE_MAX_LAG"),
                         "ROTATE_MAX_LAG 已被删除（它不影响选片结果），不要加回来")

    def test_env_var_does_not_resurrect_the_knob(self):
        """半删更糟：常量删了但还留着 `os.environ.get("ROTATE_MAX_LAG")`,
        下次有人「顺手」接上就复活了一个假旋钮。这里只禁「读取」，不禁文档里提历史。"""
        # 机制现在住在 rotate.py，抓取/编排在 build_feed.py：两处都要扫，
        # 只扫一处等于给「在另一个文件里接回来」留后门
        src = ""
        for path in (R.__file__, R.__file__.replace("rotate.py", "build_feed.py")):
            with open(path, encoding="utf-8") as f:
                src += f.read()
        patterns = ('environ.get("ROTATE_MAX_LAG"',
                    "environ.get('ROTATE_MAX_LAG'",
                    'environ["ROTATE_MAX_LAG"]',
                    "environ['ROTATE_MAX_LAG']")
        found = [p for p in patterns if p in src]
        self.assertEqual(found, [],
                         "别再读这个环境变量了：它不影响任何选片结果，接上去就是一个假旋钮")


class TestRecentAntiRepeat(RotateEdgeBase):
    """最近几窗不许出现同一个「屏上显示串」（`RECENT_MAX`，观感约束）。

    和问题清单里的内容去重不是一回事：`dedupe()` 挡的是「同一条新闻」，这里挡的是
    「标题前 13 字撞车」—— 云端只渲染第一行，两条毫无关系的新闻截到 13 字可能一模一样
    （线上实测：同一个栏目稿四连发、「CFTC：截至…」三连发都是同一个前缀）。
    被这条挡下来的条目**不写进 shown**，仍是未标注，几窗后照常上屏 —— 内容不丢。
    """

    # 屏上只显示前 13 全角字，所以这几个标题（前面 14 字相同、后面不同）在屏上一模一样，
    # 但它们的 guid 不同 —— 正是「不同条目、同一张脸」的情形。
    HEAD13 = "美股三大指数集体收涨道指涨逾"
    OTHER = "另一条完全无关的新闻标题内容够长"

    def twin(self, tag, ts):
        """造一条「屏显串相同、但身份（guid）不同」的条目。"""
        return mk(self.HEAD13 + tag, ts)

    def _pool(self):
        return sorted([self.twin("甲", 3 * self.dwell),
                       self.twin("乙", 2 * self.dwell),
                       self.twin("丙", 1 * self.dwell),
                       mk(self.OTHER, 0)], key=sort_key)

    def test_precondition_display_keys_collide(self):
        """前置条件：这几条的屏显串确实相同（否则下面的用例根本没测到这回事）。"""
        k = R._display_key(self.twin("甲", 0))
        self.assertTrue(k, "显示串不能是空的")
        self.assertEqual(k, R._display_key(self.twin("乙", 0)))
        self.assertNotEqual(k, R._display_key(mk(self.OTHER, 0)))
        self.assertLessEqual(len(k), len(self.HEAD13), "显示串就是屏上那 13 个全角字")

    def test_recent_suppresses_same_display_string(self):
        """连续三窗：不许出现同一个屏显串；被跳过的那条不写进 shown。"""
        pool = self._pool()
        st, keys = {}, []
        for w in range(100, 103):
            it, st, _ = self.pick(pool, st, w)
            keys.append(R._display_key(it))
        self.assertEqual(keys[0], R._display_key(self.twin("甲", 0)),
                         "第一窗播最新的那条")
        self.assertEqual(keys[1], R._display_key(mk(self.OTHER, 0)),
                         "第二窗要跳过同展示串的那两条，退到再下一条")
        for i in range(1, len(keys)):
            self.assertNotEqual(keys[i], keys[i - 1],
                                "第 %d 窗连着播了与上一窗相同的显示串" % (100 + i))
        shown = set(st["shown"])
        self.assertIn(item_guid(self.twin("甲", 0)), shown, "上过屏的要记进 shown")
        self.assertNotIn(item_guid(self.twin("丙", 0)), shown,
                         "被 recent 跳过≠播过：它还排在队里等着上屏")

    def test_recent_falls_back_instead_of_blanking(self):
        """候选全落在 recent 里（无处可选）→ 仍返回一条，绝不 None。

        这是刻意的最后一道：屏幕「停住」和「设备坏了」在用户眼里分不出来，
        所以宁可让同一个显示串连着出，也不返回 None 让设备版停在上一版。
        """
        pool = sorted([self.twin("甲", 2 * self.dwell),
                       self.twin("乙", 1 * self.dwell)], key=sort_key)
        first, st, _ = self.pick(pool, {}, 99)
        self.assertEqual(R._display_key(first), R._display_key(self.twin("甲", 0)))

        second, st, note = self.pick(pool, st, 100)
        self.assertIsNotNone(second, "无处可选时也必须给内容，不能空屏")
        self.assertIn("新窗推进", note)
        self.assertNotEqual(item_guid(second), item_guid(first), "换一条上，别重播同一条")

    def test_recent_is_trimmed_to_max(self):
        """recent 是滑动记忆：最近的在前，长度封顶 RECENT_MAX。"""
        pool = sorted([mk("第%d条完全不同的新闻内容足够长的标题" % i, i * 60)
                       for i in range(8)], key=sort_key)
        st, last = {}, None
        for w in range(100, 106):
            last, st, _ = self.pick(pool, st, w)
            self.assertLessEqual(len(st["recent"]), R.RECENT_MAX,
                                 "recent 不能随时间增长")
        self.assertEqual(len(st["recent"]), R.RECENT_MAX, "跑满之后应当正好顶到上限")
        self.assertEqual(st["recent"][0], R._display_key(last), "最近的排在最前")

    def test_skipped_item_still_airs_later(self):
        """被 recent 跳过只是「往后挪」：几窗之后它照样上屏，并记进 shown。"""
        pool = sorted([self.twin("甲", 2 * self.dwell),
                       self.twin("乙", 1 * self.dwell),
                       mk(self.OTHER, 0)], key=sort_key)
        st, titles = {}, []
        for w in range(100, 106):
            it, st, _ = self.pick(pool, st, w)
            titles.append(it["title"])
        self.assertIn(self.twin("乙", 0)["title"], titles,
                      "被 recent 挡过的条目最终要上屏，内容不能丢")
        self.assertIn(item_guid(self.twin("乙", 0)), set(st["shown"]),
                      "上屏之后它才有资格写进 shown")


class TestNeverBlanks(RotateEdgeBase):
    """永不空屏：只要池子里还有一条没播过的，就必须有内容上屏。

    这是产品裁决的前提 —— 墨水屏上没有发布时间，「停住不动」和「设备坏了」无法区分，
    有内容变化才说明设备还活着。所以宁可播旧的也不返回 None。
    """

    def test_does_not_blank_screen(self):
        """本窗没有新条目时不能空屏：必须退回全池补播，而不是返回 None。"""
        pool = sorted([at(100), at(101)], key=sort_key)
        it, st, _ = self.pick(pool, {}, 110)
        self.assertIsNotNone(it, "池子里有 2 条，不该返回 None（空屏）")
        self.assertEqual(it["title"], "B101")
        self.assertEqual(st["back"], 0)

    def test_takes_current_window_when_available(self):
        """池里有本窗到达的条目时，选中的必须是它（back=0），不能越过它去选更旧的。"""
        now = 120 * self.dwell
        cur = mk("本窗", now + 10)              # 片号 = 120
        stale = at(80, "两小时前的")
        pool = sorted([cur, stale], key=sort_key)
        it, st, _ = self.pick(pool, {}, 120)
        self.assertEqual(it["title"], "本窗")
        self.assertEqual(st["back"], 0)

    def test_single_item_pool_still_airs(self):
        """池子只有上一窗那一条：照样播它，不空屏（也不要求池里有更新的内容）。"""
        pool = [at(119, "上一窗的")]
        it, st, _ = self.pick(pool, {}, 120)
        self.assertIsNotNone(it, "MAX_LAG=0 不是空屏开关")
        self.assertEqual(it["title"], "上一窗的")
        self.assertEqual(st["back"], 0)

    def test_backfills_from_whole_pool(self):
        """池里只剩 20 片以前的旧条目：照样补播 —— 补播不设年龄门槛。"""
        pool = sorted([at(b) for b in range(80, 100)], key=sort_key)
        it, st, _ = self.pick(pool, {}, 120)
        self.assertIsNotNone(it)
        self.assertEqual(it["title"], "B99", "全池未播里最新的一条")
        self.assertEqual(st["back"], 0)


class TestItemAgeUncappedByDesign(RotateEdgeBase):
    """唯一的那条规则不设年龄上限 —— **这是刻意的产品取舍，不是缺陷**。

    理由（产品裁决）：墨水屏上没有发布时间，「停住不动」和「设备坏了」无法区分，
    有内容变化才说明设备活着，所以「宁可播旧的也不空屏」。
    代价是极端停更 / 低产能时上屏内容可能很旧（实测最差 22.9 小时，见 README），
    这个代价是明确接受的。本类用例锁住这个决定，避免以后有人「顺手」加上限。
    """

    def test_relaxed_pick_can_be_arbitrarily_old(self):
        """池里只剩一条很旧的条目 → 照样上屏，屏上这条是 7.5 小时前的。

        这是取舍的边界：只要池子里还有一条没播过的，就不会返回 None。
        """
        pool = [at(10, "很久以前")]
        it, st, _ = self.pick(pool, {}, 100)
        self.assertIsNotNone(it, "宁可播旧的也不空屏（产品裁决）")
        self.assertEqual(it["title"], "很久以前")
        age_min = (100 * self.dwell - it["ts"]) / 60.0
        self.assertAlmostEqual(age_min, 450.0, delta=1.0,
                               msg="屏上这条是 7.5 小时前的")

    def test_uncapped_backtrack_survives_a_two_hour_outage(self):
        """停更 2 小时（24 窗）的实际形状：屏上内容一路退回两小时前，日期不受封顶。

        这是「不设上限」这个决定在真实故障下的样子，写死它，让以后读代码的人
        一眼看到代价具体长什么样，而不是只看一句「刻意取舍」。
        """
        pool = sorted([at(b, "B%d" % b) for b in range(10, 22)], key=sort_key)
        st, ages, titles = {}, [], []
        for w in range(22, 34):
            it, st, _ = self.pick(pool, st, w)
            self.assertIsNotNone(it, "停更期间也不空屏")
            titles.append(it["title"])
            ages.append((w * self.dwell - it["ts"]) / 60.0)
        self.assertEqual(len(set(titles)), 12, "12 窗把池子里 12 条各播一遍")
        self.assertEqual(ages, sorted(ages), "内容只能越来越旧")
        self.assertGreater(ages[-1], 100, "停更 12 窗后屏上已是一两小时前的内容")


class TestSingleItem(RotateEdgeBase):
    """池子只有 1 条：屏幕会被同一条占住，直到有新内容把它挤下去。"""

    def test_same_item_five_windows_and_cycle_increments(self):
        """连跑 5 窗 → 同一条上屏 5 次，cycle 每窗 +1。

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
        self.assertEqual(len(st["shown"]), 1, "shown 恒定只留这一条，不会膨胀")


class TestEmptyPool(RotateEdgeBase):
    def test_returns_none_and_keeps_state(self):
        """空池必须返回 (None, state, ...)，不抛异常，且不改状态。

        main() 靠 pick is None 决定「不写设备版、不落盘状态」，
        这里如果抛异常，整轮构建就红了；如果改了状态，就会凭空推进一窗。
        """
        before = {"version": R.ROTATE_STATE_VERSION, "window": 500, "back": 1,
                  "current": "abc", "shown": ["abc"], "cycle": 3,
                  "pool": 1, "items": 1, "dwell": self.dwell}
        it, st, note = self.pick([], dict(before), 501)
        self.assertIsNone(it)
        self.assertEqual(st, before, "空池不该改动状态")
        self.assertIn("没有可播的条目", note)

    def test_all_items_have_no_timestamp(self):
        """ts 全是 0 / None（源没写时间）：排在池子最末，不该崩，也不该被当成未来剔除。"""
        pool = [{"title": "坏时间", "link": "http://x/a", "src": "s", "ts": None},
                {"title": "坏时间2", "link": "http://x/b", "src": "s", "ts": 0}]
        it, st, _ = self.pick(pool, {}, 100)
        self.assertIsNotNone(it, "有内容就该出一条，哪怕时间不可信")
        self.assertEqual(st["pool"], 2, "时间不可信不等于发布时间在未来")
        self.assertEqual(st["items"], 2)
        self.assertEqual(st["at"], "发布时间未知", "不能把 1970 当成真实发布时间")


# ---------------------------------------------------------------- 时钟异常
class TestClockRollback(RotateEdgeBase):
    """now 比上次小（换机器 / NTP 回拨 / 手工补跑历史窗口）。"""

    def setUp(self):
        super().setUp()
        self.pool = sorted([at(b) for b in range(96, 100)], key=sort_key)
        _, self.st, _ = self.pick(self.pool, {}, 100)      # 先正常跑到 w=100
        self.assertEqual(self.st["window"], 100)

    def test_rollback_holds_previous_item(self):
        """回拨到 w=98：屏上那条仍在池内 → 不推进、不换条、window 不动。

        期望：屏幕停在原条目（5 分钟内换两条比晚一条更糟）。
        现状：满足 —— 按 guid 在全池里找到它，直接 hold。
        """
        b96 = at(96)
        state = {"version": R.ROTATE_STATE_VERSION, "window": 100, "back": 3,
                 "current": item_guid(b96), "shown": [item_guid(b96)], "cycle": 1,
                 "pool": 4, "items": 4, "dwell": self.dwell}
        it, st, note = self.pick(self.pool, state, 98)
        self.assertIsNotNone(it)
        self.assertEqual(it["title"], "B96")
        self.assertEqual(st["window"], 100, "回拨时不该把 window 改小")
        self.assertIn("同窗保持", note)

    def test_rollback_holds_even_when_item_looks_future(self):
        """回拨后屏上那条的发布时间会显得「在未来」，hold 仍必须认出它 → 屏幕不动。

        未来条目只能在「选下一条」时排除（rotate_pick 里的 live 过滤），
        不能在入口就剔除 —— 一旦剔除，时钟一回拨屏上那条就找不到人，
        于是走 skip：设备版不刷新、rotate.json 不落盘，白白空转一版。
        本用例钉住「hold 必须活过时钟回拨」。
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
        poisoned = dict(self.st, window=10 ** 9, back=0,
                        current="不存在的guid", shown=["不存在的guid"])
        it, st, _ = self.pick(self.pool, poisoned, 200)
        self.assertIsNotNone(it, "window 明显不合理时应当丢弃状态重新起轮")
        self.assertEqual(st["window"], 200)

    def test_skew_within_tolerance_keeps_state(self):
        """回拨不超过 CLOCK_SKEW_WINDOWS 窗（NTP 校正常见的几十秒）→ 只冻结一版，不许丢状态。

        丢状态的代价是 shown 清空、cycle 归 1，下一窗很可能重播刚上屏那条；
        而墙钟往回跳几十秒是正常的，不该触发「整份重来」。
        """
        b96 = at(96)
        prev_w = 98 + R.CLOCK_SKEW_WINDOWS          # 恰好落在容忍边界上
        state = {"version": R.ROTATE_STATE_VERSION, "window": prev_w, "back": 0,
                 "current": item_guid(b96), "shown": [item_guid(b96)], "cycle": 3,
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
        state = {"version": R.ROTATE_STATE_VERSION,
                 "window": 98 + R.CLOCK_SKEW_WINDOWS + 1, "back": 0,
                 "current": item_guid(b96), "shown": [item_guid(b96)], "cycle": 3,
                 "pool": 4, "items": 4, "dwell": self.dwell}
        it, st, note = self.pick(self.pool, state, 98)
        self.assertNotIn("同窗保持", note)
        self.assertEqual(st["window"], 98, "坏状态被丢弃后按新窗重新起轮")


class TestFutureTimestamp(RotateEdgeBase):
    """源时钟错乱会把条目时间戳造到未来（时区写错、模板把年份拼成 2030）。"""

    def test_future_item_does_not_hijack_screen(self):
        """未来条目不参与轮播：三窗里一条都不该上屏，正常条目照常轮。

        处理口径是「时间还没到就先不播」：按条目轮之后如果不做这层过滤，
        它永远排在最前、`_pickable` 之外无从谈起 —— 屏幕会被它独吞。
        """
        future = mk("2030年的消息", 1893456000)              # 2030-01-01
        real = [at(99, "真实A"), at(98, "真实B")]
        pool = sorted([future] + real, key=sort_key)
        st, aired = {}, []
        for w in (100, 101, 102):
            it, st, _ = self.pick(pool, st, w)
            if it is not None:
                aired.append(it["title"])
                self.assertGreaterEqual(st["back"], 0, "back 不许为负")
        self.assertNotIn("2030年的消息", aired,
                         "未来时间的条目不该上设备版（等它的时间真到了再说）")
        self.assertTrue(aired, "正常条目应该照常轮，不能因为它整池停摆")
        # pool 是「可播条数」，不含未来条目；items 是原始条数，含未来那条。
        # 这两个数在排障时口径不同，锁死它免得以后被合并成一个。
        self.assertEqual(st["pool"], 2, "2 条真实条目；2030 那条不算可播")
        self.assertEqual(st["items"], 3, "items 数是原始条数，未来那条仍在")

    def test_slightly_ahead_item_still_airs(self):
        """只比现在早/晚几十秒的条目必须正常上屏 —— 过滤不能误伤源的小幅提前。"""
        now = 100 * self.dwell
        pool = [mk("刚发布", now + 60), mk("稍早", now - 60)]
        pool.sort(key=sort_key)
        it, st, _ = self.pick(pool, {}, 100)
        self.assertEqual(it["title"], "刚发布")
        self.assertEqual(st["back"], 0)

    def test_only_future_items_does_not_crash(self):
        """整池都是未来条目 → 没有可播的条目，返回 None 且不抛异常（设备版保持上一版）。"""
        pool = [mk("未来1", 1893456000), mk("未来2", 1893456001)]
        it, st, _ = self.pick(pool, {}, 100)
        self.assertIsNone(it)
        self.assertEqual(st, {})


# ---------------------------------------------------------------- 池子挤兑
class TestPoolChurn(RotateEdgeBase):
    """ITEM_LIMIT 很小 + 每窗大量新条目：每窗都有条目被挤出去。"""

    def test_sixty_windows_of_churn(self):
        """连续 60 窗、每窗注入 3 条、池子截到 20 条。

        断言：不抛异常 / 不出现同一条连续两窗重播 / back 非负 / 可播条数不超过池子。
        back 的上界只要求「不为负」—— 机制没有年龄上限，挤兑时照样播池里最新的
        未播条（可能很旧），这一点由 TestItemAgeUncappedByDesign 单独钉住。
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
            self.assertGreaterEqual(st["back"], 0)
            self.assertLessEqual(st["pool"], limit, "可播条数不可能超过池子条数")
            self.assertLessEqual(len(st["shown"]), st["pool"],
                                 "shown 只留池内条目，不能超过池子")
            if aired:
                self.assertNotEqual(it["title"], aired[-1],
                                    "第 %d 窗与上一窗重复：%s" % (w, it["title"]))
            aired.append(it["title"])
        # cycle 不会涨：产能（3 条/窗）远大于消费（1 条/窗），永远有「更新的未播条」，
        # 走到重开一轮的前提（全池都播过）不会出现。代价写在 pool ≤ 上限那条断言里：
        # 每窗只有 1 次上屏机会，池尾那批在被挤出池子之前排不到。见 README「代价」。
        self.assertEqual(st["cycle"], 1, "有新内容可播时不该走到「重开一轮」")
        self.assertGreater(len(set(aired)), 40, "60 窗应当播出几十条不同的内容")


class TestPickEvictedWithinWindow(RotateEdgeBase):
    """上屏条目在同一时间窗内被池子挤掉（ITEM_LIMIT 小 + 本窗涌入大量新条目）。"""

    def test_returns_none_and_keeps_state(self):
        """guid 在全池里找不到 → skip，返回 None，状态原样返回。

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

    def test_state_back_underreports_after_stall(self):
        """已知口径偏差：back 是用「上一次落盘的那一刻」算的，停滞后会低估真实滞后。

        state 不推进时 window 冻在 100，back 也冻住；但真实时间已经走到 103，
        屏上那条实际前面又多了几条。index.html 直接拿 st.back 显示「滞后 N 条」，
        在 skip/hold 连续发生的场景下会给出偏乐观的数字（页面上已叠上「距 updated
        过去了几个窗」来补偿）。
        """
        pool = sorted([at(b) for b in range(96, 100)], key=sort_key)
        _, st, _ = self.pick(pool, {}, 100)
        frozen_back = st["back"]
        self.assertEqual(st["window"], 100)
        # 连着一窗都不推进
        _, st2, _ = self.pick(pool, st, 100)
        self.assertEqual(st2["back"], frozen_back)
        self.assertEqual(st2["window"], 100, "state 不推进 → back 不更新 → 显示偏乐观")


class TestWindowFreeze(RotateEdgeBase):
    """同一窗内又抓到更晚发布的条目：屏上那条不动（1.7.0 起的行为）。

    之前这里是「片内代表被顶掉 → 照播新代表」，那条路是「按片轮」的产物：片没了
    概念也就没了。现在的语义更简单也更值钱 —— 同一窗不去撵内容，标记跟着 guid 走，
    rotate.json 的 title/current 永远等于屏上那条。
    """

    def test_hold_keeps_the_displayed_item(self):
        old = at(99, "本窗播出的")
        _, st, _ = self.pick([old], {}, 100)
        self.assertEqual(st["current"], item_guid(old))

        newer = mk("本窗后到的更新", 99 * self.dwell + 200)   # 同窗、更晚发布
        pool = sorted([newer, old], key=sort_key)
        it, st2, note = self.pick(pool, st, 100)
        self.assertEqual(it["title"], "本窗播出的", "同一窗不该换台")
        self.assertEqual(st2["current"], item_guid(old), "状态跟着屏上那条，不能串到新条目")
        self.assertIn("同窗保持", note)
        self.assertEqual(st2["window"], 100, "同窗内不该推进 window")

    def test_new_arrivals_are_picked_in_the_next_window(self):
        """本窗忍住的更新内容，下一窗必须立刻顶上来 —— 只是延后一窗，不是丢掉。"""
        old = at(99, "本窗播出的")
        _, st, _ = self.pick([old], {}, 100)
        newer = mk("本窗后到的更新", 99 * self.dwell + 200)
        pool = sorted([newer, old], key=sort_key)
        it, st2, note = self.pick(pool, st, 101)
        self.assertEqual(it["title"], "本窗后到的更新")
        self.assertIn("新窗推进", note)
        self.assertEqual(st2["back"], 0)


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
        """全池播完重开一轮时，池子最新那条往往正是刚上屏那条 → 不许连播两窗。

        根因曾经在「重开一轮」那一步：清空 shown 后直接取最新一条，没排除刚上屏的
        那一条。触发条件很常见（本窗没有更新的条目入池 + 其余都已播过），
        现在是刻意的例外分支 —— `_pick_item` 会跳过 `state["current"]` 起轮。
        """
        a, b = at(10, "A"), at(11, "B")
        pool = sorted([a, b], key=sort_key)
        st = {"version": R.ROTATE_STATE_VERSION, "window": 12, "back": 1,
              "current": item_guid(a), "shown": [item_guid(a)], "cycle": 1,
              "pool": 2, "items": 2, "dwell": self.dwell}
        first, st, _ = self.pick(pool, st, 13)
        self.assertEqual(first["title"], "B")          # 本窗唯一的「新条目」是 B
        second, st, _ = self.pick(pool, st, 14)        # 全池播过 → 重开一轮
        self.assertNotEqual(second["title"], "B",
                            "第 14 窗把第 13 窗刚上屏的同一条又播了一遍")
        self.assertEqual(st["cycle"], 2, "确实走了「重开一轮」这条分支")


if __name__ == "__main__":
    unittest.main(verbosity=2)
