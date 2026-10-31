#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁自己的测试：锁住「每条检查在该红的时候真的会红」。

为什么单独一个文件：`tools/` 下的脚本过去没有任何用例，于是 review_check 出了
七类「静默通过」——生成物被删了说全部通过、生成器崩了降级成提示、版本号子串
v1.7.20 能顶替 v1.7.2……门禁是质量门禁的门禁，它自己不能靠肉眼验证。

写法约定：
  * 全部在临时目录里造最小仓库，绝不读写真实仓库（`RC.ROOT` 指向 tmp）；
  * 不联网、不跑真生成器、不依赖当前版本号/用例数这些会漂移的数字；
  * 每条用例只断言「严重级 + 是否命中」，不断言整段文案（文案会改）。

跑法：
    python -m unittest tests.test_gate -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import review_check as RC      # noqa: E402  ← 被测：门禁本体
import check_secrets as CS     # noqa: E402  ← 被测：凭据规则
import gen_minute_workflow as GW   # noqa: E402  ← 被测：工作流生成器
import golden_snapshot as GS   # noqa: E402  ← 被测：行为基线
import sim_rotate as SR        # noqa: E402  ← 被测：轮播仿真

# 夹具里的地址拼出来而不是写字面量：本文件也要过 H5（硬编码外链），
# 整串写在源码里会被自己的规则命中。
SCHEME = "https://"
HOST = "example.com"


