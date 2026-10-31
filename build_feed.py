#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 聚合器 —— 专为 Dot (Quote/0) 墨水屏设备开发

读 feeds.txt（注释行 = 中文源名，下一行 = 地址）
→ 每源抓最新 PER_SOURCE 条 → 跨源合并 → 按发布时间倒序 → 去重 → 截取最新 ITEM_LIMIT 条（默认 120）
→ 生成 RSS 2.0

（排序在去重之前，不是笔误：`dedupe()` 保留先到者，先按时间排完才留得住「更新的那条」。
`run_once()` 的 docstring 里有同样的顺序说明。）

输出三份：
  docs/feed.xml       完整版：标题原样、全部条目按时间倒序 + 摘要正文，给电脑 / 手机上的 RSS 阅读器
  docs/feed-wrap.xml  设备版：只出「当前该显示的那一条」，标题截到屏宽，
                      余下文字接进 <description>（正文）；填进 Dot App 的「循环内容」即可
  docs/rotate.json    轮播状态：哪一条已展示、当前在第几窗，随 Pages 一起发布

轮播规则（为什么设备版只有一条）：
  Dot 云端只渲染文档里的第一条，按时间倒序排的话屏幕永远停在最新那条，其余条目一辈子轮不到。
  所以设备版每版只发一条，选谁由「已播标记」决定 —— **规则本身、以及为什么这么定，
  全在 rotate.py 的模块说明与 `_pick_item` 的 docstring 里，这里不复制一份**（复制必然漂移）。
  一句话版本：每窗播「池子里还没播过、最新的一条」，全播过才清标记重来；同一时间窗内重跑不推进。

特性：
  * 零第三方依赖，Python 3.8+
  * 只读通路：只抓 RSS、只写静态文件，不向任何设备/第三方接口发起写请求，不需要任何密钥
  * 单源失败时回退到上一份 feed 中该源的条目（不会凭空丢源）
  * 单次运行内可循环多轮（REFRESH_SECONDS）
  * 自动识别 UTF-8 / GB18030 / HTTP 头声明的编码，修补常见 HTML 实体

分工：
  render.py     显示规则（怎么切、怎么排、怎么拼 RSS），纯函数可单测 —— 改「屏上怎么显示」去这里
  rotate.py     轮播（这一窗该播哪一条、已播标记怎么记、游标状态怎么落盘），选条是纯函数
                —— 改「上屏顺序 / 该播哪条」去这里（规则只有一条，别再拆台阶，见 _pick_item）
  本文件        抓取、失败回退、写文件、编排。轮播相关只留「状态文件与线上地址在哪」这一层配置。

本文件从 rotate 里 re-export 了轮播入口（rotate_pick / _pick_item / ROTATE_SECONDS /
ROTATE_STATE_VERSION / CLOCK_SKEW_WINDOWS），是为了让 tests/ 与 tools/ 继续按
`build_feed.xxx` 调用，不必跟着搬家 —— 别把它们当成本模块的实现（新代码请直接 `from rotate`）。

版本号在 version.py（唯一真源），`python3 build_feed.py --version` 可直接打印。

