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

# 灰阶：e-ink 只有 16 级灰，浅灰在屏上会糊。次要文字（时间戳、页眉时间、页脚）取 140，
# 和常见的 #888(136) 是同一档；再往浅走一档在 Kindle 上就发虚，再往深又会和正文抢注意力。
WHITE = 255
BLACK = 0
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
_fallback_warned = False


def load_font(size, bold=False):
    """按候选顺序找一个真有汉字、能用的字体；找不到就退成位图默认字体。

    两层探测：
    - 外层遍历 pick_font_paths() 的文件（.ttc 里装了多个 family，同一文件能出多套字）；
    - 内层 index 0..7 逐个试，用 _usable() 确认「中」真的能画出来。
    内层捕获到异常时用 break 而不是 continue：同一个文件里 index=1 报错就说明这个
    文件不吃 index（.ttf 只认 0），换下一个文件比继续试 index 更快，也避免把
    OSError 重试 7 次。找不到任何 CJK 字体时退成 load_default()（西文可读、汉字是
    豆腐块），并且往 stderr 叫一声——否则整屏豆腐块而日志里一句话都没有。
    只叫一次：一屏要用 4 套字号 × 若干尺寸，每套都叫一遍会把 journal 刷满。
    """
    global _fallback_warned
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
    if not _fallback_warned:
        _fallback_warned = True
        sys.stderr.write("警告：没有可用的 CJK 字体，退成位图默认字体（汉字会变豆腐块）\n")
    _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def atomize(text):
    """切成不可拆的最小单位：英文/数字单词整体，汉字逐字。"""
    return SPLIT_KEEP.findall(text)


def _cut_to_width(draw, text, font, max_w):
    """把放不下的文字按字符切到「恰好放得下」，返回 (前段, 剩下的)。

    二分找最长能放下的前缀，避免逐字量（一个 140 字的 URL 逐字量要 140 次 textlength）。
    至少返回 1 个字符：版面窄到连一个字都放不下时也要往前走，否则调用方会死循环。
    """
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid], font=font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], text[lo:]


def wrap(draw, text, font, max_w, max_lines):
    """贪心折行；行首不出现收尾标点（宁可让上一行略微溢出）。

    单个原子比一整行还宽时（长 URL、没有空格的超长英文词）做字符级硬切：原子是
    折行的最小单位，切不开就只能整段画出去——实测 140 个 a 在 1024px 宽的版面上
    量出 1960px，直接糊到画布外面。切完剩下的部分继续按原子往下排。
    """
    lines, cur = [], ""
    for atom in atomize(text):
        if atom.isspace():
            if cur and draw.textlength(cur, font=font) <= max_w:
                cur += " "
            continue
        while atom:
            if draw.textlength(cur + atom, font=font) <= max_w:
                cur += atom
                break
            if cur:                      # 这一行到此为止；同一个原子在空行上重新量
                lines.append(cur.rstrip())
                cur = ""
                if len(lines) >= max_lines:
                    atom = ""
                    break
                continue
            head, atom = _cut_to_width(draw, atom, font, max_w)   # 原子自己就超宽
            lines.append(head)
            if len(lines) >= max_lines:
                cur = atom               # 剩下那截留给下面的「…」逻辑
                atom = ""
                break
    if len(lines) < max_lines and cur.strip():
        lines.append(cur.strip())
    # 上面的循环里两处 `lines.append()` 都在**追加之后**才数行数，所以放不下时最多可能
    # 多出一行（max_lines + 1）。这里补一道硬截断：`max_lines` 对调用方是排版契约
    # （_draw_list 按 len(lines) 预留高度），多给一行会把页脚或下一条挤掉。
    # 截断后下面的分支会因为「末行不等于原文末尾」自动补上「…」。
    if len(lines) > max_lines:
        lines = lines[:max_lines]
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
        if not "".join(atomize(text)).endswith(last):
            # 用「…」替换末字可能反而更宽（末字是半角时），所以要退到量出来放得下为止
            while last and draw.textlength(last + "…", font=font) > max_w:
                last = last[:-1]
            fixed[-1] = last + "…"
    return fixed


def parse_feed(path):
    """返回 [(title, summary, hhmm, source)]，取不到就退成空列表而不是崩。

    hhmm 是**直接从 pubDate 原串里正则抓的 HH:MM，不转时区**：feed 里各源的时间
    带着各自的时区后缀（+0800 / +0000 / GMT 混着来），所以时间列在混源时可能
    「非单调」（00:03 后面跟着 16:02）。要改成按 SOURCE_TZ 归一得先动 render 侧
    的 date_raw 口径，别只在这里加转换。

    title 开头若带「【源名】」会剥掉：当前 render 不写这个前缀，留着是兼容更早的
    产物与个别仍然自带方括号标题的源。
    """
    items = []
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:
        sys.stderr.write("feed parse failed: %s\n" % exc)
        return items
    for it in root.iter("item"):
        def g(tag):
            """取 <tag> 的文本并清洗（去标签、解实体、压空白）；缺字段给空串。"""
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
    """按缩放系数 s 造出四套字：head 页眉标题 / meta 时间戳 / list 条目标题 / foot 页脚。

    四个角色各自对应版式里的一块，字号写在各处调用点上（_draw_header / _draw_list /
    draw_board）；这里只管一次取齐，顺带吃 load_font 的缓存。
    """
    return {
        "head": load_font(_fs(s, 22), bold=True),
        "meta": load_font(_fs(s, 18)),
        "list": load_font(_fs(s, 26)),
        "foot": load_font(_fs(s, 17)),
    }


