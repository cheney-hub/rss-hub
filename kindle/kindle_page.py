#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 feed.xml 渲成一个自动刷新的网页看板，供 Kindle / 手机 / PC 浏览器直接打开。

用法：
    python3 kindle_page.py --feed /home/user/rss-hub/docs/feed.xml \\
                           --out-dir /home/user/kindle --limit 80

产出：<out-dir>/index.html。serve.py 用 --prefix= 托管这个目录时，浏览器访问
根路径就会命中它（serve.py 的 _target 把空路径兜成 index.html）。

和 kindle_screen.py 的分工：
    那边渲 PNG 给 eips 画图（只读同一份 feed.xml，一屏，不能交互、不能点链接）；
    这边渲网页给浏览器看（能滚动、能点原文、条目数量级大得多）。
    两者互不依赖，删掉任一个另一个照常工作 —— 所以这里没有 import kindle_screen：
    它依赖 Pillow 和 CJK 字体，绑上来会让「没装 Pillow 的机器出不了网页」。

为什么写法这么保守（目标浏览器是 Kindle Paperwhite 2 的实验性浏览器，WebKit 很老）：
- 刷新靠 <meta http-equiv="refresh"> 而不是 JS：这类浏览器的 JS 能力残缺，
  而 meta refresh 是浏览器原生行为，不需要 JS 就一定能生效。
- CSS 不用 flex / grid / CSS 变量 / rem：老 WebKit 认不全，认不全就整块塌掉。
  只用 CSS1 就有、且从没变过语义的那几个属性（margin / padding / border / color）。
- 不引外部字体、图片、CDN：设备可能只被允许访问这一个主机。

