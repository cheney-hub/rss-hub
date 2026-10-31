#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库凭据扫描：本地提交前 / CI 里运行，命中即退出码 1。

用法：
    python3 tools/check_secrets.py          # 扫 tracked 文件
    python3 tools/check_secrets.py --all    # 扫工作目录全部文件
"""
import os
import re
import subprocess
import sys

# 只认「像真凭据」的形态，避免把文档里的占位说明误判为泄露
RULES = [
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "GitHub token"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", "GitHub fine-grained token"),
    (r"\bdot_app_[A-Za-z0-9_-]{12,}\b", "Dot API 密钥"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    # 「明文口令」这条必须放过 `SECRET = os.environ.get(...)` 这类**推荐写法**：
    # 否则照着 H5（走环境变量）改代码的人会被 H6 拦下，两条硬规则互相打架。
    (r"(?i)\b(pass(?:word|wd)?|pwd|secret)\s*[:=]\s*"
     r"(?!os\.environ|os\.getenv|environ\[)['\"]?[^\s'\"#]{6,}", "明文口令"),
    (r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}", "Bearer 令牌"),
]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
SKIP_FILES = {"tools/check_secrets.py"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2"}


def tracked_files():
    """要扫的文件列表：默认取 `git ls-files`（只覆盖已入库文件）。

    两个坑都来自「相对路径」这件事：
      * `git ls-files` 的输出**相对于当前工作目录**，从子目录跑（`cd docs && ...`）
        只会列出那棵子树，根目录里的 key 反而扫不到、而且退出码是 0；
      * 未 `git add` 的新文件不在列表里，所以「写完还没暂存就自检」这一步
        token 是隐形的。
    git 不可用时回落到 os.walk(".")（同样是相对 cwd），覆盖范围比 tracked 更宽。
    """
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True,
                             text=True, check=True).stdout
        return [p for p in out.splitlines() if p]
    except Exception:
        found = []
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                found.append(os.path.relpath(os.path.join(root, f), "."))
        return found


def scan(paths):
    """逐文件逐行跑规则，返回 [(路径, 行号, 规则名, 摘录)]。

    跳过规则文件自己（否则 RULES 里的 `-----BEGIN ... PRIVATE KEY-----` 会自匹配）
    与二进制扩展名；读不动的文件直接跳过——扫描器不该因为一个坏文件整体失败。
    摘录取前 90 字符，够定位又不至于把疑似凭据整行打进 CI 日志。
    """
    hits = []
    for p in paths:
        p_norm = p.replace("\\", "/")
        if p_norm in SKIP_FILES or os.path.splitext(p_norm)[1].lower() in BINARY_EXT:
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    for pattern, label in RULES:
                        if re.search(pattern, line):
                            hits.append((p_norm, lineno, label, line.strip()[:90]))
        except (OSError, UnicodeError):
            continue
    return hits


def main():
    """默认扫 tracked 文件；`--all` 扫工作目录全部文件（含未暂存的新文件）。

    默认模式更贴近「提交前自检」（只看会被提交的东西）；`--all` 用于排查
    「本地有个配置文件带 key」这类不在版本控制里的泄露。
    """
    use_all = "--all" in sys.argv
    paths = tracked_files() if not use_all else [
        os.path.join(r, f) for r, ds, fs in os.walk(".")
        for f in fs if not (set(r.replace("\\", "/").split("/")) & SKIP_DIRS)
    ]
    hits = scan(paths)
    if not hits:
        print("凭据扫描通过：%d 个文件，未发现 token / 口令 / 私钥" % len(paths))
        return 0
    print("发现疑似凭据，请移除后再提交：", file=sys.stderr)
    for p, lineno, label, snippet in hits:
        print("  %s:%d  [%s]  %s" % (p, lineno, label, snippet), file=sys.stderr)
    print("\n提示：真实值放仓库 Secret，仓库里只留说明。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
