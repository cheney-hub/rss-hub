#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行为快照：把渲染层全部纯函数 + RSS 生成在固定输入上跑一遍，输出可 diff 的文本。

用途有两个：

1. **重构证据**——任何「只搬代码、不改行为」的改动，都要在改前改后各跑一次，
   diff 为空才算真的没改行为：

       python3 tools/golden_snapshot.py > /tmp/before.txt
       # ……重构……
       python3 tools/golden_snapshot.py > /tmp/after.txt
       diff /tmp/before.txt /tmp/after.txt      # 必须无输出

2. **回归基线**——`tests/test_golden.py` 拿当前输出跟入库的 `tests/golden.txt`
   逐字节比对，任何「顺手改了排版」的动作都会在 CI 里现形：

       python3 tools/golden_snapshot.py --write   # 确认是有意改动后更新基线
       # 别忘了在 CHANGELOG.md 里写清改了什么、为什么

日期、构建时间这类天然会变的东西统一替换成 <DATE>，保证两次输出可逐字节比较。
输入写死在文件里（不读 feeds.txt、不联网），所以它同时是一份「已知输出的样本集」。
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import build_feed as B   # noqa: E402
import render as R       # noqa: E402
from version import __version__  # noqa: E402

GOLDEN = os.path.join(ROOT, "tests", "golden.txt")

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
    # 数字 + 量词被切开：「…超3」+「亿条」——_avoid_orphan_number() 的回归样本
    "日本今年前10月外泄信息超3亿条 或系AI无差别攻击",
]

SUMMARIES = [
    "",
    "国机精工第二大股东国新投资有限公司三季度增仓346.71万股，持股比例升至5.2%。",
    "【卡特彼勒第三季度电力与能源业务营收增长12%】金十数据10月29日讯，"
    "卡特彼勒(CAT.N)第三季度调整后每股收益5.20美元，市场预估4.90美元。",
    "<p>带 HTML 标签的摘要&nbsp;内容，含 &amp; 实体与<br>换行，用来验证纯文本清洗。</p>",
]


def norm(s):
    """把时间戳和版本号换成占位符，让两次输出可逐字节比较。

    版本号也必须归一：不然每次发版都要重生成基线，
    基线一改就没人知道「这次变的到底是排版还是版本号」。
    版本是否真的写进了 <generator>，由 tests/test_core.py 单独断言。
    """
    s = re.sub(r"\w{3}, \d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}", "<DATE>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?", "<DATE>", s)
    s = re.sub(r"\d{2} \w{3} \d{4} \d{2}:\d{2}:\d{2}", "<DATE>", s)
    return s.replace(__version__, "<VER>")


def _sec_layout(w):
    """参数与宽度折算：改 SCREEN_LINE_WIDTH / 宽度算法会动这一段。"""
    w("== 版本 ==")
    w("version <VER>（格式校验见 tests/test_core.py）")
    w("LINE_WIDTH %g  BODY_MAX %g  FEED_BODY_MAX %g" % (R.LINE_WIDTH, R.BODY_MAX,
                                                       R.FEED_BODY_MAX))

    w("\n== 宽度 ==")
    for t in TITLES:
        w("%-30s %.1f %s" % (t[:30], R.text_width(t),
                             "".join("%.1f" % R.char_width(c) for c in t[:6])))

    w("\n== split_title ==")
    for t in TITLES:
        head, tail = R.split_title(t)
        w("IN  %r" % t)
        w("  HEAD %-16r %.1f" % (head, R.text_width(head)))
        w("  TAIL %r %.1f" % (tail, R.text_width(tail)))

    w("\n== split_title 不同屏宽 ==")
    for lw in (10, 12, 13, 14):
        w("lw=%d -> %r" % (lw, R.split_title(TITLES[0], line_w=lw)))


def _sec_text(w):
    """清理与正文合成：电头剥离、去重键、摘要拼接、长度上限。"""
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