环境变量（**按模块逐个点名，别按前缀推断**：前缀与读取方并不一一对应，
SCREEN_OUT / ROTATE_STATE 两个带屏/轮播前缀的其实是本文件读的）：
  ── 本文件读：
  FEEDS_FILE         订阅源列表，默认 ./feeds.txt
  OUT_FILE           完整版输出，默认 ./docs/feed.xml
  FEED_URL           频道 <link>，默认 https://cheney-hub.github.io/rss-hub/feed.xml
  REFRESH_SECONDS    循环刷新到第几秒结束（0 = 只跑一轮），默认 0
  PER_SOURCE         每源取几条，默认 5
  ITEM_LIMIT         去重后条目上限，默认 120（0 = 不限）：按发布时间倒序后节选最新的 120 条
  FETCH_TIMEOUT      单源超时秒数，默认 20
  CONCURRENCY        并发数，默认 12
  SCREEN_OUT         设备版输出，默认与 OUT_FILE 同目录的 feed-wrap.xml（**本文件写**）
  ROTATE_STATE       轮播状态文件，默认与 OUT_FILE 同目录的 rotate.json（**本文件写**）
  ROTATE_STATE_URL   线上状态地址，默认 FEED_URL 同目录的 rotate.json
  SOURCE_TZ_OFFSET   源没写时区时按哪个时区解释，默认 8（北京 +08）
  ── render.py 读：
  SCREEN_LINE_WIDTH  标题行宽度（全角字符数），默认 13
                     —— 设备 296×152 屏是大字号单行，实测 13 个全角字满宽，
                     再多就被 Dot 云端从右边切掉。所以标题只放得下多少给多少。
  SCREEN_BODY_MAX    设备版正文（description）最多保留多少全角字，默认 90
                     —— 正文 = 标题装不下的余文 + 原 feed 的 description/摘要原文；
                     296×152 屏正文区约 4 行 × 22 全角 ≈ 88 字，取 90 刚好填满。
                     太长的正文尾端会补省略号。
  FEED_BODY_MAX      完整版正文最多保留多少全角字，默认 0 = 不限（完整版全文照发）
                     —— 正数才用来防止单条摘要把整份文件撑大，与屏宽无关。
  ── rotate.py 读：
  ROTATE_SECONDS     每条在屏上驻留多少秒（= 设备刷新周期），默认 300，下限 60
                     —— 调大等于放慢轮播。Pages CDN 缓存 600 秒，
                     若真机上出现「同一条连显两次」，把它改成 600。
  ROTATE_POOL_MAX    轮播池上限，默认 120（0 = 不限）
                     —— 设备端只消费「最新 120 条」：最差滞后 = 池条数 ÷ 每窗新增，
                     池子涨到 245 条会把最差滞后从 4.3 小时推到 8 小时以上，
                     所以设备池与完整版条数分开管：两边都钉在最新 120 条。
