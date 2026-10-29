"""行为回归：渲染层的输出必须与入库的 tests/golden.txt 逐字节一致。

这是「重构不改行为」的自动化版本。以前靠人工在重构前后各跑一次
`tools/golden_snapshot.py` 再肉眼比 diff，谁都可以忘记跑；
现在它是一条单测，改坏了排版、少了一个字段、多了个空格都会红。

基线是有意改排版时才更新的：

    python3 tools/golden_snapshot.py --write     # 更新基线
    # 同时必须在 CHANGELOG.md 里说明改了什么、为什么

跑法：
    python -m unittest discover -s tests -v
"""
import difflib
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import golden_snapshot as G  # noqa: E402


class TestGoldenSnapshot(unittest.TestCase):
    """渲染行为回归。失败信息里带 diff，不用自己去跑一遍才知道差在哪。"""

    def test_matches_baseline(self):
        if not os.path.exists(G.GOLDEN):
            self.fail("缺少基线 %s，请先跑 python3 tools/golden_snapshot.py --write"
                      % os.path.relpath(G.GOLDEN, ROOT))
        with io.open(G.GOLDEN, "r", encoding="utf-8") as f:
            want = f.read()
        got = G.snapshot()
        if got == want:
            return
        diff = list(difflib.unified_diff(want.splitlines(), got.splitlines(),
                                         "tests/golden.txt", "当前输出",
                                         lineterm="", n=1))
        self.fail("渲染行为与基线不一致（%d 处差异）。\n"
                  "确认是有意改动后跑 python3 tools/golden_snapshot.py --write 更新基线，"
                  "并在 CHANGELOG.md 记一笔：\n%s"
                  % (sum(1 for d in diff if d.startswith(("+", "-")) and
                         not d.startswith(("+++", "---"))),
                     "\n".join(diff[:40])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
