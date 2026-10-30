#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「显示滚动」从「抓取」里拆出来：只读 feed.xml，按显示时间线选一条写 feed-wrap.xml。

为什么单独一个脚本（这是本次改动的核心，别合并回 build_feed）：

    抓取时间线 = 后台多久去源站拿一次（白天 5 分钟 / 夜间整点，跑一次要抓 49 个源）
    显示时间线 = 屏幕多久换一条（ROTATE_SECONDS = 300 秒，全天一致）

这两件事节奏不同、开销差两个数量级，不能捆在一起。以前选条写在 build_feed 的
run_once() 里，于是「抓取一小时跑一次」直接导致「屏幕一小时才换一条」——夜间的
症状就是屏幕上那条一动不动。

现在：feed.xml 由抓取任务更新（池子）；本脚本每 5 分钟跑一次，不联网、不抓源，
只负责「这一窗播哪一条」，池子没换血也能继续往下走（会播池子里还没标注的旧条目）。

用法（systemd 由 rss-rotate.service 拉起）：
    python3 /home/user/pi-pipeline/rotate_screen.py

产出：feed-wrap.xml（一条）+ rotate.json（显示游标）。
选条规则本身在 rotate.py 里，本文件不复制任何规则。
"""
import email.utils
import json
import os
import sys
import xml.etree.ElementTree as ET

# 选条规则与渲染都在引擎代码里，本脚本只复用、不重写
sys.path.insert(0, "/home/user/rss-hub")
import rotate                                    # noqa: E402
from render import LINE_WIDTH, build_rss         # noqa: E402

# 路径写死而不是做成环境变量：这是 Pi 上唯一部署位置的专用脚本，
# 做成旋钮就得同步进根目录 OPERATIONS.md 的参数表（那是根系统的文档，不该记 Pi 的路径）。
RSS_HUB = "/home/user/rss-hub"
FEED = os.path.join(RSS_HUB, "docs", "feed.xml")          # 池子，抓取任务写
WRAP_OUT = os.path.join(RSS_HUB, "docs", "feed-wrap.xml")  # 设备版，本脚本写
STATE = os.path.join(RSS_HUB, "docs", "rotate.json")       # 显示游标，本脚本写
CHANNEL = "RSS 头条 · 小屏"


def _text(node):
    return (node.text or "").strip() if node is not None else ""


def parse_feed_xml(path):
    """把完整版 feed.xml 还原成条目字典，并取出 channel 的 <link>。

    条目只取渲染和选条真正用到的六个键：title / link / summary / ts / src / src_name。
    guid 不在这里算 —— render.item_guid 用 link + src 现算，与本文件无关。

    返回 (条目列表, channel 地址)。地址要带出来是因为设备版地址由它推导，
    不写死：地址的真源只有 FEED_URL 一处，写死就会两处漂移。
    """
    root = ET.parse(path).getroot()
    link_node = root.find("./channel/link")
    feed_url = (link_node.text or "").strip() if link_node is not None else ""
    items = []
    for node in root.iter("item"):
        pub = _text(node.find("pubDate"))
        try:
            ts = int(email.utils.parsedate_to_datetime(pub).timestamp())
        except Exception:
            ts = 0                       # 时间解析不出来就落到池子最末，不抢屏
        src_node = node.find("source")
        items.append({
            "title": _text(node.find("title")),
            "link": _text(node.find("link")),
            "summary": _text(node.find("description")),
            "ts": ts,
            "src": (src_node.get("url") or "") if src_node is not None else "",
            "src_name": _text(src_node),
        })
    return items, feed_url


def wrap_url_of(feed_url):
    """设备版地址 = 完整版地址同目录下的 feed-wrap.xml。"""
    if not feed_url:
        return ""
    return feed_url.rsplit("/", 1)[0] + "/feed-wrap.xml"


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}       # 状态坏了就重开一轮，宁可重复一条也不能乱序


def wrap_description(n_src):
    return ("多源头条，纯文本；每 %d 分钟轮换一条，走完全部再从头开始。"
            "标题只放屏上显示得下的 %g 全角字，余下原文接在正文里，源 %d 个"
            % (rotate.ROTATE_SECONDS // 60, LINE_WIDTH, n_src))


def main():
    if not os.path.isfile(FEED):
        sys.exit("feed.xml 不存在：%s" % FEED)
    items, feed_url = parse_feed_xml(FEED)
    if not items:
        sys.stderr.write("feed.xml 里没有条目，保持上一版不动\n")
        return 1

    pool = items[:rotate.ROTATE_POOL_MAX] if rotate.ROTATE_POOL_MAX else items
    state = load_state()
    pick, new_state, note = rotate.rotate_pick(pool, state)
    if pick is None:
        # 同窗内原条目已离开池子：宁可停在上一版，也不在 5 分钟内换两条
        sys.stderr.write("skip: %s\n" % note)
        return 0

    n_src = len({it["src"] for it in items if it.get("src")})
    _write_atomic(WRAP_OUT, build_rss(
        [pick], wrap_url_of(feed_url), {"total": n_src},
        channel_title=CHANNEL, description=wrap_description(n_src), screen=True))
    _write_atomic(STATE, json.dumps(new_state, ensure_ascii=False, indent=1))
    print("上屏：%s ｜ %s" % (note, (pick.get("title") or "")[:40]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
