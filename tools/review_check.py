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

四项「跨文件契约」检查（版本号 / workflow / timer 节拍 / 用例数 ↔ 文档）在 tools/gate_contracts.py。
"""

import ast
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

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


def as_number(s):
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
        want, got = as_number(defaults[n]), as_number(documented[n])
        if want is None or got is None or abs(want - got) < 1e-9:
            continue
        rep.add(REQUIRED, "OPERATIONS.md", 0,
                "参数表里 %s 写的是 %s，代码默认值是 %s —— 文档与常量必须一致"
                % (n, documented[n].strip("`* "), defaults[n]))


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

    四项「跨文件契约」检查在 tools/gate_contracts.py（文档↔代码↔CI/单元文件），
    那里反过来要用本模块的 Report / read / ROOT，所以只能**延迟 import**：
    两边都在模块级 import 会成环。import 失败要留一条必改，不能让这四项静默消失。
    """
    args = sys.argv[1:]
    strict = "--strict" in args
    rep = Report()
    try:
        import gate_contracts as GC
    except Exception as e:
        GC = None
        rep.add(REQUIRED, "tools/gate_contracts.py", 0,
                "契约检查模块加载失败（%s: %s），版本/workflow/节拍/用例数四项没查"
                % (type(e).__name__, e))

    count, total_lines = scan_sources(rep)
    print("代码审查门禁 · %s" % os.path.basename(ROOT))
    print("扫描 %d 个文件 / %d 行\n" % (count, total_lines))

    check_secrets_script(rep)
    if "--no-tests" not in args:
        check_generated(rep)
        check_tests(rep)
        if GC:
            GC.check_test_count_sync(rep)
    check_doc_sync(rep)
    if GC:
        GC.check_workflow_sync(rep)
        GC.check_rotate_timer_sync(rep)
        GC.check_version_sync(rep)
    return print_report(rep, strict)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