"""
import os
import re
import sys
import time
import zlib
import email.utils
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# 显示规则住在 render.py，build_feed 只负责抓取与编排。
from render import (BODY_MAX, ENTITY_FIX, LINE_WIDTH, build_rss, clean_text,
                    dedupe, device_description, item_guid)
# 轮播住在 rotate.py。这里 re-export 几个名字，让 tests/、tools/ 继续用 build_feed.xxx：
#   rotate_pick、_pick_item   —— 行为入口（_pick_item 是「按条选一条」的实现，1.7.0 起
#                                取代按时间片选代表的 _pick_bucket；名字跟着语义一起换了，
#                                留旧名会让读代码的人以为还在切片）
#   ROTATE_SECONDS、ROTATE_STATE_VERSION、CLOCK_SKEW_WINDOWS —— 测试要把窗口号算成秒
# `__all__` 同时起两个作用：声明「本模块对外承诺的名字」，以及告诉 pyflakes
# 这几个 import 不是死代码（它认 __all__，不认没有 flake8 的 # noqa）。
# load_rotate_state / save_rotate_state 只在本文件的 main() 里用，路径由这里给。
from rotate import (CLOCK_SKEW_WINDOWS, ROTATE_POOL_MAX, ROTATE_SECONDS,
                    ROTATE_STATE_VERSION, _pick_item, load_rotate_state,
                    rotate_pick, save_rotate_state)
from version import __version__

__all__ = ["__version__", "read_feed_entries", "parse_items", "run_once", "write_feed",
           "main", "rotate_pick", "_pick_item", "ROTATE_SECONDS",
           "ROTATE_STATE_VERSION", "CLOCK_SKEW_WINDOWS"]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.environ.get("FEEDS_FILE", os.path.join(BASE_DIR, "feeds.txt"))
OUT_FILE = os.environ.get("OUT_FILE", os.path.join(BASE_DIR, "docs", "feed.xml"))
FEED_URL = os.environ.get("FEED_URL", "https://cheney-hub.github.io/rss-hub/feed.xml")
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "0"))
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
CONCURRENCY = max(1, int(os.environ.get("CONCURRENCY", "12")))   # 并发上限（下限 1）

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

# 伪装成桌面 Chrome：不少源站对 urllib 的默认 UA 直接回 403，换 UA 是最省事的兼容手段
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# 失败后隔多久重试一次（秒）。只重试一次：单轮总时长要压在 5 分钟节拍内，
# 源站真挂了重试十次也还是挂，1 秒只是给偶发的连接重置 / 5xx 一个机会。
RETRY_DELAY = 1.0
# 单源响应体上限。绝大多数 RSS 在 1 MB 以内，最大的源约 2~3 MB，
# 设这个上限只为一种情况：源地址被改成一个巨大的文件（视频、整站导出），
# 一次 read() 就把 runner 的内存吃光，整轮采集跟着崩。宁可丢这一个源。
MAX_BODY_BYTES = 12 * 1024 * 1024


def _cap(body):
    """超过上限就抛错（由 worker 转成「该源本轮失败」，走回退或跳过）。"""
    if len(body) > MAX_BODY_BYTES:
        raise RuntimeError("响应体 %d MB 超过 %d MB 上限"
                           % (len(body) >> 20, MAX_BODY_BYTES >> 20))
    return body

PER_SOURCE = max(1, int(os.environ.get("PER_SOURCE", "5")))   # 每源取几条（下限 1）
ITEM_LIMIT = max(0, int(os.environ.get("ITEM_LIMIT", "120")))   # 去重后上限，默认 120（0 = 不限）


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
                    # 源名后面常跟括号备注（「财联社（快讯）」）：备注只给人看，
                    # 上屏时占宽度，所以从第一个中/英文左括号起整段截掉
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
def _inflate(body, wbits):
    """有界解压：最多产出 MAX_BODY_BYTES，多一个字节就判超限。

    不能写成 `_cap(gzip.decompress(body))`：那是**先全量物化再量**，而压缩比可以到
    1:1000 量级（实测 20 MB 全零压成 20 KB），12 MB 的响应对应 GB 级的解压结果，
    `_cap` 执行时内存早就没了。这里用 decompressobj 的 max_length，多要 1 字节即可
    判定越界，永远不会物化超过上限的中间结果。
    """
    d = zlib.decompressobj(wbits)
    out = d.decompress(body, MAX_BODY_BYTES + 1)
    if len(out) > MAX_BODY_BYTES or d.unconsumed_tail:
        raise RuntimeError("解压后超过 %d MB 上限" % (MAX_BODY_BYTES >> 20))
    out += d.flush()
    if len(out) > MAX_BODY_BYTES:
        raise RuntimeError("解压后超过 %d MB 上限" % (MAX_BODY_BYTES >> 20))
    return out


def http_get(url):
    """抓一个地址，返回 (状态码, 已解压的字节串, 响应头声明的字符集或 None)。

    源站大多按 Accept-Encoding 压缩，但各家回的算法不一样：gzip 最常见，
    少数（尤其老 nginx）回 deflate。以前只解 gzip，碰上 deflate 就把压缩流当
    正文去解码，解析必然失败、该源整轮走回退——所以这里两种都解。

    体积上限查两次：读到的压缩体不超过 MAX_BODY_BYTES，解压过程本身也封顶
    （`_inflate` 是流式的，不会先把几百 MB 解出来再判断）。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        # +1 是为了让「刚好等于上限」与「超过上限」可区分，不是笔误
        body = _cap(resp.read(MAX_BODY_BYTES + 1))
        enc = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in enc:
            body = _inflate(body, 16 + zlib.MAX_WBITS)
        elif "deflate" in enc:
            # 规范里的 deflate 是 zlib 包装；有实现直接发裸 deflate，两种都试。
            # 只吞 zlib.error：超限抛的是 RuntimeError，不能被当成「换种算法再试」。
            try:
                body = _inflate(body, zlib.MAX_WBITS)
            except zlib.error:
                body = _inflate(body, -zlib.MAX_WBITS)
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
    钳到抓取时刻看着自然，但抓取每 5 分钟重跑一次、钳值跟着往前跑，它的发布时间永远是「本窗」，
    轮播每窗都把它当成没播过的最新一条选中 —— 同一条每窗重播一次（实测连跑 6 窗全是它），
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
    """把源站的脏 XML 修到 ET 能解析：去 DOCTYPE、修实体、剥控制字符、去编码声明。

    顺序有意义：先剥 DOCTYPE（它里面可能有实体声明与 `>`，留在后面会干扰实体修补），
    再修命名实体，最后处理控制字符与编码声明。
    """
    def fix(m):
        """命名实体 → 已知映射 / 保留 XML 五个预定义实体 / 其余换成空格。

        未知实体不能原样留着：ET 遇到未定义的实体直接报错，整个源就得走兜底通路。
        换成空格是唯一代价最小的选择（丢一个符号 vs 丢整条）。
        """
        e = m.group(0)
        if e in ENTITY_FIX:
            return ENTITY_FIX[e]
        if e.startswith(("&amp;", "&lt;", "&gt;", "&quot;", "&apos;")):
            return e
        return " "

    # DOCTYPE 显式剥掉：内部实体展开（billion laughs）没有任何正当用途，
    # 而本文件原先对它其实是「偶然防住」的 —— 下面那条实体正则只匹配 2~9 字符的
    # 实体名，名字更长的照样展开（实测 805 字节的输入能放出 10 万字符）。
    xml_text = re.sub(r"<!DOCTYPE[^\[>]*(\[[\s\S]*?\])?[^>]*>", "", xml_text, flags=re.I)
    # 只处理「&amp;」这类命名实体；数字实体（&#8212;）正则匹配不到，ET 自己能处理
    xml_text = re.sub(r"&[a-zA-Z][a-zA-Z0-9]{1,8};", fix, xml_text)
    xml_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_text)
    # 去掉声明里的 encoding，避免 ET 对 str 报错或按错误编码解码
    return re.sub(r'(<\?xml[^>]*?)\s+encoding="[^"]*"', r"\1", xml_text, count=1)


