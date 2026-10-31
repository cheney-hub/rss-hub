"""轮播状态「形状不对」时的容错（stdlib unittest，零第三方依赖，不联网）。

为什么单独一个文件：这几条守的是**状态读入口的健壮性**，不是选条规则 ——
它们和 test_rotate_edge 的「时钟错乱 / 池子挤兑」不是一回事，塞进那边会把它顶过
750 行的拆分提醒线；单独放，坏状态这一类问题以后就往这里加。

rotate.json 是**发布出去的产物**，形状不由本模块保证：CDN 缓存住的旧版本、
别的分支写过、手工编辑坏了，都可能让它变成 `{"recent": 12345}`。
列表推导遇到整数是 TypeError，而 `build_feed.main()` 没有 try —— 整个这一轮
会死在选条之前，feed 和 rotate.json 都不落盘，屏幕就此冻在上一版（和「设备坏了」
在用户眼里分不出来）。所以口径是：形状不对就当这个字段不存在，代价只是重复播一条。

这条口径覆盖**所有**字段，不只是列表：`recent` / `shown` 这类列表字段由 `_str_list`
兜，`window` / `dwell` / `cycle` 这类数值字段由 `_int` 兜。数值字段的坏法更毒
（字符串抛 ValueError、list/dict 抛 TypeError，两类异常都得吞），而且 Pi 上
`rotate_screen.py` 也没有 try —— 崩一次就永远崩，状态永远写不回去。

跑法：
    python -m unittest tests.test_state_hygiene -v
"""
import os
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


class TestMalformedStateFields(unittest.TestCase):
    """状态字段形状不对时不能崩（列表与数值字段都算）：这一轮死了，屏幕就冻在上一版。"""

    def _pool(self, n=3):
        """n 条发布时间递减、屏显串各不相同的条目。"""
        return sorted([mk("第%d条互不相关的新闻标题内容足够长" % i, (n - i) * DWELL)
                       for i in range(n)], key=sort_key)

    def _state(self, **kw):
        """一份「看起来像真状态」的字典：版本号与节拍都对，只是字段形状被写坏。"""
        st = {"version": R.ROTATE_STATE_VERSION, "window": 99, "dwell": DWELL,
              "current": "", "shown": [], "recent": [], "cycle": 1}
        st.update(kw)
        return st

    def _pick(self, state, w=100):
        return R.rotate_pick(self._pool(), state, now=w * DWELL)

    def test_recent_not_a_list_is_treated_as_absent(self):
        """recent 是个整数：当没有这个字段，照常选条、照常回写。"""
        it, st, note = self._pick(self._state(recent=12345))
        self.assertIsNotNone(it, "recent 形状不对就该当它不存在")
        self.assertIn("新窗推进", note)
        self.assertEqual(st["recent"], [R._display_key(it)],
                         "本窗上屏那条照常记进 recent")

    def test_shown_not_a_list_is_treated_as_absent(self):
        """shown 是个整数：当「一条都没播过」，从头开始轮。"""
        it, st, _ = self._pick(self._state(shown=12345))
        self.assertIsNotNone(it)
        self.assertEqual(st["shown"], [item_guid(it)],
                         "本窗上屏那条照常记进 shown，不该带着垃圾值")

    def test_string_field_is_not_split_into_characters(self):
        """字符串也能迭代：不防的话 "abcdef" 会被逐字符拆成 6 个键塞进 recent。"""
        it, st, _ = self._pick(self._state(recent="abcdef"))
        self.assertEqual(st["recent"], [R._display_key(it)],
                         "字符串不是列表，不能被逐字符拆开当记忆用")
        self.assertNotIn("a", st["recent"])

    def test_good_state_still_works(self):
        """形状正确时必须照常生效 —— 上面三条放行得太宽的话，这条会红。

        校验类改动都配一条「正常值仍然有效」：只锁「坏值不崩」的话，
        把 `recent` 一律清空也能全绿，那条约束等于被悄悄摘掉了。
        """
        pool = self._pool()
        blocked = R._display_key(pool[0])
        it, st, _ = self._pick(self._state(recent=[blocked], cycle=1))
        self.assertNotEqual(R._display_key(it), blocked,
                            "recent 里记着的显示串必须被避开")
        self.assertEqual(len(st["recent"]), 2, "本窗这条要压进记忆")

    def test_window_not_a_number_is_treated_as_absent(self):
        """window 是字符串：当「没有状态」，照常推进到当前窗。"""
        it, st, note = self._pick(self._state(window="not-a-number"))
        self.assertIsNotNone(it, "坏 window 不能让选条失败")
        self.assertIn("新窗推进", note)
        self.assertEqual(st["window"], 100, "坏 window 当成 0，于是当前窗就是新窗")

    def test_window_not_a_scalar_does_not_crash(self):
        """window 是列表：int() 抛的是 TypeError 而不是 ValueError，守卫要一起吞。"""
        it, st, _ = self._pick(self._state(window=[1]))
        self.assertIsNotNone(it, "TypeError 型坏值同样不能崩")
        self.assertEqual(st["window"], 100)

    def test_dwell_not_a_number_discards_state(self):
        """dwell 写坏 = 节拍不符 → 整份状态作废重开，不能回落到默认值继续用。

        回落成默认 300 会让 `int(dwell) != dwell` 恒假、旧 cycle / shown 被静默沿用，
        比崩溃更隐蔽，所以这里断的是「重开的可观测证据」而不是「没崩」。
        """
        it, st, _ = self._pick(self._state(dwell="not-a-number", cycle=7, shown=["旧的"]))
        self.assertEqual(st["cycle"], 1, "坏 dwell 必须重开一轮")
        self.assertEqual(st["shown"], [item_guid(it)], "旧标记必须被清掉")

    def test_cycle_not_a_number_falls_back_to_first_round(self):
        it, st, _ = self._pick(self._state(cycle="not-a-number"))
        self.assertEqual(st["cycle"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
