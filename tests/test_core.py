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
    """

    def setUp(self):
        self.items = [mk("T%d" % i, src="s%d" % i, ts=1000 - i) for i in range(4)]

    def pick(self, state, w):
        return B.rotate_pick(self.items, state, now=w * B.ROTATE_SECONDS)

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

    def test_new_item_jumps_queue(self):
        st = {}
        _, st, _ = self.pick(st, 1)
        self.items = [mk("NEW", src="n", ts=9999)] + self.items
        it, _, _ = self.pick(st, 2)
        self.assertEqual(it["title"], "NEW")

    def test_no_repeat_within_cycle(self):
        st, seen = {}, []
        for w in range(1, 6):
            it, st, _ = self.pick(st, w)
            seen.append(it["title"])
        self.assertEqual(len(seen[:4]), len(set(seen[:4])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