安全：feed 的内容来自外部站点，插进 HTML 前一律转义；link 只允许 http/https，
javascript: 之类的伪协议直接丢掉 —— 这个页面是要放到公网上的。
"""
import argparse
import email.utils
import html
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone

TAG_RE = re.compile(r"<[^>]+>")
# 展示时区统一东八区：和 kindle_screen.LOCAL_TZ 是同一套口径，两屏显示的时间要一致。
LOCAL_TZ = timezone(timedelta(hours=8))
HHMM_RE = re.compile(r"(\d{2}):(\d{2}):\d{2}")
# 兜底路径用：日期串不合 RFC 822 时只求把 HH:MM 抠出来，秒可有可无
HHMM_LOOSE = re.compile(r"(\d{2}):(\d{2})")
OK_SCHEME = ("http://", "https://")


def clean_text(s):
    """去 HTML 标签/实体、压平空白。和 kindle_screen.clean_text 同逻辑，

    这里复制一份而不是引用：那边带 Pillow 与字体的依赖链，引过来就要装 Pillow
    才能生成一个纯文本的网页。算法变了要同步改这两处。
    """
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def to_hhmm(pub):
    """pubDate → 东八区的 HH:MM；解析不动就退回原串里的 HH:MM，再不行给空串。

    和 kindle_screen.to_hhmm 同逻辑，这里复制一份而不是引用 —— 同样是为了不把
    Pillow 依赖链拽进这个纯文本脚本。算法变了要同步改两处。

    归一的原因：feed 里各源的后缀混着（+0800 / +0000 / GMT / +0900），直接抄原串会
    把格林威治时间当北京时间显示，一排 12:45 中间冒出个 04:45。不带后缀按 +8 解释，
    与 build_feed 的 SOURCE_TZ_OFFSET 一致。
    """
    raw = (pub or "").strip()
    if not raw:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(LOCAL_TZ).strftime("%H:%M")
    m = HHMM_LOOSE.search(raw)        # 兜底用宽松版：秒可能没有，比如 31/10/2026 13:07
    return "%s:%s" % (m.group(1), m.group(2)) if m else ""


def parse_items(path, limit):
    """返回 [(title, desc, hhmm, source, link)]；失败返回空列表。

    比 kindle_screen.parse_feed 多一个 link：网页能点开原文，PNG 不能，
    这是两边唯一的差异（也正是两边分开解析的原因）。
    """
    out = []
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        sys.stderr.write("feed parse failed: %s\n" % exc)
        return out
    for it in root.iter("item"):
        def g(tag):
            """取 tag 的文本并清洗；字段缺失时给空串，不让缺字段的源断掉整份 feed。"""
            node = it.find(tag)
            return clean_text(node.text) if node is not None and node.text else ""

        title = g("title")
        if not title:
            continue
        if len(out) >= limit:
            break
        link = g("link")
        # 只放行 http/https：feed 是外部数据源，里面塞一个 javascript: 或 data:
        # 的链接会在这个页面上变成可点击的东西。
        if not link.lower().startswith(OK_SCHEME):
            link = ""
        hhmm = to_hhmm(g("pubDate"))
        out.append((title, g("description"), hhmm, g("source") or g("category"), link))
    return out


CSS = """
html { background: #fff; }
body {
  margin: 0; padding: 10px 12px 24px 12px;
  background: #fff; color: #000;
  font-family: sans-serif; font-size: 18px; line-height: 1.45;
}
h1 {
  margin: 0 0 2px 0; padding: 0 0 6px 0;
  font-size: 22px; font-weight: bold;
  border-bottom: 2px solid #000;
}
.sub { margin: 0 0 12px 0; padding: 4px 0 0 0; font-size: 14px; color: #666; }
ol { margin: 0; padding: 0; list-style: none; }
li { margin: 0; padding: 10px 0 10px 0; border-bottom: 1px solid #999; }
.t { margin: 0; font-size: 19px; font-weight: bold; line-height: 1.35; }
.t a { color: #000; text-decoration: none; border-bottom: 1px solid #000; }
.t a.nolink { border-bottom: 0; }
.d { margin: 4px 0 0 0; font-size: 16px; }
.m { margin: 5px 0 0 0; font-size: 14px; color: #666; }
.foot {
  margin: 16px 0 0 0; padding: 8px 0 0 0;
  font-size: 14px; color: #666; border-top: 2px solid #000;
}
.foot a { color: #000; }
"""


def build_page(items, built_at, desc_limit, refresh):
    """拼出完整 HTML 字符串。所有插入的文本与属性值都在这里转义。"""
    def esc(s):
        """HTML 转义 + 保留在属性值里也安全（两边都用双引号，所以 quote=True）。"""
        return html.escape(s or "", quote=True)

    head = [
        '<!DOCTYPE html>',
        '<html>',
        '<head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="refresh" content="%d">' % refresh,
        '<title>RSS 头条看板</title>',
        '<style>%s</style>' % CSS,
        '</head>',
        '<body>',
        '<h1>RSS 头条看板</h1>',
        '<p class="sub">更新 %s · 共 %d 条 · 每 %d 秒自动刷新</p>' % (
            esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(built_at))),
            len(items), refresh),
        '<ol>',
    ]
    body = []
    for title, desc, hhmm, src, link in items:
        t = '<a href="%s">%s</a>' % (esc(link), esc(title)) if link else \
            '<a class="nolink">%s</a>' % esc(title)
        parts = ['<li>', '<p class="t">%s</p>' % t]
        if desc:
            d = desc[:desc_limit]
            if len(desc) > desc_limit:
                d += "…"
            parts.append('<p class="d">%s</p>' % esc(d))
        meta = " · ".join(x for x in (hhmm, src) if x)
        if meta:
            parts.append('<p class="m">%s</p>' % esc(meta))
        parts.append('</li>')
        body.append("".join(parts))
    # 「看板图」指向同一目录下的默认图，文件名要和 kindle_screen.py 的 BOARD_PNG 一致。
    # 不直接 import kindle_screen 取常量：那边依赖 Pillow，而本脚本刻意保持零依赖。
    tail = [
        '</ol>',
        '<p class="foot">'
        '<a href="./">手动刷新</a> · '
        '<a href="./rss-pw2.png">看板图</a>'
        '</p>',
        '</body>',
        '</html>',
    ]
    return "\n".join(head + body + tail)


def render(feed_path, out_dir, limit, refresh, desc_limit):
    """读 feed.xml → 写 index.html；池子空返回 1 且不碰旧文件。

    和 kindle_screen.render 一样走「写 .tmp + os.replace」：浏览器可能正停在旧页上，
    原子替换保证它要么读到完整旧页要么读到完整新页，不会得到半截 HTML。
    """
    items = parse_items(feed_path, limit)
    if not items:
        sys.stderr.write("no items, keep old html untouched\n")
        return 1
    try:
        built_at = os.path.getmtime(feed_path)
    except OSError:
        built_at = time.time()
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "index.html")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(build_page(items, built_at, desc_limit, refresh))
    os.replace(tmp, p)
    print("rendered %d items -> %s" % (len(items), p), flush=True)
    return 0


def main():
    """命令行入口；退出码透传 render（0 = 出了新页，1 = 池子空、旧页未动）。"""
    ap = argparse.ArgumentParser(description="feed.xml -> 自动刷新的网页看板")
    ap.add_argument("--feed", default="/home/user/rss-hub/docs/feed.xml")
    ap.add_argument("--out-dir", default="/home/user/kindle")
    ap.add_argument("--limit", type=int, default=80,
                    help="最多渲染多少条（网页能滚动，可以比 PNG 那一屏多得多）")
    ap.add_argument("--refresh", type=int, default=300,
                    help="meta refresh 秒数，应不小于 feed 的重建间隔")
    ap.add_argument("--desc-limit", type=int, default=120,
                    help="摘要最多保留多少字")
    args = ap.parse_args()
    if not os.path.isfile(args.feed):
        sys.exit("feed 不存在: %s" % args.feed)
    return render(args.feed, args.out_dir, args.limit, args.refresh, args.desc_limit)


if __name__ == "__main__":
    sys.exit(main())
