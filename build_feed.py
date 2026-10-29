#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 聚合器 —— 专为 Dot (Quote/0) 墨水屏设备开发

读 feeds.txt（注释行 = 中文源名，下一行 = 地址）
→ 每源抓最新 PER_SOURCE 条 → 跨源去重 → 按发布时间倒序、超 ITEM_LIMIT 砍最旧 → 生成 RSS 2.0

输出三份：
  docs/feed.xml       完整版：标题原样、全部条目按时间倒序，给电脑 / 手机上的 RSS 阅读器
  docs/feed-wrap.xml  设备版：只出「当前该显示的那一条」，标题截到屏宽，
                      余下文字接进 <description>（正文）；填进 Dot App 的「循环内容」即可
  docs/rotate.json    轮播状态：哪一条已展示、当前在第几窗，随 Pages 一起发布

轮播规则（为什么设备版只有一条）：
  Dot 云端只渲染文档里的第一条，按时间倒序排的话屏幕永远停在最新那条，
  其余条目一辈子轮不到。所以设备版只发一条，选谁由「已显示标记」决定：

    信息按时间倒序排；第一条上屏，并标注已显示
    下一轮：取「没有已显示标注」里最新的一条上屏，标注已显示
    全部都有标注了 → 清掉所有标注，从最新一条重新开始
    同一时间窗内（w = now // ROTATE_SECONDS）重复构建不推进，后台多跑几轮也不跳条

特性：
  * 零第三方依赖，Python 3.8+
  * 只读通路：只抓 RSS、只写静态文件，不向任何设备/第三方接口发起写请求，不需要任何密钥
  * 单源失败时回退到上一份 feed 中该源的条目（不会凭空丢源）
  * 单次运行内可循环多轮（REFRESH_SECONDS）
  * 自动识别 UTF-8 / GB18030 编码、修补常见 HTML 实体

环境变量：
  FEEDS_FILE         订阅源列表，默认 ./feeds.txt
  OUT_FILE           完整版输出，默认 ./docs/feed.xml
  FEED_URL           频道 <link>，默认 https://cheney-hub.github.io/rss-hub/feed.xml
  REFRESH_SECONDS    循环刷新到第几秒结束（0 = 只跑一轮），默认 0
  PER_SOURCE         每源取几条，默认 3
  ITEM_LIMIT         去重后条目上限，默认 120（超出按时间砍最旧）
  FETCH_TIMEOUT      单源超时秒数，默认 20
  CONCURRENCY        并发数，默认 12
  SCREEN_OUT         设备版输出，默认与 OUT_FILE 同目录的 feed-wrap.xml
  SCREEN_LINE_WIDTH  标题行宽度（全角字符数），默认 13
                     —— 设备 296×152 屏是大字号单行，实测 13 个全角字满宽，
                     再多就被 Dot 云端从右边切掉。所以标题只放得下多少给多少。
  SCREEN_BODY_MAX    正文（description）最多保留多少全角字，默认 90
                     —— 正文 = 标题装不下的余文 + 原 feed 的 description/摘要原文；
                     296×152 屏正文区约 4 行 × 22 全角 ≈ 88 字，取 90 刚好填满。
                     太长的正文尾端会补省略号。
  ROTATE_SECONDS     每条在屏上驻留多少秒（= 设备刷新周期），默认 300
                     —— 调大等于放慢轮播。Pages CDN 缓存 600 秒，
                     若真机上出现「同一条连显两次」，把它改成 600。
  ROTATE_POOL_MAX    轮播池上限，默认 0 = 不限（= 去重后的全部条目）
  ROTATE_STATE       轮播状态文件，默认与 OUT_FILE 同目录的 rotate.json
  ROTATE_STATE_URL   线上状态地址，默认 FEED_URL 同目录的 rotate.json