def _sec_dedupe(w):
    """跨源去重与条目身份。"""
    w("\n== dedupe ==")
    items = _sample_items()
    kept, dropped = R.dedupe([dict(i) for i in items])
    w("kept %d dropped %d" % (len(kept), dropped))
    for k in kept:
        w("  %-20r %s" % (k["title"][:20], k["src"]))

    w("\n== item_guid / esc ==")
    for i in items:
        w("%s  %r" % (R.item_guid(i), R.esc(i["title"][:12])))


def _sec_rss(w):
    """两份 RSS 的逐字节输出：任何字段顺序 / 转义 / 截断的变化都会在这里现形。"""
    rich = _rich_items()
    stats = {"total": 3, "ok": 3, "stale": 0, "fail": 0, "items": len(rich), "dropped": 0}
    w("\n== build_rss（设备版）==")
    w(norm(R.build_rss([dict(r) for r in rich], "http://x/feed.xml", stats, screen=True)))
    w("\n== build_rss（完整版）==")
    w(norm(R.build_rss([dict(r) for r in rich], "http://x/feed.xml", stats)))


def _sec_rotate(w):
    """轮播游标序列 + 纯文本化。"""
    w("\n== rotate_pick 序列 ==")
    # 每条间隔一个时间片（= 设备刷新周期），这样才有「逐片推进」可看。
    # 若都塞在同一个片里，按片轮播只会出一条代表，等于什么都没测到。
    pool = [{"title": "T%d" % i, "link": "http://x/r%d" % i, "src": "s%d" % (i % 4),
             "ts": 1000 - i * B.ROTATE_SECONDS} for i in range(9)]
    st, seq = {}, []
    for win in range(1, 13):
        pick, st, _ = B.rotate_pick([dict(p) for p in pool], st,
                                    now=win * B.ROTATE_SECONDS)
        seq.append("%s(%s,%s)" % (pick["title"], st["cycle"], len(st["shown"])))
    w(" ".join(seq))

    w("\n== plain_text ==")
    for s in ["A🚀B🔥C", "→指针←", "零\u200b宽\ufeff字符", "全角　空格", "  ·|、,， 收尾  "]:
        w("%r -> %r" % (s, R.plain_text(s)))


def _sample_items():
    """去重样本：同一事件两源、同标题不同链接、短标题（不该参与标题去重）。"""
    return [
        {"title": TITLES[2], "link": "http://x/1?utm_source=a", "src": "a", "ts": 5},
        {"title": TITLES[2].replace("财联社10月29日电，", ""), "link": "http://x/1",
         "src": "b", "ts": 4},
        {"title": "港股收盘", "link": "http://x/2", "src": "c", "ts": 3},
        {"title": "港股收盘", "link": "http://x/3", "src": "d", "ts": 2},
        {"title": TITLES[0], "link": "http://x/4", "src": "e", "ts": 1},
    ]


def _rich_items():
    """RSS 样本：带 summary / src_name / date_raw，覆盖四种摘要形态（含空摘要）。"""
    rich = []
    for n, t in enumerate(TITLES):
        rich.append({"title": t, "link": "http://x/%d" % n, "src": "s%d" % (n % 3),
                     "src_name": "源%d" % (n % 3), "ts": 2000 - n,
                     "date_raw": "Thu, 29 Oct 2026 12:00:00 +0800",
                     "summary": SUMMARIES[n % len(SUMMARIES)]})
    return rich


def snapshot():
    """跑一遍全部样本，返回快照文本（纯函数：不读文件、不联网、不写文件）。"""
    out = []
    for section in (_sec_layout, _sec_text, _sec_dedupe, _sec_rss, _sec_rotate):
        section(out.append)
    return "\n".join(out) + "\n"


def main():
    text = snapshot()
    if "--write" in sys.argv[1:]:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        with io.open(GOLDEN, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print("已更新基线 %s（%d 行）" % (GOLDEN, text.count("\n")))
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
