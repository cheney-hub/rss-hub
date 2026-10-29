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

环境变量：
  FEEDS_FILE       订阅源列表，默认 ./feeds.txt
  OUT_FILE         输出文件，默认 ./docs/feed.xml
  PREV_FILE        上一份 feed（失败回退用），默认同 OUT_FILE
  FEED_URL         频道 <link>，默认 https://cheney-hub.github.io/rss-hub/feed.xml
  REFRESH_SECONDS  循环刷新到第几秒结束（0 = 只跑一轮），默认 0
  FETCH_TIMEOUT    单源超时秒数，默认 20
  CONCURRENCY      并发数，默认 12
"""
import os
import re
import sys
import time
import gzip
import html
import email.utils
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


# ---------------- 生成 RSS ----------------
def esc(s):
    return html.escape(s or "", quote=True)


def build_rss(items, feed_url, stats):
    now = email.utils.format_datetime(datetime.now(timezone.utc))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>RSS 聚合头条</title>",
        "<link>%s</link>" % esc(feed_url),
        '<atom:link href="%s" rel="self" type="application/rss+xml"/>' % esc(feed_url),
        "<description>多源 RSS 头条聚合，按发布时间倒序（源 %d 个）</description>" % stats["total"],
        "<language>zh-CN</language>",
        "<lastBuildDate>%s</lastBuildDate>" % now,
        "<generator>github-actions/rss-hub</generator>",
        "<ttl>5</ttl>",
    ]
    for it in items:
        lines.append("<item>")
        lines.append("<title>%s</title>" % esc(it["title"]))
        lines.append("<link>%s</link>" % esc(it["link"]))
        if it.get("date_raw"):
            lines.append("<pubDate>%s</pubDate>" % esc(it["date_raw"]))
        elif it.get("ts"):
            lines.append("<pubDate>%s</pubDate>" % email.utils.format_datetime(
                datetime.fromtimestamp(it["ts"], timezone.utc)))
        lines.append('<guid isPermaLink="false">%s</guid>'
                     % esc((it["link"] or it["title"]) + "|" + str(it["src"])))
        if it.get("src_name"):
            lines.append('<source url="%s">%s</source>' % (esc(it["src"]), esc(it["src_name"])))
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


def write_feed(items, stats):
    os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)
    xml_text = build_rss(items, FEED_URL, stats)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml_text)
    os.replace(tmp, OUT_FILE)
    return xml_text


def main():
    entries = read_feed_entries()
    if not entries:
        print("feeds.txt 中没有有效地址")
        return 1
    print("订阅源 %d 个，输出 %s" % (len(entries), OUT_FILE))

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
