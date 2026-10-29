"""核心纯函数的行为测试（stdlib unittest，零第三方依赖，不联网）。

只测「改坏了会静默出错」的地方：中文宽度折算、标题切分、电头剥离、去重键、
正文合成、轮播游标。这些函数不碰网络、不写文件，可以放心在 CI 里跑。

跑法：
    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_feed as B  # noqa: E402
import render as R      # noqa: E402  （纯函数住在渲染层）


def mk(title, link=None, src="s", ts=1000):
    return {"title": title, "link": link or "http://x/" + title, "src": src, "ts": ts}


class TestLayering(unittest.TestCase):
    """分层契约：抓取层必须用渲染层那一个 build_rss，不能各带一份实现。"""

    def test_pipeline_uses_render(self):
        self.assertIs(B.build_rss, R.build_rss)

    def test_render_has_no_network(self):
        """渲染层不许 import 网络模块（看 AST，不看注释里的字眼）。"""
        import ast
        src = open(R.__file__, encoding="utf-8").read()
        mods = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                mods.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        for bad in ("urllib", "requests", "socket", "http"):
            self.assertNotIn(bad, mods, "渲染层不该碰网络：%s" % bad)


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

    def test_short_title_kept_whole(self):
        head, tail = R.split_title("港股收盘")
        self.assertEqual(head, "港股收盘")
        self.assertEqual(tail, "")


class TestDateline(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(R.strip_dateline("财联社10月29日电，瑞银迁址影响已评估"),
                         "瑞银迁址影响已评估")
        self.assertEqual(R.strip_dateline("金十数据10月29日讯，微软发行债券"),
                         "微软发行债券")

    def test_tag_with_dateline(self):
        self.assertEqual(R.strip_dateline("【独家】财联社10月29日电，某公司获订单"),
                         "某公司获订单")

    def test_plain_title_untouched(self):
        self.assertEqual(R.strip_dateline("【公告】某某公司三季度业绩预增"),
                         "【公告】某某公司三季度业绩预增")


class TestDedupe(unittest.TestCase):
    def test_same_story_two_sources(self):
        # 同一件事被两家快讯发出来，只应留一条
        a = mk("财联社10月29日电，瑞士政府已评估瑞银迁址影响，称若无总部将面临更严监管", src="a")
        b = mk("瑞士政府已评估瑞银迁址影响 称若无总部将面临更严监管", src="b")
        out, dropped = R.dedupe([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(dropped, 1)

    def test_utm_params_ignored(self):
        a = mk("标题甲甲甲甲甲甲", link="http://x/1?utm_source=a")
        b = mk("标题乙乙乙乙乙乙", link="http://x/1")
        out, _ = R.dedupe([a, b])
        self.assertEqual(len(out), 1)


class TestBody(unittest.TestCase):
    def test_title_echo_removed(self):
        # 「【标题】导语」格式，正文不该再从标题复述一遍开始
        t = "微软启动2017年以来首次美国高评级债券发行"
        body = R.compose_body("", "【%s】金十数据10月29日讯，规模预计超过百亿美元" % t, t)
        self.assertNotIn("微软启动", body)
        self.assertIn("规模预计", body)

    def test_body_within_limit(self):
        body = R.compose_body("", "字" * 500, "标题")
        self.assertLessEqual(R.text_width(body), R.BODY_MAX)

    def test_body_strips_leading_punct(self):
        body = R.compose_body("，同比由盈转亏", "", "标题")
        self.assertFalse(body.startswith("，"))


class TestRotate(unittest.TestCase):
    """轮播规则（用户定稿口径）：

    信息按时间倒序排 → 第一条上屏并标注已显示 → 下一轮只显示没有标注的最新一条
    → 全部都有标注则清空所有标注，重新开始。同一时间窗内重复构建不推进。

    标记的单位是「时间片」（dwell 秒一片），不是单条：
    每 5 分钟各源会产出十几条，屏幕只放得下 1 条，按条轮永远追不上，
    老条目还没轮到就被池子上限挤出去 —— 所以一个片只出一条代表。
    """

    def setUp(self):
        # 每条落在不同的时间片（间隔 = dwell），模拟「每 5 分钟一条」
        self.dwell = B.ROTATE_SECONDS
        self.items = [mk("T%d" % i, src="s%d" % i, ts=1000 - i * self.dwell)
                      for i in range(4)]

    def pick(self, state, w, items=None):
        return B.rotate_pick(items if items is not None else self.items,
                             state, now=w * self.dwell)

    def test_advances_one_per_window(self):
        st, seq = {}, []
        for w in range(1, 5):
            it, st, _ = self.pick(st, w)
            seq.append(it["title"])
        self.assertEqual(seq, ["T0", "T1", "T2", "T3"])

    def test_idempotent_in_same_window(self):
        it, st, _ = self.pick({}, 10)
        for _ in range(5):
            it2, st, _ = self.pick(st, 10)
            self.assertEqual(it2["title"], it["title"])

    def test_clears_after_full_cycle(self):
        st = {}
        for w in range(1, 5):
            _, st, _ = self.pick(st, w)
        it, st, _ = self.pick(st, 5)
        self.assertEqual(st["cycle"], 2)
        self.assertEqual(len(st["shown"]), 1)
        self.assertEqual(it["title"], "T0")

    def test_same_bucket_takes_latest(self):
        """同一个 5 分钟片里的多条只出一条（取最新的），且只占队列一格。"""
        items = [
            mk("B0-old", src="a", ts=1000 + 10),
            mk("B0-new", src="b", ts=1000 + 90),   # 同片、更新
            mk("B1", src="c", ts=1000 - 300 + 5),
        ]
        items.sort(key=lambda x: x["ts"], reverse=True)
        st, seq = {}, []
        for w in range(1, 4):
            it, st, _ = self.pick(st, w, items)
            seq.append(it["title"])
        self.assertEqual(seq[:2], ["B0-new", "B1"])
        self.assertEqual(len(st["queue"]), 2)      # 两片，不是三条

    def test_new_item_waits_next_cycle(self):
        """新条目不在本轮插队：本轮清单是开轮时拍好的快照，播完才重排。"""
        st = {}
        _, st, _ = self.pick(st, 1)                        # 本轮从 T0 开始
        items = [mk("NEW", src="n", ts=9999)] + self.items  # 来了一条更新的
        it, st, _ = self.pick(st, 2, items)
        self.assertEqual(it["title"], "T1")                # 不插队，继续走清单
        for w in range(3, 5):
            _, st, _ = self.pick(st, w, items)
        it, st, _ = self.pick(st, 5, items)                # 一轮播完，重排
        self.assertEqual(it["title"], "NEW")

    def test_queue_survives_pool_rollover(self):
        """池子在滚动（老条目被上限挤掉）也不该让本轮清单中途重排。"""
        st = {}
        _, st, _ = self.pick(st, 1)
        queue = list(st["queue"])
        # 模拟新条目涌入，但清单里的片都还在
        more = [mk("N%d" % k, src="z%d" % k, ts=9000 + k) for k in range(5)]
        items = sorted(more + self.items, key=lambda x: x["ts"], reverse=True)
        _, st, _ = self.pick(st, 2, items)
        self.assertEqual(st["queue"], queue)               # 清单没被改写

    def test_no_repeat_within_cycle(self):
        st, seen = {}, []
        for w in range(1, 7):
            it, st, _ = self.pick(st, w)
            seen.append(it["title"])
        self.assertEqual(len(seen[:4]), len(set(seen[:4])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
