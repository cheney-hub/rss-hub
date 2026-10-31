#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码审查门禁：把 CODE_REVIEW.md 里「机器能判的」规则变成可执行检查。

人工评审负责正确性、架构、命名这些需要判断的事；能写成规则的（文件多大、
函数多长、有没有裸 except、生成文件是否被手改）不该占用人的时间。

用法：
    python tools/review_check.py             # 全量检查，有 Critical/Required 则退出码 1
    python tools/review_check.py --strict    # 连 Nit 也当失败（合入前最后一遍）
    python tools/review_check.py --no-tests  # 跳过「生成物漂移 / 模块有用例 / 用例数同步」三项

分级口径与 CODE_REVIEW.md 一致：
    Critical  阻断合入：语法错误、裸 except、凭据、YAML 非法
    Required  必须改完才能合入：文件/函数超限、生成物与脚本不一致、模块无测试、硬编码地址、
              文档参数值与代码默认值不一致、版本号缺失
    Nit       可忽略：行宽、TODO、可变全局
    Info      提示：文件接近上限、Pi 侧脚本没有用例

设计上的一条硬要求：**任何检查失败都不能静默变成通过**。生成器崩了、PyYAML 没装、
目标文件不见了，都要留下可见的记录——"结论行写全部通过"是最坏的一种失败方式。
"""

import ast
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 阈值（与 CODE_REVIEW.md 表格一一对应，改这里必须同步改文档）----
MAX_FILE_LINES = 1000      # 单文件硬上限
WARN_FILE_LINES = 750      # 超过就提示该拆了
MAX_FUNC_LINES = 60        # 单函数上限
MAX_LINE_LEN = 100         # 单行上限
TODO_LIMIT = 0             # 遗留 TODO 数量上限

# .qa-tmp 是 QA 放临时脚本的地方（仿真、压测）：它会被扫进「文件行数 / 模块要有测试」
# 这类检查里，一不留神就把门禁搞红，而那里的东西本来就不进仓库。
# docs 也在这里：那是发布目录，页面里写的都是真实外链（github 地址等），跑 H5 会全是误报。
# docs 的 .html 另外走 check_site_html()，只查 eval / document.write 这类安全写法。
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".workbuddy", "docs", ".qa-tmp"}
# 只认「带真实域名」的外链；http://x/1 这类测试夹具、以及规则文本里的裸 "http://" 不算
_RE_OUTBOUND = re.compile(r"https?://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
GENERATED = {
    ".github/workflows/minute.yml": "tools/gen_minute_workflow.py",
    "tests/golden.txt": "tools/golden_snapshot.py",
}
# 生成物、第三方产物、数据文件不参与风格类检查
STYLE_EXEMPT = set(GENERATED) | {"docs/feed.xml", "docs/feed-wrap.xml", "feeds.txt"}
# 版本号必须出现在这几处页面上（人一眼能看到的地方），漏一处就是发版漏了
VERSION_DOCS = ("README.md", "OPERATIONS.md", "docs/index.html", "docs/screen.html")
# 参数表所在文档：H8 既要求变量名出现，也要求表格里写的值与代码默认值一致
DOC_FILES = ("README.md", "OPERATIONS.md")

CRITICAL, REQUIRED, NIT, INFO = "Critical", "Required", "Nit", "Info"
LABEL = {CRITICAL: "阻断", REQUIRED: "必改", NIT: "可选", INFO: "提示"}


class Report(object):
    """一次检查的结果集：每行是 (严重级, 文件, 行号, 说明)。

    故意只做一个薄容器：分级与打印都在 main() 里，检查函数只往里丢行，
    这样新增一条检查不需要碰输出逻辑，也方便 tests/test_gate.py 直接断言 rows。
    """

    def __init__(self):
        """空报告：没有任何行时 main() 会打印「全部通过」。"""
        self.rows = []

    def add(self, sev, path, line, msg):
        """记一条。line 为 0 表示「整文件/整规则」级别的问题，打印时显示为 `-`。"""
        self.rows.append((sev, path, line, msg))

    def of(self, sev):
        """取某一严重级的全部行，供 main() 分级打印与统计。"""
        return [r for r in self.rows if r[0] == sev]


def rel(p):
    """把绝对路径转成仓库相对路径并统一成 `/` —— 报告里的路径要能直接点开。"""
    return os.path.relpath(p, ROOT).replace("\\", "/")


def source_files():
    """要跑风格检查的源文件（.py/.yml/.html/.js），已排序。

    docs/ 与 .qa-tmp/ 通过 SKIP_DIRS 排除：前者是发布产物（外链满屏，H5 会误报），
    后者是本地临时脚本（不进仓库）。这也意味着 docs 下的 .html **不在这里**，
    它们由 check_site_html() 单独负责——两条路都留着，别以为漏了。
    """
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".py", ".yml", ".yaml", ".html", ".js")):
                out.append(os.path.join(base, f))
    return sorted(out)


def read(path):
    """按 UTF-8 读文本（坏编码用替换字符兜住）。

    用 errors="replace" 而不是直接抛：一个源文件里有半个 GBK 字节不该让整轮门禁崩掉，
    那会逼着人加 --no-tests 绕过检查，比检查本身不严更糟。
    """
    with io.open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


# ---------------- 检查项 ----------------

def _doc_lines(tree):
    """模块/类/函数文档字符串所占的行号，这些行里的说明文字不该被当成代码规则判。"""
    out = set()
    for node in [tree] + [n for n in ast.walk(tree)
                          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]:
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            out.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return out


def _check_hardcoded_urls(name, lines, tree, rep):
    """只抓「带真实域名的裸地址」。四种不算：

    * 文档字符串里的说明文字；
    * `os.environ.get("X", "https://…")` 的默认值 —— 那正是可配置写法；
    * `KEY: "value"` 形态的配置项（含 YAML 模板里的 env 块）；
    * XML 命名空间，规范要求写死。

    已知不足：`http://192.168.1.20:8080/...` 这种 IP:端口地址不会被 `_RE_OUTBOUND`
    匹配（正则要求字母 TLD），而 Pi 侧确实有这类地址。要补的话在 `_RE_OUTBOUND`
    里加 IP 分支，别在这里按行特判。
    """
    docs = _doc_lines(tree)
    for i, ln in reversed(list(enumerate(lines, 1))):
        if i in docs or "os.environ" in ln:
            continue
        if not _RE_OUTBOUND.search(ln):
            continue
        if re.search(r"xmlns|w3\.org|purl\.org|schema", ln) or ln.strip().startswith("#"):
            continue
        if re.match(r'\s*[A-Z][A-Z0-9_]*:\s*["\']', ln):
            continue
        rep.add(REQUIRED, name, i, "硬编码外链，应走环境变量或模块常量")


def check_python(path, text, rep):
    """单个 .py 的七项检查：语法、裸 except、函数长度、文件长度、行宽、TODO、硬编码外链。

    顺序有讲究：语法过不了就直接返回（后面基于 AST 的检查没意义）；
    函数长度在「生成物豁免」之前判，因为生成物也可能有超长函数；
    文件长度/行宽/TODO/外链属于风格类，生成物与数据文件豁免（它们本来就不该手改）。
    """
    name = rel(path)
    # 1) 语法：连 compile 都过不去，后面的 AST 检查没意义
    try:
        tree = ast.parse(text, filename=name)
    except SyntaxError as e:
        rep.add(CRITICAL, name, e.lineno or 0, "语法错误：%s" % e.msg)
        return

    # 2) 裸 except —— 连 KeyboardInterrupt / SystemExit 一起吞掉，故障现场会消失
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            rep.add(CRITICAL, name, node.lineno,
                    "裸 except 会吞掉 KeyboardInterrupt/SystemExit，至少写 except Exception")

    # 3) 函数长度
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = (node.end_lineno or node.lineno) - node.lineno + 1
            if span > MAX_FUNC_LINES:
                rep.add(REQUIRED, name, node.lineno,
                        "函数 %s() %d 行 > %d，按职责拆分" % (node.name, span, MAX_FUNC_LINES))
    st = STYLE_EXEMPT
    if name in st:
        return

    lines = text.splitlines()
    # 4) 文件长度
    if len(lines) > MAX_FILE_LINES:
        rep.add(REQUIRED, name, 1, "文件 %d 行 > %d，先拆模块再加功能" % (len(lines), MAX_FILE_LINES))
    elif len(lines) > WARN_FILE_LINES:
        rep.add(INFO, name, 1, "文件 %d 行，已过 %d 行的拆分提醒线"
                % (len(lines), WARN_FILE_LINES))

    # 5) 行宽
    for i, ln in enumerate(lines, 1):
        if len(ln) > MAX_LINE_LEN:
            rep.add(NIT, name, i, "行宽 %d > %d" % (len(ln), MAX_LINE_LEN))

    # 6) 遗留标记（只认注释形态，避免规则说明文字里的 TODO 被误判）
    todo = [(i, ln) for i, ln in enumerate(lines, 1)
            if re.search(r"(?:#|//|--)\s*(TODO|FIXME|XXX|HACK)\b", ln)]
    if len(todo) > TODO_LIMIT:
        for i, _ in todo[:5]:
            rep.add(NIT, name, i, "遗留 TODO/FIXME，请转成 issue 或补完")
        if len(todo) > 5:
            rep.add(NIT, name, todo[5][0], "……另有 %d 处" % (len(todo) - 5))

    # 7) 硬编码外链
    _check_hardcoded_urls(name, lines, tree, rep)


def check_html(path, text, rep):
    """HTML/JS 里的两条安全写法：`document.write(` 与 `eval(`。

    只做字符串匹配、不做语法分析——这两串在正常页面里几乎不会出现，
    真出现就是「有人图省事把外部内容当代码执行」，宁可误报也不放过。
    注意 docs/ 的页面不经过这里（它们在 check_site_html 里单独跑同一个函数）。
    """
    name = rel(path)
    for i, ln in enumerate(text.splitlines(), 1):
        for bad, why in (("document.write(", "会阻塞解析"), ("eval(", "执行任意字符串")):
            if bad in ln:
                rep.add(REQUIRED, name, i, "%s %s" % (bad, why))


def check_yaml(path, text, rep):
    """工作流 YAML 必须能被 safe_load —— 语法错的工作流会被 GitHub 静默忽略。

    PyYAML 只在本地校验时用（运行时代码仍零依赖）。没装时**不能静默 return**：
    那样本地门禁会装作查过 YAML，而这份文件恰好是最容易写出缩进错的地方。
    这里记一条 Info 提示「没校验」，让结论行能看出这一项缺席。
    """
    name = rel(path)
    try:
        import yaml
    except ImportError:
        rep.add(INFO, name, 0, "未安装 PyYAML，跳过 YAML 语法校验（CI 的 review.yml 会查）")
        return
    try:
        yaml.safe_load(text)
    except Exception as e:
        rep.add(CRITICAL, name, getattr(e, "problem_mark", None) and
                e.problem_mark.line + 1 or 0, "YAML 非法：%s" % e)


def check_generated(rep):
    """生成物与脚本是否一致 —— 手改生成文件是最难发现的一类漂移。

    三种失败都要报，**一条都不能降级成 Info**（降级等于这条规则静默消失）：
      * 生成脚本不存在 / 生成物文件不存在（后者曾经被静默 continue 掉，
        于是「删掉 minute.yml」反而让门禁回"全部通过"）；
      * 生成器非零退出（把 stderr 尾部带进报告，否则只看到「不一致」看不出是崩了）；
      * 生成器超时/OSError 之类的环境异常。

    做法：把根目录的 .py 与生成脚本一起搬进临时沙箱，在沙箱里跑，
    这样脚本永远写不到工作区，也不需要 `--no-tests` 来绕开。
    """
    root_py = [f for f in os.listdir(ROOT) if f.endswith(".py")]
    for target, gen in sorted(GENERATED.items()):
        target_path = os.path.join(ROOT, *target.split("/"))
        gen_path = os.path.join(ROOT, *gen.split("/"))
        if not os.path.exists(gen_path):
            rep.add(REQUIRED, target, 1, "生成脚本 %s 不存在，无法校验生成物" % gen)
            continue
        if not os.path.exists(target_path):
            rep.add(REQUIRED, target, 1, "生成物缺失，请跑一遍 %s" % gen)
            continue
        tmp = tempfile.mkdtemp()
        cmd = [sys.executable, os.path.join(tmp, "tools", os.path.basename(gen_path))]
        try:
            # 复制成同样的相对结构，脚本就会在沙箱里生成，不碰工作区
            dst = os.path.join(tmp, "tools")
            os.makedirs(dst)
            for f in root_py:                                # 根目录模块
                shutil.copy2(os.path.join(ROOT, f), os.path.join(tmp, f))
            shutil.copy2(gen_path, os.path.join(dst, os.path.basename(gen_path)))
            if os.path.basename(gen_path) == "golden_snapshot.py":
                cmd.append("--write")                        # 该脚本默认打印到 stdout
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True,
                                  text=True, timeout=120)
            if proc.returncode != 0:
                tail = [ln.strip() for ln in (proc.stderr or proc.stdout or "").splitlines()
                        if ln.strip()]
                rep.add(REQUIRED, target, 1, "生成脚本失败（exit %d）：%s"
                        % (proc.returncode, (tail[-1][:200] if tail else "无输出")))
                continue
            fresh_p = os.path.join(tmp, *target.split("/"))
            if not os.path.exists(fresh_p):
                rep.add(REQUIRED, target, 1, "生成脚本退出码 0 但没有产出该文件，检查 %s" % gen)
                continue
            if read(fresh_p) != read(target_path):
                rep.add(REQUIRED, target, 1,
                        "与 %s 生成结果不一致，请跑一遍脚本而不是手改" % gen)
        except Exception as e:
            rep.add(REQUIRED, target, 1, "生成物一致性检查无法完成（%s: %s）"
                    % (type(e).__name__, e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _imported_modules(paths):
    """这些测试文件里**真正 import 过**的顶层模块名（看 AST，不认注释）。

    以前用「全文里有没有 `import xxx` 这个子串」判，于是测试文件里一句
    `# 记得 import helper 之后补用例` 就能让新模块免检——注释不该有放行权。
    """
    out = set()
    for p in paths:
        try:
            tree = ast.parse(read(p), filename=p)
        except (SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                out.add(node.module.split(".")[0])
    return out


def check_tests(rep):
    """每个模块至少被一个测试 import 过 —— 没有测试的改动无法验证。

    三个范围，严格程度不同：
      * 根目录模块：必改。它们是线上真正跑的那几个文件。
      * tools/：必改。门禁脚本自身也在里面——「门禁没人测」正是它自己出过 7 类
        假绿却不被发现的原因，所以不给它开后门。
      * Pi/*.py：只提示。它们是拷到树莓派上当成独立脚本跑的，不属于可 import 的
        仓库模块（Pi 侧的正确性由 Pi 的部署验证负责）。
    """
    tests_dir = os.path.join(ROOT, "tests")
    imported = set()
    if os.path.isdir(tests_dir):
        imported = _imported_modules([os.path.join(tests_dir, f)
                                      for f in os.listdir(tests_dir) if f.endswith(".py")])
    targets = [(f, REQUIRED) for f in sorted(os.listdir(ROOT))
               if f.endswith(".py") and not f.startswith("_")]
    for sub, sev in (("tools", REQUIRED), ("Pi", INFO)):
        d = os.path.join(ROOT, sub)
        if os.path.isdir(d):
            targets += [("%s/%s" % (sub, f), sev) for f in sorted(os.listdir(d))
                        if f.endswith(".py") and not f.startswith("_")]
    for name, sev in targets:
        mod = os.path.basename(name)[:-3]
        if mod not in imported:
            rep.add(sev, name, 1, "没有任何测试 import 该模块（tests/ 下需覆盖核心函数）")


def _count_cases(tests_dir, pattern):
    """用 unittest 的加载器数用例 —— **只加载不执行**，一次几毫秒。"""
    return unittest.defaultTestLoader.discover(
        tests_dir, pattern=pattern, top_level_dir=tests_dir).countTestCases()


# 目录树里每个用例文件的计数只认这一个形状：文件名后紧跟 `# N 例`。
# 写成「# core 逻辑 68 例」就抽不出数字（见 check_test_count_sync 的状态 2）。
_RE_CASE_COUNT = re.compile(r"(test[\w.]*\.py)\s*#\s*(\d+)\s*例")
# 目录树里 tests/ 那一行（`├── tests/  # 166 个用例…`），用来切出 tests 子树
_RE_TESTS_MARK = re.compile(r"^[│\s]*[├└]──\s+tests/\s*(?:#.*)?$")
# 「还是目录树的一行」：以树形字符开头。用于判断 tests/ 子树到哪结束
_RE_TREE_ROW = re.compile(r"^[│├└\s]*[├└]──")
_RE_TOTAL_TREE = re.compile(r"tests/\s*#\s*(\d+)\s*个用例")
_RE_TOTAL_PROSE = re.compile(r"单测：(\d+)\s*个用例")


def _tests_tree_block(text):
    """README 目录树里 `tests/` 那一段（**含标记行本身**），找不到就退回整篇。

    为什么要切这一段：状态 2 与状态 3 都叫「没抽到计数」，区别只在于文件名
    **在不在目录树里**——必须在树内部判断，否则正文里一句「详见 tests/test_core.py」
    也会被当成「提到了」，报出来的「在目录树里出现了」就是假话。

    两个坑：标记行自己带着「# N 个用例」的总数，切片必须带上它，否则树的总数
    核对会随着标记样式一改就静默消失；反过来找不到标记时退回整篇——宁可放宽，
    也不能因为样式变了就不查。返回 (切片文本, 切片首行的绝对行号)。
    """
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not _RE_TESTS_MARK.match(ln):
            continue
        block = [ln]
        for nxt in lines[i + 1:]:
            if not _RE_TREE_ROW.match(nxt):
                break
            block.append(nxt)
        return "\n".join(block), i + 1
    return text, 1


def _case_count_hits(block, base_line, name):
    """目录树切片里 name 的每一处出现：[(行号, 计数)]，行尾没有 `# N 例` 时计数为 None。

    行号取的是 README 里的**绝对行号**（切片起始行号 + 偏移），报告里要能直接跳过去。
    """
    mention = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])")
    counted = re.compile(re.escape(name) + r"\s*#\s*(\d+)\s*例")
    out = []
    for i, ln in enumerate(block.splitlines(), base_line):
        if not mention.search(ln):
            continue
        m = counted.search(ln)
        out.append((i, int(m.group(1)) if m else None))
    return out


def check_test_count_sync(rep):
    """README 里写的用例数必须等于 `unittest discover` 实际数到的数。

    这条来自真实脱节：README 的目录树写着「94 个用例 / core 64 / rotate_edge 29」，
    而实际是 106，同一页另一处又写着 106 —— 一页自相矛盾，且树里少了新建的
    `tests/test_dedupe.py`。数字是手抄的，加一个文件就少同步一处；
    机器数得出来的东西不该让人抄（本项目栽过两次：`ROTATE_MAX_LAG`、`sim --seed`，
    都是「参数变了没人能证明」）。所以这里按实际数核对，写错就是必改。

    它自己还栽过一次**文案**事故（第三次同类）：有人把行尾写成
    `# core 逻辑 68 例`，计数正则抽不出数字，旧实现就报
    「目录树里没有 test_core.py（实际 68 例）」——文件明明在树里，这条却叫人
    再去加一行，照做就变成两行重复、数字各说各的。所以下面按五态分开给文案：

      1. 树里有且计数一致          → 不报
      2. 树里有但行尾不是 `# N 例` → 必改，**点名是格式问题**，不叫人加文件
      3. 树里压根没提到            → 必改，请同步新增文件
      4. 树里提到了、tests/ 下没有 → 必改，删掉那行或补上文件
      5. 都提到了但数字不符        → 必改，两个数都报出来

    状态 2 与 3 靠「文件名在目录树切片里有没有出现」区分，只看抽出来的数字
    会把这两件事混成一个（那正是上面那次事故的原因）。同一文件被写了两处计数
    也报必改：留着两行就等于没有唯一答案。
    """
    tests_dir = os.path.join(ROOT, "tests")
    readme = os.path.join(ROOT, "README.md")
    if not (os.path.isdir(tests_dir) and os.path.exists(readme)):
        return
    names = sorted(f for f in os.listdir(tests_dir)
                   if f.startswith("test") and f.endswith(".py"))
    try:
        actual = {n: _count_cases(tests_dir, n) for n in names}
    except Exception as e:                                   # 加载失败不该连带门禁崩掉
        rep.add(INFO, "README.md", 0, "用例数核对跳过：%s" % e)
        return
    total = sum(actual.values())

    text = read(readme)
    block, base = _tests_tree_block(text)
    _check_case_files(rep, names, actual, block, base)
    _check_case_totals(rep, text, block, base, total, actual)


def _declared_counts(block, base):
    """目录树里声明过的用例文件：{名字: [(行号, 计数)]}，含 tests/ 下已没有的那些。"""
    out = {}
    for m in _RE_CASE_COUNT.finditer(block):
        out.setdefault(m.group(1), []).append(
            (base + block[:m.start()].count("\n"), int(m.group(2))))
    return out


def _check_case_files(rep, names, actual, block, base):
    """状态 1–5：tests/ 下每个文件的计数行「有没有 / 格式对不对 / 数字对不对」。

    拆出来是因为五种状态各有一套文案，全塞进 check_test_count_sync 会超过
    单函数 60 行的上限——但拆分不能把某一种状态悄悄丢掉，每态都要能被造红。
    """
    declared = _declared_counts(block, base)
    for n in names:                                          # 状态 1/2/3/5
        hits = _case_count_hits(block, base, n)
        if not hits:                                         # 状态 3
            rep.add(REQUIRED, "README.md", 0,
                    "目录树里没有 %s（实际 %d 例），新增测试文件请同步" % (n, actual[n]))
            continue
        counted = [(ln, v) for ln, v in hits if v is not None]
        if not counted:                                      # 状态 2
            rep.add(REQUIRED, "README.md", hits[0][0],
                    "%s 在目录树里出现了，但那一行的行尾不是「# N 例」计数格式"
                    "（实际 %d 例）；数字必须能被机器抽出来，别写成散文"
                    % (n, actual[n]))
            continue
        if len(counted) > 1:
            rep.add(REQUIRED, "README.md", counted[0][0],
                    "%s 在目录树里写了 %d 处计数（第 %s 行），只保留一处，"
                    "否则以哪个为准说不清"
                    % (n, len(counted), "、".join(str(ln) for ln, _ in counted)))
        if counted[0][1] != actual[n]:                       # 状态 5
            rep.add(REQUIRED, "README.md", counted[0][0],
                    "%s 写的 %d 例，实际 %d 例" % (n, counted[0][1], actual[n]))

    for n in sorted(set(declared) - set(names)):             # 状态 4
        rep.add(REQUIRED, "README.md", declared[n][0][0],
                "README 目录树里的 %s 不存在（tests/ 下没有）" % n)


def _check_case_totals(rep, text, block, base, total, actual):
    """用例总数：目录树 `tests/  # N 个用例` 与正文「单测：N 个用例」两处。

    前者在树切片里找（标记行本身就在切片里），后者在正文里找；切片退化成整篇时
    两处都还能找到——「标记样式变了」不能让总数核对静默消失。
    """
    for m in _RE_TOTAL_TREE.finditer(block):
        if int(m.group(1)) != total:
            rep.add(REQUIRED, "README.md", base + block[:m.start()].count("\n"),
                    "目录树写的 %s 个用例，实际 %d 个（%s）"
                    % (m.group(1), total,
                       " + ".join("%s %d" % (k, v) for k, v in sorted(actual.items()))))
    for m in _RE_TOTAL_PROSE.finditer(text):
        if int(m.group(1)) != total:
            rep.add(REQUIRED, "README.md", text[:m.start()].count("\n") + 1,
                    "「改动代码前后跑这三条」那行写的 %s 个用例，实际 %d 个"
                    % (m.group(1), total))


# ---- 环境变量 / 文档参数值 ----

def _env_calls(tree):
    """一个模块里所有 `os.environ.get("NAME", "默认值")` 的 (名字, 默认值)。

    走 AST 而不是正则：正则会把注释/文档字符串里的示例
    （`os.environ.get("NAME", "默认值")` 这种写法在说明文字里很常见）当成真实变量，
    于是文档里凭空多出一个「没出现过的环境变量」。AST 只看真实调用，天然免疫。
    默认值不是字符串字面量的（`os.path.join(...)`）拿不到值，跳过——宁可少查一个，
    也不要把路径拼进检查里。
    """
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"):
            continue
        owner = node.func.value
        if not (isinstance(owner, ast.Attribute) and owner.attr == "environ"):
            continue
        if len(node.args) < 2:
            continue
        name, default = node.args[0], node.args[1]
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            continue
        if not (isinstance(default, ast.Constant) and isinstance(default.value, str)):
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", name.value):
            out.append((name.value, default.value))
    return out


def code_defaults():
    """代码里 `os.environ.get("NAME", "默认值")` 的 {NAME: 默认值}，扫全部 .py。

    docs/ 与 .qa-tmp/ 被 SKIP_DIRS 排除，所以那里面的临时脚本不会污染参数表。
    同一个变量出现多次时以第一次为准（`max(1, int(...))` 这类包装不影响取值）。
    """
    out = {}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(base, f)
            try:
                tree = ast.parse(read(p), filename=p)
            except (SyntaxError, ValueError):
                continue                        # 语法错由 check_python 报，这里跳过
            for name, default in _env_calls(tree):
                out.setdefault(name, default)
    return out


def _as_number(s):
    """把文档/代码里的取值解析成数字；解析不出返回 None。

    只接受「整串就是一个数字」或「数字后面跟括号说明」（如 `0（不限）`）两种形态。
    `自动`、`与 OUT_FILE 同目录`、`10 分钟` 这类说明性取值一律返回 None ——
    说明性文字本来就没法比，硬猜只会制造假红。同义词 `0/空/不限` 因此自然归到
    「0（不限）」这种形态上，不需要单独维护一张同义词表。
    """
    v = (s or "").strip().strip("`* ")
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(?:[（(].*)?$", v)
    return float(m.group(1)) if m else None


def _doc_table_values(text):
    """从 markdown 表格里抽 `| \\`VAR\\` | 值 |` 的取值，返回 {VAR: 值原文}。

    第一张表里出现的同名变量优先（同一份文档后面的「轮播调参」表是摘录，可能有遗漏）。
    只认首列是纯大写变量名的行，`| 轮数 | 13 |` 这种人类可读标签不参与比对。
    """
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        name = cells[0].strip("`* ")
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", name):
            out.setdefault(name, cells[1])
    return out


def check_doc_sync(rep):
    """代码里的可调参数必须出现在文档里，且**参数表里的值要等于代码默认值**。

    前半条（名字出现）只是防「文档里压根没提」，挡不住真正的漂移：OPERATIONS 的
    参数表曾长期写着「55 轮 / SCREEN_BODY_MAX 60」，而代码早已是「13 轮 / 90」——
    照文档调参等于没调。所以这里再解析一次表格里的值：能解析成数字就与代码默认值比，
    解析不出（`自动`、`与 OUT_FILE 同目录`）就跳过——说明性文字没法比，硬猜只会假红。
    """
    doc = ""
    for f in DOC_FILES:
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            doc += read(p)
    if not doc:
        return
    defaults = code_defaults()
    for n in sorted(defaults):
        if n not in doc:
            rep.add(REQUIRED, "OPERATIONS.md", 0,
                    "环境变量 %s 在文档里没有出现，改代码请同步参数表" % n)
    documented = _doc_table_values(doc)
    for n in sorted(documented):
        if n not in defaults:
            continue
        want, got = _as_number(defaults[n]), _as_number(documented[n])
        if want is None or got is None or abs(want - got) < 1e-9:
            continue
        rep.add(REQUIRED, "OPERATIONS.md", 0,
                "参数表里 %s 写的是 %s，代码默认值是 %s —— 文档与常量必须一致"
                % (n, documented[n].strip("`* "), defaults[n]))


def check_version_sync(rep):
    """版本号必须从 version.py 一路传到页面上。

    漏更新的后果不是报错，而是「页脚写着 v1.0.0、线上跑的是 1.1.0」，
    排查时按旧版行为去推断，越查越偏。这里只做「有没有出现」的判断，
    CHANGELOG 里出现历史版本号是正常的，所以不查那一份。

    匹配必须带边界：`ver not in text` 会让 v1.7.2 被 v1.7.20 命中——
    页脚写错一位反而算通过，而这正是要抓的错。
    """
    vp = os.path.join(ROOT, "version.py")
    if not os.path.exists(vp):
        rep.add(REQUIRED, "version.py", 0, "缺少版本号真源版本文件")
        return
    m = re.search(r'__version__\s*=\s*"([^"]+)"', read(vp))
    if not m:
        rep.add(REQUIRED, "version.py", 0, "version.py 里没有 __version__ = \"x.y.z\"")
        return
    ver = m.group(1)
    pat = re.compile(r"(?<![\d.])" + re.escape(ver) + r"(?![\d.])")
    for name in VERSION_DOCS:
        p = os.path.join(ROOT, *name.split("/"))
        if not os.path.exists(p):
            rep.add(REQUIRED, name, 0, "版本号应展示的文件不存在")
        elif not pat.search(read(p)):
            rep.add(REQUIRED, name, 0, "没有出现当前版本号 %s，发版要同步页面" % ver)


_RE_WF_ENV = re.compile(r'^\s*([A-Z][A-Z0-9_]*):\s*"?([0-9.]+)"?\s*$', re.M)


def check_workflow_sync(rep):
    """workflow 里显式写的数字调参必须等于代码默认值。

    为什么放在门禁里、而不是只靠 test_core 的那条用例：那条只覆盖 4 个参数
    （PER_SOURCE/ITEM_LIMIT/FEED_BODY_MAX/ROTATE_POOL_MAX），把 `CONCURRENCY`
    从 12 改成 24 时门禁和用例都不会响——而「改了代码忘改 workflow」正是
    本项目出过一次的事故（完整版 228 条被即时发布盖回 120 条）。

    Pi/services/*.service 里的值**不参与比对**：树莓派比 runner 弱，
    CONCURRENCY=10 / FETCH_TIMEOUT=15 是有意调低的；拿它跟 workflow 比只会制造假红。
    """
    defaults = {k: _as_number(v) for k, v in code_defaults().items()}
    defaults = {k: v for k, v in defaults.items() if v is not None}
    wf_dir = os.path.join(ROOT, ".github", "workflows")
    if not defaults or not os.path.isdir(wf_dir):
        return
    for f in sorted(os.listdir(wf_dir)):
        if not f.endswith((".yml", ".yaml")):
            continue
        seen = {}
        for m in _RE_WF_ENV.finditer(read(os.path.join(wf_dir, f))):
            seen[m.group(1)] = m.group(2)          # 同名多行（minute.yml 每轮一段）取最后
        for k in sorted(seen):
            if k not in defaults:
                continue
            if abs(float(seen[k]) - defaults[k]) > 1e-9:
                rep.add(REQUIRED, ".github/workflows/" + f, 0,
                        "%s=%s 与代码默认值 %g 不一致（改一处忘另一处，线上产物就和本地不同）"
                        % (k, seen[k], defaults[k]))


# Pi 的 timer 单元：推进节拍与 ROTATE_SECONDS 是一份契约。只改一边就是第三个「假旋钮」——
# 前两个（ROTATE_MAX_LAG、sim --seed）都栽在「参数变了没人能证明」上。
# 节拍写在 timer，dwell 可能被同名的 service 用 Environment= 覆盖；两处都要看：
# service 里的值真正进到 rotate_screen.py 的进程环境，timer 里的只是约定写法（systemd 忽略它），
# 但两种写法都有人用，所以 service 优先、timer 次之、代码默认值兜底。
ROTATE_TIMER = "Pi/services/rss-rotate.timer"
ROTATE_UNIT = "Pi/services/rss-rotate.service"
# `*:0/5`、`*-*-* 07..23:0/5:00`：只认「:00 起、步长 N 分钟」的写法，才能换算成「每 N 分钟」
_RE_CAL_STEP = re.compile(r":(\d+)/(\d+)")
_RE_ON_UNIT = re.compile(r"^\s*OnUnitActiveSec\s*=\s*(\d+)\s*(min|m|s)?\s*$", re.M)
_RE_TIMER_DWELL = re.compile(
    r"^\s*Environment\s*=\s*(?:\S+\s+)?ROTATE_SECONDS\s*=\s*(\d+)\s*$", re.M)


def _timer_interval_minutes(text):
    """从 systemd timer 文本里解析「每 N 分钟推进一次」，返回 (分钟, 依据, 行号)。

    只认两种明确表示周期的写法：`OnCalendar=*:0/5`（含 `…:0/5:00`）与
    `OnUnitActiveSec=5min`。`OnCalendar=hourly`、`*:00` 这类「整点」写法换算不出
    「每 N 分钟」——返回 None 让调用方报提示，而不是猜一个数字。
    解析不出不是错，静默放过才是。
    """
    m = _RE_ON_UNIT.search(text)
    if m:
        value = float(m.group(1))
        unit = m.group(2) or "min"
        lineno = text[:m.start()].count("\n") + 1
        return (value if unit != "s" else value / 60.0), m.group(0).strip(), lineno
    for m in re.finditer(r"^.*OnCalendar\s*=.*$", text, re.M):
        step = _RE_CAL_STEP.search(m.group(0))
        if step and int(step.group(1)) == 0:
            lineno = text[:m.start()].count("\n") + 1
            return float(step.group(2)), m.group(0).strip(), lineno
    return None, "没有可解析的周期写法（如 OnCalendar=*:0/5 或 OnUnitActiveSec=5min）", 0


def check_rotate_timer_sync(rep):
    """`ROTATE_SECONDS` 与 Pi 的 `rss-rotate.timer` 必须表示同一个节拍。

    两个值分散在两个仓库文件里（代码常量 / systemd 单元），谁都不会因为另一个改了而报错：
    把 ROTATE_SECONDS 改成 60 想让屏上 1 分钟换一条，timer 仍 5 分钟敲一次 → 窗口一跳
    就是 5 个，屏幕看起来照旧 5 分钟一换；反过来把 timer 调密而代码没动，则每次 tick 都
    落在同一个窗里 → 「同窗保持」，屏幕不动而作业在空转。

    dwell 以同名 service / timer 里的 `Environment=ROTATE_SECONDS=…` 为准（Pi 上确实这么
    覆盖），两处都没有才用代码默认值。解析不出周期时只报提示：`OnCalendar=hourly` 这种写法换算不出「每 N 分钟」，
    猜一个数字去比只会制造假红——但也不能静默放过，得留一行让人去人工确认。
    """
    timer_p = os.path.join(ROOT, *ROTATE_TIMER.split("/"))
    dwell_txt = code_defaults().get("ROTATE_SECONDS")
    if dwell_txt is None:
        rep.add(INFO, ROTATE_TIMER, 0, "代码里没找到 ROTATE_SECONDS 默认值，跳过节拍核对")
        return
    if not os.path.exists(timer_p):
        rep.add(INFO, ROTATE_TIMER, 0, "找不到 timer 文件，无法核对推进节拍，请人工确认")
        return
    text = read(timer_p)
    dwell = _as_number(dwell_txt)
    for unit in (ROTATE_UNIT, ROTATE_TIMER):        # service 覆盖优先，其次 timer 里的写法
        p = os.path.join(ROOT, *unit.split("/"))
        if not os.path.exists(p):
            continue
        override = _RE_TIMER_DWELL.search(read(p))
        if override:
            dwell = float(override.group(1))
            break
    if dwell is None:
        rep.add(INFO, ROTATE_TIMER, 0, "ROTATE_SECONDS 默认值不是数字，跳过节拍核对")
        return
    minutes, why, lineno = _timer_interval_minutes(text)
    if minutes is None:
        rep.add(INFO, ROTATE_TIMER, lineno, "无法核对推进节拍（%s），请人工确认它与 "
                "ROTATE_SECONDS=%g 秒一致" % (why, dwell))
        return
    if abs(minutes * 60 - dwell) > 1e-6:
        rep.add(REQUIRED, ROTATE_TIMER, lineno,
                "推进节拍 %s = 每 %g 分钟，与 ROTATE_SECONDS = %g 秒节拍不一致；"
                "只改一边会让「一条待多久」与「多久推进一次」错位（跳条或重复）"
                % (why, minutes, dwell))


def check_site_html(rep):
    """docs/ 下真正发布的页面也要查 eval / document.write。

    这些页面不在 source_files() 里（见那里的注释），所以 H5、行宽这些会产生误报的
    检查确实不覆盖它们——但「页面里执行任意字符串」这件事必须覆盖，
    否则 check_html 全仓只作用在 Pi/site/index.html 一个文件上，形同虚设。
    """
    base_dir = os.path.join(ROOT, "docs")
    if not os.path.isdir(base_dir):
        return
    for base, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if f.endswith((".html", ".js")):
                p = os.path.join(base, f)
                check_html(p, read(p), rep)


def scan_sources(rep):
    """遍历 source_files()，按扩展名分派到各检查，返回 (文件数, 总行数)。

    生成物跳过风格检查（它们只做一致性校验）；docs/ 的页面不在这里，
    由 check_site_html() 补上——两处都调 check_html，但覆盖面不同。
    """
    files = source_files()
    total_lines = 0
    for path in files:
        name = rel(path)
        if name in GENERATED:
            continue                       # 生成物只做一致性校验，不做风格检查
        text = read(path)
        total_lines += len(text.splitlines())
        if path.endswith(".py"):
            check_python(path, text, rep)
        elif path.endswith((".yml", ".yaml")):
            check_yaml(path, text, rep)
        elif path.endswith((".html", ".js")):
            check_html(path, text, rep)
    check_site_html(rep)                   # docs/ 的页面（不在 source_files 里）
    return len(files), total_lines


def check_secrets_script(rep):
    """复用 tools/check_secrets.py 扫凭据 —— 两套规则各写一份必然漂移。

    cwd 必须是仓库根：那个脚本用 `git ls-files` 取文件列表，从子目录跑
    （比如 `cd docs && python ../tools/review_check.py`）只会扫到子树，
    根目录里的 key 反而"扫不到"，且结论行还是全绿。
    """
    sec = os.path.join(ROOT, "tools", "check_secrets.py")
    if not os.path.exists(sec):
        rep.add(REQUIRED, "tools/check_secrets.py", 0, "凭据扫描脚本不存在")
        return
    r = subprocess.run([sys.executable, sec], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        rep.add(CRITICAL, "tools/check_secrets.py", 0,
                "凭据扫描未通过：%s" % (r.stdout or r.stderr).strip()[:300])


def print_report(rep, strict):
    """分级打印并返回退出码：Critical/Required 必失败，`--strict` 连 Nit 也算。"""
    if not rep.rows:
        print("全部通过，没有发现任何问题。")
        return 0
    for sev in (CRITICAL, REQUIRED, NIT, INFO):
        rows = rep.of(sev)
        if not rows:
            continue
        print("[%s] %s · %d 项" % (LABEL[sev], sev, len(rows)))
        for _, path, line, msg in sorted(rows, key=lambda r: (r[1], r[2])):
            print("  %s:%s  %s" % (path, line or "-", msg))
        print()

    fails = len(rep.of(CRITICAL)) + len(rep.of(REQUIRED))
    if strict:
        fails += len(rep.of(NIT))
    print("结论：%s（阻断 %d / 必改 %d / 可选 %d / 提示 %d）"
          % ("不通过" if fails else "通过",
             len(rep.of(CRITICAL)), len(rep.of(REQUIRED)),
             len(rep.of(NIT)), len(rep.of(INFO))))
    return 1 if fails else 0


def main():
    """跑全部检查并打印分级报告，返回退出码（0 通过 / 1 不通过）。

    `--strict` 把 Nit 也算失败（合入前最后一遍）；`--no-tests` 跳过三项依赖
    测试目录与生成器的检查（生成物漂移、模块有用例、用例数同步）——
    它是给「只想快速看风格」的场景用的，跳过哪三项写在用法里，别让人以为
    它只跳过生成物那一条（以前注释就是这么写的，理由还错：生成物检查根本不用 git）。
    """
    args = sys.argv[1:]
    strict = "--strict" in args
    rep = Report()

    count, total_lines = scan_sources(rep)
    print("代码审查门禁 · %s" % os.path.basename(ROOT))
    print("扫描 %d 个文件 / %d 行\n" % (count, total_lines))

    check_secrets_script(rep)
    if "--no-tests" not in args:
        check_generated(rep)
        check_tests(rep)
        check_test_count_sync(rep)
    check_doc_sync(rep)
    check_workflow_sync(rep)
    check_rotate_timer_sync(rep)
    check_version_sync(rep)
    return print_report(rep, strict)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