def _rx(raw, pattern):
    """在原始 XML 片段里取第一个捕获组，取不到返回空串（正则兜底解析用）。"""
    m = re.search(pattern, raw, re.S | re.I)
    return m.group(1) if m else ""


def _item_nodes(xml_text):
    """取出所有 <item>/<entry> 节点，两种形状对下游等价。

    两条通路：
      * 正常走 ElementTree，命名空间无关（RSS 2.0 / Atom / RDF 都能读）；
      * ET 报错时退回正则抽 <item>/<entry> 块——线上确实有源发不合法的 XML
        （未转义的 & 、未闭合标签），整源丢弃的代价比少几个字段大。
    返回的元素因此可能是 ET 节点，也可能是 `{"__raw__": 原始片段}`；下游只经
    `_entry_of()` 读它们。传进来的文本必须已经过 `_sanitize`（兜底通路也用同一份）。
    """
    try:
        root = ET.fromstring(xml_text)
        nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    except ET.ParseError:
        blocks = re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", xml_text,
                            re.S | re.I)
        if not blocks:
            raise RuntimeError("XML 解析失败且无 item/entry")
        nodes = [{"__raw__": b} for b in blocks]
    if not nodes:
        raise RuntimeError("未解析到条目")
    return nodes


def _entry_of(node):
    """把一条 <item>/<entry> 归一成下游唯一的字段契约。

    输入两种形状（见 `_item_nodes`），输出固定五个键：
    title / link / date_raw / ts / summary，`src` 由调用方补 ——
    build_feed 与 render 都只认这几个键，改键名要两边一起改。
    """
    if isinstance(node, dict):                # 正则兜底通路
        raw = node["__raw__"]
        title = _rx(raw, r"<title\b[^>]*>(.*?)</title>")
        # 两条正则的顺序就是优先级：先试 href 属性（Atom 的写法），
        # 再退回元素文本（RSS 2.0 的写法）。反过来的话 Atom 会拿到空串。
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


