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

用法（systemd 由 rss-rotate.service 拉起，节拍由 rss-rotate.timer 定）：
    python3 /home/user/pi-pipeline/rotate_screen.py

产出：feed-wrap.xml（一条）+ rotate.json（显示游标）。
选条规则本身在 rotate.py 里，本文件不复制任何规则。
"""
import email.utils
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

# 路径写死而不是做成环境变量：这是 Pi 上唯一部署位置的专用脚本，
# 做成旋钮就得同步进根目录 OPERATIONS.md 的参数表（那是根系统的文档，不该记 Pi 的路径）。
# 常量放在 import 之前：下面的 sys.path.insert 要用 RSS_HUB，而这条路径只能写一处——
# 两处字面量迟早漂移成「import 从 A 读、读写却走 B」。
RSS_HUB = "/home/user/rss-hub"
FEED = os.path.join(RSS_HUB, "docs", "feed.xml")            # 池子，抓取任务写
WRAP_OUT = os.path.join(RSS_HUB, "docs", "feed-wrap.xml")   # 设备版，本脚本写
STATE = os.path.join(RSS_HUB, "docs", "rotate.json")        # 显示游标，本脚本写
CHANNEL = "RSS 头条 · 小屏"

# build_rss 每次都会把 lastBuildDate 改成「现在」，所以两份产物直接比全文永远不相等。
# 比较前把它摘掉：「时间戳变了、内容没变」对下游不算更新，跳过写入反而让这个字段
# 保持「内容最后一次变化的时间」的原意。
_STAMP_RE = re.compile(r"<lastBuildDate>[^<]*</lastBuildDate>")

# 选条规则与渲染都在引擎代码里，本脚本只复用、不重写
sys.path.insert(0, RSS_HUB)
import rotate                                     # noqa: E402
from render import LINE_WIDTH, build_rss          # noqa: E402

try:                                              # 文案的真源在 render，本文件只转发
    from render import device_description         # noqa: E402
except ImportError:                               # 抽出 render.device_description 前先顶上
    def device_description(n_src):
        """与 render.device_description 等价的临时实现（抽取落地后可删）。"""
        return ("多源头条，纯文本；每 %d 分钟轮换一条，走完全部再从头开始。"
                "标题只放屏上显示得下的 %g 全角字，余下原文接在正文里，源 %d 个"
                % (rotate.ROTATE_SECONDS // 60, LINE_WIDTH, n_src))


def _text(node):
    """取节点文本并压掉首尾空白；节点不存在时给空串（feed 里缺字段很常见）。"""
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
    """写临时文件再 os.replace：读的一方永远看到完整文件，不会读到写了一半的 XML。

    只保证「替换」这一瞬间的原子性，不 fsync 目录——断电最坏是丢这一版，
    上一版仍然完整，对「大不了重复一条」的轮播来说够用。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _write_if_changed(path, text):
    """内容与现有一致时不落盘；返回是否真的写了。

    rss-rotate.timer 每 5 分钟一跑 = 每天 288 次写同一张 SD 卡，而池子没换血时
    绝大多数轮次算出来的正文逐字节相同。跳过后既省卡的擦写，也免掉一次无谓的
    mtime 变化（feed-wrap.xml 的 mtime 会被下游当「有新内容」看）。

    比较时摘掉 <lastBuildDate>（见 _STAMP_RE），所以「只刷了时间戳」也算没变。
    对 rotate.json 这种 JSON 当然没有该字段，行为退化成逐字节比较。
    """
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            old = f.read()
        if _STAMP_RE.sub("", old) == _STAMP_RE.sub("", text):
            return False
    except OSError:
        pass                    # 文件不存在 / 没权限读，照写不误
    _write_atomic(path, text)
    return True


def load_state():
    """读显示游标（rotate.json）；文件缺失或内容坏了就当空状态重开一轮。

    宁可重复播一条，也不能因为一个坏 JSON 让游标乱序、甚至整条时间线停住。
    """
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}       # 状态坏了就重开一轮，宁可重复一条也不能乱序


def wrap_description(n_src):
    """设备版 <description> 文案：统一由 render.device_description 出，别在这儿另写一份。

    以前这段文案在 build_feed.write_feed 与本文件各写一遍，改一个数字要改两处，
    而且两处「源 %d 个」的算法还不一样（build 用 stats["total"]，这里数的是去重后的
    源地址）。现在只有 render 一处真源，本函数只是转发。
    """
    return device_description(n_src)


def main():
    """跑一轮：读池子 → 选这一窗播哪条 → 写 feed-wrap.xml 与 rotate.json。

    退出码：0 = 正常（含「本窗不推进」这种主动跳过）；1 = 池子空，保持上一版不动。
    """
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
    wrap = build_rss([pick], wrap_url_of(feed_url), {"total": n_src},
                     channel_title=CHANNEL, description=wrap_description(n_src),
                     screen=True)
    wrote = _write_if_changed(WRAP_OUT, wrap)
    _write_if_changed(STATE, json.dumps(new_state, ensure_ascii=False, indent=1))
    print("上屏：%s ｜ %s%s" % (note, (pick.get("title") or "")[:40],
                              "" if wrote else "（内容未变，跳过写入）"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
