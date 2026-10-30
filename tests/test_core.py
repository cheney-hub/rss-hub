"""核心纯函数的行为测试（stdlib unittest，零第三方依赖，不联网）。

只测「改坏了会静默出错」的地方：中文宽度折算、标题切分、电头剥离、去重键、
正文合成、轮播游标。这些函数不碰网络、不写文件，可以放心在 CI 里跑。

跑法：
    python -m unittest discover -s tests -v
"""
import glob
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_feed as B  # noqa: E402
import render as R      # noqa: E402  （纯函数住在渲染层）
import version as V     # noqa: E402  （版本号唯一真源）


def mk(title, link=None, src="s", ts=1000):
    return {"title": title, "link": link or "http://x/" + title, "src": src, "ts": ts}


class TestVersion(unittest.TestCase):
    """版本号：单一真源 + 必须真的写进输出，否则线上跑的是哪一版永远说不清。"""

    def test_semver_format(self):
        self.assertRegex(V.__version__, r"^\d+\.\d+\.\d+$")

    def test_two_modules_agree(self):
        self.assertEqual(B.__version__, V.__version__)
        self.assertEqual(R.__version__, V.__version__)

    def test_generator_carries_version(self):
        xml = R.build_rss([], "http://x/feed.xml", {"total": 0})
        self.assertIn("<generator>rss-hub %s</generator>" % V.__version__, xml)
        # 设备版刻意省掉 generator，谁改回来都会多出这几字节被测试抓住
        self.assertNotIn("generator", R.build_rss([], "http://x/feed.xml", {}, screen=True))


class TestLayering(unittest.TestCase):
    """分层契约：抓取层必须用渲染层那一个 build_rss，不能各带一份实现。"""

    def test_pipeline_uses_render(self):
        self.assertIs(B.build_rss, R.build_rss)

    def test_render_has_no_network(self):
        """渲染层不许 import 网络模块（看 AST，不看注释里的字眼）。"""
        import ast
        with open(R.__file__, encoding="utf-8") as f:
            src = f.read()
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

    def test_utm_params_ignored(self):
        a = mk("标题甲甲甲甲甲甲", link="http://x/1?utm_source=a")
        b = mk("标题乙乙乙乙乙乙", link="http://x/1")
        out, _ = R.dedupe([a, b])
        self.assertEqual(len(out), 1)

    def test_meaningful_query_params_kept(self):
        """id 写在查询串里的站点（RTI: /news?uid=3&pid=N）不能被折叠成一条。"""
        a = mk("标题甲甲甲甲甲甲", link="http://x/news?uid=3&pid=1")
        b = mk("标题乙乙乙乙乙乙", link="http://x/news?uid=3&pid=2")
        out, _ = R.dedupe([a, b])
        self.assertEqual(len(out), 2)

    def test_tracking_param_not_identity(self):
        """追踪参数不构成身份：?utm_source=rss&id=9 与 ?id=9 是同一条。"""
        a = mk("标题甲甲甲甲甲甲", link="http://x/1?utm_source=rss&id=9")
        b = mk("标题乙乙乙乙乙乙", link="http://x/1?id=9")
        out, _ = R.dedupe([a, b])
        self.assertEqual(len(out), 1)

    def test_query_param_order_insensitive(self):
        """参数顺序不影响链接身份：?a=1&b=2 与 ?b=2&a=1 是同一条。"""
        a = mk("标题甲甲甲甲甲甲", link="http://x/1?a=1&b=2")
        b = mk("标题乙乙乙乙乙乙", link="http://x/1?b=2&a=1")
        out, _ = R.dedupe([a, b])
        self.assertEqual(len(out), 1)


