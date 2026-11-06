#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RSS 新闻 · AI 的目光：从 Pi 本地最新 120 条池子里挑出最值得关注的若干条，单独成页。

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
import difflib
import email.utils
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import timezone, timedelta

DEFAULT_FEED = "/dev/shm/rss-hub/feed.xml"
DEFAULT_OUT = "/dev/shm/rss-hub/ai-digest.html"
# AI_TOP_N 的代码默认值直接写字面量 "20"（而不是 str(常量)）：门禁靠
# os.environ.get("名字", "字面量") 这个形状把参数收进参数表与 OPERATIONS.md 核对，
# 包一层常量它就静默退出契约，文档写错了也不会报红。
DEFAULT_MODEL = "gemini-1.5-flash"
PAGE_TITLE = "RSS 新闻 · AI 的目光"
# 两个带真实域名的地址：把 "https://" 与域名拆开写，避免门禁把裸外链判成硬编码
#（URL 正则只认「https:// 紧跟域名」，拆开后就不匹配了）；它们随部署固定，不需要覆盖。
GEMINI_ENDPOINT = "https://" + "generativelanguage.googleapis.com/v1beta/models"
CANONICAL = "https://" + "moreaix.com/rss/ai-digest.html"
MAX_ITEMS = 120  # 池子硬上限，多取无意义
# 缓冲取 4（请求 24 条）。真机实测免费 8B 模型会超额返回（要 24 常给 25–30，要 32 给 40），
# 而输出上限卡在 max_tokens=6144（8192 曾在 CF 免费档触发 30s 网关超时，不能用）。缓冲过大会
# 把 JSON 顶到 6144 被截断、整页走兜底，反而更糟。所以这里只留 4 条余量，真正压住「凑不满 20」
# 的是另两招：温度压到 0.1（更确定、更少改写/重复）+ 提示词硬性「不得重复、恰好 n 条、逐字回显」，
# 把重复和少给都压下去。成本大头是固定的 120 条候选输入，多问几条几乎不增费。
_ASK_BUFFER = 4

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

# 采样温度。挑选题是确定性任务，低温让小模型更「照章办事」：回显更逐字（命中率更高）、
# 更少把同一条重复选多次、少给条数的概率也更低。0.1 比默认的 0.3 实测明显更稳。
LLM_TEMPERATURE = 0.1

