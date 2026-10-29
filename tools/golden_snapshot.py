#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行为快照：把渲染层全部纯函数 + RSS 生成在固定输入上跑一遍，输出可 diff 的文本。

用途：重构类改动的前后证据。任何「只是搬代码、不改行为」的改动，都要在改前改后
各跑一次，diff 为空才算真的没改行为。

    python3 tools/golden_snapshot.py > /tmp/before.txt
    # ……重构……
    python3 tools/golden_snapshot.py > /tmp/after.txt
    diff /tmp/before.txt /tmp/after.txt      # 必须无输出

日期、构建时间这类天然会变的东西统一替换成 <DATE>，保证两次输出可逐字节比较。
输入写死在文件里（不读 feeds.txt、不联网），所以它同时是一份「已知输出的样本集」。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_feed as B
import render as R

TITLES = [
    "卡特彼勒第三季度电力与能源业务营收增长12%",
    "净亏损20.15亿元，同比由盈转亏",
    "财联社10月29日电，瑞士政府已评估瑞银迁址影响，称若无总部将面临更严监管",
    "【独家】金十数据10月29日讯，微软启动2017年以来首次美国高评级债券发行",
    "万事达(MA.N)盘前上涨1.8%，第三季度业绩超出预期。",
    "港股收盘",
    "《中国与欧亚经济联盟经贸合作协定》联委会第五次会议召开，双方就深化合作交换意见",
    "国机精工第二大股东国新投资有限公司三季度增仓346.71万股，持股比例升至5.2%",
    "美联储维持利率不变 鲍威尔称通胀仍然偏高 🚀🔥 → 后续路径取决于数据",
    "AAAA BBBB CCCC DDDD 这是一段中英混排的标题 with spaces and 1234 numbers",
    "  前后有空格和全角空格　的标题  ",
    "短",
]

SUMMARIES = [
    "",
    "国机精工第二大股东国新投资有限公司三季度增仓346.71万股，持股比例升至5.2%。",
    "【卡特彼勒第三季度电力与能源业务营收增长12%】金十数据10月29日讯，"
    "卡特彼勒(CAT.N)第三季度调整后每股收益5.20美元，市场预估4.90美元。",
    "<p>带 HTML 标签的摘要&nbsp;内容，含 &amp; 实体与<br>换行，用来验证纯文本清洗。</p>",
]


def norm(s):
    s = re.sub(r"\w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}", "<DATE>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?", "<DATE>", s)
    s = re.sub(r"\d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2}", "<DATE>", s)
    return s


out = []
w = out.append

w("== 宽度 ==")
for t in TITLES:
    w("%-30s %.1f %s" % (t[:30], R.text_width(t), "".join("%.1f" % R.char_width(c) for c in t[:6])))

w("\n== split_title ==")
for t in TITLES:
    head, tail = R.split_title(t)
    w("IN  %r" % t)
    w("  HEAD %-16r %.1f" % (head, R.text_width(head)))
    w("  TAIL %r %.1f" % (tail, R.text_width(tail)))

w("\n== split_title 不同屏宽 ==")
for lw in (10, 12, 13, 14):
    w("lw=%d -> %r" % (lw, R.split_title(TITLES[0], line_w=lw)))

w("\n== strip_dateline / norm_title ==")
for t in TITLES:
    w("%-24r -> %r | key=%r" % (t[:24], R.strip_dateline(t), R.norm_title(t)))

w("\n== compose_body ==")
for t, s in zip(TITLES[:6], SUMMARIES):
    _, tail = R.split_title(t)
    body = R.compose_body(tail, s, t)
    w("T %r\n  BODY %r  w=%.1f" % (t[:24], body, R.text_width(body)))

w("\n== compose_body 长度上限 ==")
for bm in (0, 40, 60, 90):
    w("bm=%d -> %r" % (bm, R.compose_body("", "字" * 200, "标题", body_max=bm)))

w("\n== dedupe ==")
items = [
    {"title": TITLES[2], "link": "http://x/1?utm_source=a", "src": "a", "ts": 5},
    {"title": TITLES[2].replace("财联社10月29日电，", ""), "link": "http://x/1", "src": "b", "ts": 4},
    {"title": "港股收盘", "link": "http://x/2", "src": "c", "ts": 3},
    {"title": "港股收盘", "link": "http://x/3", "src": "d", "ts": 2},
    {"title": TITLES[0], "link": "http://x/4", "src": "e", "ts": 1},
]
kept, dropped = R.dedupe([dict(i) for i in items])
w("kept %d dropped %d" % (len(kept), dropped))
for k in kept:
    w("  %-20r %s" % (k["title"][:20], k["src"]))

w("\n== item_guid / esc ==")
for i in items:
    w("%s  %r" % (R.item_guid(i), R.esc(i["title"][:12])))

w("\n== build_rss（设备版）==")
rich = []
for n, t in enumerate(TITLES):
    rich.append({"title": t, "link": "http://x/%d" % n, "src": "s%d" % (n % 3),
                 "src_name": "源%d" % (n % 3), "ts": 2000 - n,
                 "date_raw": "Thu, 29 Oct 2026 12:00:00 +0800",
                 "summary": SUMMARIES[n % len(SUMMARIES)]})
stats = {"total": 3, "ok": 3, "stale": 0, "fail": 0, "items": len(rich), "dropped": 0}
w(norm(R.build_rss([dict(r) for r in rich], "http://x/feed.xml", stats, screen=True)))

w("\n== build_rss（完整版）==")
w(norm(R.build_rss([dict(r) for r in rich], "http://x/feed.xml", stats)))

w("\n== rotate_pick 序列 ==")
pool = [{"title": "T%d" % i, "link": "http://x/r%d" % i, "src": "s%d" % (i % 4), "ts": 1000 - i}
        for i in range(9)]
st, seq = {}, []
for win in range(1, 13):
    pick, st, note = B.rotate_pick([dict(p) for p in pool], st, now=win * B.ROTATE_SECONDS)
    seq.append("%s(%s,%s)" % (pick["title"], st["cycle"], len(st["shown"])))
w(" ".join(seq))

w("\n== plain_text ==")
for s in ["A🚀B🔥C", "→指针←", "零\u200b宽\ufeff字符", "全角　空格", "  ·|、,， 收尾  "]:
    w("%r -> %r" % (s, R.plain_text(s)))

print("\n".join(out))
