#!/usr/bin/env python3
# 一次性诊断：多温度/多 token 上限下，统计命中率与丢弃归因。
# 用法: python3 diag_ai.py <trials> <temp> <maxtok>
import os, sys
sys.path.insert(0, "/home/user/pi-pipeline")
import ai_digest as AD

env = {}
for p in ("/home/user/site/llm.env",):
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"')
os.environ.update(env)

FEED = "/dev/shm/rss-hub/feed.xml"
os.environ["FEED_XML"] = FEED
os.environ["AI_TOP_N"] = "20"
os.environ["LLM_PROVIDER"] = "openai"
os.environ["LLM_MODEL"] = "@cf/meta/llama-3.1-8b-instruct-fp8-fast"

N = int(sys.argv[1]) if len(sys.argv) > 1 else 4
TEMP = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
MAXTOK = int(sys.argv[3]) if len(sys.argv) > 3 else 8192
AD.LLM_TEMPERATURE = TEMP

items = AD.load_items(FEED)
pool = {AD._norm_title(it["title"]): it for it in items}
api_key = os.environ.get("LLM_API_KEY", "").strip()

ok = 0
for trial in range(N):
    prompt = AD.build_prompt(items, 20 + AD._ASK_BUFFER, "openai")
    try:
        raw = AD.call_openai(AD.DEFAULT_LLM_BASE_URL, api_key, os.environ["LLM_MODEL"],
                             AD.SYSTEM, prompt, max_tokens=MAXTOK)
    except Exception as e:
        print("trial %d 调用失败: %s" % (trial, e)); continue
    raw_list = raw if isinstance(raw, list) else []
    matched = 0
    seen = set()
    dropped = 0
    for obj in raw_list:
        if not isinstance(obj, dict):
            dropped += 1; continue
        it = AD._pick_item(obj, pool, items)
        if it is None:
            dropped += 1
        else:
            key = AD._norm_title(it["title"])
            if key in seen:
                dropped += 1
            else:
                seen.add(key); matched += 1
    kept = min(matched, 20)
    if kept >= 20:
        ok += 1
    print("trial %d: 返回%d 去重后%d 截留%d %s" % (trial, len(raw_list), matched, kept, "✓" if kept >= 20 else "✗不足"))
print("配置 temp=%.2f maxtok=%d -> 达标 %d/%d" % (TEMP, MAXTOK, ok, N))
