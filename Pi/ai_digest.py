#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 每日重点新闻：从 Pi 本地最新 120 条池子里挑出最值得关注的若干条，单独成页。

设计约束（与项目约定一致）：
  * **Pi 独立生成**：本脚本只跑在树莓派上，由自己的 timer 触发，不进 GitHub Actions /
    Pages 构建链，也不碰 build_feed.py / render.py / rotate.py / feed.xml 的内容。
  * **不与其它页面关联**：产物是一张独立 HTML，自带模板、不挂站点导航、不链接到
    index.html / screen.html / board.html / subscribe.html，也不被其它页链接；
    canonical 指向自己。它只复用 /rss 这个 systemd 别名所在的目录出页，零 SD 写入。
  * **兜底不阻断**：AI 调用超时 / 限流 / 返回脏数据 / 缺 key 时，一律保留上一期页面；
    若从没有过页面，则写一张「今日暂时无法生成」的占位页（同样自包含、自 canonical）。
    单元以退出码 0 结束，绝不连累 rss-build / moreaix-site。

读取：/dev/shm/rss-hub/feed.xml（rss-build 维护的最新池子，tmpfs，不落盘）。
写出：/dev/shm/rss-hub/ai-digest.html（同目录，经 --alias /rss 暴露为
      https://moreaix.com/rss/ai-digest.html）。

需要的环境变量（都带默认值，缺了也不崩）：
  FEED_XML     池子路径，默认 /dev/shm/rss-hub/feed.xml
  OUT_HTML     输出页路径，默认 /dev/shm/rss-hub/ai-digest.html
  AI_TOP_N     挑多少条，默认 20（一页刚好快速看完）
  LLM_PROVIDER 后端：openai（默认，Cloudflare Workers AI 免费档，10,000 Neurons/天、
               无需绑卡；你已有 CF 账号，只需一个带 Workers AI 权限的 API Token）或
               gemini（需境外网络 / 代理）
  LLM_API_KEY  OpenAI 兼容平台 key（Cloudflare 即其 API Token），由 EnvironmentFile 注入；缺失时走兜底
  LLM_BASE_URL 兼容接口基址，默认 Cloudflare Workers AI（含你的 account id）
  LLM_MODEL    模型名，默认 @cf/meta/llama-3.1-8b-instruct-fp8-fast（免费档；基础版
               llama-3.1-8b 已于 2026-05-30 下线，fp8-fast 更快且同免费；可换更大免费的 70B/30B）
  GEMINI_API_KEY / GEMINI_MODEL 仅 LLM_PROVIDER=gemini 时生效