class TestCleanText(unittest.TestCase):
    """HTML → 纯文本：段落边界的处理是这里最容易出错的一处。

    旧实现把 `<[^>]+>` 一律删掉，于是 `</p><p>` 两侧的英文单词粘成一个词
    （`worldSecond`），整句读不出词界。修法必须**只**在半角词字符之间补空格，
    汉字之间照旧直接相接 —— 否则中文正文会被塞进一堆排版噪音。
    """

    def test_paragraph_boundary_keeps_english_word_gap(self):
        self.assertEqual(R.clean_text("<p>Hello world</p><p>Second paragraph</p>"),
                         "Hello world Second paragraph")
        self.assertEqual(R.clean_text("jumped<br>Apple rose"), "jumped Apple rose")

    def test_paragraph_boundary_does_not_add_space_between_cjk(self):
        self.assertEqual(R.clean_text("<p>第一段。</p><p>第二段。</p>"), "第一段。第二段。")

    def test_paragraph_boundary_with_newline_keeps_single_space(self):
        # 源里写成 `</p>\n<p>` 的时候，换行本来就变成了一个空格，别叠加成两个
        self.assertEqual(R.clean_text("<p>a</p>\n<p>b</p>"), "a b")
        self.assertEqual(R.clean_text("<p>第一段。</p>\n<p>第二段。</p>"), "第一段。 第二段。")

    def test_nbsp_space_is_preserved(self):
        # `&nbsp;` 是源站主动要求的空格，不属于「段落边界」，不能被吃掉
        self.assertEqual(R.clean_text("摘要&nbsp;内容"), "摘要 内容")

    def test_tags_entities_and_cdata(self):
        self.assertEqual(R.clean_text("<![CDATA[<b>粗体</b> &amp; <i>斜体</i>]]>"),
                         "粗体 & 斜体")
        self.assertEqual(R.clean_text("  \n\t "), "")


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
    """轮播规则：**锚定当前窗 —— 每窗播池子里最新的、还没播过的那一条**。

    全池播完才清标记重来；同一时间窗内重复构建不推进。

    标记的单位是**条目身份（guid）** —— 不是时间片，也不是「池子里第几条」：
    池子每窗都在滚动（新条目从头部进来、旧的被 `ROTATE_POOL_MAX` 挤出去），按序号记
    "第 3 条播过了"会在下一窗错位到别的条目身上；按时间片记（1.7.0 之前）更糟，
    同一片只出一条代表，片内其它条目永远得不到"已播"以外的命运 —— 实测 120 条只切出
    26~44 片，大半条目从未上过屏。按条轮 + guid 标注之后每条都有一票，且不会错位。

    与更早的旧机制（开轮时拍清单快照、从新到旧推进）的差别就在「锚定」两字：
    旧机制第 k 窗必播「第 k 新」的那条，延迟随轮次线性增长（线上实测滞后 75 分钟、
    仿真最差 4.3 小时），而运行期间新涌入的条目根本不在冻结的清单里，本轮一条都播不到。
    所以 test_new_item_airs_next_window 是这次修复的核心用例，其余用例守的是
    「延迟不增长」「先新后旧」「播完才回头」「同一窗不推进」这几条边界。
    """

    def setUp(self):
        # 每条落在不同的时间片（间隔 = dwell），模拟「每 5 分钟一条」
        self.dwell = B.ROTATE_SECONDS
        self.items = [mk("T%d" % i, src="s%d" % i, ts=1000 - i * self.dwell)
                      for i in range(4)]
        # 这批条目的片号是 3、2、1、0，所以窗口从第 4 窗开始用（片号 ≤ 当前窗）。
        # 用更小的窗号会把它们当成「发布时间在未来」而被轮播剔掉 —— 见 _pickable。
        self.w0 = 4

    def pick(self, state, w, items=None):
        return B.rotate_pick(items if items is not None else self.items,
                             state, now=w * self.dwell)

    def at(self, bucket, title=None):
        """造一条落在第 bucket 片里的条目（+1 秒保证片号就是这个 bucket）。"""
        return mk(title or ("B%d" % bucket), src="s%d" % bucket,
                  ts=bucket * self.dwell + 1)

    def test_advances_one_per_window(self):
        st, seq = {}, []
        for w in range(self.w0, self.w0 + 4):
            it, st, _ = self.pick(st, w)
            seq.append(it["title"])
        self.assertEqual(seq, ["T0", "T1", "T2", "T3"])

    def test_idempotent_in_same_window(self):
        """同一窗内重跑不推进：后台一窗可能构建好几次，跳条比晚一条更糟。"""
        it, st, _ = self.pick({}, 10)
        for _ in range(5):
            it2, st, _ = self.pick(st, 10)
            self.assertEqual(it2["title"], it["title"])

    def test_clears_after_full_cycle(self):
        """全池播完才清标记重来：cycle +1，shown 只剩刚上屏那一条。"""
        st = {}
        for w in range(self.w0, self.w0 + 4):
            _, st, _ = self.pick(st, w)
        it, st, _ = self.pick(st, self.w0 + 4)
        self.assertEqual(st["cycle"], 2)
        self.assertEqual(len(st["shown"]), 1)
        self.assertEqual(it["title"], "T0")
        self.assertEqual(st["back"], 0)         # 新一轮从池子最新的一条起

    def test_same_span_items_each_get_a_turn(self):
        """同一个 5 分钟片里的多条**各有一次机会**（这是 1.7.0 修掉的缺陷）。

        按片轮那阵同一片只出一条代表：早到的那条既当不上代表、也永远标注不上，
        等于进池即弃。现在按条轮，三条 = 三个座位。
        """
        items = [
            mk("B0先到", src="a", ts=1000 + 10),
            mk("B0后到", src="b", ts=1000 + 90),   # 同片、更新
            mk("B1", src="c", ts=1000 - 300 + 5),
        ]
        items.sort(key=lambda x: x["ts"], reverse=True)
        st, seq = {}, []
        for w in range(4, 7):
            it, st, _ = self.pick(st, w, items)
            seq.append(it["title"])
        self.assertEqual(seq, ["B0后到", "B0先到", "B1"])
        self.assertEqual(st["pool"], 3)            # 三条三个座位，不是两片

    def test_steady_state_airs_current_window(self):
        """稳态（每窗产出 3 条，消费 1 条）：每窗都播这一窗的新内容，延迟不累积。

        旧机制在这里会沿清单往下走：第 k 窗播「第 k 新」的那条，池子越大延迟越大，
        20 窗之后屏上已经是十几窗前的消息。锚定当前窗后 back 恒定在 0 条。
        """
        pool, st, titles, backs = [], {}, [], []
        for w in range(100, 120):
            # 本窗 3 条，发布时间比现在早 5~60 秒（源有发布延迟），都落在 w-1 片
            for k in range(3):
                pool.append(mk("T%d_%d" % (w, k), src="s%d" % k,
                               ts=w * self.dwell - 5 - k * 20))
            pool.sort(key=lambda x: x["ts"], reverse=True)
            it, st, _ = self.pick(st, w, pool)
            titles.append(it["title"])
            backs.append(st["back"])
        self.assertEqual(titles, ["T%d_0" % w for w in range(100, 120)])
        self.assertEqual(set(backs), {0})           # 恒定「最新那条」，不随轮次后退

    def test_prefers_recent_unplayed_over_deep_backlog(self):
        """最近有没播过的片时，不会跳到更旧的那批 —— 稳态的行为就是「先播新的」。

        规则只有一条「播全池最新的未播片」，所以只要最近几片还有没播的，最新未播片
        自然就落在最近这几片里；20 片前的那批要等前面播完才轮得到。
        换句话说：会一路往旧里走的前提是「最新未播片在很旧的地方」—— 那正是低产能时段。
        """
        base = 200
        recent = [self.at(base - k, "R%d" % (base - k)) for k in range(3)]
        deep = [self.at(base - 20 - k, "D%d" % (base - 20 - k)) for k in range(3)]
        pool = sorted(recent + deep, key=lambda x: x["ts"], reverse=True)
        st, seq, backs = {}, [], []
        for w in range(base + 1, base + 4):
            it, st, _ = self.pick(st, w, pool)
            seq.append(it["title"])
            backs.append(st["back"])
        self.assertEqual(seq, ["R200", "R199", "R198"])    # 旧的几条一条都没插进来
        self.assertEqual(backs, [0, 1, 2])                 # 落后量按条计数，逐窗加一

    def test_relaxes_to_whole_pool_when_recent_done(self):
        """最近几片都播过（真没新内容了）→ 播池子里最新的那片旧片，而不是原地打转。"""
        base = 200
        recent = [self.at(base - k, "R%d" % (base - k)) for k in range(3)]
        deep = [self.at(base - 20 - k, "D%d" % (base - 20 - k)) for k in range(3)]
        pool = sorted(recent + deep, key=lambda x: x["ts"], reverse=True)
        st = {}
        for w in range(base + 1, base + 4):
            _, st, _ = self.pick(st, w, pool)
        it, st, _ = self.pick(st, base + 4, pool)
        self.assertEqual(it["title"], "D180")            # 全池里最新的一条旧条目
        self.assertEqual(st["back"], 3)                  # 前面还压着 3 条更新的
        self.assertEqual(st["cycle"], 1)                 # 还没播完，不是重开一轮

    def test_backfills_when_newest_is_already_shown(self):
        """最新那条播过了就播次新的那一条，绝不空屏 —— 这条规则没有年龄门槛。

        曾经有个 `ROTATE_MAX_LAG` 想控制「往旧里找几片」，已删除：两级取的都是
        「全池未播里最新的一条」，参数取什么值都不改变结果（穷举 5 万组状态零反例，
        24 小时端到端 0/1/6/24 逐项相同）。留着只会让人以为能调新鲜度。
        """
        newest, second, third = self.at(101, "A"), self.at(100, "B"), self.at(99, "C")
        pool = [newest, second, third]              # 已按发布时间倒序
        ga = R.item_guid(newest)
        pick, shown, cycle = B._pick_item({"shown": [ga], "cycle": 1}, pool)
        self.assertEqual(R.item_guid(pick), R.item_guid(second))   # 最新的播过 → 次新
        self.assertEqual(shown, {ga})               # 补播不动已有标记
        pick2, _, cycle2 = B._pick_item(
            {"shown": [R.item_guid(it) for it in pool], "current": R.item_guid(third),
             "cycle": 1}, pool)
        self.assertEqual(R.item_guid(pick2), ga)    # 全播过 → 重开一轮，从最新那条起
        self.assertEqual(cycle2, 2)

    def test_new_item_airs_next_window(self):
        """核心用例：池子里还有旧片没播，新条目下一窗就能上屏。

        旧机制把清单冻结在开轮那一刻，新条目要等整轮播完（几小时）才可能上屏 ——
        这正是线上「屏幕上是 75 分钟前的消息」的成因。
        """
        st = {}
        w0 = 50
        pool = [self.at(w0, "OLD-0"), self.at(w0 - 1, "OLD-1")]
        it, st, _ = self.pick(st, w0, pool)
        self.assertEqual(it["title"], "OLD-0")

        pool.insert(0, self.at(w0 + 1, "NEW"))           # 下一窗来了一条更新的
        it, st, _ = self.pick(st, w0 + 1, pool)
        self.assertEqual(it["title"], "NEW")             # 立刻上屏，不排队等整轮
        self.assertEqual(st["back"], 0)

    def test_shown_is_trimmed(self):
        """shown 只留池子里还在的片：稳态跑 60 窗后它的规模由池子决定，不随时间增长。"""
        pool, st = [], {}
        for w in range(100, 160):
            pool.append(self.at(w - 1, "T%d" % w))
            pool.sort(key=lambda x: x["ts"], reverse=True)
            _, st, _ = self.pick(st, w, pool)
        self.assertLessEqual(len(st["shown"]), st["pool"])
        self.assertEqual(len(st["shown"]), st["pool"])   # 60 窗全播过，正好一片一个标记

    def test_stopped_production_restarts_cleanly(self):
        """产能停了（池子冻住）：仍要「一轮内不重播 → 全播完才回头」，cycle 真的在涨。

        这是 shown 裁剪规则的回归用例：按「最近的窗」裁会抹掉仍可能被选中的标记，
        结果是同一条隔几窗就重播、cycle 永远停在 1、lag 无限上涨。
        """
        pool = [self.at(b, "B%d" % b) for b in range(30)]
        st, seq, cycles = {}, [], []
        for w in range(31, 91):
            it, st, _ = self.pick(st, w, pool)
            seq.append(it["title"])
            cycles.append(st["cycle"])
        self.assertEqual(len(set(seq[:30])), 30)         # 一轮 30 片不重播
        self.assertEqual(seq[:30], seq[30:60])           # 播完才回头，顺序一致
        self.assertGreater(cycles[-1], cycles[0])        # cycle 在递增，不是卡住

    def test_future_dated_item_never_hogs_screen(self):
        """源时钟错乱（2030 年的 pubDate）不能让屏幕被它独吞。

        未来时间的片号永远最大，若参与轮播「取最近几片里最新的未播片」每窗都选它。
        现在的口径是「时间还没到就先不播」：它一次都不上屏，真正的新闻照常轮。
        """
        real = [self.at(100 - k, "R%d" % (100 - k)) for k in range(3)]
        future = mk("Y2030", src="bad", ts=1893456000)     # 2030-01-01
        pool = sorted(real + [future], key=lambda x: x["ts"], reverse=True)
        st, seq, notes = {}, [], []
        for w in range(101, 106):
            it, st, note = self.pick(st, w, pool)
            seq.append(it["title"])
            notes.append(note)
        self.assertNotIn("Y2030", seq)                     # 一次都没上屏
        self.assertEqual(seq[:3], ["R100", "R99", "R98"])  # 正常内容照常轮
        self.assertEqual(st["pool"], 3)                    # 未来那条不算进可播条数
        self.assertGreaterEqual(st["back"], 0)             # back 不会出现负数
        # 剔除不能是静默的：日志里必须能看出「有个源的时间戳在未来」
        self.assertIn("已排除出设备版轮播", notes[0])
        self.assertIn("另有 1 条", notes[0])

    def test_future_window_state_is_discarded(self):
        """状态里的 window 跑到未来（写它的那次运行时钟错了 / 文件被写坏）必须丢弃重来。

        不丢的话 w > prev_w 永远为假 → 一直 hold → window 永不前进 → 屏幕永久卡死，
        而且每次运行都会把这份坏状态原样读回来，自己治不了自己。
        """
        pool = [self.at(199, "R199"), self.at(198, "R198")]
        poisoned = {"version": B.ROTATE_STATE_VERSION, "window": 10 ** 6,
                    "back": 0, "current": "deadbeefdead", "shown": ["deadbeefdead"],
                    "cycle": 9, "pool": 2, "items": 2, "dwell": self.dwell}
        it, st, note = self.pick(poisoned, 200, pool)
        self.assertIsNotNone(it, "坏状态必须被丢掉，否则这一版就是空转")
        self.assertEqual(st["window"], 200)
        self.assertEqual(st["cycle"], 1)                   # 从头起轮，不是接着第 10 轮
        self.assertIn("新窗推进", note)

    def test_small_clock_rollback_still_holds(self):
        """小幅回拨（NTP 校正，CLOCK_SKEW_WINDOWS 以内）不丢状态：冻结一版就好。"""
        pool = [self.at(99, "屏上这条"), self.at(98, "旧的")]
        _, st, _ = self.pick({}, 100, pool)
        it, st2, note = self.pick(st, 100 - B.CLOCK_SKEW_WINDOWS, pool)
        self.assertEqual(it["title"], "屏上这条")
        self.assertIn("同窗保持", note)
        self.assertEqual(st2["window"], 100)               # 没有推进

    def test_large_clock_rollback_reinitialises(self):
        """回拨超过容忍窗口 → 丢状态重来（否则之后每次构建都 hold，永不推进）。"""
        pool = [self.at(99, "屏上这条"), self.at(98, "旧的")]
        _, st, _ = self.pick({}, 100, pool)
        it, st2, _ = self.pick(st, 100 - B.CLOCK_SKEW_WINDOWS - 1, pool)
        # 池子里的片对新窗号来说都还在未来 → 这一版没得播，但坏状态已经清掉
        self.assertIsNone(it)
        self.assertEqual(st2, {})

    def test_single_item_restart_does_not_crash(self):
        """池子只有一条：重开一轮只能重播同一条（无解），但绝不能抛 IndexError。"""
        pool = [self.at(10, "唯一一条")]
        st, seq = {}, []
        for w in range(11, 14):
            it, st, _ = self.pick(st, w, pool)
            seq.append(it["title"])
        self.assertEqual(seq, ["唯一一条"] * 3)
        self.assertEqual(st["cycle"], 3)                   # 每窗都算「播完一轮重开」

    def test_same_window_ignores_fresh_arrivals(self):
        """同窗内又抓到更晚发布的条目：屏幕**照旧播这一条**，不追新。

        1.7.0 之前这里是「片内代表被顶掉 → 照播新代表」；按条轮之后屏上那条由 guid
        认，        不再有代表的概念 —— 同一窗里换内容比晚 5 分钟看到那条更新的更伤，
        而且「一条刚看到没多久就被顶掉」久了会让人以为设备在跳条。
        """
        w = 60
        pool = [mk("A", src="s1", ts=w * self.dwell - 200)]
        it, st, _ = self.pick({}, w, pool)
        self.assertEqual(it["title"], "A")

        pool.insert(0, mk("A2", src="s2", ts=w * self.dwell - 100))   # 同窗、更晚
        it2, st, note = self.pick(st, w, pool)
        self.assertEqual(it2["title"], "A")                # 屏幕不换台
        self.assertEqual(st["current"], R.item_guid(it2))  # 状态与屏上内容一致
        self.assertIn("同窗保持", note)

    def test_old_state_discarded(self):
        """旧版状态（带 queue 的 v2）直接丢弃：结构变了，宁可重复一条也不能乱序。"""
        legacy = {"version": 2, "window": 10, "bucket": 3, "current": "x",
                  "queue": [3, 2, 1], "shown": [3], "cycle": 4, "dwell": self.dwell}
        it, st, _ = self.pick(legacy, 11)
        self.assertIsNotNone(it)
        self.assertEqual(st["version"], B.ROTATE_STATE_VERSION)
        self.assertNotIn("queue", st)
        self.assertEqual(st["cycle"], 1)                 # 从第 1 轮重新开始

    def test_no_repeat_within_cycle(self):
        st, seen = {}, []
        for w in range(self.w0, self.w0 + 6):
            it, st, _ = self.pick(st, w)
            seen.append(it["title"])
        self.assertEqual(len(seen[:4]), len(set(seen[:4])))


