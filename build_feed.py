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

分工：
  render.py     显示规则（怎么切、怎么排、怎么拼 RSS），纯函数可单测 —— 改「屏上怎么显示」去这里
  rotate.py     轮播（池子怎么切片、这一窗该播哪一条、游标状态怎么落盘），选条是纯函数 ——
                改「上屏顺序 / 该播哪条」去这里（规则只有一条，别再拆台阶，见 _pick_bucket）
  本文件        抓取、失败回退、写文件、编排。轮播相关只留「状态文件与线上地址在哪」这一层配置。

本文件从 rotate 里 re-export 了轮播入口（rotate_pick / _pick_bucket / _bucketize /
ROTATE_SECONDS / ROTATE_STATE_VERSION / CLOCK_SKEW_WINDOWS），是为了让 tests/ 与 tools/
继续按 `build_feed.xxx` 调用，不必跟着搬家 —— 别把它们当成本模块的实现。

版本号在 version.py（唯一真源），`python3 build_feed.py --version` 可直接打印。

环境变量（带 SCREEN_/FEED_BODY_ 前缀的由 render.py 读，ROTATE_ 前缀的由 rotate.py 读，
其余在本文件；改前缀请连模块一起看，别只改这里的说明）：
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
  ROTATE_SECONDS     每条在屏上驻留多少秒（= 设备刷新周期），默认 300（rotate.py 读）
                     —— 调大等于放慢轮播。Pages CDN 缓存 600 秒，
                     若真机上出现「同一条连显两次」，把它改成 600。
  ROTATE_POOL_MAX    轮播池上限，默认 0 = 不限（= 去重后的全部条目）（rotate.py 读）
  ROTATE_STATE       轮播状态文件，默认与 OUT_FILE 同目录的 rotate.json
  ROTATE_STATE_URL   线上状态地址，默认 FEED_URL 同目录的 rotate.json
  SOURCE_TZ_OFFSET   源没写时区时按哪个时区解释，默认 8（北京 +08）
"""
import os
import re
import sys
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
# 轮播住在 rotate.py。这里 re-export 六个名字，让 tests/、tools/ 继续用 build_feed.xxx：
#   rotate_pick、_pick_bucket、_bucketize  —— 行为入口
#   ROTATE_SECONDS、ROTATE_STATE_VERSION、CLOCK_SKEW_WINDOWS —— 测试要把窗口号算成秒
# load_rotate_state / save_rotate_state 只在本文件的 main() 里用，路径由这里给。
from rotate import (CLOCK_SKEW_WINDOWS, ROTATE_POOL_MAX, ROTATE_SECONDS,
                    ROTATE_STATE_VERSION, _bucketize, _pick_bucket,
                    load_rotate_state, rotate_pick, save_rotate_state)
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

# 轮播：设备版一次只发布一条。节拍与池上限由 rotate.py 从同名环境变量读，
# 这里只保留「状态写在哪、线上地址是什么」——两者都跟着 OUT_FILE / FEED_URL 走。
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


# ---------------- 轮播的位置配置 ----------------
# 机制实现全在 rotate.py（怎么切片、这一窗该播哪条、状态里放什么、怎么落盘）。
# 本文件只留两件跟路径有关的配置：状态文件在哪（上面的 ROTATE_STATE，跟着 OUT_FILE 走）、
# 线上状态地址在哪（下面的 _rotate_url，跟着 FEED_URL 走）。
#
# 为什么用「时间窗」而不是「跑了第几轮」驱动：后台一小时内可能构建 12 次，也可能因为
# 重跑、手动触发变成 30 次，用计数器就会跳条；用 w = now // ROTATE_SECONDS 做幂等键，
# 同一个 5 分钟窗内构建多少次都只前进一格，节奏只跟墙上时钟走。
def _rotate_url():
    return (os.environ.get("ROTATE_STATE_URL")
            or FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(ROTATE_STATE))

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
        state = load_rotate_state(ROTATE_STATE, _rotate_url(), UA)
        pick, state, note = rotate_pick(pool, state)
        write_feed(items, stats, pick)
        if pick is not None:
            save_rotate_state(state, ROTATE_STATE)
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
