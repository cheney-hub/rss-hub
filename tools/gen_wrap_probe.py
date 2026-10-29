#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「标题折行」探针源：docs/wrap/t1.xml ~ t4.xml

背景
----
Dot Quote/0 的 RSS 卡片是**单行**大字号渲染，一屏只放得下 13～14 个全角字。
想一屏显示多行标题，唯一可能的口子是：云端抓取时是否保留标题里的换行符。
这个不试不知道，且不同渲染端行为不同（有的保留 \\n，有的把标题当 HTML 认 <br>，
有的两种都吃掉）。所以用**同一句话**做四个源，加进 Dot App 的循环内容逐个看：

    t1  真实换行（<title> 里写 &#10;）
    t2  HTML <br> 标签（不转义）
    t3  U+2028 LINE SEPARATOR
    t4  多条 item 分段（同一句话拆成 3 条，测云端是否轮播同一源的多条）

判读方法（每源一句话，看到屏幕就知道结果）
    t1/t2/t3 屏上是「3 行」→ 该分隔符生效；仍是「1 行 + 省略号」→ 被吃掉
    t4 屏上 3 段内容会变 → 云端会轮播；一直停在第一段 → 只取最新 1 条

用法
    python3 tools/gen_wrap_probe.py            # 生成到 docs/wrap/
部署后地址：https://cheney-hub.github.io/rss-hub/wrap/t1.xml （t2/t3/t4 同理）
"""
import os
import sys
import email.utils
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from build_feed import wrap_title, join_title_xml, esc    # noqa: E402

BASE_URL = "https://cheney-hub.github.io/rss-hub/wrap"
OUT_DIR = os.path.join(BASE, "docs", "wrap")
LINE_W = 13          # 与 SCREEN_TITLE_WIDTH 一致：实测单行容量
LINES = 3            # 想验证的折行数
SAMPLE = "国家能源局：9月核发绿证3.2亿个，同比增长21%，全年累计突破20亿个"

NOW = datetime.now(timezone.utc)
BUILD = email.utils.format_datetime(NOW)

PROBES = [
    ("t1", "①真实换行", "lf", "标题里插入真实换行符，看云端是否保留"),
    ("t2", "②br标签", "br", "标题里放 CDATA 包裹的 <br>，看云端是否按 HTML 解析"),
    ("t3", "③U+2028", "u2028", "标题里放 U+2028 行分隔符"),
]


def channel(title, desc, items, self_url):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0">',
           "<channel>",
           "<title>%s</title>" % esc(title),
           "<link>%s</link>" % esc(self_url),
           "<description>%s</description>" % esc(desc),
           "<language>zh-CN</language>",
           "<lastBuildDate>%s</lastBuildDate>" % BUILD]
    for it in items:
        out += ["<item>",
                "<title>%s</title>" % it["title_xml"],
                "<link>%s</link>" % esc(it["link"]),
                "<pubDate>%s</pubDate>" % it["pub"],
                '<guid isPermaLink="false">%s</guid>' % esc(it["guid"]),
                '<source url="%s">RSS 头条 · 折行探针</source>' % esc(self_url),
                "</item>"]
    out.append("</channel></rss>")
    return "\n".join(out) + "\n"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    written = []

    # t1 ~ t3：同一句话，三种分隔符
    for i, (slug, name, sep, note) in enumerate(PROBES):
        self_url = "%s/%s.xml" % (BASE_URL, slug)
        rows = wrap_title(SAMPLE, LINE_W, LINES)
        item = {
            "title_xml": join_title_xml(rows, sep),
            "link": self_url,
            "pub": email.utils.format_datetime(NOW - timedelta(minutes=i)),
            "guid": "probe-%s" % slug,
        }
        xml = channel(name, note + "｜折 %d 行 × %d 全角" % (LINES, LINE_W), [item], self_url)
        path = os.path.join(OUT_DIR, slug + ".xml")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(xml)
        written.append((slug, name, rows))

    # t4：多条 item 分段，测云端是否轮播同一源的多条
    self_url = "%s/t4.xml" % BASE_URL
    rows = wrap_title(SAMPLE, LINE_W, LINES)
    items = [{
        "title_xml": esc(r),
        "link": "%s#%d" % (self_url, i),
        "pub": email.utils.format_datetime(NOW - timedelta(minutes=i)),
        "guid": "probe-t4-%d" % i,
    } for i, r in enumerate(rows)]
    xml = channel("④分段多条", "同一句话拆成 %d 条 item，看设备是否会依次轮播" % len(rows),
                  items, self_url)
    with open(os.path.join(OUT_DIR, "t4.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write(xml)
    written.append(("t4", "④分段多条", rows))

    for slug, name, rows in written:
        print("%-3s %-10s %d 行：%s" % (slug, name, len(rows), " | ".join(rows)))
    print("\n输出目录：%s" % OUT_DIR)


if __name__ == "__main__":
    main()
