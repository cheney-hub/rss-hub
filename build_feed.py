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

分工：显示规则（怎么切、怎么排、怎么拼 RSS）全在 render.py（纯函数，可单测）；
      本文件只管抓取、回退、轮播游标、写文件。改「屏上怎么显示」请去 render.py。

环境变量（带 SCREEN_ 前缀的两个由 render.py 读取，其余在本文件）：
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
import email.utils
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# 显示规则住在 render.py，build_feed 只负责抓取与编排。
from render import (BODY_MAX, ENTITY_FIX, LINE_WIDTH, build_rss, clean_text,
                    dedupe, item_guid)

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

# 轮播：设备版一次只发布一条，按时间窗前进，走完一轮才回头
ROTATE_SECONDS = max(60, int(os.environ.get("ROTATE_SECONDS", "300")))   # 每条驻留 = 设备刷新周期
ROTATE_POOL_MAX = max(0, int(os.environ.get("ROTATE_POOL_MAX", "0")))    # 0 = 池子不限
ROTATE_BUCKETS = max(0, int(os.environ.get("ROTATE_BUCKETS", "0")))      # 一轮最多几片，0 = 池子里有多少片就走多少
ROTATE_STATE = os.environ.get(
    "ROTATE_STATE", os.path.join(os.path.dirname(OUT_FILE) or ".", "rotate.json"))

# 源没写时区时按这个时区解释（默认北京 +08）。写死时区而不是用运行机本地时区，
# 否则本地（+08）与 GitHub runner（UTC）会把同一份数据排出两套顺序。
SOURCE_TZ = timezone(timedelta(hours=float(os.environ.get("SOURCE_TZ_OFFSET", "8"))))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
RETRY_DELAY = 1.0

PER_SOURCE = max(1, int(os.environ.get("PER_SOURCE", "3")))   # 每源取几条
ITEM_LIMIT = max(1, int(os.environ.get("ITEM_LIMIT", "120")))  # 去重后上限，超出按时间砍最旧


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
    """把源里的时间字符串转成 epoch 秒。

    源没写时区时必须自己补：naive 时间默认按北京时间解释（中文媒体惯例），
    不能用运行机器的本地时区 —— 本地是 +08、GitHub runner 是 UTC，
    同一份数据会排出两套顺序，本地测通了线上却是乱的。
    """
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except Exception:
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SOURCE_TZ)
    return dt.timestamp()


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


def _rx(raw, pattern):
    """在原始 XML 片段里取第一个捕获组，取不到返回空串（正则兜底解析用）。"""
    m = re.search(pattern, raw, re.S | re.I)
    return m.group(1) if m else ""


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
            link = (_rx(raw, r"<link\b[^>]*href=\"([^\"]+)\"")
                    or _rx(raw, r"<link\b[^>]*>(.*?)</link>"))
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


# ---------------- 轮播（设备版一条一条轮） ----------------
# 目标：设备每 5 分钟刷新一次，每次都要换一条；全池走完才回头，不重不漏。
#
# 为什么用「时间窗」而不是「跑了第几轮」驱动：
#   后台一小时内可能构建 12 次，也可能因为重跑、手动触发变成 30 次。
#   用计数器就会跳条。用 w = now // ROTATE_SECONDS 做幂等键，
#   同一个 5 分钟窗内构建多少次都只前进一格，节奏只跟墙上时钟走。
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


ROTATE_STATE_VERSION = 2      # 状态结构版本，改了就自动丢弃旧状态（宁可重复一条，不能乱序）


