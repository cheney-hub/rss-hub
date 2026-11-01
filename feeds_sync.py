#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地那份 feeds.txt 与仓库对齐 —— 只有 Pi 需要。

为什么单独一个文件：这是一件「自成一体的小事」——不碰条目、不碰 XML、不碰轮播，
只是「取回来 → 判断像不像清单 → 原子替换」。塞进 `build_feed.py` 会让那个已经最重的
文件继续变长，而它与抓取逻辑唯一的关系就是「必须在读清单之前跑」。

谁需要它：只有 Pi。`Pi/` 上的那份清单是仓库的**副本**（见那里的部署方式），
靠「记得 scp 一次」维持同步，实测漂过 3 行注释 —— 两边各出一版 feed，屏幕上看不出来。
CI 不需要：那里的 feeds.txt 本来就是 checkout 出来的，多一次网络往返没有收益，
所以 `FEEDS_SYNC_URL` 默认为空 = 不联网。

本文件读（ OPERATIONS.md 的参数表与这里是同一份契约，改一边要改另一边）：
  FEEDS_SYNC_URL          从这个地址拉一份清单覆盖本地，默认空（不同步）。Pi 上指仓库 raw
  FEEDS_SYNC_TIMEOUT      拉清单的超时秒数，默认 20
  FEEDS_SYNC_MIN_SOURCES  拉到的文件至少有几个地址才算清单，默认 10，低于它一律不落盘
"""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import version  # noqa: E402  （版本号唯一真源）

__version__ = version.__version__

FEEDS_SYNC_URL = os.environ.get("FEEDS_SYNC_URL", "")
FEEDS_SYNC_TIMEOUT = max(3, int(os.environ.get("FEEDS_SYNC_TIMEOUT", "20")))
# 「少得不像清单」用地址条数判：404 页、跳转页、WAF 拦截页都过不了这关，
# 而正常清单有几十条，余量很大。
FEEDS_SYNC_MIN_SOURCES = max(1, int(os.environ.get("FEEDS_SYNC_MIN_SOURCES", "10")))

MAX_LIST_BYTES = 2 * 1024 * 1024          # 清单再大也要有上限，别让人喂一个 GB 进来


def looks_like_feeds(text):
    """拉回来的东西像不像一份清单：不是 HTML 页、地址条数够。

    两个判据分开的原因是它们防的事不一样：数条数能挡住空文件/报错文本，
    但 WAF 拦截页、仓库 404 页都可能带着一堆链接，只有 `<html` 这一条能抓住。
    """
    if "<html" in text.lower():
        return False
    n = sum(1 for ln in text.splitlines() if ln.strip().startswith(("http://", "https://")))
    return n >= FEEDS_SYNC_MIN_SOURCES


def write_list(path, body):
    """原子替换清单；写不进去返回 False，调用方继续用旧的。

    先写同名 + `.new` 再 `os.replace`：读到一半的清单会让这一轮少抓几十个源，
    而 replace 在同一文件系统上是原子的，读方只会看到旧内容或新内容。
    """
    tmp = path + ".new"
    try:
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    except OSError as exc:
        # 磁盘满、目录只读都会走到这里。宁可用旧清单继续构建，也不要一轮都跑不出来
        print("[同步] feeds.txt 写不进去，沿用本地那份（%s）" % exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def sync(feeds_file, ua, url=None):
    """勾一轮清单同步，返回 "updated" / "same" / "kept"。

    `ua` 由调用方传（`build_feed.UA`）：伪装 UA 是抓取层的既有约定，这里再养一份
    必然漂移。返回值只用于日志 —— 调用方**不需要**根据它决定走不走，
    因为同步失败从来不应阻断构建。

    设计上的硬要求：**任何失败都保留本地那份继续往下跑**。拉不到、超时、返回 HTML
    登录页、内容太少、甚至落盘失败，一律返回 "kept"。宁可这一轮用旧清单，
    也不能因为配置同步这种小事让整份 feed 停更 —— 后者在屏上的表现和「全网故障」
    一模一样，却没有任何报错线索。
    """
    url = url if url is not None else FEEDS_SYNC_URL
    if not url:
        return "kept"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": ua or "",
            # 中间可能有 CDN：清单是配置，要的就是「一改就生效」，不要缓存副本
            "Cache-Control": "no-cache",
        })
        with urllib.request.urlopen(req, timeout=FEEDS_SYNC_TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return "kept"
            body = resp.read(MAX_LIST_BYTES)
    except Exception as exc:                                 # noqa: BLE001
        # 网络问题连日志都不必刷屏：每 5 分钟一轮，断了会一直打
        print("[同步] feeds.txt 取不到，沿用本地那份（%s）" % exc)
        return "kept"

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "kept"
    if not looks_like_feeds(text):
        return "kept"

    try:
        with open(feeds_file, "rb") as f:
            old = f.read()
    except OSError:
        old = None
    if old == body:
        return "same"
    if not write_list(feeds_file, body):
        return "kept"
    print("[同步] feeds.txt 已更新（%d 字节）" % len(body))
    return "updated"