class TestFullFeed(unittest.TestCase):
    """完整版（给电脑 / 手机阅读器）必须有正文。

    以前的坑：完整版把正文写死成空串，摘要全丢，阅读器里只剩标题，
    「RSS 里的其他文字尽量显示」这条要求在完整版上完全没兑现。
    """

    def test_summary_present(self):
        it = {"title": "标题", "link": "http://x/1", "src": "s", "ts": 1,
              "summary": "这是原 feed 的导语，阅读器里应该看得到。"}
        xml = R.build_rss([it], "http://x/feed.xml", {"total": 1})
        self.assertIn("<description>这是原 feed 的导语", xml)

    def test_summary_truncated_by_feed_body_max(self):
        """显式传 body_max=300 时超长摘要要截断补省略号。

        刻意不依赖 R.FEED_BODY_MAX 的环境默认值：默认值是调参对象（1.3.0 起默认 0 = 不限），
        测试不该跟随调参漂移，行为测试只锁函数本身。
        """
        body = R.compose_body("", "字" * 900, "标题", body_max=300)
        self.assertLessEqual(R.text_width(body), 300)
        self.assertTrue(body.endswith("…"))

    def test_summary_not_truncated_when_body_max_zero(self):
        """body_max=0 = 不限（完整版默认口径）：超长摘要原样返回，不出现省略号。"""
        body = R.compose_body("", "字" * 900, "标题", body_max=0)
        self.assertEqual(len(body), 900)
        self.assertNotIn("…", body)

    def test_title_not_truncated_in_full_feed(self):
        """完整版标题要原样保留 —— 截断只该发生在设备版。"""
        long_title = "很长" * 20
        xml = R.build_rss([{"title": long_title, "link": "http://x/1", "src": "s",
                            "ts": 1}], "http://x/feed.xml", {})
        self.assertIn("<title>%s</title>" % long_title, xml)


