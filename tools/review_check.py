#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""代码审查门禁：把 CODE_REVIEW.md 里「机器能判的」规则变成可执行检查。

人工评审负责正确性、架构、命名这些需要判断的事；能写成规则的（文件多大、
函数多长、有没有裸 except、生成文件是否被手改）不该占用人的时间。

用法：
    python tools/review_check.py             # 全量检查，有 Critical/Required 则退出码 1
    python tools/review_check.py --strict    # 连 Nit 也当失败（合入前最后一遍）
    python tools/review_check.py --no-tests  # 跳过生成物漂移检查（本地没装 git 时）

分级口径与 CODE_REVIEW.md 一致：
    Critical  阻断合入：语法错误、裸 except、凭据、YAML 非法
    Required  必须改完才能合入：文件/函数超限、生成物与脚本不一致、模块无测试、硬编码地址
    Nit       可忽略：行宽、TODO、可变全局
    Info      提示：文件接近上限
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

CRITICAL, REQUIRED, NIT, INFO = "Critical", "Required", "Nit", "Info"
RANK = {CRITICAL: 0, REQUIRED: 1, NIT: 2, INFO: 3}
LABEL = {CRITICAL: "阻断", REQUIRED: "必改", NIT: "可选", INFO: "提示"}


class Report(object):
    def __init__(self):
        self.rows = []

    def add(self, sev, path, line, msg):
        self.rows.append((sev, path, line, msg))

    def of(self, sev):
        return [r for r in self.rows if r[0] == sev]


def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def source_files():
    out = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith((".py", ".yml", ".yaml", ".html", ".js")):
                out.append(os.path.join(base, f))
    return sorted(out)


def read(path):
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
    name = rel(path)
    for i, ln in enumerate(text.splitlines(), 1):
        for bad, why in (("document.write(", "会阻塞解析"), ("eval(", "执行任意字符串")):
            if bad in ln:
                rep.add(REQUIRED, name, i, "%s %s" % (bad, why))


def check_yaml(path, text, rep):
    name = rel(path)
    try:
        import yaml
    except ImportError:
        return
    try:
        yaml.safe_load(text)
    except Exception as e:
        rep.add(CRITICAL, name, getattr(e, "problem_mark", None) and
                e.problem_mark.line + 1 or 0, "YAML 非法：%s" % e)


def check_generated(rep):
    """生成物与脚本是否一致 —— 手改 711 行生成文件是最难发现的一类漂移。"""
    # 生成脚本可能要 import 仓库根目录的模块（golden_snapshot 就依赖 build_feed/render），
    # 所以把根目录的 .py 一起搬进沙箱，脚本在沙箱里跑，不碰工作区
    root_py = [f for f in os.listdir(ROOT) if f.endswith(".py")]
    for target, gen in GENERATED.items():
        target_path = os.path.join(ROOT, *target.split("/"))
        gen_path = os.path.join(ROOT, *gen.split("/"))
        if not (os.path.exists(target_path) and os.path.exists(gen_path)):
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
            subprocess.run(cmd, cwd=tmp, capture_output=True, timeout=120)
            fresh_p = os.path.join(tmp, *target.split("/"))
            if not os.path.exists(fresh_p):
                rep.add(REQUIRED, target, 1, "生成脚本没有产出该文件，检查 %s" % gen)
                continue
            if read(fresh_p) != read(target_path):
                rep.add(REQUIRED, target, 1,
                        "与 %s 生成结果不一致，请跑一遍脚本而不是手改" % gen)
        except Exception as e:
            rep.add(INFO, target, 1, "生成物一致性检查跳过：%s" % e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def check_tests(rep):
    """每个顶层模块至少有一个测试文件引用它 —— 没有测试的改动无法验证。"""
    tests_dir = os.path.join(ROOT, "tests")
    blob = ""
    if os.path.isdir(tests_dir):
        for f in os.listdir(tests_dir):
            if f.endswith(".py"):
                blob += read(os.path.join(tests_dir, f))
    for f in sorted(os.listdir(ROOT)):
        if not f.endswith(".py") or f.startswith("_"):
            continue
        mod = f[:-3]
        if ("import " + mod) not in blob and ("from " + mod) not in blob:
            rep.add(REQUIRED, f, 1, "没有任何测试引用该模块（tests/ 下需覆盖核心纯函数）")


def check_doc_sync(rep):
    """代码里的可调参数必须出现在 README / OPERATIONS 里 —— 文档与常量是一份契约。

    这条规则来自真实事故：OPERATIONS 的参数表曾长期写着「55 轮 / SCREEN_BODY_MAX 60」，
    而代码早已是「13 轮 / 90」，照着文档调参等于没调。
    """
    doc = ""
    for f in ("README.md", "OPERATIONS.md"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            doc += read(p)
    if not doc:
        return
    names = set()
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith(".py"):
                continue
            names.update(re.findall(r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]+)',
                                    read(os.path.join(base, f))))
    for n in sorted(names):
        if n not in doc:
            rep.add(REQUIRED, "OPERATIONS.md", 0,
                    "环境变量 %s 在文档里没有出现，改代码请同步参数表" % n)


def check_version_sync(rep):
    """版本号必须从 version.py 一路传到页面上。

    漏更新的后果不是报错，而是「页脚写着 v1.0.0、线上跑的是 1.1.0」，
    排查时按旧版行为去推断，越查越偏。这里只做「有没有出现」的判断，
    CHANGELOG 里出现历史版本号是正常的，所以不查那一份。
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
    for name in VERSION_DOCS:
        p = os.path.join(ROOT, *name.split("/"))
        if not os.path.exists(p):
            rep.add(REQUIRED, name, 0, "版本号应展示的文件不存在")
        elif ver not in read(p):
            rep.add(REQUIRED, name, 0, "没有出现当前版本号 %s，发版要同步页面" % ver)


def main():
    args = sys.argv[1:]
    strict = "--strict" in args
    rep = Report()

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

    # 凭据扫描复用已有脚本，避免两套规则漂移
    sec = os.path.join(ROOT, "tools", "check_secrets.py")
    if os.path.exists(sec):
        r = subprocess.run([sys.executable, sec], capture_output=True, text=True)
        if r.returncode != 0:
            rep.add(CRITICAL, "tools/check_secrets.py", 0,
                    "凭据扫描未通过：%s" % (r.stdout or r.stderr).strip()[:300])

    if "--no-tests" not in args:
        check_generated(rep)
        check_tests(rep)
    check_doc_sync(rep)
    check_version_sync(rep)

    # ---- 输出 ----
    print("代码审查门禁 · %s" % os.path.basename(ROOT))
    print("扫描 %d 个文件 / %d 行\n" % (len(files), total_lines))
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


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
