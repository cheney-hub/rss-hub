#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 feed.xml 渲染成 Kindle 能直接 eips -g 显示的灰度 PNG。

用法：
    python3 kindle_screen.py --feed /home/user/rss-hub/docs/feed.xml \
                             --out-dir /home/user/kindle --sizes 1024x758

产出：每个尺寸一个 kindle-<W>x<H>.png，另外把第一个尺寸复制成 kindle.png（默认图）。
当前目标机是 Kindle Paperwhite 2（第 6 代，序列号前缀 90D4），固定 1024x758。
图像为 8bit 灰度（"L" 模式），白底黑字，e-ink 直出无需再转。

为什么不做成网页而要做 PNG：Kindle 原生不支持常驻网页，越狱后只有 eips/FBInk 能往
屏幕直接画图，它们吃的是图片文件。所以「实时更新」= 服务端定期重画 PNG + 客户端
定时 wget + eips -g。

依赖：Pillow（树莓派已装 9.4.0）+ 任一 CJK 字体（Noto Sans CJK SC 已装）。
"""
import argparse
import glob
import html
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

# 灰阶：e-ink 只有 16 级灰，浅灰在屏上会糊，所以正文用较深的灰而不是常见的 #888
WHITE = 255
BLACK = 0
GRAY_BODY = 70
GRAY_META = 140

TAG_RE = re.compile(r"<[^>]+>")
NO_LINE_START = "、。，；：）】》」』！？%!?,.;:)]}"
SPLIT_KEEP = re.compile(r"[A-Za-z0-9]+(?:[._%+-][A-Za-z0-9]+)*|\s+|.")


def clean_text(s):
    """去掉 HTML 标签/实体，压平空白。feed 的 description 是 CDATA 的 HTML。"""
    if not s:
        return ""
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def pick_font_paths(bold):
    """按优先级列出 CJK 字体候选（.ttc 里装了多个 family，靠 index 试）。"""
    pats = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-*.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-*.ttc",
        "/usr/share/fonts/truetype/wqy/*.ttc",
        "/usr/share/fonts/truetype/wqy/*.ttf",
        "/usr/share/fonts/truetype/arphic/*.ttf",
        "/usr/share/fonts/**/DroidSansFallback*.ttf",
    ]
    out = []
    for p in pats:
        out.extend(sorted(glob.glob(p, recursive=True)))
    # 粗体优先放前面
    if bold:
        out.sort(key=lambda x: (0 if "Bold" in x else 1, x))
    return out


def _usable(font, size):
    """字体里真有汉字吗？缺字时 getbbox 会返回空或极窄。"""
    try:
        box = font.getbbox("中")
    except Exception:
        return False
    return bool(box) and (box[2] - box[0]) >= size * 0.5


_font_cache = {}


def load_font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for path in pick_font_paths(bold):
        for idx in range(0, 8):
            try:
                f = ImageFont.truetype(path, size, index=idx)
            except Exception:
                break
            if _usable(f, size):
                _font_cache[key] = f
                return f
    _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def atomize(text):
    """切成不可拆的最小单位：英文/数字单词整体，汉字逐字。"""
    return SPLIT_KEEP.findall(text)


def wrap(draw, text, font, max_w, max_lines):
    """贪心折行；行首不出现收尾标点（宁可让上一行略微溢出）。"""
    lines, cur = [], ""
    for atom in atomize(text):
        if atom.isspace():
            if cur and draw.textlength(cur, font=font) <= max_w:
                cur += " "
            continue
        trial = cur + atom
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur.rstrip())
            cur = atom.lstrip()
            if len(lines) >= max_lines:
                break
        else:
            cur = trial
    if len(lines) < max_lines and cur.strip():
        lines.append(cur.strip())
    # 行首标点回挂
    fixed = []
    for ln in lines:
        if ln and ln[0] in NO_LINE_START and fixed:
            fixed[-1] = fixed[-1] + ln[0]
            ln = ln[1:]
        if ln:
            fixed.append(ln)
    if len(fixed) == max_lines and fixed:
        last = fixed[-1]
        full = "".join(atomize(text))
        if not full.endswith(last):
            fixed[-1] = last[:-1] + "…" if len(last) > 1 else last
    return fixed


def parse_feed(path):
    """返回 [(title, summary, hhmm, source)]，取不到就退成空列表而不是崩。"""
    items = []
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        sys.stderr.write("feed parse failed: %s\n" % exc)
        return items
    for it in root.iter("item"):
        def g(tag):
            node = it.find(tag)
            clean = clean_text(node.text) if node is not None and node.text else ""
            return clean
        title = g("title")
        if not title:
            continue
        desc = g("description")
        pub = g("pubDate")
        hhmm = ""
        m = re.search(r"(\d{2}):(\d{2}):\d{2}", pub)
        if m:
            hhmm = "%s:%s" % (m.group(1), m.group(2))
        src = g("source") or g("category")
        if src and title.startswith("【%s】" % src):
            title = title[len(src) + 2:]
        items.append((title, desc, hhmm, src))
    return items


def _fs(s, base):
    """字号：所有尺寸都按短边比例缩放，横竖屏共用一套排版。"""
    return max(10, int(round(base * s)))


def _fonts(s):
    return {
        "head": load_font(_fs(s, 22), bold=True),
        "meta": load_font(_fs(s, 18)),
        "lead": load_font(_fs(s, 40), bold=True),
        "body": load_font(_fs(s, 24)),
        "list": load_font(_fs(s, 26)),
        "foot": load_font(_fs(s, 17)),
    }


def _rule(d, x0, x1, y, s):
    d.line((x0, y, x1, y), fill=GRAY_META, width=max(1, int(s * 2)))


def _draw_header(d, x0, x1, y, s, fonts, built_at):
    d.text((x0, y), "RSS 头条", font=fonts["head"], fill=BLACK)
    stamp = time.strftime("%m-%d %H:%M", time.localtime(built_at))
    tw = d.textlength(stamp, font=fonts["meta"])
    d.text((x1 - tw, y + _fs(s, 4)), stamp, font=fonts["meta"], fill=GRAY_META)
    y += int(_fs(s, 22) * 1.5)
    _rule(d, x0, x1, y, s)
    return y + int(_fs(s, 22) * 0.55)


def _draw_lead(d, x0, x1, y, s, fonts, item, content_w):
    """头条：大字标题（最多 3 行）+ 摘要（最多 3 行），末尾一条分隔线。"""
    title, desc = item[0], item[1]
    lh_lead = int(_fs(s, 40) * 1.28)
    for ln in wrap(d, title, fonts["lead"], content_w, 3):
        d.text((x0, y), ln, font=fonts["lead"], fill=BLACK)
        y += lh_lead
    y += int(_fs(s, 24) * 0.35)
    if desc:
        lh_body = int(_fs(s, 24) * 1.35)
        for ln in wrap(d, desc, fonts["body"], content_w, 3):
            d.text((x0, y), ln, font=fonts["body"], fill=GRAY_BODY)
            y += lh_body
    y += int(_fs(s, 24) * 0.5)
    _rule(d, x0, x1, y, s)
    return y + int(_fs(s, 26) * 0.5)


def _draw_list(d, x0, y, s, fonts, items, content_w, bottom):
    """列表：时间戳 + 单行标题，按剩余高度塞，塞不下就停。"""
    lh = int(_fs(s, 26) * 1.32)
    time_w = int(_fs(s, 26) * 2.6)
    gap = int(_fs(s, 26) * 0.18)
    for title, _desc, hhmm, _src in items:
        if y + lh + gap > bottom:
            break
        if hhmm:
            d.text((x0, y), hhmm, font=fonts["meta"], fill=GRAY_META)
        lines = wrap(d, title, fonts["list"], content_w - time_w, 1)
        d.text((x0 + time_w, y), lines[0] if lines else "", font=fonts["list"], fill=BLACK)
        y += lh + gap
    return y


def draw_board(w, h, items, built_at):
    """排版：页眉 / 头条大字（含摘要）/ 分隔线 / 列表 / 页脚。横竖屏通用。"""
    img = Image.new("L", (w, h), WHITE)
    d = ImageDraw.Draw(img)
    ref = float(min(w, h))          # 以短边为基准缩放，横竖屏都能用
    s = ref / 758.0
    fonts = _fonts(s)

    m = int(round(ref * 0.045))
    x0, x1 = m, w - m
    y = _draw_header(d, x0, x1, m, s, fonts, built_at)
    if items:
        y = _draw_lead(d, x0, x1, y, s, fonts, items[0], x1 - x0)
    foot_h = int(_fs(s, 17) * 1.8)
    _draw_list(d, x0, y, s, fonts, items[1:], x1 - x0, h - m - foot_h)
    d.text((x0, h - m - int(_fs(s, 17) * 1.1)),
           "moreaix.com · 共 %d 条" % len(items), font=fonts["foot"], fill=GRAY_META)
    return img


def render(feed_path, out_dir, sizes):
    items = parse_feed(feed_path)
    if not items:
        sys.stderr.write("no items, keep old png untouched\n")
        return 1
    try:
        built_at = os.path.getmtime(feed_path)
    except OSError:
        built_at = time.time()
    os.makedirs(out_dir, exist_ok=True)
    first = None
    for (w, h) in sizes:
        img = draw_board(w, h, items, built_at)
        p = os.path.join(out_dir, "kindle-%dx%d.png" % (w, h))
        tmp = p + ".tmp"
        img.save(tmp, "PNG", optimize=True)
        os.replace(tmp, p)          # 原子替换，客户端不会读到半张图
        if first is None:
            first = p
    if first:
        dst = os.path.join(out_dir, "kindle.png")
        shutil.copyfile(first, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
    print("rendered %d items -> %s (%s)" % (
        len(items), out_dir, ", ".join("%dx%d" % t for t in sizes)), flush=True)
    return 0


def parse_sizes(text):
    out = []
    for part in text.split(","):
        part = part.strip().lower()
        m = re.match(r"^(\d+)\s*x\s*(\d+)$", part)
        if not m:
            sys.exit("尺寸格式不对: %r（应形如 1024x758）" % part)
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def main():
    ap = argparse.ArgumentParser(description="feed.xml -> Kindle 灰度 PNG")
    ap.add_argument("--feed", default="/home/user/rss-hub/docs/feed.xml")
    ap.add_argument("--out-dir", default="/home/user/kindle")
    ap.add_argument("--sizes", default="1024x758",
                    help="逗号分隔的 WxH 列表，第一个同时存为 kindle.png")
    args = ap.parse_args()
    if not os.path.isfile(args.feed):
        sys.exit("feed 不存在: %s" % args.feed)
    return render(args.feed, args.out_dir, parse_sizes(args.sizes))


if __name__ == "__main__":
    sys.exit(main())