class TestDecode(unittest.TestCase):
    """解码顺序：HTTP 头声明的编码 → UTF-8 → GB18030 → 带替换的 UTF-8。"""

    def test_charset_from_header_wins(self):
        self.assertEqual(B.decode_body("中文标题".encode("gbk"), "gbk"), "中文标题")

    def test_gb18030_fallback(self):
        self.assertEqual(B.decode_body("中文标题".encode("gb18030")), "中文标题")

    def test_bogus_charset_falls_back(self):
        # 源站声明的编码名本地不认（LookupError）也要能退到 UTF-8，不能整源失败
        self.assertEqual(B.decode_body("中文".encode("utf-8"), "x-not-a-charset"), "中文")


class TestFeedList(unittest.TestCase):
    """feeds.txt 解析：注释行 = 下一个地址的源名；写错的行要被收集而不是静默丢掉。"""

    def test_name_from_comment_and_skipped_lines(self):
        text = "# 财联社·电报\nhttps://a/1.xml\n这一行不是地址\nhttps://a/2.xml\n"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write(text)
            path = f.name
        try:
            skipped = []
            got = B.read_feed_entries(path, skipped)
        finally:
            os.unlink(path)
        self.assertEqual([e["name"] for e in got], ["财联社·电报", "https://a/2.xml"])
        self.assertEqual(len(skipped), 1)

    def test_duplicate_url_dropped(self):
        text = "https://a/1.xml\nhttps://a/1.xml\n"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write(text)
            path = f.name
        try:
            self.assertEqual(len(B.read_feed_entries(path)), 1)
        finally:
            os.unlink(path)


