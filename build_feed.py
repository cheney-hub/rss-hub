#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 聚合器 —— 专为 Dot (Quote/0) 墨水屏设备开发

读 feeds.txt（注释行 = 中文源名，下一行 = 地址）
→ 每源抓最新 1 条 → 跨源去重 → 按发布时间倒序生成 RSS 2.0

输出两份：
  docs/feed.xml       完整版：标题原样，给电脑 / 手机上的 RSS 阅读器
  docs/feed-wrap.xml  设备版：标题只放屏上真正显示得下的那部分，
                      余下的文字接进 <description>（正文），
                      填进 Dot App 的「循环内容」列表即可

特性：
  * 零第三方依赖，Python 3.8+
  * 只读通路：只抓 RSS、只写静态文件，不向任何设备/第三方接口发起写请求，不需要任何密钥
  * 单源失败时回退到上一份 feed 中该源的条目（不会凭空丢源）
  * 单次运行内可循环多轮（REFRESH_SECONDS）
  * 自动识别 UTF-8 / GB18030 编码、修补常见 HTML 实体

环境变量：
  FEEDS_FILE        订阅源列表，默认 ./feeds.txt
  OUT_FILE          完整版输出，默认 ./docs/feed.xml
  FEED_URL          频道 <link>，默认 https://cheney-hub.github.io/rss-hub/feed.xml
  REFRESH_SECONDS   循环刷新到第几秒结束（0 = 只跑一轮），默认 0
  FETCH_TIMEOUT     单源超时秒数，默认 20
  CONCURRENCY       并发数，默认 12
  SCREEN_OUT        设备版输出，默认与 OUT_FILE 同目录的 feed-wrap.xml
  SCREEN_LINE_WIDTH 标题行宽度（全角字符数），默认 13
                    —— 设备 296×152 屏是大字号单行，实测 13 个全角字满宽，
                    再多就被 Dot 云端从右边切掉。所以标题只放得下多少给多少。
  SCREEN_BODY_MAX   正文（description）最多保留多少全角字，默认 60
                    —— 标题装不下的部分原文接进正文；太长的正文尾端补省略号。
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
FEED_URL = os.environ.get("FEED_URL", "https://cheney-hub.github.io/rss-hub/feed.xml")
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "0"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "12")))

# 设备版（Dot Quote/0）：标题截到屏宽，余下文字进正文，交给 Dot 云端渲染成位图
SCREEN_OUT = os.environ.get(
    "SCREEN_OUT", os.path.join(os.path.dirname(OUT_FILE) or ".", "feed-wrap.xml"))
LINE_WIDTH = float(os.environ.get("SCREEN_LINE_WIDTH", "13"))
BODY_MAX = max(0.0, float(os.environ.get("SCREEN_BODY_MAX", "60")))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
RETRY_DELAY = 1.0

ITEM_LIMIT = 60         # 输出条目上限，避免无限膨胀
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
# 只放中文分隔符：半角「.」「:」可能属于 20.15 / 12:30 这类数字，剥掉就把数字弄残了。
HEAD_STRIP = "，,、；;。"

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
    t = plain_text(title)
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
        head, tail = split_title(it["title"]) if screen else (it["title"], "")
        lines.append("<item>")
        lines.append("<title>%s</title>" % esc(head))
        # 标题装不下的原文接进正文：云端正文区只要能渲染，屏上就能接着读到后半句
        if tail:
            lines.append("<description>%s</description>" % esc(tail))
        lines.append("<link>%s</link>" % esc(it["link"]))
        if it.get("date_raw"):
            lines.append("<pubDate>%s</pubDate>" % esc(it["date_raw"]))
        elif it.get("ts"):
            lines.append("<pubDate>%s</pubDate>" % email.utils.format_datetime(
                datetime.fromtimestamp(it["ts"], timezone.utc)))
        if screen:
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
    if len(items) > ITEM_LIMIT:
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


def write_feed(items, stats):
    """写两份：完整版 feed.xml + 设备版 feed-wrap.xml。"""
    _write(OUT_FILE, build_rss(items, FEED_URL, stats))

    wrap_url = FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(SCREEN_OUT)
    _write(SCREEN_OUT, build_rss(
        items, wrap_url, stats,
        channel_title="RSS 头条 · 小屏",
        description="多源头条，纯文本；标题只放屏上显示得下的 %g 全角字，"
                    "余下原文接在正文里，源 %d 个" % (LINE_WIDTH, stats["total"]),
        screen=True))


def main():
    entries = read_feed_entries()
    if not entries:
        print("feeds.txt 中没有有效地址")
        return 1
    print("订阅源 %d 个，输出 %s / %s（标题 %g 全角、正文最多 %g 全角）"
          % (len(entries), OUT_FILE, SCREEN_OUT, LINE_WIDTH, BODY_MAX))

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