def _bucketize(items, dwell):
    """把池子按 dwell 秒切成时间片，每片取最新一条当代表。

    返回 (片号 → 条目, 片号倒序表)。items 必须已按时间倒序，
    所以 setdefault 拿到的第一条就是片内最新的那条。
    """
    buckets = {}
    for it in items:
        buckets.setdefault(int((it.get("ts") or 0) // dwell), it)
    return buckets, sorted(buckets, reverse=True)


def _next_in_queue(state, keys):
    """从本轮清单里取下一条没播过的片；清单播完就按当前池子重开一轮。

    清单是开轮时拍的快照。池子一直在滚动（每窗新增十几条，最旧的被上限挤掉），
    不快照的话游标往下走两格就发现底下几片已经没了，只能回到顶部 ——
    看起来就是「明明没走完就从头开始」。快照之后一轮就是确定的 N 条。
    """
    live = set(keys)
    shown = set(state.get("shown") or [])
    cycle = int(state.get("cycle") or 0)
    queue = [b for b in (state.get("queue") or []) if b in live]
    cand = [b for b in queue if b not in shown]
    if cand:
        return cand[0], queue, shown, cycle

    queue = keys[:ROTATE_BUCKETS] if ROTATE_BUCKETS > 0 else list(keys)
    if not queue:
        return None, [], set(), cycle
    return queue[0], queue, set(), cycle + 1


def _hold_current(state, buckets, w, prev_w):
    """同一个时间窗内重复构建：屏幕锁死在这一条，不推进。

    这是「后台多跑几轮也不会跳条」的唯一保证。找不到原条目时宁可返回 None
    （调用方保留上一版 feed-wrap 不动），也不要在 5 分钟内换两条。
    """
    if w > prev_w:
        return "go", None
    by_guid = {item_guid(it): it for it in buckets.values()}
    cur = by_guid.get(state.get("current"))
    if cur is not None:
        return "hold", cur
    b = int(state.get("bucket") or 0)
    if b in buckets:
        return "hold", buckets[b]
    return "skip", None


def rotate_pick(items, state, now=None):
    """取「这一窗该上屏的那一条」，返回 (条目 或 None, 新状态, 说明)。

    items 必须已按发布时间倒序（run_once 保证）。

    为什么按「时间片」而不是「条目」轮：
      每 5 分钟各源新产出十几条，而屏幕 5 分钟只放得下 1 条（产能是消费的十几倍）。
      若按条目「取未显示里最新的一条」，新条目永远比旧的新 —— 每一窗都在追新，
      池子里剩的上百条还没轮到就被 120 条上限挤出去，等于从来没上过屏。
      按时间片轮就没有这个问题：一个片只出一条代表，产出和消费对齐，
      队列一定走得完，走完才回头。

    规则（对应「按时间排序 → 打标记 → 下一窗取未标记的最新一条 → 全部标完重来」）：
      1. 把池子按 dwell 切成时间片，片内取最新一条作为该片代表；
      2. 每个片只上屏一次，记在 shown 里（存片号不存 guid，池子滚动也不丢标记）；
      3. 一个片一个窗，沿清单往下走；清单走完才拿当前池子开新一轮；
      4. 同一时间窗内重复调用不推进。
    """
    dwell = ROTATE_SECONDS
    w = int((now if now is not None else time.time()) // dwell)
    # 节拍改过（60 秒 ↔ 300 秒）或状态结构升级：片号基数变了，重新起轮
    if (state.get("version") != ROTATE_STATE_VERSION
            or int(state.get("dwell") or dwell) != dwell):
        state = {}
    prev_w = int(state.get("window") or 0)

    buckets, keys = _bucketize(items, dwell)
    if not buckets:
        return None, state, "池子为空"

    action, held = _hold_current(state, buckets, w, prev_w)
    if action == "hold":
        return held, state, "同窗保持"
    if action == "skip":
        return None, state, "同窗内该条离开池子，保持上一版"

    pick_b, queue, shown, cycle = _next_in_queue(state, keys)
    if pick_b is None:
        return None, state, "池子为空"
    pick = buckets[pick_b]

    new_state = {
        "version": ROTATE_STATE_VERSION,
        "window": w,
        "bucket": pick_b,
        "current": item_guid(pick),
        "queue": queue,
        "shown": sorted(shown | {pick_b}),
        "cycle": cycle,
        "pool": len(buckets),           # 池子里有多少个时间片
        "items": len(items),            # 池子里有多少条原始条目
        "dwell": dwell,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": (pick.get("title") or "")[:40],
        "at": datetime.fromtimestamp(pick_b * dwell, timezone.utc).strftime("%m-%d %H:%M"),
    }
    note = ("新窗推进（%s 片 · 本轮 %d/%d · 池 %d 片/%d 条 · 第 %d 轮）"
            % (new_state["at"], len(new_state["shown"]), len(queue),
               len(buckets), len(items), cycle))
    return pick, new_state, note


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
    """写两份：完整版 feed.xml（全部条目）+ 设备版 feed-wrap.xml（只有轮播到的那一条）。

    pick 为 None 表示这一窗不推进（同窗重跑，且原条目已离开池子）：
    此时只刷新完整版，设备版保留上一版不动 —— 5 分钟内换两条比晚一条更糟。
    """
    _write(OUT_FILE, build_rss(items, FEED_URL, stats))
    if pick is None:
        return

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
        if pick is not None:
            save_rotate_state(state)
        rounds += 1
        print("[第 %d 轮] 成功 %d / 回退 %d / 失败 %d → 条目 %d（去重 %d）耗时 %.1fs"
              % (rounds, stats["ok"], stats["stale"], stats["fail"],
                 stats["items"], stats["dropped"], time.time() - t0))
        print("        上屏：%s ｜ %s"
              % (note, (pick.get("title") or "")[:36] if pick else "（设备版保持上一版）"))

        left = deadline - time.time()
        if left < 45:                      # 剩余时间不够一轮就收工
            break
        time.sleep(min(ROTATE_SECONDS, left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