class TestParseItems(unittest.TestCase):
    """解析层：RSS 2.0 与 Atom 都要能读，正文按「原文优先」取。"""

    def test_atom_entry(self):
        xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
               "<title>标题甲</title><link href=\"https://a/1\"/>"
               "<updated>2026-10-29T12:00:00Z</updated><summary>摘要甲</summary>"
               "</entry></feed>")
        got = B.parse_items(xml, "https://a/feed", 3)
        self.assertEqual(got[0]["title"], "标题甲")
        self.assertEqual(got[0]["link"], "https://a/1")
        self.assertEqual(got[0]["summary"], "摘要甲")
        self.assertIsNotNone(got[0]["ts"])

    def test_content_encoded_preferred(self):
        xml = ('<rss version="2.0" '
               'xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
               "<title>标题乙</title><link>https://b/1</link>"
               "<description>简短摘要</description>"
               "<content:encoded>完整正文</content:encoded>"
               "<pubDate>Thu, 29 Oct 2026 12:00:00 +0800</pubDate>"
               "</item></channel></rss>")
        got = B.parse_items(xml, "https://b/feed", 3)
        self.assertEqual(got[0]["summary"], "完整正文")

    def test_sorted_desc_and_limited(self):
        xml = "<rss><channel>%s</channel></rss>" % "".join(
            "<item><title>T%d</title><link>https://c/%d</link>"
            "<pubDate>Thu, 29 Oct 2026 1%d:00:00 +0800</pubDate></item>" % (i, i, i)
            for i in range(1, 6))
        got = B.parse_items(xml, "https://c/feed", 2)
        self.assertEqual([g["title"] for g in got], ["T5", "T4"])

    def test_same_ts_order_is_deterministic(self):
        """同一时间戳的多条，输出次序必须唯一（次级键 guid）。

        并发抓取的到达顺序、源自己的抖动都会让文档顺序在两次运行里不同；
        排序是稳定的，不补次级键就会「同样的数据排出两个池子」，
        而轮播按片取第一条当代表 —— 屏幕会莫名换内容。
        """
        def doc(titles):
            return "<rss><channel>%s</channel></rss>" % "".join(
                "<item><title>%s</title><link>https://d/%s</link>"
                "<pubDate>Thu, 29 Oct 2026 12:00:00 +0800</pubDate></item>" % (t, t)
                for t in titles)

        a = B.parse_items(doc(["甲甲甲甲甲", "乙乙乙乙乙", "丙丙丙丙丙"]), "https://d/feed", 5)
        b = B.parse_items(doc(["丙丙丙丙丙", "甲甲甲甲甲", "乙乙乙乙乙"]), "https://d/feed", 5)
        self.assertEqual([g["title"] for g in a], [g["title"] for g in b])


