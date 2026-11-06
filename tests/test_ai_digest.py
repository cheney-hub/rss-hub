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
        prompt = AD.build_prompt(items, 25, "openai")
        self.assertIn("甲事件", prompt)
        self.assertIn("来源B", prompt)
        self.assertIn("25", prompt)
        self.assertIn("idx", prompt)

    def test_prompt_tail_openai_asks_items_object(self):
        items = AD.load_items(_write_feed(_tmp()))
        prompt = AD.build_prompt(items, 25, "openai")
        self.assertIn("items", prompt)
        self.assertIn("JSON 对象", prompt)

    def test_prompt_tail_gemini_asks_array(self):
        items = AD.load_items(_write_feed(_tmp()))
        prompt = AD.build_prompt(items, 25, "gemini")
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
        prompt = AD.build_prompt(items, 25, "gemini")
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
        prompt = AD.build_prompt(items, 25, "openai")
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
