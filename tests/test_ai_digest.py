"""Pi/ai_digest.py 的离线测试：真起 Gemini 会烧配额且不稳，这里把网络调用打桩。

覆盖：feed 解析、提示词构造、模型返回→原文映射、HTML 渲染（自 canonical / 无站内
交叉链接 / 外链原文）、以及最关键的兜底——API 出错时绝不连累主链路、保留上一期。
"""
import io
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Pi"))
import ai_digest as AD  # noqa: E402

_FEED = """<?xml version="1.0"?>
<rss><channel>
<item><title>甲事件</title><link>http://a/1</link>
<description>摘要甲</description><pubDate>Mon, 01 Jan 2026 00:00:00 +0800</pubDate>
<source>来源A</source></item>
<item><title>乙事件</title><link>http://b/2</link>
<description>摘要乙</description><pubDate>Tue, 02 Jan 2026 00:00:00 +0800</pubDate>
<source>来源B</source></item>
<item><title>丙事件</title><link>http://c/3</link>
<description>摘要丙</description><pubDate>Wed, 03 Jan 2026 00:00:00 +0800</pubDate>
<source>来源C</source></item>
</channel></rss>"""

_OTHER_PAGES = ("index.html", "screen.html", "board.html", "subscribe.html",
                "/rss/screen.html")


def _write_feed(tmp):
    p = os.path.join(tmp, "feed.xml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(_FEED)
    return p


class TestLoadItems(unittest.TestCase):
    def test_load_items_basic(self):
        with mock.patch.object(AD, "DEFAULT_FEED", "/nope"):
            pass
        import tempfile
        d = tempfile.mkdtemp()
        p = _write_feed(d)
        items = AD.load_items(p)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "甲事件")
        self.assertEqual(items[0]["source"], "来源A")
        self.assertEqual(items[0]["link"], "http://a/1")

    def test_load_items_missing_file(self):
        self.assertEqual(AD.load_items("/no/such/feed.xml"), [])


class TestBuildPrompt(unittest.TestCase):
    def test_build_prompt_includes_items_and_count(self):
        items = AD.load_items(_write_feed(_tmp()))
        prompt = AD.build_prompt(items, 20, "openai")
        self.assertIn("甲事件", prompt)
        self.assertIn("来源B", prompt)
        self.assertIn("20", prompt)
        # 新契约：模型必须回显它选中的标题（title），配对由 title↔reason 锁定
        self.assertIn("title", prompt)
        self.assertIn("reason", prompt)
        # 每条精华概况限制为 ≤10 汉字（2026-11-06 需求：压缩成一页快速扫读的极简要点）
        self.assertIn("10", prompt)
        self.assertIn("精华", prompt)

    def test_prompt_tail_openai_asks_items_object(self):
        items = AD.load_items(_write_feed(_tmp()))
        prompt = AD.build_prompt(items, 20, "openai")
        self.assertIn("items", prompt)
        self.assertIn("JSON 对象", prompt)

    def test_prompt_tail_gemini_asks_array(self):
        items = AD.load_items(_write_feed(_tmp()))
        prompt = AD.build_prompt(items, 20, "gemini")
        self.assertIn("JSON 数组", prompt)
        self.assertNotIn("items", prompt)


def _tmp():
    import tempfile
    d = tempfile.mkdtemp()
    return d