class TestWorkflowParams(unittest.TestCase):
    """workflow 里显式写的调参必须与代码默认值一致，且三份 workflow 之间必须一致。

    minute.yml 是 gen_minute_workflow.py 的产物，rss.yml / nightly.yml 是手写的；
    参数改默认值时只改其中一处，另一处就会把线上产物悄悄改回旧口径
    （2026-10-29 实测：完整版 228 条被「改源即时发布」盖回 120 条）。

    两个方向都要盯：
      * **值**：写了就必须等于代码默认值（test_workflow_env_matches_code_defaults）；
      * **全**：生成器模板里的每个参数，另两份手写工作流也得写出来
        （test_handwritten_workflows_carry_all_tuned_params）—— 少写一项不会报错，
        只会悄悄落回代码默认值，等默认值一改，那一条路径就与主线不一致了。
    """

    KEYS = {
        "PER_SOURCE": lambda: __import__("build_feed").PER_SOURCE,
        "ITEM_LIMIT": lambda: __import__("build_feed").ITEM_LIMIT,
        "FEED_BODY_MAX": lambda: __import__("render").FEED_BODY_MAX,
        "ROTATE_POOL_MAX": lambda: __import__("rotate").ROTATE_POOL_MAX,
    }
    # 生成器模板是「参数全集」的权威副本；轮次号每轮自增，不参与比对
    GENERATED = "minute.yml"
    HANDWRITTEN = ("rss.yml", "nightly.yml")
    PER_CYCLE = {"CYCLE"}

    @staticmethod
    def _wf_dir():
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".github", "workflows")

    @staticmethod
    def _read(name):
        with open(os.path.join(TestWorkflowParams._wf_dir(), name), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _env_values(src):
        """抓 env 段里的 `KEY: "value"`，返回 {KEY: value}（同名取最后一次）。"""
        return {m.group(1): m.group(2)
                for m in re.finditer(r'^\s*([A-Z][A-Z0-9_]*):\s*"?([^"\n]*?)"?\s*$',
                                     src, re.M)}

    def test_workflow_env_matches_code_defaults(self):
        for path in glob.glob(os.path.join(self._wf_dir(), "*.yml")):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            for key, getter in self.KEYS.items():
                # 「扫到就比、扫不到不报」：刻意只在某个 workflow 覆盖参数不算错，
                # 但只要写了，就必须与代码默认值一致。
                for m in re.finditer(r'^\s*%s:\s*"?([0-9.]+)"?\s*$' % key, src, re.M):
                    self.assertEqual(
                        float(m.group(1)), float(getter()),
                        "%s 里的 %s=%s 与代码默认值 %s 不一致"
                        % (os.path.basename(path), key, m.group(1), getter()))

    def test_handwritten_workflows_carry_all_tuned_params(self):
        tuned = {k: v for k, v in self._env_values(self._read(self.GENERATED)).items()
                 if k not in self.PER_CYCLE}
        self.assertTrue(tuned, "生成器模板里一个参数都没解析到，正则或模板变了")
        for name in self.HANDWRITTEN:
            got = self._env_values(self._read(name))
            for key, want in sorted(tuned.items()):
                self.assertIn(key, got, "%s 少了参数 %s，会悄悄落回代码默认值" % (name, key))
                self.assertEqual(got[key], want,
                                 "%s 的 %s=%s 与 %s 的 %s 不一致"
                                 % (name, key, got[key], self.GENERATED, want))


if __name__ == "__main__":
    unittest.main(verbosity=2)
