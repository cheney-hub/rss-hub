"""Pi/ai_digest.py 的离线测试：真起 Gemini 会烧配额且不稳，这里把网络调用打桩。

覆盖：feed 解析、提示词构造、模型返回→原文映射、HTML 渲染（自 canonical / 无站内
交叉链接 / 外链原文）、以及最关键的兜底——API 出错时绝不连累主链路、保留上一期。
"""
import io
import os
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


if __name__ == "__main__":
    unittest.main()
