#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨文件契约检查：真源分散在两个文件里的那些约定，由这个模块核对。

从 tools/review_check.py 拆出来的四项（那个文件 892 行，超过它自己定的
750 行提醒线；这四项搬走后回到 600 行以内）：

  * check_version_sync      —— version.py ↔ README/OPERATIONS/页面上的版本号
  * check_workflow_sync     —— .github/workflows 里的调参 ↔ 代码默认值
  * check_rotate_timer_sync —— Pi 的 systemd timer 节拍 ↔ ROTATE_SECONDS
  * check_test_count_sync   —— README 目录树的用例数 ↔ tests/ 下实际用例数

为什么按「跨文件契约」这条线切，而不是按行随便切：这四项查的都是**两份文件
有没有对上**，改哪一条都能一眼看出该放哪边；它们还有一个共性——解析不出来就
跳过并留痕（说明性取值、`OnCalendar=hourly` 这种换算不出「每 N 分钟」的写法），
宁可少查一个也不猜数字制造假红。

引用方向只有一条：`import review_check as RC`，反过来由 review_check 在 main()
里延迟 import 本模块，两边都在模块级 import 会成环。共享的取值解析
（code_defaults / _as_number / _doc_table_values）留在 review_check ——
check_doc_sync 也在用它们，搬走就得让 review_check 反向依赖本模块。
另外一律写成 `RC.xxx` 而不是 `from review_check import xxx`：tests 会把
RC.ROOT 指到临时目录，绑成局部常量就失效了。
"""
import os
import re
import unittest

import review_check as RC

# 版本号必须出现在这几处页面上（人一眼能看到的地方），漏一处就是发版漏了
VERSION_DOCS = ("README.md", "OPERATIONS.md", "docs/index.html", "docs/screen.html")


def check_version_sync(rep):
    """版本号必须从 version.py 一路传到页面上。

    漏更新的后果不是报错，而是「页脚写着 v1.0.0、线上跑的是 1.1.0」，
    排查时按旧版行为去推断，越查越偏。这里只做「有没有出现」的判断，
    CHANGELOG 里出现历史版本号是正常的，所以不查那一份。

    匹配必须带边界：`ver not in text` 会让 v1.7.2 被 v1.7.20 命中——
    页脚写错一位反而算通过，而这正是要抓的错。
    """
    vp = os.path.join(RC.ROOT, "version.py")
    if not os.path.exists(vp):
        rep.add(RC.REQUIRED, "version.py", 0, "缺少版本号真源版本文件")
        return
    m = re.search(r'__version__\s*=\s*"([^"]+)"', RC.read(vp))
    if not m:
        rep.add(RC.REQUIRED, "version.py", 0, "version.py 里没有 __version__ = \"x.y.z\"")
        return
    ver = m.group(1)
    pat = re.compile(r"(?<![\d.])" + re.escape(ver) + r"(?![\d.])")
    for name in VERSION_DOCS:
        p = os.path.join(RC.ROOT, *name.split("/"))
        if not os.path.exists(p):
            rep.add(RC.REQUIRED, name, 0, "版本号应展示的文件不存在")
        elif not pat.search(RC.read(p)):
            rep.add(RC.REQUIRED, name, 0, "没有出现当前版本号 %s，发版要同步页面" % ver)


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
    defaults = {k: RC._as_number(v) for k, v in RC.code_defaults().items()}
    defaults = {k: v for k, v in defaults.items() if v is not None}
    wf_dir = os.path.join(RC.ROOT, ".github", "workflows")
    if not defaults or not os.path.isdir(wf_dir):
        return
    for f in sorted(os.listdir(wf_dir)):
        if not f.endswith((".yml", ".yaml")):
            continue
        seen = {}
        for m in _RE_WF_ENV.finditer(RC.read(os.path.join(wf_dir, f))):
            seen[m.group(1)] = m.group(2)          # 同名多行（minute.yml 每轮一段）取最后
        for k in sorted(seen):
            if k not in defaults:
                continue
            if abs(float(seen[k]) - defaults[k]) > 1e-9:
                rep.add(RC.REQUIRED, ".github/workflows/" + f, 0,
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
    timer_p = os.path.join(RC.ROOT, *ROTATE_TIMER.split("/"))
    dwell_txt = RC.code_defaults().get("ROTATE_SECONDS")
    if dwell_txt is None:
        rep.add(RC.INFO, ROTATE_TIMER, 0, "代码里没找到 ROTATE_SECONDS 默认值，跳过节拍核对")
        return
    if not os.path.exists(timer_p):
        rep.add(RC.INFO, ROTATE_TIMER, 0, "找不到 timer 文件，无法核对推进节拍，请人工确认")
        return
    text = RC.read(timer_p)
    dwell = RC._as_number(dwell_txt)
    for unit in (ROTATE_UNIT, ROTATE_TIMER):        # service 覆盖优先，其次 timer 里的写法
        p = os.path.join(RC.ROOT, *unit.split("/"))
        if not os.path.exists(p):
            continue
        override = _RE_TIMER_DWELL.search(RC.read(p))
        if override:
            dwell = float(override.group(1))
            break
    if dwell is None:
        rep.add(RC.INFO, ROTATE_TIMER, 0, "ROTATE_SECONDS 默认值不是数字，跳过节拍核对")
        return
    minutes, why, lineno = _timer_interval_minutes(text)
    if minutes is None:
        rep.add(RC.INFO, ROTATE_TIMER, lineno, "无法核对推进节拍（%s），请人工确认它与 "
                "ROTATE_SECONDS=%g 秒一致" % (why, dwell))
        return
    if abs(minutes * 60 - dwell) > 1e-6:
        rep.add(RC.REQUIRED, ROTATE_TIMER, lineno,
                "推进节拍 %s = 每 %g 分钟，与 ROTATE_SECONDS = %g 秒节拍不一致；"
                "只改一边会让「一条待多久」与「多久推进一次」错位（跳条或重复）"
                % (why, minutes, dwell))


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
    tests_dir = os.path.join(RC.ROOT, "tests")
    readme = os.path.join(RC.ROOT, "README.md")
    if not (os.path.isdir(tests_dir) and os.path.exists(readme)):
        return
    names = sorted(f for f in os.listdir(tests_dir)
                   if f.startswith("test") and f.endswith(".py"))
    try:
        actual = {n: _count_cases(tests_dir, n) for n in names}
    except Exception as e:                                   # 加载失败不该连带门禁崩掉
        rep.add(RC.INFO, "README.md", 0, "用例数核对跳过：%s" % e)
        return
    total = sum(actual.values())

    text = RC.read(readme)
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
            rep.add(RC.REQUIRED, "README.md", 0,
                    "目录树里没有 %s（实际 %d 例），新增测试文件请同步" % (n, actual[n]))
            continue
        counted = [(ln, v) for ln, v in hits if v is not None]
        if not counted:                                      # 状态 2
            rep.add(RC.REQUIRED, "README.md", hits[0][0],
                    "%s 在目录树里出现了，但那一行的行尾不是「# N 例」计数格式"
                    "（实际 %d 例）；数字必须能被机器抽出来，别写成散文"
                    % (n, actual[n]))
            continue
        if len(counted) > 1:
            rep.add(RC.REQUIRED, "README.md", counted[0][0],
                    "%s 在目录树里写了 %d 处计数（第 %s 行），只保留一处，"
                    "否则以哪个为准说不清"
                    % (n, len(counted), "、".join(str(ln) for ln, _ in counted)))
        if counted[0][1] != actual[n]:                       # 状态 5
            rep.add(RC.REQUIRED, "README.md", counted[0][0],
                    "%s 写的 %d 例，实际 %d 例" % (n, counted[0][1], actual[n]))

    for n in sorted(set(declared) - set(names)):             # 状态 4
        rep.add(RC.REQUIRED, "README.md", declared[n][0][0],
                "README 目录树里的 %s 不存在（tests/ 下没有）" % n)


def _check_case_totals(rep, text, block, base, total, actual):
    """用例总数：目录树 `tests/  # N 个用例` 与正文「单测：N 个用例」两处。

    前者在树切片里找（标记行本身就在切片里），后者在正文里找；切片退化成整篇时
    两处都还能找到——「标记样式变了」不能让总数核对静默消失。
    """
    for m in _RE_TOTAL_TREE.finditer(block):
        if int(m.group(1)) != total:
            rep.add(RC.REQUIRED, "README.md", base + block[:m.start()].count("\n"),
                    "目录树写的 %s 个用例，实际 %d 个（%s）"
                    % (m.group(1), total,
                       " + ".join("%s %d" % (k, v) for k, v in sorted(actual.items()))))
    for m in _RE_TOTAL_PROSE.finditer(text):
        if int(m.group(1)) != total:
            rep.add(RC.REQUIRED, "README.md", text[:m.start()].count("\n") + 1,
                    "「改动代码前后跑这三条」那行写的 %s 个用例，实际 %d 个"
                    % (m.group(1), total))


