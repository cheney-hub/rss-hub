#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 聚合器（GitHub Actions / 任意机器通用版）

读 feeds.txt（注释行 = 中文源名，下一行 = 地址）
→ 每源抓最新 1 条 → 跨源去重 → 按时间倒序生成 RSS 2.0 → 写入 docs/feed.xml

特性：
  * 零第三方依赖，Python 3.8+
  * 单源失败时回退到上一份 feed.xml 中该源的条目（不会凭空丢源）
  * 单次运行内可循环多轮（REFRESH_SECONDS），让 feed 更接近实时
  * 自动识别 UTF-8 / GB18030 编码、修补常见 HTML 实体

同时输出两份 feed：
  docs/feed.xml         完整版，标题原样，给电脑/手机上的阅读器
  docs/feed-screen.xml  墨水屏版（Dot Quote/0）：纯文本标题、字段极简，供 Dot 云端渲染
  docs/feed-wrap.xml    折行版：同墨水屏版，但标题折成多行（取决于渲染端是否保留换行符）

环境变量：
  FEEDS_FILE       订阅源列表，默认 ./feeds.txt
  OUT_FILE         输出文件，默认 ./docs/feed.xml
  PREV_FILE        上一份 feed（失败回退用），默认同 OUT_FILE
  FEED_URL         频道 <link>，默认 https://cheney-hub.github.io/rss-hub/feed.xml
  REFRESH_SECONDS  循环刷新到第几秒结束（0 = 只跑一轮），默认 0
  FETCH_TIMEOUT    单源超时秒数，默认 20
  CONCURRENCY      并发数，默认 12
  SCREEN_OUT       墨水屏版输出路径，默认与 OUT_FILE 同目录的 feed-screen.xml
  SCREEN_TITLE_WIDTH 墨水屏标题每行宽度上限（全角字符数），默认 13
                    —— RSS 卡片在设备上是大字号单行，296px 宽实测只放得下 13～14 个全角字，
                    超过就被云端从右边硬切（还会补自己的省略号）。这里按 13 让我们的截断先发生。
  SCREEN_ITEMS     墨水屏版条目数上限，默认 60
  WRAP_OUT         折行版输出路径，默认同目录的 feed-wrap.xml
  SCREEN_WRAP_LINES  折行版最多折几行，默认 3
  SCREEN_WRAP_SEP    折行分隔符：lf（换行）/ br（<br> 标签）/ u2028，默认 lf