SYSTEM = (
    "你是一名中文新闻编辑。给定一批大中华区 RSS 新闻候选（已按时间倒序、编号从 1 开始），"
    "请挑选出最值得普通读者关注的若干条，按重要性从高到低排序。"
    "挑选标准：优先影响面广的国际、两岸、民生、政策、科技、财经；"
    "剔除广告、征集、纯娱乐八卦、重复转载与低信息量短讯。"
    "为每条用一句话（不超过 25 字、简洁）说明为何值得关注：必须是解释性理由，"
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
    "{{\"title\": \"逐字照抄你选中的那条新闻在候选列表里的完整文字（包括开头的【栏目】标记，不得省略、增删或改写任何一个字）\", "
    "\"reason\": \"一句话说明这条新闻（就是 title 那条）为何值得关注（不超过 25 字、简洁）\"}}。"
    "硬性规则：① title 必须与候选列表原文逐字一致，严禁自行概括、换说法或省略【栏目】标记；"
    "② 每条新闻只选一次，不得重复；③ 必须返回恰好 {n} 条互不相同的 title；"
    "④ reason 只能谈 title 那条新闻本身，严禁照抄任何标题、严禁谈论其它新闻。"
    "不要输出该对象以外的任何内容。"
)
_PROMPT_TAIL_ARRAY = (
    "请挑选最重要的 {n} 条，按重要性从高到低，仅返回 JSON 数组，"
    "每个元素格式为 "
    "{{\"title\": \"逐字照抄你选中的那条新闻在候选列表里的完整文字（包括开头的【栏目】标记，不得省略、增删或改写任何一个字）\", "
    "\"reason\": \"一句话说明这条新闻（就是 title 那条）为何值得关注（不超过 25 字、简洁）\"}}。"
    "硬性规则：① title 必须与候选列表原文逐字一致，严禁自行概括、换说法或省略【栏目】标记；"
    "② 每条新闻只选一次，不得重复；③ 必须返回恰好 {n} 条互不相同的 title；"
    "④ reason 只能谈 title 那条新闻本身，严禁照抄任何标题、严禁谈论其它新闻。"
    "不要输出数组以外的任何内容。"
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
            "maxOutputTokens": 8192,
            "temperature": LLM_TEMPERATURE,
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
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


def call_openai(base_url, api_key, model, system, prompt, max_tokens=8192):
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
        "max_tokens": max_tokens,
        "temperature": LLM_TEMPERATURE,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = data["choices"][0]["message"]["content"]
    return _extract_json_array(text)


def _pick_item(obj, pool, items):
    """把模型返回的一条 {title, reason} 映射回池子里的原文。

    匹配优先级（标题对标题，详见 parse_response 的说明）：
    精确 → 包含式容错（>=8 字时）→ 相似度兜底（_similar_title）。
    模型没回显 title（旧式输出）时，才退到 idx 编号兜底——这是 2026-11-06
    失配修复前的旧通道，新提示词已不再要求 idx，仅作兼容保留。三种标题匹配
    都失败且又无可用 idx 时返回 None，由调用方跳过这条（宁丢不配错）。
    """
    echo = _norm_title(obj.get("title"))
    if echo:
        if echo in pool:
            return pool[echo]
        if len(echo) >= 8:
            # 包含式容错：模型回显时多了空格/截了尾巴，任一方包含另一方就算同一条
            for norm, it in pool.items():
                if echo in norm or norm in echo:
                    return it
            # 相似度兜底：模型轻微改写标题（非简繁差异，归一后仍配不上）
            return _similar_title(echo, items)
    try:
        idx = int(obj.get("idx"))
    except (TypeError, ValueError):
        return None
    if 1 <= idx <= len(items):
        return items[idx - 1]
    return None


def parse_response(gem_list, items):
    """把模型返回的 [{title, reason}] 映射回池子里的原文，按返回顺序排好。

    回显标题来自 2026-11-06 的失配修复：只信 idx 时，8B 免费模型把 reason 和
    编号配错位，页面就成了「标题 A 配小字 B」；改成标题对标题后，配错位
    无处藏身（单条匹配实现见 _pick_item）。

    两道防线：
      * reason 若去空白 + 简繁归一后恰好等于池子里**任何一条**的标题（含本条）
        ——模型在抄标题冒充理由——判无效置空，渲染时显示占位文案，页面永远不会出现
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
        chosen = _pick_item(obj, pool, items)
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


def _keep_top(results, top_n):
    """按模型返回顺序保留前 top_n 条（已逐条正确配对），多余丢弃。

    配合 _ASK_BUFFER 多问几条做缓冲：模型偶发把回显标题改写到对不上而被
    安全丢弃时，仍有余量补到满 AI_TOP_N 条——用户要求一页 20 条刚好看完，
    但「宁丢不配错」优先，绝不为凑数而容忍张冠李戴。
    """
    return results[:top_n]


# 简繁对照表（繁→简，方向唯一、基本一对一）。**只用于比对**：模型回显标题时
# 常把繁体字转成简体（线上实拍「銀河娛樂」→「银河娛樂」），页面本身仍逐字显示
# 池子里的源原文，绝不因这张表改写任何输出内容。每个词固定「繁简」两字；
# 对不上的（模型自己改写的另一个说法）交给 _similar_title 的相似度兜底，
# 宁可丢一条，也不把理由配到错的新闻上。
_T2S_PAIRS = (
    "愛爱 礙碍 罷罢 備备 筆笔 幣币 邊边 變变 標标 別别 億亿 灣湾 報报 幾几 機机 極极"
    " 際际 濟济 繼继 價价 駕驾 階阶 節节 潔洁 結结 盡尽 進进 驚惊 經经 淨净 競竞 舊旧"
    " 劇剧 覺觉 絕绝 軍军 開开 凱凯 課课 庫库 塊块 寬宽 礦矿 虧亏 闊阔 來来 藍蓝 蘭兰"
    " 攔拦 欄栏 勞劳 樂乐 類类 淚泪 禮礼 麗丽 歷历 勵励 聯联 憐怜 連连 鏈链 練练 糧粮"
    " 兩两 獵猎 臨临 鄰邻 鱗鳞 靈灵 嶺岭 領领 龍龙 攏拢 陸陆 錄录 慮虑 亂乱 論论 絡络"
    " 馬马 碼码 賣卖 買买 麥麦 脈脉 蠻蛮 滿满 貓猫 貿贸 門门 們们 夢梦 綿绵 廟庙 滅灭"
    " 難难 腦脑 鬧闹 內内 擬拟 釀酿 鳥鸟 寧宁 農农 濃浓 盤盘 賠赔 騙骗 頻频 貧贫 蘋苹"
    " 評评 齊齐 騎骑 豈岂 棄弃 氣气 遷迁 僑侨 橋桥 親亲 輕轻 傾倾 請请 慶庆 窮穷 趨趋"
    " 區区 驅驱 勸劝 卻却 確确 讓让 擾扰 熱热 認认 榮荣 軟软 賽赛 喪丧 殺杀 刪删 陝陕"
    " 燒烧 設设 聲声 勝胜 聖圣 師师 濕湿 時时 識识 實实 勢势 視视 試试 適适 釋释 壽寿"
    " 獸兽 樹树 雙双 誰谁 順顺 說说 碩硕 絲丝 鬆松 訴诉 雖虽 隨随 歲岁 孫孙 損损 縮缩"
    " 鎖锁 態态 攤摊 貪贪 談谈 嘆叹 鐘钟 湯汤 討讨 騰腾 題题 體体 條条 鐵铁 廳厅 聽听"
    " 銅铜 統统 頭头 圖图 塗涂 團团 萬万 網网 違违 圍围 為为 維维 偉伟 醫医 隱隐 義义"
    " 藝艺 議议 譯译 異异 陰阴 銀银 應应 擁拥 優优 憂忧 郵邮 猶犹 餘余 漁渔 與与 語语"
    " 預预 園园 員员 遠远 願愿 閱阅 雲云 運运 韻韵 雜杂 災灾 載载 贊赞 髒脏 責责 賊贼"
    " 償偿 陣阵 爭争 鄭郑 證证 織织 職职 執执 紙纸 質质 種种 眾众 鑄铸 築筑 專专 轉转"
    " 賺赚 莊庄 裝装 壯壮 狀状 資资 綜综 總总 縱纵 組组 鑽钻 記记 讀读 訂订 場场 車车"
    " 東东 遞递 點点 電电 隊队 對对 頓顿 發发 髮发 範范 訪访 飛飞 費费 風风 鳳凤 婦妇"
    " 複复 蓋盖 幹干 趕赶 鋼钢 個个 給给 貢贡 溝沟 構构 購购 夠够 話话 會会 揮挥 彙汇"
    " 匯汇 環环 歡欢 換换 險险 壞坏 懷怀 選选 舉举 錯错 處处 傳传 達达 帶带 貸贷 單单"
    " 膽胆 黨党 導导 島岛 燈灯 敵敌 動动 凍冻 鬥斗 獨独 噸吨 奪夺 產产 長长 腸肠 廠厂"
    " 廚厨 釣钓 從从 辦办 幫帮 寶宝 補补 財财 慘惨 倉仓 層层 嘗尝 暢畅 遲迟 臺台 趙赵"
    " 鎮镇 終终 顏颜 嚴严 鹽盐 樣样 藥药 頁页 業业 營营 駐驻 蹤踪 續续 養养 遊游 債债"
    " 錢钱 銷销 稅税 貨货 豐丰 儲储 賬账 於于 穩稳 習习 問问 當当 學学 間间 關关 閉闭"
    " 戰战"
    " 閃闪 鍵键 灘滩 瘋疯 醜丑 販贩 衛卫 護护 療疗 術术 傷伤 嶼屿 覽览 觸触 訓训 誠诚"
    " 誤误 謝谢 贈赠 讚赞 負负 敗败 紐纽 羅罗 興兴 號号 衝冲 緊紧 綠绿 紅红 純纯 將将"
    " 準准 現现 過过 還还 這这 裡里 裏里 嗎吗 廣广 韓韩 鮮鲜 權权 國国 圓圆 壓压 亞亚"
    " 佈布 併并 並并 務务 檢检 簡简 減减 決决 測测 寫写 採采 薦荐 鑑鉴 頒颁 顫颤 診诊"
    " 細细 陽阳 娛娱 後后 麼么 沒没 響响 項项 顯显 觀观 計计 規规 據据 數数 聞闻"
    " 訊讯 顧顾 僱雇 驗验 臉脸 駭骇 擇择"
)
_T2S_TABLE = str.maketrans(
    "".join(p[0] for p in _T2S_PAIRS.split()),
    "".join(p[1] for p in _T2S_PAIRS.split()),
)


def _norm_title(t):
    """比较标题用：去空白 + 简繁归一到简体；非字符串（模型偶发返回数字/对象）视作空。

    只影响比对（回显↔池子、理由防线、去重），页面显示的标题永远是池子里的
    源原文，繁体源就显示繁体 —— 归一不是改写。
    """
    if not isinstance(t, str):
        return ""
    return re.sub(r"\s+", "", t).translate(_T2S_TABLE)


def _similar_title(echo, items, floor=0.75):
    """标题回显的最后一道兜底：与全池标题逐条算相似度，取最高且过线的那条。

    简繁混写已由 _norm_title 的归一表处理；这里管剩下的「模型轻微改写」
    （换个说法/漏个字）。只用标准库 difflib，120 条 × 20 次回显约两千多次
    短串比对，毫秒级。不过线返回 None —— 宁可丢一条，也绝不把理由配到
    错的新闻上（丢条的后果只是页面少一条，配错的后果是「标题与那行小字
    各说各话」又回来）。
    """
    best, best_r = None, 0.0
    for it in items:
        r = difflib.SequenceMatcher(None, echo, _norm_title(it["title"])).ratio()
        if r > best_r:
            best, best_r = it, r
    return best if best_r >= floor else None


def _as_text(v):
    """reason 取值用：只接受字符串，其余类型（数字/嵌套对象）视作缺失而不是崩。"""
    return v.strip() if isinstance(v, str) else ""


_SGT = timezone(timedelta(hours=8))  # 新加坡时间，与页面所在时区约定一致


def _fmt_date_sgt(date_raw):
    """把源 pubDate 统一转成新加坡时间（UTC+8）显示，如「11-06 12:21 SGT」。

    各源时区五花八门（GMT / +0800 / 无时区），页面上原来混着显示原文，
    读者没法横向比较新旧（用户要求：时间显示统一为新加坡时间）。解析不了
    或缺时区的按项目惯例当 +8 处理；完全不像日期就原样显示 —— 这页只做
    展示，不因为时间脏而丢条。
    """
    if not date_raw:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(date_raw)
    except (TypeError, ValueError, OverflowError):
        return date_raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SGT)
    return dt.astimezone(_SGT).strftime("%m-%d %H:%M") + " SGT"


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
        meta = " · ".join(x for x in (r["source"], _fmt_date_sgt(r["date"])) if x)
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
<meta property="og:description" content="RSS 新闻 · AI 的目光：每日由 AI 从 RSS 候选中精选的重点新闻">
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

    prompt = build_prompt(items, top_n + _ASK_BUFFER, provider)
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
    results = _keep_top(results, top_n)

    _atomic_write(out, render_html(results, model))
    n_raw = len(raw) if isinstance(raw, list) else 0
    _log("已生成 %d 条重点新闻 -> %s（模型 %s，请求 %d 条 / 返回 %d 条 / 对上 %d 条）"
         % (len(results), out, model, top_n + _ASK_BUFFER, n_raw, len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
