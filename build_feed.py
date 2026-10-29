#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 聚合器 —— 专为 Dot (Quote/0) 墨水屏设备开发

读 feeds.txt（注释行 = 中文源名，下一行 = 地址）
→ 每源抓最新 PER_SOURCE 条 → 跨源去重 → 按发布时间倒序、超 ITEM_LIMIT 砍最旧 → 生成 RSS 2.0

输出三份：
  docs/feed.xml       完整版：标题原样、全部条目按时间倒序 + 摘要正文，给电脑 / 手机上的 RSS 阅读器
  docs/feed-wrap.xml  设备版：只出「当前该显示的那一条」，标题截到屏宽，
                      余下文字接进 <description>（正文）；填进 Dot App 的「循环内容」即可
  docs/rotate.json    轮播状态：哪一条已展示、当前在第几窗，随 Pages 一起发布

轮播规则（为什么设备版只有一条）：
  Dot 云端只渲染文档里的第一条，按时间倒序排的话屏幕永远停在最新那条，
  其余条目一辈子轮不到。所以设备版只发一条，选谁由「已显示标记」决定：

    把池子按 ROTATE_SECONDS 切成时间片，片内取最新一条当代表
    每窗锚定当前时间窗：取「全池没播过里最新的一片」上屏，标注已播
      —— 没有新内容时就往旧里找，没有年龄上限（宁可播旧的也不空屏，见 _pick_bucket）
    全池都播过 → 清掉所有标注，从池子最新的片重新开始
    同一时间窗内（w = now // ROTATE_SECONDS）重复构建不推进，后台多跑几轮也不跳条

  关键词是「锚定当前窗」：每窗都从「现在」往回看，而不是沿着一张开轮时拍好的清单往下走。
  后者第 k 窗必播「第 k 新」，延迟随轮次线性增长（实测 4.3 小时），新条目本轮一条都播不到。

特性：
  * 零第三方依赖，Python 3.8+
  * 只读通路：只抓 RSS、只写静态文件，不向任何设备/第三方接口发起写请求，不需要任何密钥
  * 单源失败时回退到上一份 feed 中该源的条目（不会凭空丢源）
  * 单次运行内可循环多轮（REFRESH_SECONDS）
  * 自动识别 UTF-8 / GB18030 / HTTP 头声明的编码，修补常见 HTML 实体

分工：显示规则（怎么切、怎么排、怎么拼 RSS）全在 render.py（纯函数，可单测）；
      本文件只管抓取、回退、轮播游标、写文件。改「屏上怎么显示」请去 render.py。

版本号在 version.py（唯一真源），`python3 build_feed.py --version` 可直接打印。

环境变量（带 SCREEN_/FEED_BODY_ 前缀的由 render.py 读取，其余在本文件）：
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
  SCREEN_BODY_MAX    设备版正文（description）最多保留多少全角字，默认 90
                     —— 正文 = 标题装不下的余文 + 原 feed 的 description/摘要原文；
                     296×152 屏正文区约 4 行 × 22 全角 ≈ 88 字，取 90 刚好填满。
                     太长的正文尾端会补省略号。
  FEED_BODY_MAX      完整版正文最多保留多少全角字，默认 300（0 = 不限）
                     —— 只用来防止单条摘要把整份文件撑大，与屏宽无关。
  ROTATE_SECONDS     每条在屏上驻留多少秒（= 设备刷新周期），默认 300
                     —— 调大等于放慢轮播。Pages CDN 缓存 600 秒，
                     若真机上出现「同一条连显两次」，把它改成 600。
  ROTATE_POOL_MAX    轮播池上限，默认 0 = 不限（= 去重后的全部条目）
  ROTATE_STATE       轮播状态文件，默认与 OUT_FILE 同目录的 rotate.json
  ROTATE_STATE_URL   线上状态地址，默认 FEED_URL 同目录的 rotate.json
  SOURCE_TZ_OFFSET   源没写时区时按哪个时区解释，默认 8（北京 +08）
