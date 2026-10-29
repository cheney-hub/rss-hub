#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染层：一条资讯 → 屏上能显示的一段文字 → 一份合法 RSS。

这里全是纯函数：不联网、不写文件，除三个正文长度参数外不读任何外部状态，
所以 tests/test_core.py 能把它们直接拉进内存里跑，不用打桩。
`tests/test_golden.py` 另外拿固定输入跟 tests/golden.txt 逐字节比对，
任何「顺手改了排版」的动作都会在那里现形。

和 build_feed.py 的分工：
  * render.py     显示规则 —— 纯文本化、宽度折算、标题切分、正文合成、去重键、RSS 生成
  * build_feed.py 抓取编排 —— 拉源、解析、失败回退、轮播游标、写文件、主循环

为什么拆：原先 776 行的单文件里，排版规则和 urllib 重试挤在一起，
改一个「13 全角」的常量要在一堆网络代码里翻。分开后两边都能独立读、独立测。

版本：见 version.py（唯一真源）。本文件只消费，不定义。
"""

import os
import re
import html
import hashlib
import unicodedata
import email.utils
from datetime import datetime, timezone

from version import __version__

# 屏幕参数走环境变量，其余显示规则都是常量。
LINE_WIDTH = float(os.environ.get("SCREEN_LINE_WIDTH", "13"))
BODY_MAX = max(0.0, float(os.environ.get("SCREEN_BODY_MAX", "90")))
# 完整版（给电脑 / 手机阅读器）的正文上限，和屏宽无关。
# 0 = 不限（完整版全文照发，用户要能在阅读器里读到全文；1.3.0 起默认 0）；
# 正数才是「防止单条摘要把整份文件撑大」的保险丝。
FEED_BODY_MAX = max(0.0, float(os.environ.get("FEED_BODY_MAX", "0")))

# ---------------- 文本清洗 ----------------
# RSS 里的 description 常带 HTML 标签、命名实体、连续空白。这里统一洗成
# 「纯文本 + 单空格」，后面所有宽度计算和截断才有意义。
ENTITY_FIX = {
    "&nbsp;": " ", "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
    "&ldquo;": "“", "&rdquo;": "”", "&lsquo;": "‘", "&rsquo;": "’",
    "&middot;": "·", "&copy;": "©", "&reg;": "®", "&trade;": "™",
    "&bull;": "•", "&deg;": "°", "&permil;": "‰", "&times;": "×",
}


def clean_text(s):
    if not s:
        return ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------- 去重 ----------------
# 快讯电头：「财联社10月29日电，」「金十数据10月29日讯，」这类前缀白占屏宽，
# 来源已经由 <source> 显示，屏上只留真正的新闻内容。
_TAG = r"(?:【[^】]{1,6}】\s*)?"
_RE_DATELINE = re.compile(
    "^" + _TAG +
    r"(?:财联社|金十数据|格隆汇|智通财经|华尔街见闻|新浪财经|新华财经|第一财经|"
    r"每日经济新闻|界面新闻|澎湃新闻|证券时报|中国证券报)\s*"
    r"(?:\d{1,2}月\d{1,2}日)?\s*(?:电|讯|消息|报道)?\s*[，,:：]\s*")
_RE_DATELINE2 = re.compile(
    "^" + _TAG + r"[\u4e00-\u9fa5]{0,10}\d{1,2}月\d{1,2}日\s*(?:电|讯)\s*[，,:：]?\s*")


def strip_dateline(t):
    """去掉标题开头的电头，留下一句真正的新闻。"""
    if not t:
        return t
    for _ in range(2):                       # 「【独家】财联社10月29日电，」要剥两层
        t2 = _RE_DATELINE.sub("", t) or t
        t2 = _RE_DATELINE2.sub("", t2) or t2
        if t2 == t:
            break
        t = t2
    return t


def norm_title(t):
    t = strip_dateline(t or "")
    t = re.sub(r"[「」【】\[\]（）()“”\"'‘’：:，,。.！!？?、\s|·\-—–…~]", "", t)
    return re.sub(r"[a-zA-Z0-9]", "", t)[:20]


def dedupe(items):
    seen_link, seen_title, out = set(), set(), []
    dropped = 0
    for it in items:
        lk = re.sub(r"[#?].*$", "", it.get("link") or "")
        tt = norm_title(it.get("title"))
        if lk and lk in seen_link:
            dropped += 1
            continue
        if len(tt) >= 8 and tt in seen_title:
            dropped += 1
            continue
        if lk:
            seen_link.add(lk)
        if len(tt) >= 8:
            seen_title.add(tt)
        out.append(it)
    return out, dropped


# ---------------- 设备版（Dot Quote/0） ----------------
# 显示链路：App 把 RSS 地址加进「循环内容」→ Dot 云端抓取解析 → 云端模板渲染成位图 →
# 设备按刷新间隔拉图显示。设备端（ESP32-C3，400KB 内存）不解析 XML，显示几条、字号多大
# 都由云端模板决定，订阅方控制不了。
#
# 实测（2026-10-29，296×152 屏，用户真机截图）：
#   * RSS 卡片标题 = 一行大字号，13 个全角字就满宽（「卡特彼勒第三季度电力与能源」正好顶格），
#     之后的文字被云端直接切掉；
#   * 标题里塞换行符（&#10; / <br> / U+2028）云端一律不认，折行方案在真机上只显示第一行。
# 所以宽度抢不回来，改往下要空间：标题只放屏上显示得下的那 13 个字，
# 余下的原文接进 <description>，让云端的正文区域接着排版。
#
# 这份设备版只做云端渲染照顾不到的四件事：
#   1. 纯文本 —— 摘掉 emoji、箭头、几何图形、零宽字符（黑白墨水屏上它们是豆腐块或占位）；
#   2. 标题截到屏宽 —— 不让云端从字中间硬切，切点由我们定；
#   3. 余文入正文 —— 标题装不下的原文写进 description，字不丢；
#   4. 字段极简 —— guid 用短哈希、去掉 atom:link/generator/ttl，云端解析更轻。
# 全程只走 RSS 只读通路，不调用任何需要密钥的 API。
#
# 标题行收尾用这些标点更自然（只在需要回退时参考）。
BREAK_CHARS = "，,、；;｜|—）)！!？?·】」』 "
# 正文开头要剥掉的连接标点（正文另起一段，行首标点是中文排版大忌）。
# 只放中文分隔符和句末标点：半角「.」「:」可能属于 20.15 / 12:30 这类数字，剥掉就把数字弄残了。
HEAD_STRIP = "，,、；;。！!？?"

# 黑白墨水屏上这些字符要么显示成方块要么是透明占位，还白占宽度，直接摘掉。
_RE_INVISIBLE = re.compile(
    "[\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff"          # 零宽 / 方向控制 / BOM
    "\ufe0e\ufe0f\u20e3"                                      # 变体选择符、键帽
    "\U0001f000-\U0001faff"                                   # emoji、符号、交通、旗帜
    "\U000e0020-\U000e007f"                                   # 标签字符
    "\u2190-\u21ff\u2600-\u27bf\u2b00-\u2bff\u25a0-\u25ff"     # 箭头、杂项符号、几何图形
    "]+")


def plain_text(s):
    """把小屏用不到的字符摘干净，只留能真正印在墨水屏上的文字。"""
    s = _RE_INVISIBLE.sub("", s or "")
    s = s.replace("\u3000", " ")                      # 全角空格 → 半角
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip(" 　·|、,，")


def char_width(ch):
    """全角算 1、半角算 0.5。CJK 环境里 “…” “—” 这类歧义宽度按全角算。"""
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.5


def text_width(s):
    return sum(char_width(c) for c in s or "")


def _word_char(ch):
    """词内字符：半角字母 / 数字 / 小数点和词内连符号。这些拼成的片段不能从中间切开。"""
    return ch.isascii() and (ch.isalnum() or ch in "._-")


# 数字后面紧跟这些字时，数字属于「数量」，不能留在上一行末尾
# （「…超3」+「亿条」、「…亏损2.59亿」+「元」在屏上都读起来像断了）。
# 只列单位 / 量词，不含普通名词字。
_UNIT_AFTER_NUMBER = "亿万千百元%％个条人年月日号吨台辆架艘份项家只张片次倍成点分秒克斤升米里度"
_NUMBER_CHARS = set("0123456789.．")


def _avoid_orphan_number(t, cut, line_w):
    """切点落在「数量」中间时，退到这个数量前面。

    「…外泄信息超3|亿条」「…亏损2.59亿|元」这类切法，屏上第一行断在半个数量上，
    正文从「亿条」「元」开始，读起来是残句。退到数量之前，宁可少显示三四个字，
    换整句通顺；退得太狠（不足行宽一半）或数量正好在行首，就保持硬切。

    数量的判定：切点左侧连续的【数字 / 小数点 / 单位】里必须含数字，
    且切点右侧仍是单位或数字 —— 只是「…亿元」结尾（前面没有数字）不算。
    """
    if cut <= 0 or cut >= len(t):
        return cut
    right = t[cut] in _UNIT_AFTER_NUMBER or t[cut] in _NUMBER_CHARS
    if not right:
        return cut
    i = cut
    while i > 0 and (t[i - 1] in _UNIT_AFTER_NUMBER or t[i - 1] in _NUMBER_CHARS):
        i -= 1
    if i == cut or not any(c in _NUMBER_CHARS for c in t[i:cut]):
        return cut
    while i > 0 and t[i - 1] in "+-−":        # 减号跟数字一起走，别留在行尾
        i -= 1
    return i if text_width(t[:i]) >= line_w * 0.5 else cut


def _cut_index(s, max_w):
    """返回不超过 max_w 全角宽度的最大字符下标。"""
    acc = 0.0
    for i, ch in enumerate(s):
        w = char_width(ch)
        if acc + w > max_w:
            return i
        acc += w
    return len(s)


def split_title(title, line_w=None, body_max=None):
    """把标题切成「屏上标题行」+「续到正文的余下文字」。

    设备实测（296×152 屏）：一行大字号只放得下 line_w 个全角字，超出部分云端直接从
    右边切掉（「卡特彼勒第三季度电力与能源」正好顶格）；标题里的换行符它也不认，
    折行方案到真机上只会显示第一行。所以标题只管放得下的那一段，
    装不下的原文整段返回，由调用方写进 <description>，字一个不丢。

    返回 (head, tail)；标题本来就不超宽时 tail 为空串。
    """
    line_w = LINE_WIDTH if line_w is None else line_w
    body_max = BODY_MAX if body_max is None else body_max
    t = strip_dateline(plain_text(title))          # 先去掉「财联社10月29日电，」占的位置
    # 去掉 “标题 - 来源名” 这类尾巴（来源单独用 <source> 显示，重复占位）
    t = re.sub(r"\s*[-_|｜]\s*[^-_|｜]{1,8}$", "", t) or t
    if text_width(t) <= line_w:
        return t, ""

    cut = _cut_index(t, line_w)
    if cut <= 0:
        return t, ""
    # 切点落在半个词里就往左退到词首：「净亏损20|15亿元」退成「净亏损|2015亿元」，
    # 「signal patie|nce」退成「signal|patience」。代价是标题少显示几个字，
    # 换来正文从完整的词/数字开头；退得太狠（不足行宽 60%）就不退，硬切更好。
    if 0 < cut < len(t) and _word_char(t[cut - 1]) and _word_char(t[cut]):
        i = cut
        while i > 0 and _word_char(t[i - 1]):
            i -= 1
        if text_width(t[:i]) >= line_w * 0.6:
            cut = i
    cut = _avoid_orphan_number(t, cut, line_w)
    # 切点落在连接标点上（「万事达盘前上涨1.8%，」）时，标题别拖着逗号收尾，
    # 那是被切断的痕迹；余文本来就从下一句开始。
    head = t[:cut].rstrip(" 　").rstrip("，,、；;") or t[:cut].rstrip(" 　")
    # 正文不能以标点开头（「，停建124个猪场」）：那是被切断处的连接符，
    # 正文本来就另起一段，读起来不需要它。
    tail = t[cut:].lstrip(" 　" + HEAD_STRIP)
    if body_max and text_width(tail) > body_max:
        tc = _cut_index(tail, body_max - 1)         # 留 1 个位给省略号
        tail = tail[:tc].rstrip(BREAK_CHARS + "-—…") + "…"
    return head, tail


def compose_body(tail, summary, title="", body_max=None):
    """正文 = 标题装不下的余文 + 原 feed 里的摘要原文，装不下就截断。

    原 feed 的 <description> / <content:encoded> 里常有一段导语（财经快讯尤其多），
    以前全丢掉了；墨水屏正文区还能排四行，白扔可惜，接着标题往下排。
    """
    body_max = BODY_MAX if body_max is None else body_max
    parts = []
    head_text = plain_text(tail)
    if head_text:
        parts.append(head_text)

    s = plain_text(clean_text(summary or ""))
    s = re.sub(r"[ \t]+", " ", s).strip()
    if s:
        tt = plain_text(title)
        # 「【标题】正文」这种格式（金十、财联社常见）：方括号里是标题的复述，整段去掉
        m = re.match(r"^[【\[〔](.{2,80}?)[】\]〕]\s*", s)
        if m and tt and (m.group(1) == tt or m.group(1) in tt or tt.startswith(m.group(1))):
            s = s[m.end():]
        # 摘要把标题原样重复一遍（「标题 — 导语」），开头这段重复去掉
        if tt and s.startswith(tt):
            s = s[len(tt):].lstrip(" 　:：,，。、-—|｜")
        if s and s not in parts:
            parts.append(s)

    body = " ".join(parts)
    # body_max=0 = 不限（完整版默认，全文照发）；`if body_max` 恰好把 0 当假值跳过截断。
    if body_max and text_width(body) > body_max:
        bc = _cut_index(body, body_max - 1)         # 留 1 个位给省略号
        body = body[:bc].rstrip(BREAK_CHARS + "-—…") + "…"
    return body


# 条目的稳定身份：轮播游标和 RSS 的 <guid> 都用它，所以住在渲染层。
def item_guid(it):
    """条目的稳定身份：链接 + 源地址的短哈希，与 feed 里的 guid 完全一致。"""
    seed = (it.get("link") or it.get("title") or "") + "|" + str(it.get("src") or "")
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]


# ---------------- 生成 RSS ----------------
def esc(s):
    return html.escape(s or "", quote=True)


def _channel_lines(feed_url, channel_title, description, total, screen):
    """<channel> 头部：设备版砍掉 atom:link / generator / ttl 三项省体积。"""
    now = email.utils.format_datetime(datetime.now(timezone.utc))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>%s</title>" % esc(channel_title),
        "<link>%s</link>" % esc(feed_url),
    ]
    if not screen:
        lines.append('<atom:link href="%s" rel="self" type="application/rss+xml"/>'
                     % esc(feed_url))
    lines += [
        "<description>%s</description>" % esc(description),
        "<language>zh-CN</language>",
        "<lastBuildDate>%s</lastBuildDate>" % now,
    ]
    if not screen:
        # 版本号写进 generator：从订阅端就能看出线上跑的是哪一版，排障不用去翻提交历史
        lines += ["<generator>rss-hub %s</generator>" % esc(__version__), "<ttl>5</ttl>"]
    return lines


def _item_lines(it, screen):
    """一个 <item> 的字段。两种版本的差别集中在这里，一眼能对照。"""
    if screen:
        head, tail = split_title(it["title"])
        body = compose_body(tail, it.get("summary"), it["title"])
    else:
        # 完整版不丢摘要：以前这里正文恒为空，阅读器里只剩标题，原 feed 的导语一段都读不到。
        # 标题保持原样（不截断），摘要单独截到 FEED_BODY_MAX。
        head = it["title"]
        body = compose_body("", it.get("summary"), it["title"], body_max=FEED_BODY_MAX)

    lines = ["<item>", "<title>%s</title>" % esc(head)]
    # 设备版：标题装不下的余文 + 原 feed 摘要；完整版：原 feed 摘要
    if body:
        lines.append("<description>%s</description>" % esc(body))
    lines.append("<link>%s</link>" % esc(it["link"]))
    if it.get("date_raw"):
        lines.append("<pubDate>%s</pubDate>" % esc(it["date_raw"]))
    elif it.get("ts"):
        lines.append("<pubDate>%s</pubDate>" % email.utils.format_datetime(
            datetime.fromtimestamp(it["ts"], timezone.utc)))
    if screen:
        # 原来是把「链接|源地址」整串当 guid，动辄上百字节，小屏完全用不上。
        # 换成短哈希，稳定、唯一，体积降到十几字节；也是轮播判断「是否已展示」的键。
        lines.append('<guid isPermaLink="false">%s</guid>' % item_guid(it))
    else:
        # 完整版仍用「链接|源地址」：同一篇文章被两个源收录时 guid 不同，阅读器不会吞条目
        lines.append('<guid isPermaLink="false">%s</guid>'
                     % esc((it["link"] or it["title"]) + "|" + str(it["src"])))
    if it.get("src_name"):
        lines.append('<source url="%s">%s</source>'
                     % (esc(it["src"]), esc(plain_text(it["src_name"]))))
    lines.append("</item>")
    return lines


def build_rss(items, feed_url, stats, channel_title="RSS 聚合头条",
              description=None, screen=False):
    """出一份 RSS 2.0。

    screen=False 完整版：标题原样（可含 emoji），正文放清洗后的原 feed 摘要，
                 字段齐全（含 atom:link / generator / ttl），给标准阅读器用。
    screen=True  设备版：标题截到屏宽 + 余文进 description + 字段极简，
                 省略 atom:link / generator / ttl，guid 改成 12 位短哈希，
                 单条体积降到完整版的三成左右；对解析端来说仍是合法 RSS 2.0。

    stats 用 .get 取：调用方可能只传部分键（CI 烟测就只传六项），
    这里不该因为少一个统计字段把整份 feed 搞崩。
    """
    total = stats.get("total", 0)
    if description is None:
        description = "多源 RSS 头条聚合，按发布时间倒序（源 %d 个）" % total
    lines = _channel_lines(feed_url, channel_title, description, total, screen)
    for it in items:
        lines.extend(_item_lines(it, screen))
    lines.append("</channel></rss>")
    return "\n".join(lines)