class Sandbox(unittest.TestCase):
    """基类：把 RC.ROOT 指到临时目录，造一个「最小仓库」给检查函数看。

    检查函数都从 RC.ROOT 出发找文件，所以改这一个全局就够了——不需要 mock 文件系统。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gate-")
        self._root = RC.ROOT
        self._generated = dict(RC.GENERATED)
        RC.ROOT = self.tmp

    def tearDown(self):
        RC.ROOT = self._root
        RC.GENERATED.clear()
        RC.GENERATED.update(self._generated)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        """在临时仓库里写一个文件，自动建父目录。"""
        p = os.path.join(self.tmp, *rel.split("/"))
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p


class TestGeneratedCheck(Sandbox):
    """H2：生成物与脚本的一致性检查，三种失败都必须红。"""

    GEN = "open(%r, 'w', encoding='utf-8').write('内容\\n')\n"

    def _setup(self, gen_body, target_text=None):
        self.write("tools/fake_gen.py", gen_body)
        RC.GENERATED.clear()
        RC.GENERATED["out.txt"] = "tools/fake_gen.py"
        if target_text is not None:
            self.write("out.txt", target_text)

    def test_missing_target_is_required(self):
        """生成物被删掉必须报必改 —— 以前这里静默 continue，删了反而全绿。"""
        self._setup(self.GEN % "out.txt")
        rep = RC.Report()
        RC.check_generated(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("缺失", rep.rows[0][3])

    def test_matching_output_passes(self):
        """脚本产出与仓库里一致 → 不报。这条防「检查永远红」的假阳性。"""
        self._setup(self.GEN % "out.txt", "内容\n")
        rep = RC.Report()
        RC.check_generated(rep)
        self.assertEqual(rep.rows, [])

    def test_hand_edit_is_required(self):
        """手改生成物（内容不一致）必须报必改。"""
        self._setup(self.GEN % "out.txt", "被手改过\n")
        rep = RC.Report()
        RC.check_generated(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("不一致", rep.rows[0][3])

    def test_missing_generator_is_required(self):
        """连生成脚本都没了，也要红 —— 否则「删脚本 + 删产物」就双免检。"""
        RC.GENERATED.clear()
        RC.GENERATED["out.txt"] = "tools/fake_gen.py"
        self.write("out.txt", "内容\n")
        rep = RC.Report()
        RC.check_generated(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])

    def test_generator_nonzero_exit_is_required(self):
        """生成器非零退出：报必改，并把 stderr 尾行带进来（否则只看到「不一致」）。"""
        self.write("tools/fake_gen.py",
                   "import sys\n"
                   "open('out.txt', 'w', encoding='utf-8').write('内容\\n')\n"
                   "sys.stderr.write('boom: 上游字段变了\\n')\n"
                   "sys.exit(3)\n")
        RC.GENERATED.clear()
        RC.GENERATED["out.txt"] = "tools/fake_gen.py"
        self.write("out.txt", "内容\n")          # 内容其实一致，但脚本失败也要红
        rep = RC.Report()
        RC.check_generated(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("exit 3", rep.rows[0][3])

    def test_generator_exception_is_required(self):
        """超时 / OSError 这类环境异常不能降级成提示 —— 以前降成 Info 等于这条规则消失。"""
        self._setup("pass\n", "内容\n")
        real = RC.subprocess.run

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired("cmd", 120)

        RC.subprocess.run = boom
        try:
            rep = RC.Report()
            RC.check_generated(rep)
        finally:
            RC.subprocess.run = real
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("TimeoutExpired", rep.rows[0][3])


class TestVersionSync(Sandbox):
    """H9：版本号必须是「出现且精确匹配」。"""

    DOCS = ("README.md", "OPERATIONS.md", "docs/index.html", "docs/screen.html")

    def _setup(self, page_text="rss-hub v1.7.2"):
        self.write("version.py", '#!/usr/bin/env python3\n__version__ = "1.7.2"\n')
        for d in self.DOCS:
            self.write(d, page_text if d.startswith("docs/") else "当前版本 **v1.7.2**")

    def test_exact_version_passes(self):
        self._setup()
        rep = RC.Report()
        RC.check_version_sync(rep)
        self.assertEqual(rep.rows, [])

    def test_longer_version_does_not_count(self):
        """页脚写 v1.7.20 不能顶替 v1.7.2 —— 子串匹配会把错的那一位放过去。"""
        self._setup("rss-hub v1.7.20")
        rep = RC.Report()
        RC.check_version_sync(rep)
        self.assertEqual([r[1] for r in rep.rows], ["docs/index.html", "docs/screen.html"])
        self.assertTrue(all(r[0] == RC.REQUIRED for r in rep.rows))

    def test_missing_version_file_is_required(self):
        rep = RC.Report()
        RC.check_version_sync(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("version.py", rep.rows[0][1])


class TestDocValues(Sandbox):
    """H8：参数表里的值必须等于代码默认值（不只是名字出现过）。"""

    def _setup(self, doc_value="90", env_default="90"):
        self.write("cfg.py",
                   'BODY = max(0.0, float(os.environ.get("SCREEN_BODY_MAX", "%s")))\n'
                   % env_default)
        self.write("OPERATIONS.md",
                   "| 变量 | 现值 | 说明 |\n|---|---|---|\n"
                   "| `SCREEN_BODY_MAX` | `%s` | 正文上限 |\n" % doc_value)

    def test_value_mismatch_is_required(self):
        self._setup(doc_value="60")
        rep = RC.Report()
        RC.check_doc_sync(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("SCREEN_BODY_MAX", rep.rows[0][3])

    def test_value_match_passes(self):
        self._setup()
        rep = RC.Report()
        RC.check_doc_sync(rep)
        self.assertEqual(rep.rows, [])

    def test_annotation_value_is_skipped(self):
        """`自动`、`与 OUT_FILE 同目录` 这类说明性取值没法比，跳过而不是硬猜报红。"""
        self._setup(doc_value="自动")
        rep = RC.Report()
        RC.check_doc_sync(rep)
        self.assertEqual(rep.rows, [])

    def test_zero_with_note_is_understood(self):
        """`0（不限）` 按 0 处理 —— 同义写法不该制造假红。"""
        self._setup(doc_value="0（不限）", env_default="0")
        rep = RC.Report()
        RC.check_doc_sync(rep)
        self.assertEqual(rep.rows, [])

    def test_missing_name_is_still_required(self):
        """值比对不能替代「名字至少出现过一次」这条老规则。"""
        self.write("cfg.py",
                   'X = int(os.environ.get("TOTALLY_NEW_KNOB", "3"))\n')
        self.write("OPERATIONS.md", "| 变量 | 现值 |\n|---|---|\n")
        rep = RC.Report()
        RC.check_doc_sync(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("TOTALLY_NEW_KNOB", rep.rows[0][3])


class TestModuleHasTests(Sandbox):
    """H4：模块必须被测试 import —— 看 AST，不看注释。"""

    def test_comment_mention_does_not_count(self):
        """测试文件里写一句 `# import helper` 不能给模块免检（子串匹配时代的漏洞）。"""
        self.write("helper.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/test_x.py",
                   "import unittest\n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_ok(self):\n"
                   "        self.assertTrue(True)\n"
                   "# 记得 import helper 之后补用例\n")
        rep = RC.Report()
        RC.check_tests(rep)
        self.assertEqual([(r[0], r[1]) for r in rep.rows], [(RC.REQUIRED, "helper.py")])

    def test_real_import_counts(self):
        self.write("helper.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/test_x.py",
                   "import helper\n\ndef test_add():\n    assert helper.add(1, 2) == 3\n")
        rep = RC.Report()
        RC.check_tests(rep)
        self.assertEqual(rep.rows, [])

    def test_tools_module_is_required(self):
        """tools/ 是必改级 —— 门禁自己也得被测，不给它开后门。"""
        self.write("tools/some_tool.py", "X = 1\n")
        rep = RC.Report()
        RC.check_tests(rep)
        self.assertEqual([(r[0], r[1]) for r in rep.rows], [(RC.REQUIRED, "tools/some_tool.py")])

    def test_pi_script_is_info_only(self):
        """Pi/*.py 是拷到设备上跑的独立脚本，缺用例只提示、不挡合入。"""
        self.write("Pi/board.py", "X = 1\n")
        rep = RC.Report()
        RC.check_tests(rep)
        self.assertEqual([(r[0], r[1]) for r in rep.rows], [(RC.INFO, "Pi/board.py")])


class TestWorkflowSync(Sandbox):
    """H8 的另一半：workflow 里写的数字调参必须等于代码默认值。"""

    def _setup(self, wf_value="12"):
        self.write("cfg.py", 'N = max(1, int(os.environ.get("CONCURRENCY", "12")))\n')
        self.write(".github/workflows/a.yml",
                   "jobs:\n  x:\n    steps:\n      - env:\n"
                   '          CONCURRENCY: "%s"\n' % wf_value)

    def test_workflow_value_drift_is_required(self):
        self._setup("24")
        rep = RC.Report()
        RC.check_workflow_sync(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("CONCURRENCY", rep.rows[0][3])

    def test_matching_workflow_passes(self):
        self._setup("12")
        rep = RC.Report()
        RC.check_workflow_sync(rep)
        self.assertEqual(rep.rows, [])

    def test_pi_service_is_not_compared(self):
        """Pi 的 service 有意调低并发，不参与比对（拿它比只会假红）。"""
        self._setup("12")
        self.write("Pi/services/rss-build.service",
                   "[Service]\nEnvironment=CONCURRENCY=10\n")
        rep = RC.Report()
        RC.check_workflow_sync(rep)
        self.assertEqual(rep.rows, [])


class TestSiteHtml(Sandbox):
    """docs/ 下的发布页面也要查 eval / document.write（它们不在 source_files 里）。"""

    def test_eval_in_published_page_is_required(self):
        self.write("docs/index.html", "<html><body><script>eval('x')</script></body></html>\n")
        rep = RC.Report()
        RC.check_site_html(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("docs/index.html", rep.rows[0][1])

    def test_normal_page_passes(self):
        page = '<html><body><a href="%s%s">x</a></body></html>\n' % (SCHEME, HOST)
        self.write("docs/index.html", page)
        rep = RC.Report()
        RC.check_site_html(rep)
        self.assertEqual(rep.rows, [])


class TestPythonRules(Sandbox):
    """check_python 的三条主规则：语法、裸 except、硬编码外链。"""

    def _run(self, body):
        p = self.write("m.py", body)
        rep = RC.Report()
        RC.check_python(p, body, rep)
        return rep

    def test_bare_except_is_critical(self):
        rep = self._run("def f():\n    try:\n        pass\n    except:\n        pass\n")
        self.assertEqual([r[0] for r in rep.rows], [RC.CRITICAL])
        self.assertIn("裸 except", rep.rows[0][3])

    def test_syntax_error_is_critical(self):
        rep = self._run("def f(:\n    pass\n")
        self.assertEqual([r[0] for r in rep.rows], [RC.CRITICAL])

    def test_bare_url_is_required(self):
        rep = self._run('URL = "%s%s/pixel"\n' % (SCHEME, "tracker." + HOST))
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])

    def test_env_default_url_is_exempt(self):
        """`os.environ.get(..., "https://…")` 的默认值正是可配置写法，不该报。"""
        rep = self._run('URL = os.environ.get("FEED_URL", "https://a.example.com/f.xml")\n')
        self.assertEqual(rep.rows, [])

    def test_url_in_docstring_is_exempt(self):
        rep = self._run('"""样例：%s%s/x"""\nX = 1\n' % (SCHEME, "docs." + HOST))
        self.assertEqual(rep.rows, [])


class TestRotateTimerSync(Sandbox):
    """`ROTATE_SECONDS` 与 `rss-rotate.timer` 的节拍必须一致（第三个「假旋钮」的守卫）。

    这条检查是 2026-10-31 加的：只改代码里的 `ROTATE_SECONDS`、不改 timer，
    屏幕推进节奏仍是旧的，而且没有任何现有用例会红。
    """

    TIMER = "[Timer]\nOnCalendar=*:0/%d\n\n[Install]\nWantedBy=timers.target\n"
    ROTATE_PY = ('ROTATE_SECONDS = max(60, int(os.environ.get("ROTATE_SECONDS", "300")))\n')

    def _setup(self, timer_min, seconds):
        self.write("Pi/services/rss-rotate.timer", self.TIMER % timer_min)
        self.write("Pi/services/rss-rotate.service",
                   "[Service]\nEnvironment=ROTATE_SECONDS=%d\n" % seconds)
        self.write("rotate.py", self.ROTATE_PY)

    def test_mismatch_is_required(self):
        """timer 5 分钟 + 代码 300 秒 → 通过；改成 10 分钟 → 必须报必改。"""
        self._setup(5, 300)
        rep = RC.Report()
        RC.check_rotate_timer_sync(rep)
        self.assertEqual(rep.rows, [], "5 分钟 = 300 秒，不该报")

        self._setup(10, 300)
        rep = RC.Report()
        RC.check_rotate_timer_sync(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.REQUIRED])
        self.assertIn("节拍不一致", rep.rows[0][3])

    def test_service_env_wins_over_code_default(self):
        """单元里显式写的 ROTATE_SECONDS 优先于 rotate.py 默认值（Pi 就是这么部署的）。"""
        self._setup(10, 600)
        rep = RC.Report()
        RC.check_rotate_timer_sync(rep)
        self.assertEqual(rep.rows, [], "10 分钟与单元里的 600 秒一致，不该报")

    def test_unparsable_timer_is_info_not_failure(self):
        """解析不出节拍时只提示，不许拦提交 —— 否则换个 OnCalendar 写法就把人卡死。"""
        self.write("Pi/services/rss-rotate.timer", "[Timer]\nOnCalendar=Mon *-*-* 08:00:00\n")
        self.write("rotate.py", self.ROTATE_PY)
        rep = RC.Report()
        RC.check_rotate_timer_sync(rep)
        self.assertEqual([r[0] for r in rep.rows], [RC.INFO])


class TestSecretsRules(unittest.TestCase):
    """凭据规则：真凭据要命中，`os.environ` 写法不能误报。"""

    @staticmethod
    def _tmp(body):
        d = tempfile.mkdtemp(prefix="sec-")
        p = os.path.join(d, "m.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return d, p

    def test_token_shape_is_detected(self):
        # 拼出来而不是写字面量：避免测试文件自己被凭据扫描命中
        token = "AKIA" + "Q" * 16
        d, p = self._tmp('KEY = "%s"\n' % token)
        try:
            labels = [h[2] for h in CS.scan([p])]
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertIn("AWS access key", labels)

    def test_env_pattern_is_not_a_secret(self):
        """照 H5 写成 os.environ 取值，不该被 H6 当成明文口令。"""
        body = ('SECRET = os.environ.get("API_SECRET", "")\n'
                'PASSWORD = os.environ["PW"]\n')
        d, p = self._tmp(body)
        try:
            self.assertEqual(CS.scan([p]), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_plain_password_is_detected(self):
        body = "%s = %s\n" % ("pass" + "word", '"%s"' % ("s3cret-value"))
        d, p = self._tmp(body)
        try:
            self.assertIn("明文口令", [h[2] for h in CS.scan([p])])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_self_and_binary_are_skipped(self):
        d, p = self._tmp("X = 1\n")
        try:
            self.assertEqual(CS.scan([p.replace("m.py", "tools/check_secrets.py")]), [])
            self.assertEqual(CS.scan([p.replace("m.py", "logo.png")]), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestWorkflowGenerator(unittest.TestCase):
    """生成器模板：轮数与注释必须同源，参数全集必须留在模板里。"""

    def test_cycle_count_matches_comment(self):
        """HEAD 里写的轮数必须来自 CYCLES，不能是手抄的数字。"""
        head = GW.HEAD.format(cycles=GW.CYCLES)
        self.assertIn("连续跑 %d 轮" % GW.CYCLES, head)
        self.assertIn("%d × 5 分钟" % GW.CYCLES, head)

    def test_timer_is_documented_as_disabled(self):
        """产物的 on: 只有 workflow_dispatch，注释必须说清定时已停用。"""
        head = GW.HEAD.format(cycles=GW.CYCLES)
        self.assertIn("workflow_dispatch:", head)
        self.assertIn("定时已停用", head)

    def test_template_carries_all_knobs(self):
        block = GW.CYCLE_TPL.format(n=1, period=GW.PERIOD)
        for key in ("PER_SOURCE", "ITEM_LIMIT", "SCREEN_LINE_WIDTH", "SCREEN_BODY_MAX",
                    "FEED_BODY_MAX", "ROTATE_SECONDS", "ROTATE_POOL_MAX", "CONCURRENCY",
                    "FETCH_TIMEOUT", "REFRESH_SECONDS"):
            self.assertIn(key, block, "模板里少了调参 %s" % key)


class TestGoldenTool(unittest.TestCase):
    """基线工具：归一化规则与快照稳定性。"""

    def test_norm_masks_date_and_version(self):
        self.assertEqual(GS.norm("Thu, 29 Oct 2026 12:00:00 +0800"), "<DATE>")
        self.assertIn("<VER>", GS.norm("rss-hub " + GS.__version__))

    def test_snapshot_is_stable(self):
        """同一进程内两次快照必须完全相同，否则基线比对会随机假红。"""
        a, b = GS.snapshot(), GS.snapshot()
        self.assertEqual(a, b)
        self.assertIn("== rotate_pick 序列 ==", a)

    def test_pubdate_sample_is_not_normalized(self):
        """基线里要留一条**原样**的 pubDate 样本：否则「改用 ts 现算」这类
        语义变化会被 <DATE> 一起吞掉（2026-10 实际踩过：基线一行都没变）。"""
        snap = GS.snapshot()
        self.assertIn("== pubDate 来源 ==", snap)
        self.assertIn("Thu, 29 Oct 2026 12:00:00 +0800", snap)


class TestSimRotate(unittest.TestCase):
    """仿真：种子确定性、夜间边界、指标形状。"""

    def test_night_boundary_matches_pi_timer(self):
        """夜间到 06:59（Pi timer 是 00..06），07:00 起算白天。"""
        self.assertTrue(SR.night(0))
        self.assertTrue(SR.night(6))
        self.assertFalse(SR.night(7))
        self.assertFalse(SR.night(23))

    def test_poisson_is_deterministic(self):
        import random
        a = [SR._poisson(random.Random(5), 2.35) for _ in range(1)]
        b = [SR._poisson(random.Random(5), 2.35) for _ in range(1)]
        self.assertEqual(a, b)
        self.assertGreaterEqual(SR._poisson(random.Random(1), 0.0), 0)

    def test_simulate_shape_and_seed(self):
        r1 = SR.simulate(hours=6, seed=3)
        r2 = SR.simulate(hours=6, seed=3)
        self.assertEqual(r1, r2, "同一个种子必须给出同一份结果（--seed 生效的前提）")
        for k in ("minted", "aired", "cover", "delay_med", "lag_max"):
            self.assertIn(k, r1)
        self.assertGreater(r1["minted"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
