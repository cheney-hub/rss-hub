#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给「时间戳失真」的源一个可信的排序时间 —— 其余源的 pubDate 一律照用。

绝大多数源的 `<pubDate>` 是逐条各不相同的真实发布时刻，直接可信：2026-11-01 实测
62 个源里有 60 个如此（德国之声 63 条里 62 个不同时间、纽约时报跨度 82 小时）。
只有极少数源给整份 feed 打**同一个**时间戳，且每次重新生成就刷成当前时刻 ——
本仓库里是日经中文网与光明日报·版面。

后果不是报错，而是「看起来正常、实则霸榜」：它们每一轮都排在最前面五个位置，
同一批稿子里的同一条（本次是「三星…」）因为并列时的次级键固定，永远占着头一名，
别的源再新也只能排第六。屏上读到的人会发现「怎么老是这条」。

判据只有一条：**同一源这一批条目的时间戳完全相同**——那不是发布时间，是这份 feed
的生成时钟。此时改用「我们第一次见到这条的时间」：条目的年龄应当从它进入我们池子
那一刻起算，而不是从源每次刷新的那一刻起算。

「首次见到」取自**上一轮的产物**：那里每条带着的时间就是我们当时认定的时间，
格式与本轮写出的一致，所以不需要额外的状态文件，重启也不需要预热。
取 min(上一轮, 本轮标称) 而不是直接覆盖：万一源补发旧稿，取小值保证「年龄只增不减」，
条目不会换个时间戳就变年轻。
"""
import email.utils
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import version  # noqa: E402  （版本号唯一真源）

__version__ = version.__version__

# 少于这几条的源不足以判断「是不是共用时间戳」：只有 1~2 条时本来就可能同秒，
# 误判会把正常源按失真处理，代价（条目变旧）大于收益。
MIN_ITEMS = 3


def clock_stamped_sources(items):
    """返回「整份条目共用一个时间戳」的源地址集合。

    只认时间戳**完全相同**：差一秒就说明源真的在逐条标注发布时刻（对的就是对的，
    不因「这批凑巧很集中」就把它归类成失真源）。
    """
    buckets = {}
    for it in items:
        ts = it.get("ts") or 0
        # 时间戳解析失败的条目 ts 一律是 0，一堆 0 不代表「源给整份 feed 打了同一个
        # 时钟」——那是解析层的事，归不到失真源头上。把它们算进来会让
        # clock_stamped_sources() 的返回值混进「其实是没时间」的源。
        if not ts:
            continue
        buckets.setdefault(it.get("src") or "", []).append(ts)
    return {src for src, stamps in buckets.items()
            if len(stamps) >= MIN_ITEMS and len(set(stamps)) == 1}


def prev_seen_index(prev_items):
    """把 load_prev_items() 的结果压成 guid → 上一轮认定的时间戳。

    同一个 guid 出现多次时取最早的那次（`prev_items` 内部已按源分桶，这里再兜一层，
    免得将来换了存储形状后挑到一个偏晚的值）。
    """
    out = {}
    for bucket in (prev_items or {}).values():
        for it in bucket:
            ts = it.get("ts") or 0
            if not ts:
                continue
            key = _guid(it)
            # 不能写成 `ts < out.get(key, ts)`：键不存在时它拿自己当基准，
            # `ts < ts` 永远为假，结果整张表是空的（补这条用例时抓到的）。
            if key and (key not in out or ts < out[key]):
                out[key] = ts
    return out


def prev_source_index(prev_items):
    """把 load_prev_items() 压成 源地址 → 上一轮该源条目里**最早**的时间戳。

    为什么除了条目级的 `prev_seen_index` 还要按源记一份：那份索引以 guid 为键，
    而条目会被 `ITEM_LIMIT`（默认 120）裁出产物 —— 一旦掉出这 120 条，它的
    「首次见到」记录就随产物消失了。下一轮它带着源刷新的新时钟重新入池，
    条目级索引查不到它，`settle` 于是把这个新时钟当成首见时间永久锁定：
    **条目真的变年轻了**，排到全池最前。这不是推演出来的，是「怎么老是这条」
    隔几小时复发一次的成因（用 ITEM_LIMIT=3 加速复现过：条目 ts 逐轮往上爬）。

    失真源的整批条目共用一个时间戳，所以「上一轮该源任何一条的时间」就等于
    「上一轮这个源的时钟」，拿它兜底正好补上这个洞。
    取最早而不是最新：产物里可能同时留着被 settle 改写过的旧值与源刷出来的新值，
    取小值才符合「年龄只增不减」。

    代价要说清楚：该源**换新稿**时，新稿也会拿到上一轮那个时间、显得偏老。
    失真源的时间本就不可信（它们的排序整个是靠这个模块撑着的），
    「偶发排得偏后」比「周期性霸榜」轻得多。
    """
    out = {}
    for bucket in (prev_items or {}).values():
        for it in bucket:
            ts = it.get("ts") or 0
            src = it.get("src") or ""
            if not ts or not src:
                continue
            if src not in out or ts < out[src]:
                out[src] = ts
    return out


def settle(items, prev_items):
    """把失真源条目的时间改成「首次见到时间」，返回改写条数。

    改写的是 `ts`（排序依据）**和** `date_raw`（写进产物的那个字符串）：只改前者
    会被后者覆盖回去 —— 下一轮从产物里读到的又是源标称的那个时钟，记忆链就断了。

    正常情况下返回 0，池子原样不动；认不出上一轮（首次运行）也返回 0，
    因为那一刻「首次见到」就等于本轮标称的时间。
    """
    clocked = clock_stamped_sources(items)
    if not clocked:
        return 0
    prev = prev_seen_index(prev_items)
    prev_src = prev_source_index(prev_items)
    if not prev and not prev_src:
        return 0
    changed = 0
    for it in items:
        if (it.get("src") or "") not in clocked:
            continue
        # 条目级记忆优先，查不到才退到源级（为什么需要源级见 prev_source_index）
        was = prev.get(_guid(it)) or prev_src.get(it.get("src") or "")
        if not was or was >= (it.get("ts") or 0):
            continue
        it["ts"] = was
        it["date_raw"] = email.utils.format_datetime(
            datetime.fromtimestamp(was, timezone.utc))
        changed += 1
    return changed


def _guid(it):
    """条目的稳定身份：`render.item_guid`（链接 + 源的短哈希）。"""
    from render import item_guid       # 延迟导入：render 不依赖本模块，避免循环
    return item_guid(it)
