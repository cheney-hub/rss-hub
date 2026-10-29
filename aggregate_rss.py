#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS 聚合器（GitHub Pages 版）
读 feeds.txt（# 注释行 + 地址行）→ 每源抓最新 1 条 → 跨源去重 →
按时间倒序生成 RSS 2.0 → 通过 GitHub Contents API 推送到仓库（Pages 订阅）。

零第三方依赖，Python 3.8+，树莓派/任意装了 python3 的机器直接跑。

环境变量：
  GITHUB_TOKEN   Personal Access Token（需 repo 写权限）——不设则仅本地生成
  GITHUB_REPO    形如 owner/repo
  GITHUB_BRANCH  分支，默认 main
  FEED_PATH      仓库内文件路径，默认 feed.xml
  FEEDS_FILE     订阅源列表路径，默认脚本同目录 feeds.txt
  DRY_RUN        =1 时只生成本地 feed.xml，不推送
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
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDS_FILE = os.environ.get("FEEDS_FILE", os.path.join(BASE_DIR, "feeds.txt"))
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(BASE_DIR, "state.json"))
OUT_FILE = os.environ.get("OUT_FILE", os.path.join(BASE_DIR, "feed.xml"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip()
FEED_PATH = os.environ.get("FEED_PATH", "feed.xml").strip()
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))          # 秒
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
RETRY_DELAY = 1.2                                                   # 失败重试前等待（秒）


# ---------------- feeds.txt 解析（注释行=中文名，地址行=URL） ----------------
def read_feed_entries():
    entries, pending_name = [], ""
    with open(FEEDS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            if raw.startswith("#"):
                txt = raw.lstrip("#").strip()
                if txt and not txt.startswith(("=", "-")):
                    pending_name = re.sub(r"[（(].*?$", "", txt).strip()  # 截去括号备注
                continue
            url = raw.split("#")[0].strip()                             # 兼容行尾注释
            if url:
                entries.append({"name": pending_name or url, "url": url})
                pending_name = ""
    # 按地址去重
    seen, out = set(), []
    for e in entries:
        if e["url"] not in seen:
            seen.add(e["url"])
            out.append(e)
    return out


# ---------------- HTTP 抓取（gzip + 条件请求 + 重试） ----------------
def http_get(url, headers=None):
    h = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return resp.status, dict(resp.headers), body


def fetch_entry(url, state):
    """抓单源最新条目；304/失败时回退到 state 里的缓存条目。"""
    cached = state.get("cond", {}).get(url, {})
    headers = {}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]
    try:
        status, resp_headers, body = http_get(url, headers)
    except urllib.error.HTTPError as e:
        status, resp_headers, body = e.code, dict(e.headers or {}), b""
    except Exception as e:
        raise RuntimeError(str(e) or type(e).__name__)

    if status == 304 and cached.get("item"):
        return cached["item"], cached                     # 源没更新，直接用缓存

    if status != 200:
        raise RuntimeError("HTTP %s" % status)

    text = body.decode("utf-8", errors="replace")
    item = parse_latest(text, url)
    if not item:
        raise RuntimeError("未解析到条目")

    cond = {
        "etag": resp_headers.get("ETag") or resp_headers.get("etag"),
        "last_modified": resp_headers.get("Last-Modified") or resp_headers.get("last-modified"),
        "item": item,
    }
    return item, cond


# ---------------- RSS/Atom 解析 ----------------
def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _first_text(el, names):
    for child in el.iter():
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return ""


