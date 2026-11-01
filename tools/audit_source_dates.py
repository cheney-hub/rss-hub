#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅源时间戳体检：逐个源量一遍「它给的发布时间到底能不能信」。

为什么要这个脚本：时间决定排序，而排序决定谁能上屏。绝大多数源的 `<pubDate>` 是逐条
各不相同的真实发布时刻，可以直接信；但确实有源把整份 feed 打同一个时间戳、且每次
重新生成就刷成当前时刻 —— 那种源每轮都自动成为「全场最新」，别人再新也排不到前面。
光看屏幕永远查不出这件事（它不报错），只能逐个源去量，而且必须**隔一段时间采样两次**
才看得出「同一条的时间会不会变」。

体检项与判据（按严重程度排列，一个源只报最严重的那一项，其余进备注）：
  无时间   这一轮一条 pubDate 都解析不出来（取不到也算，单独记账）
  未来时间 最新一条比本机时间还晚 FUTURE_TOL 秒以上
  共用时间戳 同一轮里 >= MIN_ITEMS 条的时间戳完全相同 —— 那是 feed 的生成时钟，
            不是发布时间（日经中文网、光明日报·版面就是这种）
  逐条改时间 同一条在两次采样之间时间戳变了（源在偷偷重盖章）
  正常     两次采样里同一条的时间稳定，且各条互不相同

用法（要在能连外网、且有本仓库根目录的机器上跑，比如树莓派）：
    PYTHONPATH=/home/user/rss-hub python3 tools/audit_source_dates.py
    python3 tools/audit_source_dates.py --once          # 只采一次：快，但查不出逐条改时间
    python3 tools/audit_source_dates.py --gap 900       # 两次采样间隔 15 分钟
    python3 tools/audit_source_dates.py --only 日经 --only 光明

退出码恒为 0：这是体检报告，不是门禁 —— 源的行为每天都可能变，让它自己红不符合用途。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arrival            # noqa: E402  （共用「几条以上才算」这个门槛）
import build_feed as B    # noqa: E402  （抓取与解析跟生产同源）
from render import item_guid  # noqa: E402

# 允许多少秒的未来时间：源站与本机时钟差个几十秒很常见，超出这个才当异常。
# （历史教训：星岛头条网曾给出比当时晚 15 小时的 pubDate，会把整块看板顶到未来）
FUTURE_TOL = 300

VERDICTS = ("无时间", "未来时间", "共用时间戳", "逐条改时间", "正常")


def sample_once(entry, timeout=None):
    """抓一个源，返回 [(guid, ts)]；取不到返回 None。

    用生产的 `parse_items` 解析（而不是自己写的正则），免得体检和生产对同一份字节
    得出两套结论 —— 那种偏差最难发现，因为两边单独看都对。
    """
    try:
        status, body, charset = B.http_get(entry["url"])
        xml = B.decode_body(body, charset) if body else ""
        items = B.parse_items(xml, entry["url"]) if xml else []
    except Exception as exc:                                  # noqa: BLE001
        print("    # %s 取不到：%s %s" % (entry["name"], type(exc).__name__, exc))
        return None
    return [(item_guid(it), it.get("ts") or 0) for it in items]


def shared_stamp(sample):
    """同一批里是不是所有时间戳都一样（且条数够得上判断）。"""
    stamps = {ts for _, ts in sample if ts}
    return len(sample) >= arrival.MIN_ITEMS and len(stamps) == 1


def classify(samples, now=None):
    """给一个源下结论，返回 (判定, 备注列表)。

    samples 是多次采样的结果列表，元素为 [(guid, ts)]，允许只有一次（`--once`）。
    None 表示该次没抓到，跳过 —— 抓不到不是「时间有问题」，别混为一谈。
    """
    now = now if now is not None else time.time()
    good = [s for s in samples if s]
    if not good:
        return "取不到", []
    flat = [(g, ts) for s in good for g, ts in s]
    notes = []

    if not any(ts for _, ts in flat):
        return "无时间", notes

    newest = max(ts for _, ts in flat if ts)
    if newest - now > FUTURE_TOL:
        notes.append("最新一条比本机时间晚 %.1f 小时，会把整块看板顶到未来"
                     % ((newest - now) / 3600))
        return "未来时间", notes

    if any(shared_stamp(s) for s in good):
        n = len({g for g, _ in good[0]})
        notes.append("本轮 %d 条共用一个时间戳" % n)
        if _restamped(good):
            notes.append("两次采样之间同一个钟的值又变了（整个源重盖章）")
        return "共用时间戳", notes

    drift = _restamped(good)
    if drift:
        notes.append("%d 条的时间戳在两次采样之间变了，最多差 %d 秒"
                     % (len(drift), max(abs(d) for d in drift.values())))
        return "逐条改时间", notes
    return "正常", notes


def _restamped(good):
    """两批采样里同一条的 ts 之差；没交集返回空 dict。"""
    if len(good) < 2:
        return {}
    prev = {g: ts for g, ts in good[0]}
    out = {}
    for g, ts in good[1]:
        if g in prev and prev[g] != ts:
            out[g] = ts - prev[g]
    return out


def audit(entries, rounds=2, gap=600, timeout=None, label=""):
    """跑一轮体检，返回 [(源名, 判定, 备注)]，判定最严重的排前面。"""
    samples = {e["name"]: [] for e in entries}
    for r in range(rounds):
        if r:
            print("等 %d 秒后采样第 %d/%d 次…" % (gap, r + 1, rounds))
            time.sleep(gap)
        print("采样第 %d/%d 次（%d 个源）" % (r + 1, rounds, len(entries)))
        for e in entries:
            samples[e["name"]].append(sample_once(e, timeout))
    rows = []
    for name, ss in samples.items():
        verdict, notes = classify(ss)
        rows.append((name, verdict, notes))
    order = {v: i for i, v in enumerate(("取不到",) + VERDICTS)}
    rows.sort(key=lambda r: (order.get(r[1], 9), r[0]))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="订阅源时间戳体检")
    ap.add_argument("--feeds", default=None, help="源清单，默认同 build_feed 的 FEEDS_FILE")
    ap.add_argument("--once", action="store_true", help="只采样一次（查不出逐条改时间）")
    ap.add_argument("--gap", type=int, default=600, help="两次采样间隔秒数，默认 600")
    ap.add_argument("--timeout", type=int, default=None, help="单个源超时，默认用 FETCH_TIMEOUT")
    ap.add_argument("--only", action="append", default=[],
                    help="只体检名字里含这个词的源，可重复")
    args = ap.parse_args(argv)

    entries = B.read_feed_entries(args.feeds)
    if args.only:
        entries = [e for e in entries if any(k in e["name"] for k in args.only)]
    if not entries:
        print("没有要体检的源")
        return 0
    rows = audit(entries, rounds=1 if args.once else 2, gap=args.gap, timeout=args.timeout)

    print()
    print("%-24s %-10s %s" % ("源", "判定", "备注"))
    print("-" * 78)
    for name, verdict, notes in rows:
        print("%-24s %-10s %s" % (name[:24], verdict, "；".join(notes)))
    print()
    counts = {}
    for _, v, _ in rows:
        counts[v] = counts.get(v, 0) + 1
    print("小结：" + " / ".join("%s %d 个" % (v, n) for v, n in sorted(
        counts.items(), key=lambda kv: -kv[1])))
    print("处置：共用时间戳、逐条改时间的源由 arrival.py 在排序前兜住（改用首次见到时间）；"
          "未来时间的源需要单独盯，必要时直接从清单里摘掉。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