def _draw_header(d, x0, x1, y, s, fonts, built_at):
    """画页眉（左「RSS 头条」+ 右更新时间 + 一条分隔线），返回分隔线下方可用的 y。

    返回值就是列表第一行的起点，调用方直接接着画，不用自己再算间距。
    built_at 是 feed.xml 的 mtime，所以右上角那个时间是「数据时间」而不是「渲染时间」。
    """
    d.text((x0, y), "RSS 头条", font=fonts["head"], fill=BLACK)
    stamp = time.strftime("%m-%d %H:%M", time.localtime(built_at))
    tw = d.textlength(stamp, font=fonts["meta"])
    d.text((x1 - tw, y + _fs(s, 4)), stamp, font=fonts["meta"], fill=GRAY_META)
    y += int(_fs(s, 22) * 1.5)
    d.line((x0, y, x1, y), fill=GRAY_META, width=max(1, int(s * 2)))
    return y + int(_fs(s, 22) * 0.55)


def _fit_title(d, font, title, avail):
    """标题排版：最多 2 行。两种情况下退成「一行 + 省略号」：

    1. 任一行超出可用宽度 —— 行首标点回挂会把两行并回一行并顶出边界
       （标题只差一个字放不下时就会这样，第二行只剩个「。」）。
    2. 第二行只剩两三个字（孤字行），版面很难看。

    宁可截掉一个字，也不要出界或出现孤字行。
    """
    lines = wrap(d, title, font, avail, 2)
    wide = [ln for ln in lines if d.textlength(ln, font=font) > avail]
    if wide:
        return wrap(d, title, font, avail, 1)
    if len(lines) == 2 and d.textlength(lines[1], font=font) < avail * 0.3:
        return wrap(d, title, font, avail, 1)
    return lines


def _draw_list(d, x0, y, s, fonts, items, content_w, bottom):
    """条目列表：时间戳 + 标题。所有条目同一规格，第一条不放大。

    行高不固定（1 行还是 2 行取决于标题长度），所以先算好这一条需要多高，
    塞不下就整条不画，避免出现半截行。
    """
    lh = int(_fs(s, 26) * 1.34)
    # 时间列宽取「2.6em」与「最宽时间串实测宽」的较大者：字号随短边缩放、字体随机器
    # 不同（Noto / 文泉驿 / Arphic），写死 em 数在换字体时可能和标题贴在一起。
    time_w = max(int(_fs(s, 26) * 2.6), int(d.textlength("88:88", font=fonts["meta"])))
    gap = int(_fs(s, 26) * 0.45)
    for title, _desc, hhmm, _src in items:
        lines = _fit_title(d, fonts["list"], title, content_w - time_w)
        need = lh * len(lines) + gap
        if y + need > bottom:
            break
        if hhmm:
            d.text((x0, y), hhmm, font=fonts["meta"], fill=GRAY_META)
        for i, ln in enumerate(lines):
            d.text((x0 + time_w, y + i * lh), ln, font=fonts["list"], fill=BLACK)
        y += need
    return y


def draw_board(w, h, items, built_at):
    """排版：页眉 / 条目列表 / 页脚。所有条目同一规格，横竖屏通用。"""
    img = Image.new("L", (w, h), WHITE)
    d = ImageDraw.Draw(img)
    ref = float(min(w, h))          # 以短边为基准缩放，横竖屏都能用
    s = ref / 758.0
    fonts = _fonts(s)

    m = int(round(ref * 0.045))
    x0, x1 = m, w - m
    y = _draw_header(d, x0, x1, m, s, fonts, built_at)
    foot_h = int(_fs(s, 17) * 1.8)
    # 只画一遍：这里曾经连着两行 _draw_list（第二行传 items[1:]），两次都从同一个 y 起笔、
    # 返回值也不接，于是除最后一行外每行都叠印双字，整版不可读（27f28f1 删 _draw_lead 时漏删）。
    # 所有条目统一规格之后不存在「头条区 + 列表区」两段，别再拆成两次调用。
    _draw_list(d, x0, y, s, fonts, items, x1 - x0, h - m - foot_h)
    d.text((x0, h - m - int(_fs(s, 17) * 1.1)),
           "moreaix.com · 共 %d 条" % len(items), font=fonts["foot"], fill=GRAY_META)
    return img


def render(feed_path, out_dir, sizes):
    """把 feed 渲成每种尺寸一张 PNG，并把第一张复制成 kindle.png；返回进程退出码。

    built_at 取 feed.xml 的 mtime，所以页眉写的是「这份数据是什么时候抓的」，
    不是「这张图是什么时候画的」——两者差几分钟时前者更有意义。
    每张图都先写 .tmp 再 os.replace：Kindle 那头正在 wget 时也不会读到半张图
    （读到半张 PNG 会直接画失败，屏幕停在上一版）。
    池子空时返回 1 且不碰旧图：宁可屏上停一条旧的，也别把画面刷成空白。
    """
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
    """把 "1024x758,800x600" 解析成 [(w, h), ...]；格式不对直接 sys.exit。

    故意用 sys.exit 而不是抛异常：尺寸是 systemd 单元里写死的参数，写错就该在启动
    那一下失败并让 journal 记下原因，而不是带着错参数跑出一堆怪图。
    """
    out = []
    for part in text.split(","):
        part = part.strip().lower()
        m = re.match(r"^(\d+)\s*x\s*(\d+)$", part)
        if not m:
            sys.exit("尺寸格式不对: %r（应形如 1024x758）" % part)
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def main():
    """命令行入口：--feed / --out-dir / --sizes 三个参数，默认值就是 Pi 上的部署路径。

    退出码透传 render 的（0 = 出了新图，1 = 池子空、旧图未动），
    systemd 据此在 journal 里标 failed。
    """
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