def _parse_date(s):
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_latest(xml_text, source_url):
    """解析 RSS 2.0 / RSS 1.0 / Atom，返回最新一条。"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as e:
        raise RuntimeError("XML 解析失败: %s" % e)

    nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    if not nodes:
        return None

    def entry_of(node):
        title = _first_text(node, {"title"})
        link = ""
        for c in node.iter():
            if _local(c.tag) == "link":
                link = (c.get("href") or c.text or "").strip()
                if link:
                    break
        date_raw = _first_text(node, {"pubdate", "published", "updated", "date"})
        ts = _parse_date(date_raw)
        return {"title": title, "link": link, "date_raw": date_raw, "ts": ts}

    items = [entry_of(n) for n in nodes]
    items.sort(key=lambda x: x["ts"] or 0, reverse=True)
    best = items[0]
    best["src"] = source_url
    # 无日期的条目排到最后：给一个很旧的时间戳
    if best["ts"] is None:
        best["ts"] = 0
    return best


# ---------------- 跨源去重（与 server.js 同规则） ----------------
def norm_title(t):
    t = re.sub(r"[「」【】\[\]（）()“”\"'‘’：:，,。.！!？?、\s|·\-—–]", "", t or "")
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


# ---------------- RSS 2.0 生成 ----------------
def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                     .replace(">", "&gt;").replace('"', "&quot;")


def build_rss(items, feed_url):
    now = email.utils.format_datetime(datetime.now(timezone.utc))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        "<title>RSS 聚合头条</title>",
        "<link>%s</link>" % esc(feed_url),
        "<description>多源 RSS 头条聚合，按时间倒序</description>",
        "<language>zh-CN</language>",
        "<lastBuildDate>%s</lastBuildDate>" % now,
        "<ttl>10</ttl>",
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
        lines.append('<guid isPermaLink="true">%s</guid>' % esc(it["link"] or it["title"]))
        if it.get("src_name"):
            lines.append("<source>%s</source>" % esc(it["src_name"]))
        lines.append("</item>")
    lines.append("</channel></rss>")
    return "\n".join(lines)


# ---------------- GitHub 上传 ----------------
def github_api(path, method="GET", payload=None):
    url = "https://api.github.com" + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer %s" % GITHUB_TOKEN,
        "Accept": "application/vnd.github+json",
        "User-Agent": "rss-aggregator",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_to_github(xml_text):
    api_path = "/repos/%s/contents/%s" % (GITHUB_REPO, FEED_PATH)
    sha = None
    try:
        sha = github_api(api_path + "?ref=%s" % GITHUB_BRANCH).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    github_api(api_path, method="PUT", payload={
        "message": "update feed %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "content": __import__("base64").b64encode(xml_text.encode("utf-8")).decode("ascii"),
        "branch": GITHUB_BRANCH,
        **({"sha": sha} if sha else {}),
    })


# ---------------- 主流程 ----------------
def main():
    entries = read_feed_entries()
    if not entries:
        print("feeds.txt 中没有有效地址")
        return 1

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {"cond": {}}
    state.setdefault("cond", {})

    def fetch_one(entry):
        url, name = entry["url"], entry["name"]
        last_err = None
        for attempt in (1, 2):                       # 失败重试一次
            try:
                item, cond = fetch_entry(url, state)
                item["src_name"] = name
                return (url, item, cond, None)
            except Exception as err:
                last_err = err
                if attempt == 1:
                    time.sleep(RETRY_DELAY)
        return (url, None, None, last_err)

    items, ok, fail = [], 0, 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one, e): e for e in entries}
        for fut in as_completed(futures):
            e = futures[fut]
            url, item, cond, err = fut.result()
            if err is not None:
                fail += 1
                print("[源失败] %s (%s) -> %s" % (e["name"], url, err))
                cached = state["cond"].get(url, {}).get("item")
                if cached:
                    cached = dict(cached)
                    cached["src_name"] = e["name"]
                    items.append(cached)              # 回退上次成功条目
            else:
                ok += 1
                items.append(item)
                state["cond"][url] = cond

    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    items, dropped = dedupe(items)

    feed_url = ("https://%s.github.io/%s/%s"
                % (GITHUB_REPO.split("/")[0], GITHUB_REPO.split("/")[1], FEED_PATH)) \
        if "/" in GITHUB_REPO else "https://example.org/feed.xml"
    xml_text = build_rss(items, feed_url)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(xml_text)

    print("[聚合] 源 %d 成功 %d 失败 %d 条目 %d (去重 %d) 耗时 -"
          % (len(entries), ok, fail, len(items), dropped))

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    if DRY_RUN:
        print("DRY_RUN=1，已生成 %s，跳过推送" % OUT_FILE)
        return 0
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("未配置 GITHUB_TOKEN / GITHUB_REPO，仅完成本地生成")
        return 0
    upload_to_github(xml_text)
    print("已推送到 GitHub %s/%s（分支 %s）" % (GITHUB_REPO, FEED_PATH, GITHUB_BRANCH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