"""
import os
import re
import sys
import json
import time
import gzip
import html
import hashlib
import email.utils
import unicodedata
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.environ.get("FEEDS_FILE", os.path.join(BASE_DIR, "feeds.txt"))
OUT_FILE = os.environ.get("OUT_FILE", os.path.join(BASE_DIR, "docs", "feed.xml"))
FEED_URL = os.environ.get("FEED_URL", "https://cheney-hub.github.io/rss-hub/feed.xml")
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "0"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "12")))

# 设备版（Dot Quote/0）：标题截到屏宽，余下文字进正文，交给 Dot 云端渲染成位图
SCREEN_OUT = os.environ.get(
    "SCREEN_OUT", os.path.join(os.path.dirname(OUT_FILE) or ".", "feed-wrap.xml"))
LINE_WIDTH = float(os.environ.get("SCREEN_LINE_WIDTH", "13"))
BODY_MAX = max(0.0, float(os.environ.get("SCREEN_BODY_MAX", "90")))

# 轮播：设备版一次只发布一条，按时间窗前进，走完一轮才回头
ROTATE_SECONDS = max(60, int(os.environ.get("ROTATE_SECONDS", "300")))   # 每条驻留 = 设备刷新周期
ROTATE_POOL_MAX = max(0, int(os.environ.get("ROTATE_POOL_MAX", "0")))    # 0 = 池子不限
ROTATE_STATE = os.environ.get(
    "ROTATE_STATE", os.path.join(os.path.dirname(OUT_FILE) or ".", "rotate.json"))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
RETRY_DELAY = 1.0

PER_SOURCE = max(1, int(os.environ.get("PER_SOURCE", "3")))   # 每源取几条
ITEM_LIMIT = max(1, int(os.environ.get("ITEM_LIMIT", "120")))  # 去重后上限，超出按时间砍最旧
ENTITY_FIX = {
    "&nbsp;": " ", "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
    "&ldquo;": "“", "&rdquo;": "”", "&lsquo;": "‘", "&rsquo;": "’",
    "&middot;": "·", "&copy;": "©", "&reg;": "®", "&trade;": "™",
    "&bull;": "•", "&deg;": "°", "&permil;": "‰", "&times;": "×",
}


# ---------------- feeds.txt 解析 ----------------
def read_feed_entries():
    entries, pending = [], ""
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            if raw.startswith("#"):
                txt = raw.lstrip("#").strip()
                # 分组标题（==== / ----）不算源名
                if txt and not txt.startswith(("=", "-", "*")):
                    pending = re.sub(r"[（(].*?$", "", txt).strip()
                continue
            url = raw.split("#")[0].strip()
            if url.startswith("http"):
                entries.append({"name": pending or url, "url": url})
                pending = ""
    seen, out = set(), []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            out.append(e)
    return out


# ---------------- HTTP ----------------
def http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        body = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            body = gzip.decompress(body)
        return resp.status, body


def decode_body(body):
    for enc in ("utf-8", "gb18030"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


# ---------------- 解析 ----------------
def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _first_text(el, names):
    for child in el.iter():
        if _local(child.tag) in names:
            t = "".join(child.itertext()).strip()
            if t:
                return t
    return ""


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _sanitize(xml_text):
    """修补 XML 中未定义的 HTML 实体 + 去掉控制字符。"""
    def fix(m):
        e = m.group(0)
        if e in ENTITY_FIX:
            return ENTITY_FIX[e]
        if e.startswith(("&#", "&amp;", "&lt;", "&gt;", "&quot;", "&apos;")):
            return e
        return " "
    xml_text = re.sub(r"&[a-zA-Z][a-zA-Z0-9]{1,8};", fix, xml_text)
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    # 去掉声明里的 encoding，避免 ET 对 str 报错或按错误编码解码
    return re.sub(r'(<\?xml[^>]*?)\s+encoding="[^"]*"', r"\1", xml_text, count=1)


def parse_items(xml_text, source_url, limit=PER_SOURCE):
    """解析一个源，按发布时间倒序取前 limit 条；每条都带摘要原文（summary）。"""
    try:
        root = ET.fromstring(_sanitize(xml_text))
        nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    except ET.ParseError:
        # 兜底：正则抽取 <item>/<entry> 块
        blocks = re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", xml_text,
                            re.S | re.I)
        if not blocks:
            raise RuntimeError("XML 解析失败且无 item/entry")
        nodes = []
        for b in blocks:
            nodes.append({"__raw__": b})

    if not nodes:
        raise RuntimeError("未解析到条目")

    def entry_of(node):
        if isinstance(node, dict):
            raw = node["__raw__"]
            title = _rx(raw, r"<title\b[^>]*>(.*?)</title>")
            link = _rx(raw, r"<link\b[^>]*href=\"([^\"]+)\"") or _rx(raw, r"<link\b[^>]*>(.*?)</link>")
            date_raw = (_rx(raw, r"<pubDate\b[^>]*>(.*?)</pubDate>")
                        or _rx(raw, r"<published\b[^>]*>(.*?)</published>")
                        or _rx(raw, r"<updated\b[^>]*>(.*?)</updated>"))
            summary = (_rx(raw, r"<content:encoded\b[^>]*>(.*?)</content:encoded>")
                       or _rx(raw, r"<description\b[^>]*>(.*?)</description>")
                       or _rx(raw, r"<summary\b[^>]*>(.*?)</summary>")
                       or _rx(raw, r"<content\b[^>]*>(.*?)</content>"))
        else:
            title = _first_text(node, {"title"})
            link = ""
            for c in node.iter():
                if _local(c.tag) == "link":
                    link = (c.get("href") or "".join(c.itertext()) or "").strip()
                    if link:
                        break
            date_raw = _first_text(node, {"pubdate", "published", "updated", "date"})
            # 正文来源按「原文优先」排：content:encoded > description > summary > content
            summary = _first_text(node, {"encoded", "description", "summary", "content"})
        return {"title": clean_text(title), "link": (link or "").strip(),
                "date_raw": clean_text(date_raw), "ts": _parse_date(clean_text(date_raw)),
                "summary": clean_text(summary)}

    items = [entry_of(n) for n in nodes]
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    out = []
    for it in items[:limit]:
        it["src"] = source_url
        if it["ts"] is None:
            it["ts"] = 0
        out.append(it)
    return out


def _rx(raw, pattern):
    m = re.search(pattern, raw, re.S | re.I)
    return m.group(1) if m else ""


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
_RE_DATELINE2 = re.compile("^" + _TAG + r"[\u4e00-\u9fa5]{0,10}\d{1,2}月\d{1,2}日\s*(?:电|讯)\s*[，,:：]?\s*")


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


def load_prev_items():
    """读上一份 feed.xml，取 源地址 → 条目列表 的映射，用于失败回退。"""
    prev = {}
    if not os.path.exists(OUT_FILE):
        return prev
    try:
        with open(OUT_FILE, "r", encoding="utf-8") as f:
            root = ET.fromstring(f.read())
    except Exception:
        return prev
    for it in root.iter("item"):
        src_el = it.find("source")
        if src_el is None:
            continue
        url = (src_el.get("url") or "").strip()
        if not url:
            continue
        bucket = prev.setdefault(url, [])
        if len(bucket) >= PER_SOURCE:
            continue
        bucket.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date_raw": (it.findtext("pubDate") or "").strip(),
            "ts": _parse_date(it.findtext("pubDate") or "") or 0,
            "summary": (it.findtext("description") or "").strip(),
            "src": url,
            "src_name": (src_el.text or "").strip(),
            "stale": True,
        })
    return prev


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
    head = t[:cut].rstrip(" 　")
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
    if body_max and text_width(body) > body_max:
        bc = _cut_index(body, body_max - 1)         # 留 1 个位给省略号
        body = body[:bc].rstrip(BREAK_CHARS + "-—…") + "…"
    return body


# ---------------- 轮播（设备版一条一条轮） ----------------
# 目标：设备每 5 分钟刷新一次，每次都要换一条；全池走完才回头，不重不漏。
#
# 为什么用「时间窗」而不是「跑了第几轮」驱动：
#   后台一小时内可能构建 12 次，也可能因为重跑、手动触发变成 30 次。
#   用计数器就会跳条。用 w = now // ROTATE_SECONDS 做幂等键，
#   同一个 5 分钟窗内构建多少次都只前进一格，节奏只跟墙上时钟走。
def item_guid(it):
    """条目的稳定身份：链接 + 源地址的短哈希，与 feed 里的 guid 完全一致。"""
    seed = (it.get("link") or it.get("title") or "") + "|" + str(it.get("src") or "")
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:12]


def _rotate_url():
    return (os.environ.get("ROTATE_STATE_URL")
            or FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(ROTATE_STATE))


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_rotate_state():
    """本地那份和线上那份取 window 更新的一个。

    作业是整点 checkout 的，仓库里的 rotate.json 可能是一小时前的；
    线上那份才是上一轮真正发布出去的。谁 window 大用谁，一样大就用本地
    （本轮可能已经推进过，本地更新）。
    """
    local = _read_json(ROTATE_STATE)
    remote = {}
    try:
        req = urllib.request.Request(
            "%s?cb=%d" % (_rotate_url(), time.time()),
            headers={"User-Agent": UA, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        remote = data if isinstance(data, dict) else {}
    except Exception:
        remote = {}
    if int(remote.get("window") or 0) > int(local.get("window") or 0):
        return remote
    return local


def save_rotate_state(state):
    os.makedirs(os.path.dirname(ROTATE_STATE) or ".", exist_ok=True)
    tmp = ROTATE_STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, ROTATE_STATE)


def rotate_pick(items, state, now=None):
    """取「这一窗该上屏的那一条」，返回 (条目, 新状态, 说明)。

    items 必须已按发布时间倒序（run_once 保证）。规则只有三条：
      1. 取「未标注已显示」里发布时间最新的一条上屏，并给它打上已显示标记；
      2. 全部条目都已显示 → 清空所有标记，下一窗从最新一条重新开始；
      3. 同一个时间窗内重复调用不推进，后台多跑几轮也不会跳条。
    """
    dwell = ROTATE_SECONDS
    w = int((now if now is not None else time.time()) // dwell)
    # 节拍改过（比如从 60 秒调成 300 秒）：窗口编号的基数变了，不可比，重新起轮
    if int(state.get("dwell") or dwell) != dwell:
        state = {}
    prev_w = int(state.get("window") or 0)

    index = {}
    for it in items:
        index.setdefault(item_guid(it), it)          # 同 guid 只留第一条

    # 同一窗：内容可以刷新，但展示的条目锁死，这就是幂等
    if w <= prev_w and state.get("current") in index:
        return index[state["current"]], state, "同窗保持"

    shown = [g for g in (state.get("shown") or []) if g in index]   # 顺手清掉已消失的条目
    cycle = int(state.get("cycle") or 1)
    if len(shown) >= len(index):
        shown = []                                   # 全部都显示过 → 清空标记，新一轮
        cycle += 1

    shown_set = set(shown)
    cands = [it for it in items if item_guid(it) not in shown_set]

    pick = cands[0]
    guid = item_guid(pick)
    shown.append(guid)
    new_state = {
        "window": w,
        "current": guid,
        "shown": shown,
        "cycle": cycle,
        "pool": len(index),
        "dwell": dwell,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": (pick.get("title") or "")[:40],
    }
    note = "新窗推进（池 %d / 本轮已展示 %d / 第 %d 轮）" % (len(index), len(shown), cycle)
    return pick, new_state, note


# ---------------- 生成 RSS ----------------
def esc(s):
    return html.escape(s or "", quote=True)


def build_rss(items, feed_url, stats, channel_title="RSS 聚合头条",
              description=None, screen=False):
    """screen=True 出设备版：标题截到屏宽 + 余文进 description + 字段极简。

    字段极简指省略 atom:link / generator / ttl，guid 改成 12 位短哈希，
    单条体积降到原来的三成左右；对解析端来说仍是合法 RSS 2.0。
    """
    now = email.utils.format_datetime(datetime.now(timezone.utc))
    if description is None:
        description = "多源 RSS 头条聚合，按发布时间倒序（源 %d 个）" % stats["total"]
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
        lines += ["<generator>github-actions/rss-hub</generator>", "<ttl>5</ttl>"]
    for it in items:
        if screen:
            head, tail = split_title(it["title"])
            body = compose_body(tail, it.get("summary"), it["title"])
        else:
            head, tail, body = it["title"], "", ""
        lines.append("<item>")
        lines.append("<title>%s</title>" % esc(head))
        # 标题装不下的余文 + 原 feed 摘要，一起进正文：屏上正文区能接着读到后半句
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
            # 换成链接的短哈希，稳定、唯一，体积降到十几字节；也是轮播判断「是否已展示」的键。
            lines.append('<guid isPermaLink="false">%s</guid>' % item_guid(it))
        else:
            lines.append('<guid isPermaLink="false">%s</guid>'
                         % esc((it["link"] or it["title"]) + "|" + str(it["src"])))
        if it.get("src_name"):
            lines.append('<source url="%s">%s</source>'
                         % (esc(it["src"]), esc(plain_text(it["src_name"]))))
        lines.append("</item>")
    lines.append("</channel></rss>")
    return "\n".join(lines)


# ---------------- 单轮采集 ----------------
def run_once(entries, prev_items):
    items, ok, fail, stale = [], 0, 0, 0

    def fetch_one(e):
        try:
            status, body = http_get(e["url"])
            if status != 200:
                raise RuntimeError("HTTP %s" % status)
            got = parse_items(decode_body(body), e["url"], PER_SOURCE)
            for it in got:
                it["src_name"] = e["name"]
            return e, got, None
        except Exception as err:
            return e, None, err

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(fetch_one, e) for e in entries]
        for fut in as_completed(futs):
            e, got, err = fut.result()
            if err is not None:
                # 重试一次
                time.sleep(RETRY_DELAY)
                e2, got2, err2 = fetch_one(e)
                got, err = got2, err2
            if got:
                ok += 1
                items.extend(got)
            else:
                fail += 1
                fb = prev_items.get(e["url"])
                if fb:
                    stale += 1
                    items.extend(dict(x) for x in fb)
                    print("[回退] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))
                else:
                    print("[失败] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))

    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    items, dropped = dedupe(items)
    if len(items) > ITEM_LIMIT:                  # 超上限：按时间排序后节选最新的
        dropped += len(items) - ITEM_LIMIT
        items = items[:ITEM_LIMIT]
    stats = {"total": len(entries), "ok": ok, "fail": fail, "stale": stale,
             "items": len(items), "dropped": dropped}
    return items, stats


def _write(path, xml_text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    # 统一 LF：Windows 上默认会写成 CRLF，进仓库会反复产生换行差异
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml_text)
    os.replace(tmp, path)


def write_feed(items, stats, pick=None):
    """写两份：完整版 feed.xml（全部条目）+ 设备版 feed-wrap.xml（只有轮播到的那一条）。"""
    _write(OUT_FILE, build_rss(items, FEED_URL, stats))

    wrap_url = FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(SCREEN_OUT)
    screen_items = [pick] if pick else items
    _write(SCREEN_OUT, build_rss(
        screen_items, wrap_url, stats,
        channel_title="RSS 头条 · 小屏",
        description="多源头条，纯文本；每 %d 分钟轮换一条，走完全部再从头开始。"
                    "标题只放屏上显示得下的 %g 全角字，余下原文接在正文里，源 %d 个"
                    % (ROTATE_SECONDS // 60, LINE_WIDTH, stats["total"]),
        screen=True))


def main():
    entries = read_feed_entries()
    if not entries:
        print("feeds.txt 中没有有效地址")
        return 1
    print("订阅源 %d 个，输出 %s / %s（标题 %g 全角、正文最多 %g 全角、每 %d 秒轮换一条）"
          % (len(entries), OUT_FILE, SCREEN_OUT, LINE_WIDTH, BODY_MAX, ROTATE_SECONDS))

    deadline = time.time() + REFRESH_SECONDS
    rounds = 0
    while True:
        t0 = time.time()
        prev_items = load_prev_items()
        items, stats = run_once(entries, prev_items)
        if not items:
            print("本轮无任何条目，保留旧 feed 不覆盖")
            return 1
        pool = items[:ROTATE_POOL_MAX] if ROTATE_POOL_MAX else items
        state = load_rotate_state()
        pick, state, note = rotate_pick(pool, state)
        write_feed(items, stats, pick)
        save_rotate_state(state)
        rounds += 1
        print("[第 %d 轮] 成功 %d / 回退 %d / 失败 %d → 条目 %d（去重 %d）耗时 %.1fs"
              % (rounds, stats["ok"], stats["stale"], stats["fail"],
                 stats["items"], stats["dropped"], time.time() - t0))
        print("        上屏：%s ｜ %s" % (note, (pick.get("title") or "")[:36]))

        left = deadline - time.time()
        if left < 45:                      # 剩余时间不够一轮就收工
            break
        time.sleep(min(ROTATE_SECONDS, left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