"""
import os
import re
import sys
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
PREV_FILE = os.environ.get("PREV_FILE", OUT_FILE)
FEED_URL = os.environ.get("FEED_URL", "https://cheney-hub.github.io/rss-hub/feed.xml")
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "0"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "12")))

# 墨水屏版（Dot Quote/0，走 Dot 云端渲染，非设备直接解析）
SCREEN_OUT = os.environ.get(
    "SCREEN_OUT", os.path.join(os.path.dirname(OUT_FILE) or ".", "feed-screen.xml"))
SCREEN_TITLE_WIDTH = float(os.environ.get("SCREEN_TITLE_WIDTH", "13"))
SCREEN_ITEMS = max(1, int(os.environ.get("SCREEN_ITEMS", "60")))

# 折行版（标题折成多行，而不是一行截断）。和墨水屏版并列输出，便于对比订阅。
WRAP_OUT = os.environ.get(
    "WRAP_OUT", os.path.join(os.path.dirname(OUT_FILE) or ".", "feed-wrap.xml"))
SCREEN_WRAP_LINES = max(1, int(os.environ.get("SCREEN_WRAP_LINES", "3")))
SCREEN_WRAP_SEP = os.environ.get("SCREEN_WRAP_SEP", "lf")      # lf | br | u2028

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
RETRY_DELAY = 1.0

MAX_ITEMS = 60          # 输出条目上限，避免无限膨胀
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


def parse_latest(xml_text, source_url):
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
        else:
            title = _first_text(node, {"title"})
            link = ""
            for c in node.iter():
                if _local(c.tag) == "link":
                    link = (c.get("href") or "".join(c.itertext()) or "").strip()
                    if link:
                        break
            date_raw = _first_text(node, {"pubdate", "published", "updated", "date"})
        return {"title": clean_text(title), "link": (link or "").strip(),
                "date_raw": clean_text(date_raw), "ts": _parse_date(clean_text(date_raw))}

    items = [entry_of(n) for n in nodes]
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    best = items[0]
    best["src"] = source_url
    if best["ts"] is None:
        best["ts"] = 0
    return best


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
def norm_title(t):
    t = re.sub(r"[「」【】\[\]（）()“”\"'‘’：:，,。.！!？?、\s|·\-—–…~]", "", t or "")
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
    """读上一份 feed.xml，取 源地址 → 条目 的映射，用于失败回退。"""
    prev = {}
    if not os.path.exists(PREV_FILE):
        return prev
    try:
        with open(PREV_FILE, "r", encoding="utf-8") as f:
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
        prev[url] = {
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date_raw": (it.findtext("pubDate") or "").strip(),
            "ts": _parse_date(it.findtext("pubDate") or "") or 0,
            "src": url,
            "src_name": (src_el.text or "").strip(),
            "stale": True,
        }
    return prev


# ---------------- 墨水屏版 ----------------
# Dot Quote/0 的显示链路：App 里把 RSS 地址加入「循环内容」→ Dot 云端抓取解析 →
# 云端模板渲染成位图 → 设备按刷新间隔拉图显示。设备端（ESP32-C3，400KB 内存）不解析 XML，
# 显示几条、字号多大都由云端模板决定，订阅方控制不了。
#
# 实测（2026-10-29，296×152 屏）：RSS 卡片 = 一行大字号标题，约 13～14 个全角字就满宽，
# 超出部分被云端从右边硬切并补它自己的省略号。所以要「我们能控制的截断」先发生，
# 默认按 13 全角预截；云端就不会再动刀。
#
# 这份小屏版只做云端渲染做不到的事：
#   1. 标题纯文本 —— 摘掉 emoji、箭头、几何图形、零宽字符等（黑白墨水屏上它们是豆腐块或占位）；
#   2. 标题限长 —— 超长标题由我们这边在标点处优雅截断，而不是被云端硬切成一串省略号；
#   3. 条目给足 —— 云端每次取「最新 N 条」，N 由 Dot 决定，我们给满 60 条随它挑；
#   4. 字段极简 —— guid 用短哈希、去掉 atom:link/generator/ttl，云端抓取解析更轻。
# 未经限长的完整版照旧输出 feed.xml，普通 RSS 阅读器用它。
# 想要一屏读到更多字，靠 wrap_title() 把标题折成多行（feed-wrap.xml）：
# 第一行是标题主体，放不下的部分自动续到下一行，最多 SCREEN_WRAP_LINES 行。
# 不依赖任何 API 密钥——这也是不走 Dot 文本 API 的原因。
# 允许在这些字符处收尾（反向找最近的一个）。冒号「：」故意不在里面：
# 「雷普索尔首席执行官：展望未来…」在冒号处断开只剩主体、谓语全丢，反而不如多留几字。
BREAK_CHARS = "，,、；;｜|—）)！!？?·】」』 "

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


def _cut_index(s, max_w):
    """返回不超过 max_w 全角宽度的最大字符下标。"""
    acc = 0.0
    for i, ch in enumerate(s):
        w = char_width(ch)
        if acc + w > max_w:
            return i
        acc += w
    return len(s)


def fit_title(title, max_w=SCREEN_TITLE_WIDTH):
    """把标题裁到一行放得下：先去装饰字符，再在标点处断句，最后补省略号。"""
    t = plain_text(title)
    # 去掉 “标题 - 来源名” 这类尾巴（来源单独用 <source> 显示，重复占位）
    t = re.sub(r"\s*[-_|｜]\s*[^-_|｜]{1,8}$", "", t) or t
    if text_width(t) <= max_w:
        return t
    cut = _cut_index(t, max_w - 1)          # 留 1 个全角位给省略号
    head = t[:cut]
    # 书名号 / 引号没闭合时，从它前面断开，别把《今宵酒…》切成半截
    for opener, closer in (("《", "》"), ("“", "”"), ("「", "」"), ("（", "）"), ("(", ")")):
        if head.count(opener) > head.count(closer):
            pos = head.rfind(opener)
            if pos >= int(len(head) * 0.5):     # 只在后半段出现才回退，否则会砍太狠
                head = head[:pos]
    # 在标点处收尾更好读，但不能丢太多字：断点必须保留 80% 以上宽度，
    # 否则宁可硬切（比如「商务部：若欧方一意孤…」不要变成「商务部…」）。
    for i in range(len(head) - 1, max(0, int(len(head) * 0.8)) - 1, -1):
        if head[i] in BREAK_CHARS:
            head = head[:i]
            break
    head = head.rstrip(BREAK_CHARS + "-—…")
    return (head + "…") if head else t[:cut] + "…"


# 折行用的分隔符：不同渲染端对「标题里的换行」处理方式不同，
#     lf      真实换行（XML 里写成 &#10;）—— 多数渲染端会保留
#     br      <br> 标签（不转义）—— 少数把标题当 HTML 解析的渲染端认它
#     u2028   U+2028 LINE SEPARATOR —— 老派排版分隔符
WRAP_SEPS = ("lf", "br", "u2028")


def wrap_title(title, line_w=SCREEN_TITLE_WIDTH, max_lines=3):
    """把标题折成 ≤max_lines 行、每行 ≤line_w 全角宽，尽量在标点处换行。

    返回行列表（不是带换行符的整串），由调用方按目标渲染端自己拼分隔符。
    与 fit_title 的区别：fit_title 是「一行内截断」，本函数是「多行折行」。
    RSS 卡片本身按单行渲染（字号行数由云端模板决定），能否真正显示多行
    取决于渲染端是否保留标题里的换行符——所以这里只负责给出分行结果。
    """
    t = plain_text(title)
    t = re.sub(r"\s*[-_|｜]\s*[^-_|｜]{1,8}$", "", t) or t
    if text_width(t) <= line_w:
        return [t]

    lines, rest = [], t
    while rest:
        if len(lines) >= max_lines - 1:                 # 已是最后一行
            if text_width(rest) <= line_w:
                lines.append(rest)
            else:
                cut = _cut_index(rest, line_w - 1)      # 留 1 个位给省略号
                lines.append(rest[:cut].rstrip(BREAK_CHARS + "-—…") + "…")
            break
        cut = _cut_index(rest, line_w)
        head = rest[:cut] or rest[:1]                   # 防御：绝不返回空串
        # 行尾落在标点上更好读，但不许为此丢掉超过 30% 的宽度
        for i in range(len(head) - 1, max(0, int(len(head) * 0.7)) - 1, -1):
            if head[i] in BREAK_CHARS:
                head = head[:i + 1]
                break
        head = head.rstrip(" 　")
        if not head:
            head = rest[:1]
        lines.append(head)
        rest = rest[len(head):].lstrip(" 　" + BREAK_CHARS + "-—…")

    # 末行只剩一两个字（「8%」「元。」这种孤行）很难看：从上一行尾部挪字过来，
    # 让末行至少占一半宽度。上一行本来就是满载的，让出几个字不影响可读性。
    # 关键约束：挪到上一行末尾是标点就停——标点必须留在行尾，
    # 跟着挪下去就变成「，同比增长…」这种行首标点，中文排版的大忌。
    if len(lines) >= 2 and text_width(lines[-1]) < line_w * 0.35:
        prev, last = lines[-2], lines[-1]
        while (text_width(last) < line_w * 0.55 and len(prev) > 2
               and prev[-1] not in BREAK_CHARS and last[:1] not in BREAK_CHARS):
            last = prev[-1] + last
            prev = prev[:-1]
        if prev.strip(" 　"):
            lines[-2], lines[-1] = prev, last
        else:
            lines.pop(-2)
    return lines


def join_title_xml(rows, sep="lf"):
    """把多行标题拼成 XML 里的 title 文本，转义各段，绝不转义分隔符本身。"""
    if len(rows) <= 1:
        return esc(rows[0] if rows else "")
    if sep == "br":
        # <br> 必须放在 CDATA 里，否则整份 feed 就是非法 XML（连源都加不进去）。
        # CDATA 内不做实体转义，所以只挡一下可能提前闭合的 ]]>
        body = "<br>".join(rows).replace("]]>", "]]&gt;")
        return "<![CDATA[%s]]>" % body
    if sep == "u2028":
        return "&#8232;".join(esc(r) for r in rows)     # U+2028
    return "&#10;".join(esc(r) for r in rows)           # 默认：真实换行


# ---------------- 生成 RSS ----------------
def esc(s):
    return html.escape(s or "", quote=True)


def build_rss(items, feed_url, stats, channel_title="RSS 聚合头条",
              description=None, title_width=None, limit=None, minimal=False,
              wrap_lines=0, wrap_sep="lf"):
    """minimal=True 时输出极简版：只留小屏真正会用到的东西。

    省略 atom:link / generator / ttl 以及 guid 里的长地址（改为 12 位短哈希），
    单条体积降到原来的三成左右；设备与解析端拿到的仍然是合法 RSS 2.0。

    wrap_lines>0 时标题按 title_width 折成多行（而不是一行截断），
    分隔符由 wrap_sep 指定——用于实测哪种换行方式在设备渲染端生效。
    """
    now = email.utils.format_datetime(datetime.now(timezone.utc))
    if description is None:
        description = "多源 RSS 头条聚合，按发布时间倒序（源 %d 个）" % stats["total"]
    if limit:
        items = items[:limit]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>%s</title>" % esc(channel_title),
        "<link>%s</link>" % esc(feed_url),
    ]
    if not minimal:
        lines.append('<atom:link href="%s" rel="self" type="application/rss+xml"/>'
                     % esc(feed_url))
    lines += [
        "<description>%s</description>" % esc(description),
        "<language>zh-CN</language>",
        "<lastBuildDate>%s</lastBuildDate>" % now,
    ]
    if not minimal:
        lines += ["<generator>github-actions/rss-hub</generator>", "<ttl>5</ttl>"]
    for it in items:
        if wrap_lines:
            title_xml = join_title_xml(
                wrap_title(it["title"], title_width or SCREEN_TITLE_WIDTH, wrap_lines),
                wrap_sep)
        elif title_width:
            title_xml = esc(fit_title(it["title"], title_width))
        else:
            title_xml = esc(it["title"])
        lines.append("<item>")
        lines.append("<title>%s</title>" % title_xml)
        lines.append("<link>%s</link>" % esc(it["link"]))
        if it.get("date_raw"):
            lines.append("<pubDate>%s</pubDate>" % esc(it["date_raw"]))
        elif it.get("ts"):
            lines.append("<pubDate>%s</pubDate>" % email.utils.format_datetime(
                datetime.fromtimestamp(it["ts"], timezone.utc)))
        if minimal:
            # 原来是把「链接|源地址」整串当 guid，动辄上百字节，小屏完全用不上。
            # 换成链接的短哈希，稳定、唯一，体积降到十几字节。
            seed = (it["link"] or it["title"]) + "|" + str(it["src"])
            lines.append('<guid isPermaLink="false">%s</guid>'
                         % hashlib.md5(seed.encode("utf-8")).hexdigest()[:12])
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
            item = parse_latest(decode_body(body), e["url"])
            item["src_name"] = e["name"]
            return e, item, None
        except Exception as err:
            return e, None, err

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futs = [ex.submit(fetch_one, e) for e in entries]
        for fut in as_completed(futs):
            e, item, err = fut.result()
            if err is not None:
                # 重试一次
                time.sleep(RETRY_DELAY)
                e2, item2, err2 = fetch_one(e)
                item, err = item2, err2
            if item is not None:
                ok += 1
                items.append(item)
            else:
                fail += 1
                fb = prev_items.get(e["url"])
                if fb:
                    stale += 1
                    items.append(dict(fb))
                    print("[回退] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))
                else:
                    print("[失败] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))

    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    items, dropped = dedupe(items)
    if len(items) > MAX_ITEMS:
        dropped += len(items) - MAX_ITEMS
        items = items[:MAX_ITEMS]
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


def write_feed(items, stats):
    xml_text = build_rss(items, FEED_URL, stats)
    _write(OUT_FILE, xml_text)

    # 墨水屏版：纯文本标题 + 适度限长 + 条目给足，交给 Dot 云端渲染
    screen_url = FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(SCREEN_OUT)
    screen_text = build_rss(
        items, screen_url, stats,
        channel_title="RSS 头条 · 墨水屏",
        description="多源头条，纯文本标题，已为小屏墨水屏精简（源 %d 个）" % stats["total"],
        title_width=SCREEN_TITLE_WIDTH, limit=SCREEN_ITEMS, minimal=True)
    _write(SCREEN_OUT, screen_text)

    # 折行版：同样纯文本，但标题按行宽折成多行而不是一行截断。
    # 只有当渲染端保留标题里的换行符时才有意义，所以单独成文件，方便和上面那份对比着订阅。
    wrap_text = build_rss(
        items, FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(WRAP_OUT), stats,
        channel_title="RSS 头条 · 折行",
        description="多源头条，标题折成 %d 行，供支持换行的渲染端使用（源 %d 个）"
                    % (SCREEN_WRAP_LINES, stats["total"]),
        title_width=SCREEN_TITLE_WIDTH, limit=SCREEN_ITEMS, minimal=True,
        wrap_lines=SCREEN_WRAP_LINES, wrap_sep=SCREEN_WRAP_SEP)
    _write(WRAP_OUT, wrap_text)
    return xml_text, screen_text, wrap_text


def main():
    entries = read_feed_entries()
    if not entries:
        print("feeds.txt 中没有有效地址")
        return 1
    print("订阅源 %d 个，输出 %s / %s" % (len(entries), OUT_FILE, SCREEN_OUT))

    deadline = time.time() + REFRESH_SECONDS
    rounds = 0
    while True:
        t0 = time.time()
        prev_items = load_prev_items()
        items, stats = run_once(entries, prev_items)
        if not items:
            print("本轮无任何条目，保留旧 feed 不覆盖")
            return 1
        write_feed(items, stats)
        rounds += 1
        print("[第 %d 轮] 成功 %d / 回退 %d / 失败 %d → 条目 %d（去重 %d）耗时 %.1fs"
              % (rounds, stats["ok"], stats["stale"], stats["fail"],
                 stats["items"], stats["dropped"], time.time() - t0))

        left = deadline - time.time()
        if left < 45:                      # 剩余时间不够一轮就收工
            break
        time.sleep(min(60, left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