def parse_items(xml_text, source_url, limit=PER_SOURCE):
    """解析一个源，按发布时间倒序取前 limit 条；每条都带摘要原文（summary）。

    **消毒在入口做，两条通路共用**：兜底通路以前直接拿原始文本抽字段，
    源里的控制字符（如 U+0001）会原样进 feed.xml，让整份 XML 对所有订阅端非法
    （`ET.fromstring` 报 invalid token）。`_sanitize` 幂等，ET 通路照旧安全。
    """
    items = [_entry_of(n) for n in _item_nodes(_sanitize(xml_text))]
    for it in items:
        it["src"] = source_url
        if it["ts"] is None:
            it["ts"] = 0
    # 次级键用 guid：同一时间戳的条目在不同次抓取里先后次序可能不同（源自己也在抖），
    # 而排序是稳定的 —— 次序不定就等于「同样的数据排出两个池子」，
    # 而轮播取「排在前面的第一条」，次序不定就是同一窗这次选 A、下次选 B，屏上会莫名换内容。
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
        # 每个源最多回补 PER_SOURCE 条：旧 feed 里可能有上百条同一源的历史条目，
        # 全塞回池子会把池子撑满、把新内容挤出去
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
        })
    return prev


# ---------------- 轮播的位置配置 ----------------
# 机制实现全在 rotate.py（怎么选这一条、标记怎么记、状态里放什么、怎么落盘）。
# 本文件只留两件跟路径有关的配置：状态文件在哪（上面的 ROTATE_STATE，跟着 OUT_FILE 走）、
# 线上状态地址在哪（下面的 _rotate_url，跟着 FEED_URL 走）。
#
# 为什么用「时间窗」而不是「跑了第几轮」驱动：后台一小时内可能构建 12 次，也可能因为
# 重跑、手动触发变成 30 次，用计数器就会跳条；用 w = now // ROTATE_SECONDS 做幂等键，
# 同一个 5 分钟窗内构建多少次都只前进一格，节奏只跟墙上时钟走。
def _rotate_url():
    """线上状态地址：`ROTATE_STATE_URL` 优先，否则取 `FEED_URL` 同目录的同名文件。

    为什么不放进 rotate.py：这个默认值由 OUT_FILE / FEED_URL 推导而来，而那两个
    是本文件的配置。让 rotate 再复制一份默认值，两处必然漂移。
    """
    return (os.environ.get("ROTATE_STATE_URL")
            or FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(ROTATE_STATE))


