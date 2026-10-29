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
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS access key"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    (r"(?i)\b(pass(?:word|wd)?|pwd|secret)\s*[:=]\s*['\"]?[^\s'\"#]{6,}", "明文口令"),
    (r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}", "Bearer 令牌"),
]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
SKIP_FILES = {"tools/check_secrets.py", "pi/rss-hub.env.example"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".woff", ".woff2"}


def tracked_files():
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
    print("\n提示：真实值放仓库 Secret 或本机 /etc/rss-hub/rss-hub.env，仓库里只留说明。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