"""
import os
import re
import sys
import json
import time
import gzip
import zlib
import email.utils
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# 显示规则住在 render.py，build_feed 只负责抓取与编排。
from render import (BODY_MAX, ENTITY_FIX, LINE_WIDTH, build_rss, clean_text,
                    dedupe, item_guid)
from version import __version__

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

# 轮播：设备版一次只发布一条，每窗取「池子里最新的、还没播过的那一片」
ROTATE_SECONDS = max(60, int(os.environ.get("ROTATE_SECONDS", "300")))   # 每条驻留 = 设备刷新周期
ROTATE_POOL_MAX = max(0, int(os.environ.get("ROTATE_POOL_MAX", "0")))    # 0 = 池子不限
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
def read_feed_entries(path=None, skipped=None):
    """解析订阅源清单，返回 [{"name": 源名, "url": 地址}]，按出现顺序、地址去重。

    格式约定（用户偏好，别改成行尾注释）：
        注释行（# 开头）= 下一行的源中文名；分组标题（==== / ----）不算名字；
        地址行         = http(s) 开头的地址，行尾 # 之后的部分被当注释丢掉。

    skipped 传一个 list 进来会收集「看着像地址但没用上」的行，
    调用方可以据此提示——以前这类行是静默丢掉的，写错一个字母要排查很久。
    """
    path = path or FEEDS_FILE
    entries, pending = [], ""
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            raw = line.strip()
            if not raw:
                continue
            if raw.startswith("#"):
                txt = raw.lstrip("#").strip()
                if txt and not txt.startswith(("=", "-", "*")):
                    pending = re.sub(r"[（(].*?$", "", txt).strip()
                continue
            url = raw.split("#")[0].strip()
            if url.startswith("http"):
                entries.append({"name": pending or url, "url": url})
                pending = ""
            elif skipped is not None:
                skipped.append("%s:%d %s" % (os.path.basename(path), lineno, raw[:60]))
    seen, out = set(), []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            out.append(e)
    return out


# ---------------- HTTP ----------------
def http_get(url):
    """抓一个地址，返回 (状态码, 已解压的字节串, 响应头声明的字符集或 None)。

    源站大多按 Accept-Encoding 压缩，但各家回的算法不一样：gzip 最常见，
    少数（尤其老 nginx）回 deflate。以前只解 gzip，碰上 deflate 就把压缩流当
    正文去解码，解析必然失败、该源整轮走回退——所以这里两种都解。
    """
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
        elif "deflate" in enc:
            # 规范里的 deflate 是 zlib 包装；有实现直接发裸 deflate，两种都试
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        return resp.status, body, resp.headers.get_content_charset()


def decode_body(body, charset=None):
    """字节串 → 文本。顺序：HTTP 头声明的编码 → UTF-8 → GB18030 → 带替换的 UTF-8。

    以前不看 HTTP 头，全靠 UTF-8/GB18030 两个候选去试。GBK 系站点碰对了，
    但声明为 Big5 / Shift-JIS 的源会静默解成乱码（解码不报错，只是字全错）。
    先信声明，声明不对再按中文站的两个常见编码兜底。
    """
    cands = []
    if charset:
        cands.append(charset.split(";")[0].strip().strip('"').lower())
    cands += ["utf-8", "gb18030"]
    for enc in cands:
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


# ---------------- 解析 ----------------
def _local(tag):
    """去掉命名空间前缀：{http://www.w3.org/2005/Atom}entry → entry。"""
    return tag.rsplit("}", 1)[-1].lower()


def _index(node):
    """一次遍历把子树收成「标签名（小写）→ 文本」，同名只留第一个非空值。

    以前每个字段各扫一遍全树（title/link/date/summary 共 4 遍），
    大源（单文件 500 条）光扫描就是两千次节点遍历。这里只走一遍。

    只留第一个非空值是刻意的：RSS 里偶见重复标签（两个 <category>、两个 <link>），
    取第一个和阅读器的习惯一致，也让结果与文档顺序绑定、可预测。
    """
    idx = {}
    for child in node.iter():
        name = _local(child.tag)
        if name in idx:
            continue
        text = "".join(child.itertext()).strip()
        if text:
            idx[name] = text
    return idx


def _pick(idx, names):
    """按 names 给出的优先级，取第一个有值的字段（等价于「原文优先」）。"""
    for n in names:
        v = idx.get(n)
        if v:
            return v
    return ""


def _parse_date(s):
    """把源里的时间字符串转成 epoch 秒；认不出就返回 None（排序时当 0 处理）。

    源没写时区时必须自己补：naive 时间默认按北京时间解释（中文媒体惯例），
    不能用运行机器的本地时区 —— 本地是 +08、GitHub runner 是 UTC，
    同一份数据会排出两套顺序，本地测通了线上却是乱的。

    这里**不**处理「发布时间在未来」的时间戳（源时钟错乱会造出 2030 年）：
    钳到抓取时刻看着自然，但抓取每 5 分钟重跑一次、钳值跟着往前跑，片号永远是「本窗」，
    轮播每窗都把它当成没播过的最新片选中 —— 同一条每窗重播一次（实测连跑 6 窗全是它），
    比不钳更糟。未来时间戳由 `_pickable()` 在轮播时按「时间还没到就先不播」处理。
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
        if e.startswith(("&amp;", "&lt;", "&gt;", "&quot;", "&apos;")):
            return e
        return " "
    # 只处理「&amp;」这类命名实体；数字实体（&#8212;）正则匹配不到，ET 自己能处理
    xml_text = re.sub(r"&[a-zA-Z][a-zA-Z0-9]{1,8};", fix, xml_text)
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    # 去掉声明里的 encoding，避免 ET 对 str 报错或按错误编码解码
    return re.sub(r'(<\?xml[^>]*?)\s+encoding="[^"]*"', r"\1", xml_text, count=1)


def _rx(raw, pattern):
    """在原始 XML 片段里取第一个捕获组，取不到返回空串（正则兜底解析用）。"""
    m = re.search(pattern, raw, re.S | re.I)
    return m.group(1) if m else ""


def parse_items(xml_text, source_url, limit=PER_SOURCE):
    """解析一个源，按发布时间倒序取前 limit 条；每条都带摘要原文（summary）。

    两条通路：
      * 正常走 ElementTree，命名空间无关（RSS 2.0 / Atom / RDF 都能读）；
      * ET 报错时退回正则抽 <item>/<entry> 块——线上确实有源发不合法的 XML
        （未转义的 & 、未闭合标签），整源丢弃的代价比少几个字段大。
    """
    try:
        root = ET.fromstring(_sanitize(xml_text))
        nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    except ET.ParseError:
        blocks = re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", xml_text,
                            re.S | re.I)
        if not blocks:
            raise RuntimeError("XML 解析失败且无 item/entry")
        nodes = [{"__raw__": b} for b in blocks]

    if not nodes:
        raise RuntimeError("未解析到条目")

    def entry_of(node):
        if isinstance(node, dict):                # 正则兜底通路
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
        else:                                     # 正常通路
            idx = _index(node)
            title = idx.get("title", "")
            link = ""
            for c in node.iter():                 # Atom 的地址在 href 属性上
                if _local(c.tag) == "link":
                    link = (c.get("href") or "".join(c.itertext()) or "").strip()
                    if link:
                        break
            date_raw = _pick(idx, ("pubdate", "published", "updated", "date"))
            # 正文来源按「原文优先」排：content:encoded > description > summary > content
            summary = _pick(idx, ("encoded", "description", "summary", "content"))
        return {"title": clean_text(title), "link": (link or "").strip(),
                "date_raw": clean_text(date_raw), "ts": _parse_date(clean_text(date_raw)),
                "summary": clean_text(summary)}

    items = [entry_of(n) for n in nodes]
    for it in items:
        it["src"] = source_url
        if it["ts"] is None:
            it["ts"] = 0
    # 次级键用 guid：同一时间戳的条目在不同次抓取里先后次序可能不同（源自己也在抖），
    # 而排序是稳定的 —— 次序不定就等于「同样的数据排出两个池子」，
    # 而 _bucketize 取片内第一条当代表，同一片这次选 A、下次选 B，屏上会莫名换内容。
    items.sort(key=lambda x: (-(x["ts"] or 0), item_guid(x)))
    return items[:limit]


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
# 目标：设备每 5 分钟刷新一次，每次都要换一条，而且换上去的得是「现在的事」。
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


ROTATE_STATE_VERSION = 3      # 状态结构版本，改了就自动丢弃旧状态（宁可重复一条，不能乱序）
# 容忍的墙钟回拨窗数（1 窗 = ROTATE_SECONDS，2 窗 = 10 分钟）：NTP 校正会往回跳几十秒，
# 那不算坏状态；超过这个幅度才认定 rotate.json 的 window 是坏的（详见 rotate_pick）。
CLOCK_SKEW_WINDOWS = 2


def _bucket_of(item, dwell):
    """条目的时间片号（epoch 秒 // 片长）。片号是轮播里唯一的坐标。"""
    return int((item.get("ts") or 0) // dwell)


def _bucketize(items, dwell):
    """把池子按 dwell 秒切成时间片，每片取最新一条当代表。

    返回 (片号 → 条目, 片号倒序表)。items 必须已按时间倒序，
    所以 setdefault 拿到的第一条就是片内最新的那条。

    这里不剔除「片号大于当前窗」的片：hold 要能在时钟回拨时仍认出屏上那一条
    （回拨后它的片号会显得「在未来」）。未来片只在「选下一条」时排除，见 rotate_pick。
    """
    buckets = {}
    for it in items:
        buckets.setdefault(_bucket_of(it, dwell), it)
    return buckets, sorted(buckets, reverse=True)


def _pickable(keys, w):
    """能参与「选下一条」的片号：发布时间已到（片号 ≤ 当前窗）。

    剔掉「片号 > w」的片，也就是发布时间在未来的条目：源时钟错乱（时区写错、
    服务器漂移、模板把年份拼成 2030）会造出未来时间戳，这种片号永远最大，
    每窗都是「最近几片里最新的未播片」→ 屏幕被它独吞，lag 还变成巨大负数。
      为什么不能改在解析层「钳到抓取时刻」：抓取每 5 分钟重跑一次，钳值跟着往前跑，
      片号永远是本窗、永远「没播过」—— 同一条每窗重播一次（实测连跑 6 窗全是它），
      比不钳更糟。按「时间还没到就先不播」处理后：源的时间真走到那天自然上屏，
      一直错就一直不上设备版（条目仍完整留在 feed.xml 里），lag 回到非负。
      解析失败 ts=0 的条目落在片 0，排在池子最末，全池往回找也基本轮不到
      （只有池子退化成只剩那一条时才会播它）。

    只管「选下一条」：hold 必须看全量片 —— 时钟回拨后屏上那条的片号会显得在未来，
    这里一并剔掉的话，回拨期间就再也认不出屏上内容，会误判成「该条离场」。
    """
    return [b for b in keys if b <= w]


def _pick_bucket(state, keys):
    """选出这一窗该上屏的片号，返回 (片号 或 None, 已播集合, 轮次)。

    规则只有一条：**播池子里最新的、还没播过的那一片**。keys 已按片号倒序、
    已由 _pickable() 剔掉未来片，所以 `keys[0]` 就是「最新的可播片」。
    全池都播过才清标记、从最新的片重开一轮（cycle + 1）。这一条与用户原规则一致。

    签名里没有窗号：候选集已由调用方按窗过滤（`_pickable(keys, w)`），这里再收一个
    `w` 就是一个不参与决策的形参 —— 上一版正是「有参数、看起来像旋钮、其实不影响结果」
    的东西把人骗了一轮，别再来一个。

    为什么不是「开轮时拍一张清单快照、从新到旧推进」：
      快照 + 顺序推进等价于「第 k 窗播第 k 新的那条」，延迟随轮次线性增长 ——
      线上实测走到清单尾部已滞后 75 分钟，24 小时仿真中位 120 分、最差 4.3 小时；
      更糟的是冻结的清单里没有运行期间新涌入的条目，本轮一条新消息都播不到。
      「取全池最新的未播片」天然锚定「现在」：有新内容就播新的，延迟不随轮次累积
      （稳态 lag 中位 1 片），新条目下一窗就能上屏。

    为什么不给「往旧里找」设年龄上限（刻意的，不是漏了参数）：
      墨水屏不显示时间戳，「内容停住」和「设备坏了」在用户眼里是同一件事，
      所以宁可播一条旧闻也不空屏。代价必须说清楚：极端低产能时会真的播很旧的片 ——
      全源停 2 小时 lag 36 片，0.4 条/窗的最坏场景实测到过 22.9 小时（见 README「代价」）。
      另一头是覆盖率：白天新内容不断，几小时前的旧条目基本轮不到，会被池子上限挤掉
      （仿真约 35%）。墨水屏上「看到 3 小时前的快讯」比「少看几条旧闻」更伤，这个交换是刻意的。

    这里曾经有个 ROTATE_MAX_LAG 参数，把选择拆成「先看最近 N 片，取不到才放宽到全池」。
    它从未生效：第一级的候选集是第二级的子集、两处又都取最大片号，所以两级给出的永远是
    同一条。穷举 5 万组状态 + 24 小时端到端，参数取 0/1/6/24 结果逐项相同，
    于是连同环境变量一起删掉了 —— 留着只会让排障的人对着一个假旋钮调半天。
    """
    shown = {int(b) for b in (state.get("shown") or [])}
    cycle = int(state.get("cycle") or 1)        # 1 = 第 1 轮；全池播完重开时 +1
    if not keys:
        return None, shown, cycle

    fresh = next((b for b in keys if b not in shown), None)
    if fresh is not None:
        return fresh, shown, cycle

    # 全池都播过：按原规则清标记重开一轮（cycle + 1），从池子最新的片重新开始。
    # 唯一的例外是「刚刚上屏那一片」——池子小、换血快时 keys[0] 往往就是它，
    # 清完标记立刻又选它，屏幕上看起来像「没换」。这时改从「最新的另一片」起轮。
    # 只跳过这一片、不保留它的标记：留着的话它要到下一轮才回来，一轮就会少播一条。
    prev_b = int(state.get("bucket") or 0)
    restart = next((b for b in keys if b != prev_b), None)
    if restart is None:                  # 池子只有这一片：只能重播同一条（无解，但不能崩）
        restart = keys[0]
    return restart, set(), cycle + 1


def _trim_shown(shown, live, pick_b):
    """把 shown 裁成「池子里还在的片 + 刚上屏那一片」，避免它随运行时间无限增长。

    为什么不按「片号 > 当前窗 - N 片」裁（先这么写过，被仿真否掉了）：「取全池最新的未播片」这条
    规则让游标能走到池子里任何一片，按窗口裁就会抹掉「几小时前播过、但仍可能被再次选中」的标记，
    于是同一条隔几窗重播、`cycle` 永远数不到头、lag 一路上涨（实测：池子冻住 30 片连跑 200 窗，
    只出了 13 个不同条目、cycle 卡在 1；按「还在池子里」裁是 30 片整轮循环、cycle 正常递增）。

    按「还在池子里」裁同样有界：片数上限就是条目上限（`ITEM_LIMIT`，默认 120），所以 shown
    最多一百多个整数、不随时间增长；离开池子的片再也取不到代表条目，标记没用。
    `pick_b` 必须保留：它是刚上屏的那一片，抹掉它下一窗就会把同一条再播一遍。
    """
    return {pick_b} | {b for b in shown if b in live}


def _hold_current(state, buckets, w, prev_w):
    """同一个时间窗内重复构建：屏幕锁死在这一片，不推进。返回 (动作, 条目, 待落盘 guid)。

    保证的是「游标不推进」：一个 5 分钟窗内跑多少遍构建，都还停在同一个时间片、
    不会跳到下一片，也不会因为池子滚动而回退。找不到原条目时宁可返回 None
    （调用方保留上一版 feed-wrap 不动），也不要凭空换一条。

    查找顺序是「片号优先、guid 兜底」：片号是这一窗的稳定坐标；guid 只在
    片号已经不在池子里时用得上。反过来的话有个漏洞——同一片内后到的条目会
    把该片的代表换掉（setdefault 取第一条，新的更早入表就顶掉旧的），
    guid 找不到就认为该条离场，进而让本窗换了内容。

    「不推进」不等于「内容绝对不变」：片内代表被顶掉时（片号还在池子里，但
    `buckets[b]` 已经不是 `state["current"]` 那一条）选择「接受更新」——
    照播新代表，并把 guid 一并交回调用方改写状态。为什么不选「保持旧内容（返回 skip）」：
      skip 会让这一窗停在上一版，下一窗片号一变又正常推进，而旧 guid 滞留在状态里，
      `rotate.json` 的 title/current 与实际屏上内容长期不一致 —— 排障时按它推断会得出
      错误结论；片号失效后走 guid 兜底也会因为找不到那条而误判「该条离场」，白空转一版。
    代价：同一 5 分钟窗内若又抓到本片更新的条目，屏上那条会跟着换一次
      （仍是同一个时间片的内容，不是另一条新闻）。接受它，是因为「片内更新」正是
      新机制想要的「最新优先」，而状态与真实内容一致比省这一次切换更值钱。
    片号命中且代表的 guid 没变时返回 None，表示状态无需改写。
    """
    if w > prev_w:
        return "go", None, None
    b = int(state.get("bucket") or 0)
    if b in buckets:
        cur = buckets[b]
        guid = item_guid(cur)
        return "hold", cur, (guid if guid != state.get("current") else None)
    by_guid = {item_guid(it): it for it in buckets.values()}
    cur = by_guid.get(state.get("current"))
    if cur is not None:
        return "hold", cur, None      # 片号失效但条目还在池子里：内容不变，状态不用改
    return "skip", None, None


def _build_state(w, pick_b, pick, shown, cycle, dwell, n_buckets, n_items):
    """组装本轮要落盘的状态。

    字段含义（OPERATIONS.md 第 15 节按这份解释排障，改字段名请同步改文档）：
      window  时间窗号 = now // dwell，幂等键，同一窗内不推进
      bucket  当前上屏的片号
      lag     落后当前窗几片（= window - bucket），排障第一眼就看这一个数字
      current 当前上屏条目的 guid，片号失效时用它兜底找回
      shown   已播过的片号（只留池子里还在的片，见 _trim_shown）
      cycle   第几轮（1 起），全池播完重开时 +1
      pool    可播的时间片数（不含发布时间在未来的片）；items 池子有多少条原始条目
    """
    return {
        "version": ROTATE_STATE_VERSION,
        "window": w,
        "bucket": pick_b,
        "lag": w - pick_b,
        "current": item_guid(pick),
        "shown": sorted(shown),
        "cycle": cycle,
        "pool": n_buckets,
        "items": n_items,
        "dwell": dwell,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": (pick.get("title") or "")[:40],
        "at": datetime.fromtimestamp(pick_b * dwell, timezone.utc).strftime("%m-%d %H:%M"),
    }


def rotate_pick(items, state, now=None):
    """取「这一窗该上屏的那一条」，返回 (条目 或 None, 新状态, 说明)。

    items 必须已按发布时间倒序（run_once 保证）。

    为什么按「时间片」而不是按「条目」轮：
      每 5 分钟各源新产出十几条，而屏幕 5 分钟只放得下 1 条（产能是消费的十几倍）。
      若按条目「取未显示里最新的一条」，新条目永远比旧的新 —— 每一窗都在追新，
      池子里剩的上百条还没轮到就被 120 条上限挤出去，等于从来没上过屏。
      按时间片轮，一个片只出一条代表，产出和消费才对得上。

    规则（为什么这么定、代价是什么见 _pick_bucket）：
      1. 把池子按 dwell 切成时间片，片内取最新一条作为该片代表；
      2. 每窗取「全池没播过里最新的一片」上屏，片号记进 shown
         （存片号不存 guid，池子滚动也不丢标记）；
      3. 全池都播过 → 清空 shown，从池子最新的片重新开始（cycle + 1）；
      4. 同一时间窗内重复调用不推进。

    第 2~3 步只在 `_pickable()` 给出的片里选：发布时间在未来的片不参与（原因见该函数）。
    """
    dwell = ROTATE_SECONDS
    w = int((now if now is not None else time.time()) // dwell)
    # 节拍改过（60 秒 ↔ 300 秒）或状态结构升级：片号基数变了，重新起轮
    if (state.get("version") != ROTATE_STATE_VERSION
            or int(state.get("dwell") or dwell) != dwell):
        state = {}
    prev_w = int(state.get("window") or 0)
    # 状态里的 window 比当前窗还大，只可能是「写它的那次运行时钟错了」或文件被写坏：
    # 不丢的话 w > prev_w 永远为假 → 一直 hold → window 永不前进 → 屏幕永久卡死，
    # 且每次运行都原样读回这份坏状态，自己治不了自己。容忍 CLOCK_SKEW_WINDOWS 窗以内的
    # 回拨（NTP 校正会让墙钟往回跳几十秒，冻结一版就好），跳得更多才认定状态坏了。
    if prev_w > w + CLOCK_SKEW_WINDOWS:
        state, prev_w = {}, 0

    buckets, keys = _bucketize(items, dwell)
    if not buckets:
        return None, state, "池子里没有可播的片"

    action, held, guid = _hold_current(state, buckets, w, prev_w)
    if action == "hold":
        if guid is not None:                     # 片内代表被顶掉：内容换了，状态跟着改
            state = dict(state, current=guid, title=(held.get("title") or "")[:40])
            return held, state, "同窗保持（片内代表更新）"
        return held, state, "同窗保持"
    if action == "skip":
        return None, state, "同窗内该条离开池子，保持上一版"

    # 只在选条时排除未来片（hold 要看全量片，见 _pickable）；顺便数一下被排除了几条
    live = _pickable(keys, w)
    late = sum(1 for it in items if _bucket_of(it, dwell) > w)
    pick_b, shown, cycle = _pick_bucket(state, live)
    if pick_b is None:
        return None, state, "池子里没有可播的片"
    pick = buckets[pick_b]

    shown = _trim_shown(shown | {pick_b}, set(live), pick_b)
    new_state = _build_state(w, pick_b, pick, shown, cycle, dwell,
                             len(live), len(items))
    return pick, new_state, _advance_note(new_state, shown, live, items, late)


def _advance_note(st, shown, live, items, late):
    """「新窗推进（…）」这行日志。

    被排除的未来条目数只在 > 0 时追加：剔除的后果是「某个源在设备版上悄悄消失」，
    不打出来就没人发现源的时间戳坏了。这一句是那件事唯一的出口（`_pickable` 是纯函数，
    不该自己打印）。`main()` 已经把它和标题打在同一行，追加长一点不影响排版。
    """
    note = ("新窗推进（%s 片 · 落后 %d 片 · 已播 %d 片 · 池 %d 片/%d 条 · 第 %d 轮）"
            % (st["at"], st["lag"], len(shown), len(live), len(items), st["cycle"]))
    if late:
        note += " · 另有 %d 条时间戳晚于当前，已排除出设备版轮播" % late
    return note


# ---------------- 单轮采集 ----------------
def run_once(entries, prev_items):
    """抓全部源 → 排序 → 去重 → 截断，返回 (条目列表, 统计)。

    重试放在 worker 里做：以前在主线程的 as_completed 循环里 sleep 后重试，
    43 个源里有 5 个超时就白等 5 秒，而且等待期间其它已完成的结果全都压着不处理。
    放进 worker 后并发的 12 路各睡各的，主线程只管收结果。

    排序的次级键必须是 guid：条目是 as_completed 并发收上来的，数组顺序取决于
    各源返回的快慢，而排序是稳定的 —— 不补次级键，同一时间戳的多条在两次运行里
    会排出不同次序。轮播按片取「第一条」当代表，次序不定就是同一片这次选 A、
    下次选 B，屏上表现为「内容莫名换了」。补上 guid 后，同一批数据只有一个池子顺序。
    """
    items, ok, fail, stale = [], 0, 0, 0

    def fetch_one(e):
        try:
            status, body, charset = http_get(e["url"])
            if status != 200:
                raise RuntimeError("HTTP %s" % status)
            got = parse_items(decode_body(body, charset), e["url"], PER_SOURCE)
            for it in got:
                it["src_name"] = e["name"]
            return e, got, None
        except Exception as err:
            return e, None, err

    def fetch_with_retry(e):
        """失败一次后隔 RETRY_DELAY 再试一次：源站偶发 5xx / 连接重置很常见。"""
        _, got, err = fetch_one(e)
        if got:
            return e, got, None
        time.sleep(RETRY_DELAY)
        return fetch_one(e)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for fut in as_completed([ex.submit(fetch_with_retry, e) for e in entries]):
            e, got, err = fut.result()
            if got:
                ok += 1
                items.extend(got)
                continue
            fail += 1
            fb = prev_items.get(e["url"])
            if fb:
                stale += 1
                items.extend(dict(x) for x in fb)
                print("[回退] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))
            else:
                print("[失败] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))

    items.sort(key=lambda x: (-(x.get("ts") or 0), item_guid(x)))
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
    pick 非 None 时设备版只写一条：云端本来就只渲染第一条，多写的部分纯属浪费体积。
    """
    _write(OUT_FILE, build_rss(items, FEED_URL, stats))
    if pick is None:
        return

    wrap_url = FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(SCREEN_OUT)
    _write(SCREEN_OUT, build_rss(
        [pick], wrap_url, stats,
        channel_title="RSS 头条 · 小屏",
        description="多源头条，纯文本；每 %d 分钟轮换一条，走完全部再从头开始。"
                    "标题只放屏上显示得下的 %g 全角字，余下原文接在正文里，源 %d 个"
                    % (ROTATE_SECONDS // 60, LINE_WIDTH, stats.get("total", 0)),
        screen=True))


def _check_version_arg(argv):
    """--version / -V：打印版本号后直接退出（排查线上跑的是哪一版时最省事）。"""
    for a in argv:
        if a in ("--version", "-V", "-v"):
            print("rss-hub %s" % __version__)
            return True
    return False


def main():
    if _check_version_arg(sys.argv[1:]):
        return 0
    skipped = []
    entries = read_feed_entries(skipped=skipped)
    if skipped:
        # 地址行写错（少了 http、被引号包住、贴成 Markdown 链接）时以前静默丢源，
        # 现在打出来，省得「明明加了源却不生效」排查半天
        print("feeds.txt 有 %d 行不是有效地址，已跳过：" % len(skipped))
        for s in skipped[:10]:
            print("  " + s)
    if not entries:
        print("feeds.txt 中没有有效地址")
        return 1
    print("rss-hub %s · 订阅源 %d 个，输出 %s / %s"
          "（标题 %g 全角、正文最多 %g 全角、每 %d 秒轮换一条）"
          % (__version__, len(entries), OUT_FILE, SCREEN_OUT, LINE_WIDTH, BODY_MAX,
             ROTATE_SECONDS))

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