class TestParseResponse(unittest.TestCase):
    def test_parse_response_maps_and_orders(self):
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"idx": 3, "reason": "丙重要"}, {"idx": 1, "reason": "甲重要"}]
        out = AD.parse_response(gem, items)
        self.assertEqual([r["title"] for r in out], ["丙事件", "甲事件"])
        self.assertEqual(out[0]["rank"], 1)
        self.assertEqual(out[0]["reason"], "丙重要")

    def test_parse_response_skips_bad_idx(self):
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"idx": 99, "reason": "越界"}, {"idx": 2, "reason": "乙"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "乙事件")

    def test_parse_response_empty_on_nonlist(self):
        items = AD.load_items(_write_feed(_tmp()))
        self.assertEqual(AD.parse_response({"idx": 1}, items), [])

    def test_parse_response_matches_by_echoed_title(self):
        # 新契约：模型回显 title，没有 idx 也能对上原文
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": "乙事件", "reason": "乙重要"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "乙事件")
        self.assertEqual(out[0]["link"], "http://b/2")
        self.assertEqual(out[0]["reason"], "乙重要")

    def test_parse_response_title_match_tolerates_whitespace(self):
        # 模型回显多了/少了空格：去空白后仍应精确命中，而不是落到包含式容错
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": " 乙 事件 ", "reason": "乙重要"}]
        out = AD.parse_response(gem, items)
        self.assertEqual([r["title"] for r in out], ["乙事件"])

    def test_parse_response_prefers_title_over_idx(self):
        # 两者都在时以 title 为准 —— idx 恰恰是小模型最容易配错位的字段
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": "乙事件", "idx": 1, "reason": "为什么值得读"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(out[0]["title"], "乙事件")

    def test_parse_response_neutralizes_reason_that_is_another_title(self):
        # 2026-11-06 线上失配实拍：reason 抄了**别的**新闻的标题 —— 必须判无效，
        # 页面绝不能出现「标题 A 配小字 B」
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": "甲事件", "reason": "乙事件"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(out[0]["reason"], "")

    def test_parse_response_neutralizes_reason_that_is_own_title(self):
        # 退一步的偷懒写法：reason 原样复述本条标题，同样不是理由
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": "甲事件", "reason": "甲事件"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(out[0]["reason"], "")

    def test_parse_response_dedupes_repeated_title(self):
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": "甲事件", "reason": "甲重要"},
               {"title": "甲事件", "reason": "又是甲"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["reason"], "甲重要")

    def test_parse_response_skips_non_dict_and_unmatchable(self):
        # 非字典元素直接跳过；回显标题对不上任何原文又没有 idx，同样跳过
        items = AD.load_items(_write_feed(_tmp()))
        gem = ["garbage", {"title": "池子里没有的标题", "reason": "x"},
               {"reason": "没有 title 也没有 idx"}]
        self.assertEqual(AD.parse_response(gem, items), [])

    def test_parse_response_similar_title_rescues_minor_edit(self):
        # 简繁归一之后的剩余情况：模型轻微改写（漏个「的」）—— 精确/包含/
        # 归一都对不上，相似度 0.9+ 兜底救回
        feed = _FEED.replace("甲事件", "研究战后的日本对中国的影响")
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "feed.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(feed)
        items = AD.load_items(p)
        gem = [{"title": "研究战后日本对中国的影响", "reason": "为何重要"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "研究战后的日本对中国的影响")
        self.assertEqual(out[0]["reason"], "为何重要")

    def test_t2s_table_wellformed(self):
        # 每个词固定「繁简」两字、彼此不同、繁体键不重复（maketrans 遇重复键
        # 会静默取后一个，写表时手滑进了重复项只会悄悄丢映射）
        toks = AD._T2S_PAIRS.split()
        self.assertTrue(all(len(t) == 2 for t in toks))
        self.assertTrue(all(t[0] != t[1] for t in toks))
        keys = [t[0] for t in toks]
        self.assertEqual(len(keys), len(set(keys)),
                         "繁体键有重复：%s" % [k for k in keys if keys.count(k) > 1])

    def test_norm_title_normalizes_script(self):
        # 简繁归一（只影响比对）：繁体/混写都归到简体；简体过表不变
        self.assertEqual(AD._norm_title("銀河娛樂"), "银河娱乐")
        self.assertEqual(AD._norm_title("研究戰後的日本對中國"),
                         "研究战后的日本对中国")
        self.assertEqual(AD._norm_title("已经简化"), "已经简化")

    def test_parse_response_script_normalized_match(self):
        # 线上实拍的放大版：池子里整条繁体、模型整条转成简体回显 ——
        # 归一后精确命中，不再丢条；页面标题仍是池子原文（繁体）
        feed = _FEED.replace("乙事件", "研究戰後的日本對中國有什麼用")
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "feed.xml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(feed)
        items = AD.load_items(p)
        gem = [{"title": "研究战后的日本对中国有什么用", "reason": "为何重要"}]
        out = AD.parse_response(gem, items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "研究戰後的日本對中國有什麼用")
        self.assertEqual(out[0]["reason"], "为何重要")

    def test_parse_response_similar_title_drops_real_paraphrase(self):
        # 模型自己改写的「另一个说法」相似度过不了线 → 宁可丢，也不能硬配
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": "完全不相干的一句话标题而已", "reason": "x"}]
        self.assertEqual(AD.parse_response(gem, items), [])

    def test_keep_top_truncates_to_n_keeping_order(self):
        # 多问几条做缓冲后，按模型返回顺序只保留前 n 条（已正确配对），多余丢弃；
        # 这是凑满「一页 20 条」又不冒配错风险的机制
        items = AD.load_items(_write_feed(_tmp()))
        gem = [{"title": t, "reason": "r"} for t in
               ["甲事件", "乙事件", "丙事件"]]
        out = AD.parse_response(gem, items)
        self.assertEqual(len(out), 3)
        self.assertEqual(len(AD._keep_top(out, 2)), 2)
        self.assertEqual([r["title"] for r in AD._keep_top(out, 2)],
                         ["甲事件", "乙事件"])


class TestFmtDateSgt(unittest.TestCase):
    def test_gmt_converted_to_sgt(self):
        # 用户要求：时间显示统一为新加坡时间。GMT 04:21 → 新加坡 12:21
        self.assertEqual(AD._fmt_date_sgt("Fri, 06 Nov 2026 04:21:57 GMT"),
                         "11-06 12:21 SGT")

    def test_plus8_unchanged(self):
        # 已经是 +0800 的源：数值不动，只是换统一格式
        self.assertEqual(AD._fmt_date_sgt("Fri, 06 Nov 2026 11:45:36 +0800"),
                         "11-06 11:45 SGT")

    def test_naive_date_assumed_sgt(self):
        # 源没写时区：按项目惯例（SOURCE_TZ_OFFSET=8）当 +8，不猜 UTC
        self.assertEqual(AD._fmt_date_sgt("06 Nov 2026 09:30:00"),
                         "11-06 09:30 SGT")

    def test_garbage_kept_as_is(self):
        # 完全不是日期：原样显示，这页不因时间脏而丢条
        self.assertEqual(AD._fmt_date_sgt("不是日期"), "不是日期")
        self.assertEqual(AD._fmt_date_sgt(""), "")


class TestRenderHtml(unittest.TestCase):
    def test_render_html_self_canonical_and_no_cross_links(self):
        items = AD.load_items(_write_feed(_tmp()))
        results = AD.parse_response(
            [{"idx": 1, "reason": "甲重要"}, {"idx": 2, "reason": "乙重要"}], items)
        html = AD.render_html(results, "gemini-1.5-flash")
        self.assertIn('<link rel="canonical" href="%s">' % AD.CANONICAL, html)
        for other in _OTHER_PAGES:
            self.assertNotIn(other, html,
                             "独立页不应出现指向其它页面的链接/引用：%s" % other)
        # 应外链到原文
        self.assertIn("http://a/1", html)
        self.assertIn("http://b/2", html)

    def test_render_html_links_original(self):
        items = AD.load_items(_write_feed(_tmp()))
        results = AD.parse_response([{"idx": 3, "reason": "丙"}], items)
        html = AD.render_html(results, "gemini-1.5-flash")
        self.assertIn('href="http://c/3"', html)
        self.assertIn('target="_blank"', html)

    def test_render_html_reports_pool_size_not_result_count(self):
        items = AD.load_items(_write_feed(_tmp()))
        results = AD.parse_response([{"idx": 1, "reason": "甲"}], items)
        # 只命中 1 条结果，但候选池是 3 条 —— 文案必须显示池大小，而非结果数
        html = AD.render_html(results, "gemini-1.5-flash", pool_size=len(items))
        self.assertIn("从当日 3 条 RSS 候选中筛选", html)
        self.assertNotIn("从当日 1 条 RSS 候选中筛选", html)

    def test_render_html_truncates_essence_to_10_chars(self):
        items = AD.load_items(_write_feed(_tmp()))
        # 模型偶发给超 10 字，渲染端必须硬截断到 10 字（与「宁丢不配错」一致）
        results = AD.parse_response([{"idx": 1, "reason": "甲事件引发市场剧烈波动值得关注"}], items)
        html = AD.render_html(results, "gemini-1.5-flash", pool_size=len(items))
        # 前 10 字：甲事件引发市场剧烈波
        self.assertIn("<p class=\"reason\">甲事件引发市场剧烈波</p>", html)
        # 截断后「关注」二字不应再出现在页面任何位置（标题为甲/乙/丙，不含「关注」）
        self.assertNotIn("关注", html)


class TestPoolSignature(unittest.TestCase):
    def _items(self):
        return AD.load_items(_write_feed(_tmp()))

    def test_signature_stable_for_same_items(self):
        items = self._items()
        self.assertEqual(AD._pool_signature(items), AD._pool_signature(list(items)))

    def test_signature_changes_when_item_added(self):
        items = self._items()
        sig0 = AD._pool_signature(items)
        mutated = list(items) + [{"title": "新增条目", "link": "http://x/new",
                                  "date_raw": "Fri, 06 Nov 2026 00:00:00 +0800"}]
        self.assertNotEqual(sig0, AD._pool_signature(mutated))


class TestBudgetItems(unittest.TestCase):
    """送选按 token 预算截取前缀：存储（400）与送选（≈196 条）分离的下半场。"""

    def _items(self):
        return AD.load_items(_write_feed(_tmp()))

    def test_all_kept_within_budget(self):
        # 预算装得下全池：一条不少、顺序不变
        items = self._items()
        budget = AD._PROMPT_OVERHEAD_TOKENS + 3 * AD._EST_TOKENS_PER_ITEM
        self.assertEqual(AD._budget_items(items, budget), items)

    def test_over_budget_keeps_newest_prefix_in_order(self):
        # 装不下：只留「最新在前」的前缀，顺序不变（_merge_accum 已保证 newest-first）
        items = self._items()
        budget = AD._PROMPT_OVERHEAD_TOKENS + 2 * AD._EST_TOKENS_PER_ITEM
        self.assertEqual([it["title"] for it in AD._budget_items(items, budget)],
                         ["甲事件", "乙事件"])

    def test_tiny_budget_still_keeps_one(self):
        # 预算被调得极小：宁可超预算也至少送 1 条，绝不让页面交白卷
        items = self._items()
        self.assertEqual(len(AD._budget_items(items, 550)), 1)
        self.assertEqual(len(AD._budget_items(items, 0)), 1)

    def test_empty_items_returns_empty(self):
        # 空列表由上游兜底（feed 为空走占位页），这里原样返回空
        self.assertEqual(AD._budget_items([], 20000), [])


class TestBadEnvImport(unittest.TestCase):
    """模块级 int(env) 防崩回归：unit 的 EnvironmentFile 配错也不能崩 import。"""

    def test_bad_numeric_env_does_not_crash_import(self):
        # subprocess 而非 reload：import 恰好只发生一次，最接近 Pi 上服务
        # 「启动即 import」的真实路径；reload 会复用已编译模块，测不出 L98 的崩。
        env = dict(os.environ, AI_ACCUM_CAP="abc",
                   AI_PROMPT_TOKEN_BUDGET="20000 tokens")
        r = subprocess.run(
            [sys.executable, "-c",
             "import ai_digest as m;"
             " print(m.ACCUM_CAP, m.AI_PROMPT_TOKEN_BUDGET)"],
            env=env, cwd=os.path.dirname(os.path.abspath(AD.__file__)),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 坏值分别回退各自默认值，而不是带着半初始化模块退出
        # （坏输入故意用 "20000 tokens"≠默认值，证明回退不是原样透传）
        self.assertEqual(r.stdout.strip(), "2000 30000")


class TestMainFallback(unittest.TestCase):
    def _run_main(self, tmp, prev_exists, call_side_effect, provider="openai"):
        feed = _write_feed(tmp)
        out = os.path.join(tmp, "ai-digest.html")
        if prev_exists:
            with open(out, "w", encoding="utf-8") as f:
                f.write("<html>PREV</html>")
        if provider == "gemini":
            env = {"FEED_XML": feed, "OUT_HTML": out, "AI_TOP_N": "2",
                   "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "x",
                   "GEMINI_MODEL": "gemini-1.5-flash"}
            target = "call_gemini"
        else:
            env = {"FEED_XML": feed, "OUT_HTML": out, "AI_TOP_N": "2",
                   "LLM_PROVIDER": "openai", "LLM_API_KEY": "x",
                   "LLM_MODEL": "deepseek-chat"}
            target = "call_openai"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(AD, target, side_effect=call_side_effect):
            rc = AD.main()
        return rc, out

    def test_main_keeps_previous_on_api_error(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        rc, out = self._run_main(tmp, prev_exists=True,
                                 call_side_effect=RuntimeError("boom"))
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "<html>PREV</html>")

    def test_main_writes_placeholder_when_missing(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        rc, out = self._run_main(tmp, prev_exists=False,
                                 call_side_effect=RuntimeError("boom"))
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as f:
            txt = f.read()
        self.assertIn(AD.CANONICAL, txt)  # 占位页也自 canonical
        self.assertNotIn("PREV", txt)

    def test_main_keeps_previous_gemini_backend(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        rc, out = self._run_main(tmp, prev_exists=True,
                                 call_side_effect=RuntimeError("boom"),
                                 provider="gemini")
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "<html>PREV</html>")

    def test_main_skips_cf_when_feed_unchanged(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        feed = _write_feed(tmp)
        out = os.path.join(tmp, "ai-digest.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write("<html>PREV</html>")
        # 预写与当前池一致的指纹：源未变，应当跳过 CF 调用
        items = AD.load_items(feed)
        with open(out + ".sig", "w", encoding="utf-8") as f:
            f.write(AD._pool_signature(items))
        env = {"FEED_XML": feed, "OUT_HTML": out, "AI_TOP_N": "2",
               "LLM_PROVIDER": "openai", "LLM_API_KEY": "x",
               "LLM_MODEL": "deepseek-chat"}
        fake = mock.MagicMock()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(AD, "call_openai", fake):
            rc = AD.main()
        self.assertEqual(rc, 0)
        fake.assert_not_called()  # 没有消耗任何 CF 额度
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "<html>PREV</html>")

    def test_main_sends_budget_limited_prefix(self):
        # 送选按 token 预算截取：预算 550（固定开销 500 + 1 条 150 装不下）→
        # prompt 只含最新的 1 条候选；页面文案的候选数也写 1（AI 看到几条写几条）
        import tempfile
        tmp = tempfile.mkdtemp()
        feed = _write_feed(tmp)
        out = os.path.join(tmp, "ai-digest.html")
        fake = mock.MagicMock(return_value=[{"title": "甲事件", "reason": "甲"}])
        env = {"FEED_XML": feed, "OUT_HTML": out, "AI_TOP_N": "2",
               "LLM_PROVIDER": "openai", "LLM_API_KEY": "x",
               "LLM_MODEL": "deepseek-chat", "AI_PROMPT_TOKEN_BUDGET": "550"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(AD, "call_openai", fake):
            rc = AD.main()
        self.assertEqual(rc, 0)
        prompt = fake.call_args[0][4]  # call_openai(base, key, model, system, prompt)
        self.assertIn("甲事件", prompt)
        self.assertNotIn("乙事件", prompt)  # 预算只装得下最新 1 条
        self.assertNotIn("丙事件", prompt)
        with open(out, encoding="utf-8") as f:
            self.assertIn("从当日 1 条 RSS 候选中筛选", f.read())

    def test_main_calls_cf_when_feed_changed(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        feed = _write_feed(tmp)
        out = os.path.join(tmp, "ai-digest.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write("<html>PREV</html>")
        # 指纹与当前池不一致（陈旧）：应当触发 CF 重算
        with open(out + ".sig", "w", encoding="utf-8") as f:
            f.write("stale-signature")
        env = {"FEED_XML": feed, "OUT_HTML": out, "AI_TOP_N": "2",
               "LLM_PROVIDER": "openai", "LLM_API_KEY": "x",
               "LLM_MODEL": "deepseek-chat"}
        fake = mock.MagicMock(return_value=[{"idx": 1, "reason": "甲"}])
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(AD, "call_openai", fake):
            rc = AD.main()
        self.assertEqual(rc, 0)
        fake.assert_called_once()  # 真的去重算了
        with open(out + ".sig", encoding="utf-8") as f:
            # 成功后刷新成新指纹，下次若仍不变才会跳过
            self.assertNotEqual(f.read(), "stale-signature")


class TestCallGeminiRequest(unittest.TestCase):
    def test_call_gemini_builds_request(self):
        import json as _json
        captured = {}

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake_text = _json.dumps([{"idx": 1, "reason": "x"}])
        payload = _json.dumps(
            {"candidates": [{"content": {"parts": [{"text": fake_text}]}}]})

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["req"] = req
            captured["data"] = req.data
            return FakeResp(payload.encode("utf-8"))

        items = AD.load_items(_write_feed(_tmp()))
        prompt = AD.build_prompt(items, 20, "gemini")
        with mock.patch.object(AD.urllib.request, "urlopen", fake_urlopen):
            out = AD.call_gemini("KEY123", "gemini-1.5-flash", AD.SYSTEM, prompt)
        self.assertEqual(out, [{"idx": 1, "reason": "x"}])
        self.assertIn("gemini-1.5-flash", captured["url"])
        self.assertIn("key=KEY123", captured["url"])
        body = _json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(body["generationConfig"]["responseMimeType"],
                         "application/json")


class TestCallOpenaiRequest(unittest.TestCase):
    def test_call_openai_builds_request(self):
        import json as _json
        captured = {}

        class FakeResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake_text = _json.dumps([{"idx": 2, "reason": "y"}])
        payload = _json.dumps({"choices": [{"message": {"content": fake_text}}]})

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["req"] = req
            captured["data"] = req.data
            captured["headers"] = req.headers
            return FakeResp(payload.encode("utf-8"))

        items = AD.load_items(_write_feed(_tmp()))
        prompt = AD.build_prompt(items, 20, "openai")
        with mock.patch.object(AD.urllib.request, "urlopen", fake_urlopen):
            out = AD.call_openai(AD.DEFAULT_LLM_BASE_URL, "KEY456",
                                 "@cf/meta/llama-3.1-8b-instruct-fp8-fast", AD.SYSTEM, prompt)
        self.assertEqual(out, [{"idx": 2, "reason": "y"}])
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertIn("Bearer KEY456", str(captured["headers"]))
        body = _json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(body["model"], "@cf/meta/llama-3.1-8b-instruct-fp8-fast")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["messages"][0]["role"], "system")


class TestExtractJsonArray(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(AD._extract_json_array('[{"idx":1,"reason":"x"}]'),
                         [{"idx": 1, "reason": "x"}])

    def test_fenced_json(self):
        text = '好的，结果如下：\n```json\n[{"idx":2,"reason":"y"}]\n```'
        self.assertEqual(AD._extract_json_array(text),
                         [{"idx": 2, "reason": "y"}])

    def test_prose_wrapped_array(self):
        text = '以下是筛选结果 [{"idx":3,"reason":"z"}] 仅供参考。'
        self.assertEqual(AD._extract_json_array(text),
                         [{"idx": 3, "reason": "z"}])

    def test_items_wrapper(self):
        text = '{"items":[{"idx":4,"reason":"w"}]}'
        self.assertEqual(AD._extract_json_array(text),
                         [{"idx": 4, "reason": "w"}])

    def test_generic_object_wrapper(self):
        # json_object 格式强制顶层为对象，模型可能自选键名（如 {"fruits":[...]}）；
        # 应取第一个值为列表的键，而不是死等 "items"
        text = '{"fruits":[{"idx":5,"reason":"v"}]}'
        self.assertEqual(AD._extract_json_array(text),
                         [{"idx": 5, "reason": "v"}])

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            AD._extract_json_array("完全没有 JSON 的文字")
        with self.assertRaises(ValueError):
            AD._extract_json_array("")


class TestDedupSimilar(unittest.TestCase):
    def _mk(self, titles):
        return [{"title": t, "link": "http://x/%d" % i, "source": "S",
                 "summary": "s", "date": "", "date_raw": ""}
                for i, t in enumerate(titles)]

    def test_folds_exact_duplicates(self):
        # 两条标题逐字相同（不同源）→ 折叠成 1 条，保留最新（列表首条）
        out = AD._dedup_similar(self._mk(["甲事件", "甲事件", "乙事件"]))
        self.assertEqual([o["title"] for o in out], ["甲事件", "乙事件"])

    def test_keeps_distinct(self):
        out = AD._dedup_similar(self._mk(["甲事件", "乙事件", "丙事件"]))
        self.assertEqual(len(out), 3)

    def test_single_and_empty(self):
        self.assertEqual(AD._dedup_similar(self._mk(["甲事件"])),
                         self._mk(["甲事件"]))
        self.assertEqual(AD._dedup_similar([]), [])

    def test_near_duplicate_default_threshold(self):
        # 「台风登陆台湾岛」与「台风登陆台湾」语义近似（差一字）→ 默认阈值折叠，
        # 保留最新那条；与真正不同的「法国大选结果出炉」互不干扰
        titles = ["台风登陆台湾岛", "台风登陆台湾", "法国大选结果出炉"]
        out = AD._dedup_similar(self._mk(titles))
        self.assertEqual([o["title"] for o in out],
                         ["台风登陆台湾岛", "法国大选结果出炉"])

    def test_threshold_param_controllable(self):
        # 同一对近似稿，阈值抬到 0.99 就不再折叠（ratio 约 0.92 < 0.99）
        titles = ["台风登陆台湾岛", "台风登陆台湾", "法国大选结果出炉"]
        out = AD._dedup_similar(self._mk(titles), threshold=0.99)
        self.assertEqual(len(out), 3)


class TestSalvageJsonArray(unittest.TestCase):
    def test_salvage_returns_partial_on_truncation(self):
        # 最后一个对象被 max_tokens 截断（缺收尾 ]）→ 救回前面所有完整对象
        text = '[{"idx":1,"reason":"甲"},{"idx":2,"reason":"乙"},{"idx":3,"reason":"丙'
        self.assertEqual(len(AD._salvage_json_array(text)), 2)

    def test_salvage_handles_fenced_truncated(self):
        text = '```json\n[{"idx":1,"reason":"甲"},{"idx":2,"reason":"乙"},{"idx":3,"rea'
        self.assertEqual(len(AD._salvage_json_array(text)), 2)

    def test_salvage_no_complete_object_raises(self):
        with self.assertRaises(ValueError):
            AD._salvage_json_array('[{"idx":1,"reason":"甲')  # 连一个完整对象都没有

    def test_extract_uses_salvage_on_truncation(self):
        # _extract_json_array 在常规解析失败（典型截断）时改走抢救，而非抛错
        text = '[{"idx":1,"reason":"甲"},{"idx":2,"reason":"乙"},{"idx":3,"reason":"丙'
        self.assertEqual(len(AD._extract_json_array(text)), 2)


class TestParseResponseRawTolerance(unittest.TestCase):
    def test_parse_response_salvages_raw_truncated_text(self):
        # parse_response 直接容错原始字符串：截断文本也能救回完整稿
        items = AD.load_items(_write_feed(_tmp()))
        text = '[{"title":"甲事件","reason":"甲"},{"title":"乙事件","rea'
        out = AD.parse_response(text, items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "甲事件")


class TestMainDedupIntegration(unittest.TestCase):
    def test_main_folds_near_duplicates_before_ai(self):
        # 3 条喂入，其中两条语义近似 → 送 AI 的 prompt 只剩 2 条编号候选
        import tempfile
        tmp = tempfile.mkdtemp()
        feed_xml = (
            '<?xml version="1.0"?><rss><channel>'
            '<item><title>台风登陆台湾岛</title><link>http://a/1</link>'
            '<description>摘要甲</description>'
            '<pubDate>Mon, 01 Jan 2026 00:00:00 +0800</pubDate>'
            '<source>来源A</source></item>'
            '<item><title>台风登陆台湾</title><link>http://b/2</link>'
            '<description>摘要乙</description>'
            '<pubDate>Mon, 01 Jan 2026 00:00:00 +0800</pubDate>'
            '<source>来源B</source></item>'
            '<item><title>法国大选结果出炉</title><link>http://c/3</link>'
            '<description>摘要丙</description>'
            '<pubDate>Mon, 01 Jan 2026 00:00:00 +0800</pubDate>'
            '<source>来源C</source></item>'
            '</channel></rss>')
        feed = os.path.join(tmp, "feed.xml")
        with open(feed, "w", encoding="utf-8") as f:
            f.write(feed_xml)
        out = os.path.join(tmp, "ai-digest.html")
        fake = mock.MagicMock(return_value=[{"idx": 1, "reason": "甲"}])
        env = {"FEED_XML": feed, "OUT_HTML": out, "AI_TOP_N": "2",
               "LLM_PROVIDER": "openai", "LLM_API_KEY": "x",
               "LLM_MODEL": "deepseek-chat"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(AD, "call_openai", fake):
            AD.main()
        prompt = fake.call_args[0][4]
        numbered = [ln for ln in prompt.split("\n") if re.match(r"^\d+\. \[", ln)]
        self.assertEqual(len(numbered), 2)  # 近义两条折叠 → 2 条送 AI


if __name__ == "__main__":
    unittest.main()