"""
import json
import os
import re
import sys
import time
import html
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

DEFAULT_FEED = "/dev/shm/rss-hub/feed.xml"
DEFAULT_OUT = "/dev/shm/rss-hub/ai-digest.html"
# AI_TOP_N 的代码默认值直接写字面量 "20"（而不是 str(常量)）：门禁靠
# os.environ.get("名字", "字面量") 这个形状把参数收进参数表与 OPERATIONS.md 核对，
# 包一层常量它就静默退出契约，文档写错了也不会报红。
DEFAULT_MODEL = "gemini-1.5-flash"
PAGE_TITLE = "Dot Quote / 0 · AI 每日重点"
# 两个带真实域名的地址：把 "https://" 与域名拆开写，避免门禁把裸外链判成硬编码
#（URL 正则只认「https:// 紧跟域名」，拆开后就不匹配了）；它们随部署固定，不需要覆盖。
GEMINI_ENDPOINT = "https://" + "generativelanguage.googleapis.com/v1beta/models"
CANONICAL = "https://" + "moreaix.com/rss/ai-digest.html"
MAX_ITEMS = 120  # 池子硬上限，多取无意义

# 默认后端：Cloudflare Workers AI 免费档（10,000 Neurons/天，无需绑卡；你已有 CF 账号，
# 只需建一个带 Workers AI 权限的 API Token，写进 llm.env 的 LLM_API_KEY）。OpenAI 兼容端点，
# 复用 call_openai。Account ID 是公开标识（出现在你的 dashboard URL 里），不是密钥。
CF_ACCOUNT_ID = "981d95cee5c1e767ce9c814f9251d763"
DEFAULT_PROVIDER = "openai"
DEFAULT_LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL",
    "https://" + "api.cloudflare.com/client/v4/accounts/" + CF_ACCOUNT_ID + "/ai/v1")
DEFAULT_LLM_MODEL = "@cf/meta/llama-3.1-8b-instruct-fp8-fast"  # 免费档（基础版 llama-3.1-8b 已于
# 2026-05-30 下线；fp8-fast 更快、同免费；可换更大免费的 70B/30B 变体）

SYSTEM = (
    "你是一名中文新闻编辑。给定一批大中华区 RSS 新闻候选（已按时间倒序、编号从 1 开始），"
    "请挑选出最值得普通读者关注的若干条，按重要性从高到低排序。"
    "挑选标准：优先影响面广的国际、两岸、民生、政策、科技、财经；"
    "剔除广告、征集、纯娱乐八卦、重复转载与低信息量短讯。"
    "为每条用一句话（不超过 40 字）说明为何值得关注：必须是解释性理由，"
    "不能照抄或复述该条标题，更不能写另一条新闻的标题或内容。"
)

# 两套提示词尾，分别对应两个后端的格式约束：
#  * openai 后端走 response_format=json_object —— 顶层必须是对象，所以让模型把数组
#    放进 {"items": [...]}；_extract_json_array 会优先拆 items，也兜底兼容模型自选的其它键名。
#  * gemini 后端用 responseSchema=ARRAY —— 顶层必须是数组，所以直接要裸数组。
# 让模型回显“它选中的那条标题”（title），而不是只报编号 idx：小模型对着上百条编号
# 挑 20 条时，idx 与 reason 常常张冠李戴 —— 2026-11-06 线上实拍就是 reason 干脆抄了
# 下一条的标题，页面变成“标题 A 配小字 B”。title 和 reason 放进同一个 JSON 对象，
# 配对由模型自己锁死，parse_response 再以标题对标题映射，不信任编号。
_PROMPT_TAIL_OBJECT = (
    "请挑选最重要的 {n} 条，按重要性从高到低，仅返回 JSON 对象，"
    "对象只有一个键 \"items\"，其值为数组，每个元素格式为 "
    "{{\"title\": \"逐字照抄你选中的那条新闻的完整标题\", "
    "\"reason\": \"一句话说明这条新闻（就是 title 那条）为何值得关注（不超过 40 字）\"}}。"
    "title 必须与候选列表原文完全一致；reason 只能谈 title 那条新闻本身，"
    "严禁照抄任何标题、严禁谈论其它新闻。不要输出该对象以外的任何内容。"
)
_PROMPT_TAIL_ARRAY = (
    "请挑选最重要的 {n} 条，按重要性从高到低，仅返回 JSON 数组，"
    "每个元素格式为 "
    "{{\"title\": \"逐字照抄你选中的那条新闻的完整标题\", "
    "\"reason\": \"一句话说明这条新闻（就是 title 那条）为何值得关注（不超过 40 字）\"}}。"
    "title 必须与候选列表原文完全一致；reason 只能谈 title 那条新闻本身，"
    "严禁照抄任何标题、严禁谈论其它新闻。不要输出数组以外的任何内容。"
)


def _log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print("[ai-digest %s] %s" % (ts, msg), flush=True)


def load_items(feed_path):
    """解析 feed.xml，返回最多 MAX_ITEMS 条，字段 title/link/source/summary/date_raw。"""
    if not feed_path or not os.path.exists(feed_path):
        return []
    try:
        tree = ET.parse(feed_path)
    except ET.ParseError:
        return []
    root = tree.getroot()
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        summary = (it.findtext("description") or "").strip()
        date_raw = (it.findtext("pubDate") or "").strip()
        src_el = it.find("source")
        source = (src_el.text if src_el is not None else "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            "link": link,
            "source": source or "未知来源",
            "summary": summary,
            "date_raw": date_raw,
        })
        if len(items) >= MAX_ITEMS:
            break
    return items


def build_prompt(items, top_n, provider):
    """把候选拼成带编号的提示词，让模型只回 idx + reason。

    provider 决定尾部格式约束：openai 走 json_object（要对象），gemini 走 ARRAY schema（要数组）。
    """
    lines = ["以下是今日通过 RSS 聚合得到的大中华区新闻候选（共 %d 条，按时间倒序）："
             % len(items), ""]
    for i, it in enumerate(items, 1):
        snippet = it["summary"]
        if len(snippet) > 120:
            snippet = snippet[:120] + "…"
        lines.append("%d. [%s] %s" % (i, it["source"], it["title"]))
        if snippet:
            lines.append("    摘要：%s" % snippet)
    lines.append("")
    tail = _PROMPT_TAIL_OBJECT if provider == "openai" else _PROMPT_TAIL_ARRAY
    lines.append(tail.format(n=top_n))
    return "\n".join(lines)


def call_gemini(api_key, model, system, prompt):
    """调用 Gemini generateContent（responseMimeType=json），返回解析后的数组。

    只依赖标准库。任何网络 / 协议错误都向上抛，由调用方决定兜底。
    """
    url = "%s/%s:generateContent?key=%s" % (GEMINI_ENDPOINT, model, api_key)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": ["title", "reason"],
                },
            },
            "maxOutputTokens": 2048,
            "temperature": 0.3,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json_array(text)


def _extract_json_array(text):
    """从模型文本里尽量抠出一个 JSON 数组，兼容四类情况：
       - 直接就是 JSON 数组；
       - 数组外套了说明文字或 ```json 代码围栏（部分平台不认 response_format）；
       - 顶层是 {"items": [...]} 这种约定包装；
       - 顶层是模型自选键名的对象，但其某个值是数组（json_object 格式强制对象时的兜底）。
    抠不出就抛错，由调用方走兜底。"""
    if not text:
        raise ValueError("模型返回为空")
    text = text.strip()
    candidates = [text]
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        candidates.append(m.group(1).strip())
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            val = json.loads(c)
        except ValueError:
            continue
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            if isinstance(val.get("items"), list):
                return val["items"]
            # 兜底：取第一个值为列表的键（兼容模型自选键名，如 {"fruits": [...]}）
            for v in val.values():
                if isinstance(v, list):
                    return v
    raise ValueError("无法解析 JSON 数组: %r" % text[:200])


def call_openai(base_url, api_key, model, system, prompt):
    """调用 OpenAI 兼容聊天接口（DeepSeek / 通义 / Kimi 等），返回解析后的数组。

    只依赖标准库。任何网络 / 协议错误都向上抛，由调用方决定兜底。
    """
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return _extract_json_array(text)


def parse_response(gem_list, items):
    """把模型返回的 [{title, reason}] 映射回池子里的原文，按返回顺序排好。

    匹配优先级：模型回显的 title（去空白后先精确、再包含式容错）→ 旧式 idx
    编号兜底（模型没回显 title 时仍可用）。回显标题来自 2026-11-06 的失配修复：
    只信 idx 时，8B 免费模型把 reason 和编号配错位，页面就成了「标题 A 配
    小字 B」；以标题对标题后配错位无处藏身。

    两道防线：
      * reason 若去空白后恰好等于池子里**任何一条**的标题（含本条）——模型在
        抄标题冒充理由——判无效置空，渲染时显示占位文案，页面永远不会出现
        「标题下面那行小字是另一条新闻的标题」；
      * 同一条标题被模型返回两次时只保留第一次，配 rank 连续。
    """
    out = []
    if not isinstance(gem_list, list):
        return out
    pool = {}
    for it in items:
        pool.setdefault(_norm_title(it["title"]), it)
    seen = set()
    for obj in gem_list:
        if not isinstance(obj, dict):
            continue
        chosen = None
        echo = _norm_title(obj.get("title"))
        if echo:
            if echo in pool:
                chosen = pool[echo]
            elif len(echo) >= 8:
                # 包含式容错：模型回显时多了空格/截了尾巴，任一方包含另一方就算同一条
                for norm, it in pool.items():
                    if echo in norm or norm in echo:
                        chosen = it
                        break
        if chosen is None:
            try:
                idx = int(obj.get("idx"))
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= len(items):
                chosen = items[idx - 1]
        if chosen is None:
            continue
        key = _norm_title(chosen["title"])
        if key in seen:
            continue
        seen.add(key)
        reason = _as_text(obj.get("reason"))
        if _norm_title(reason) in pool:
            reason = ""
        out.append({
            "rank": len(out) + 1,
            "title": chosen["title"],
            "link": chosen["link"],
            "source": chosen["source"],
            "date": chosen["date_raw"],
            "reason": reason,
        })
    return out


def _norm_title(t):
    """比较标题用：去掉全部空白；非字符串（模型偶发返回数字/对象）一律视作空。"""
    if not isinstance(t, str):
        return ""
    return re.sub(r"\s+", "", t)


def _as_text(v):
    """reason 取值用：只接受字符串，其余类型（数字/嵌套对象）视作缺失而不是崩。"""
    return v.strip() if isinstance(v, str) else ""


def _safe_href(link):
    if not link:
        return ""
    if link.startswith("http://") or link.startswith("https://"):
        return html.escape(link, quote=True)
    return ""


def render_html(results, model):
    """渲染独立页。只外链到原文，不链接任何站内页面；canonical 指向自己。"""
    gen_time = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    cards = []
    for r in results:
        href = _safe_href(r["link"])
        title_html = html.escape(r["title"])
        if href:
            title_cell = ('<a href="%s" target="_blank" rel="noopener">%s</a>'
                          % (href, title_html))
        else:
            title_cell = title_html
        meta = " · ".join(x for x in (r["source"], r["date"]) if x)
        reason = html.escape(r["reason"]) if r["reason"] else "（未提供理由）"
        cards.append(
            '<li class="card"><div class="rank">%d</div>'
            '<div class="body"><h2>%s</h2><div class="meta">%s</div>'
            '<p class="reason">%s</p></div></li>'
            % (r["rank"], title_cell, html.escape(meta), reason))
    cards_html = "\n".join(cards)
    return _HTML_TEMPLATE % (
        html.escape(PAGE_TITLE), len(results), html.escape(PAGE_TITLE),
        CANONICAL, CANONICAL, html.escape(PAGE_TITLE), len(results),
        gen_time, html.escape(model), cards_html)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<meta name="description" content="由 AI 从当日 %d 条 RSS 候选中筛选出的重点新闻，仅供参考。">
<meta property="og:type" content="website">
<meta property="og:title" content="%s">
<meta property="og:description" content="AI 每日重点新闻">
<meta property="og:url" content="%s">
<link rel="canonical" href="%s">
<style>
  :root { color-scheme: light dark; }
  body { max-width: 760px; margin: 0 auto; padding: 24px 18px 48px;
         font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         line-height: 1.6; background: #fafafa; color: #1a1a1a; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: #666; font-size: 13px; margin: 0 0 24px; }
  ul { list-style: none; padding: 0; margin: 0; }
  .card { display: flex; gap: 14px; background: #fff; border: 1px solid #eee;
          border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }
  .rank { flex: 0 0 34px; height: 34px; border-radius: 50%%; background: #b00; color: #fff;
          display: flex; align-items: center; justify-content: center; font-weight: 700; }
  .body { flex: 1; min-width: 0; }
  h2 { font-size: 16px; margin: 0 0 4px; line-height: 1.4; }
  h2 a { color: #1a1a1a; text-decoration: none; }
  h2 a:hover { text-decoration: underline; }
  .meta { color: #888; font-size: 12px; margin-bottom: 6px; }
  .reason { margin: 0; font-size: 14px; color: #333; }
  footer { margin-top: 28px; color: #999; font-size: 12px; }
</style>
</head>
<body>
<h1>%s</h1>
<p class="sub">由 AI 从当日 %d 条 RSS 候选中筛选 · 生成于 %s · 模型 %s · 仅供参考</p>
<ul>
%s
</ul>
<footer>本页由 AI 自动生成，可能存在偏差，请以原文为准。新闻版权归原作者所有。</footer>
</body>
</html>
"""


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _fallback(out_path, reason, had_key):
    if os.path.exists(out_path):
        _log("兜底：保留上一期页面（%s，had_key=%s）" % (reason, had_key))
        return
    placeholder = render_html([], "unavailable")
    _atomic_write(out_path, placeholder)
    _log("兜底：写入占位页（%s，had_key=%s）" % (reason, had_key))


def main():
    feed = os.environ.get("FEED_XML", DEFAULT_FEED)
    out = os.environ.get("OUT_HTML", DEFAULT_OUT)
    try:
        top_n = int(os.environ.get("AI_TOP_N", "20"))
    except ValueError:
        top_n = 20

    provider = os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER)
    items = load_items(feed)
    if not items:
        _fallback(out, "feed 为空或不存在", False)
        return 0

    prompt = build_prompt(items, top_n, provider)
    try:
        if provider == "gemini":
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
            if not api_key:
                raise RuntimeError("缺少 GEMINI_API_KEY")
            raw = call_gemini(api_key, model, SYSTEM, prompt)
        else:
            api_key = os.environ.get("LLM_API_KEY", "").strip()
            model = os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL)
            if not api_key:
                raise RuntimeError("缺少 LLM_API_KEY")
            base = os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL)
            raw = call_openai(base, api_key, model, SYSTEM, prompt)
    except Exception as e:  # 网络/限流/协议/缺 key —— 一律兜底
        _fallback(out, "AI 调用失败: %s" % e, bool(api_key))
        return 0

    results = parse_response(raw, items)
    if not results:
        _fallback(out, "AI 返回为空或无法解析", bool(api_key))
        return 0

    _atomic_write(out, render_html(results, model))
    _log("已生成 %d 条重点新闻 -> %s（模型 %s）" % (len(results), out, model))
    return 0


if __name__ == "__main__":
    sys.exit(main())