# ---------------- 单轮采集 ----------------
def _collect(entries, prev_items):
    """并发抓全部源，失败的按「上一份 feed 里该源的条目」回退。

    返回 (条目列表, {"ok": 成功源数, "fail": 失败源数, "stale": 走了回退的源数})。
    条目顺序取决于各源返回的快慢，不能直接当池子用 —— 排序由调用方统一做。

    重试放在 worker 里做：以前在主线程的 as_completed 循环里 sleep 后重试，
    四十几个源里只要有 5 个超时就要白等 5 秒（现在 49 个源），而且等待期间其它
    已完成的结果全都压着不处理。放进 worker 后并发的 12 路各睡各的，主线程只管收结果。
    """
    items, ok, fail, stale = [], 0, 0, 0

    def fetch_one(e):
        """抓一个源并解析。任何异常都收成 (entry, None, err) 交给主线程决定回退，
        不在这里 sleep / 重试 —— 重试策略只写在 fetch_with_retry 一处。"""
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
        """失败一次后隔 RETRY_DELAY 再试一次：源站偶发 5xx / 连接重置很常见。

        不区分错因，404 / 解析失败也会重试一次 —— 判错因要把异常分类带下来，
        而代价是每个坏源多 1 秒，比误判「偶发」变「永久」便宜。
        """
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
                # 浅拷贝：回退条目会被下面的排序 / 去重 / 截断动到，
                # 不能让它们和 prev_items 缓存里的对象共享引用
                items.extend(dict(x) for x in fb)
                print("[回退] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))
            else:
                print("[失败] %-22s %s (%s)" % (e["name"][:20], e["url"][:70], err))
    return items, {"ok": ok, "fail": fail, "stale": stale}


def run_once(entries, prev_items):
    """抓全部源 → 排序 → 去重 → 截断，返回 (条目列表, 统计)。

    抓取与回退由 `_collect` 负责，这里只做「把一堆条目收敛成一个池子」：
    排序 → 去重（保留先到者，所以排序必须在前面）→ 按 ITEM_LIMIT 截断。

    排序的次级键必须是 guid：条目是 as_completed 并发收上来的，数组顺序取决于
    各源返回的快慢，而排序是稳定的 —— 不补次级键，同一时间戳的多条在两次运行里
    会排出不同次序，于是同一窗跑两遍会选出两条不同的内容（标记按 guid 走，
    池子顺序再漂就等于随机换台）。补上 guid 后，同一批数据只有一个池子顺序。
    """
    items, counts = _collect(entries, prev_items)
    items.sort(key=lambda x: (-(x.get("ts") or 0), item_guid(x)))
    items, dedup_dropped = dedupe(items)
    # 排序在前、裁剪在后，留下的一定是最新的那批（120 条 ≈ 4.3 小时产出）。
    # ITEM_LIMIT=0 仍表示不限；设备池由 ROTATE_POOL_MAX 单独收口，两者互不牵连。
    trimmed = 0
    if ITEM_LIMIT and len(items) > ITEM_LIMIT:  # 超上限：按时间排序后节选最新的
        trimmed = len(items) - ITEM_LIMIT
        items = items[:ITEM_LIMIT]
    # dedup_dropped 与 trimmed 分开统计：前者是「重复」，后者是「排在 120 名之外」，
    # 合成一个数字时日志会把「池子满了」误读成「重复变多了」
    stats = {"total": len(entries), "items": len(items),
             "dedup_dropped": dedup_dropped, "trimmed": trimmed}
    stats.update(counts)
    return items, stats


def _write(path, xml_text):
    """原子写文本文件：同目录建唯一临时文件 → 写 → `os.replace` 覆盖。

    - 统一 LF：Windows 上默认会写成 CRLF，进仓库会反复产生换行差异；
    - 临时文件名必须唯一（`mkstemp`）。固定成 `path + ".tmp"` 时两个进程同时写同一份
      文件会踩踏：B 打开旧 inode → A 先 replace → B 的写入落到「已经是正式文件」的
      那个 inode 上，读者可能读到半份内容（原子性丢失）；
    - 不 fsync：这里丢一次产物最多重跑一轮，不值得付每次落盘的同步开销。
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(xml_text)
        os.chmod(tmp, 0o644)          # mkstemp 建的是 0600，发布出去的文件要可读
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_feed(items, stats, pick=None):
    """写两份：完整版 feed.xml（全部条目）+ 设备版 feed-wrap.xml（只有轮播到的那一条）。

    pick 为 None 有两种成因（都不是「出错」）：
      * 同窗重跑、且上一窗那条已离开池子（rotate_pick 返回 skip）；
      * 池子里没有可播条目（全部条目的时间片都晚于当前窗）。
    两种情况都只刷新完整版，设备版保留上一版不动 —— 5 分钟内换两条比晚一条更糟。
    pick 非 None 时设备版只写一条：云端本来就只渲染第一条，多写的部分纯属浪费体积。
    """
    _write(OUT_FILE, build_rss(items, FEED_URL, stats))
    if pick is None:
        return

    wrap_url = FEED_URL.rsplit("/", 1)[0] + "/" + os.path.basename(SCREEN_OUT)
    _write(SCREEN_OUT, build_rss(
        [pick], wrap_url, stats,
        channel_title="RSS 头条 · 小屏",
        # 文案由 render 统一出，Pi 的 rotate_screen 也用同一份（见 device_description）
        description=device_description(stats.get("total", 0)),
        screen=True))


def _check_version_arg(argv):
    """--version / -V / -v：打印版本号后直接退出（排查线上跑的是哪一版时最省事）。"""
    for a in argv:
        if a in ("--version", "-V", "-v"):
            print("rss-hub %s" % __version__)
            return True
    return False


def _one_round(entries):
    """跑一轮：采集 → 选条 → 写两份 → 落状态，返回 (pick, note, stats, 耗时秒)。

    stats["items"] == 0 表示本轮一条都没采到，由调用方决定「保留旧 feed」。
    选条只在池子（最新 ROTATE_POOL_MAX 条）里进行，完整版照旧写全部条目 ——
    池子只管设备端，不影响阅读器能订到多少。
    """
    t0 = time.time()
    items, stats = run_once(entries, load_prev_items())
    if not items:
        return None, "", stats, time.time() - t0
    pool = items[:ROTATE_POOL_MAX] if ROTATE_POOL_MAX else items
    state = load_rotate_state(ROTATE_STATE, _rotate_url(), UA)
    pick, state, note = rotate_pick(pool, state)
    write_feed(items, stats, pick)
    if pick is not None:
        save_rotate_state(state, ROTATE_STATE)
    return pick, note, stats, time.time() - t0


def main():
    """跑一轮（或按 REFRESH_SECONDS 连跑多轮）：采集 → 选条 → 写两份 → 落状态。

    退出码：0 = 正常结束；1 = feeds.txt 没有有效地址，或本轮一条都没采到
    （此时保留上一份 feed 不覆盖）。CI 用 `python build_feed.py || echo ...` 兜住 1，
    所以「失败一轮」不会让 Pages 变空，只是内容变旧。

    Pi 上还有一处与 `Pi/rotate_screen.py` 并存的部署差异：rss-build.service 把
    SCREEN_OUT / ROTATE_STATE 都指到 /tmp，屏幕那一路由 rotate_screen 驱动。
    也就是说本函数里的选条段在那套部署下不产出对外文件（见 OPERATIONS.md 参数表）。
    """
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
        pick, note, stats, elapsed = _one_round(entries)
        if not stats["items"]:
            print("本轮无任何条目，保留旧 feed 不覆盖")
            return 1
        rounds += 1
        print("[第 %d 轮] 成功 %d / 回退 %d / 失败 %d → 条目 %d"
              "（重复 %d、超出上限裁掉 %d）耗时 %.1fs"
              % (rounds, stats["ok"], stats["stale"], stats["fail"],
                 stats["items"], stats["dedup_dropped"], stats["trimmed"], elapsed))
        print("        上屏：%s ｜ %s"
              % (note, (pick.get("title") or "")[:36] if pick else "（设备版保持上一版）"))

        left = deadline - time.time()
        # 45 秒的来历：一轮正常 15~25 秒（49 源 / 12 并发），留一倍余量。
        # 少于这个数就没必要再起一轮 —— 起了也会被作业超时砍掉，反而留下半个产物。
        if left < 45:
            break
        # 一轮最多睡一个轮播节拍：REFRESH_SECONDS 很长时也要按 5 分钟节奏刷，
        # 否则「循环多轮」会变成「睡到最后一刻只跑一轮」
        time.sleep(min(ROTATE_SECONDS, left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
