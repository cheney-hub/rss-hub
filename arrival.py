#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给「时间不可信」的源/条目一个可信的排序时间 —— 其余源的 pubDate 一律照用。

不可信分两类：
  (A) 时间戳失真：源给整份 feed 打同一个时间戳，且每次重新生成就刷成当前时刻
      （日经中文网、光明日报·版面）。详见下方正文。
  (B) 无源时间：条目根本没有 pubDate（缺失或解析失败，ts==0）。源连时间戳都没有，
      第一次见到就以「本次抓取时刻」落定并标记，之后再见到同一条沿用首次时间，
      不跟着每轮刷新往前漂。详见 `settle` 的 docstring。

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
import time
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


def settle(items, prev_items, now=None):
    """把「时间不可信」的条目改成首次见到时间，返回 (失真源改写条数, 无源时间条数)。

    时间不可信分两类，都改成「第一次见到这条的时间」——条目年龄从进入池子那一刻起算：
      (A) 失真源：整份 feed 共用一个时间戳且每轮刷新（日经、光明日报·版面）。源给的时间
          不是发布时间、是生成时钟，按上轮记忆压住，防它每轮往前爬而霸榜（详见模块说明）。
      (B) 无源时间：条目根本没 pubDate（缺失或解析失败，ts==0）。源连时间戳都没有，
          第一次见到就以「本次抓取时刻」落定并打 `no_source_time` 标记；之后再见到同一条
          就沿用首次时间，不再跟着每轮刷新往前漂 —— 与 A 是同一动机（否则无时间源永远
          ts=0、沉底被裁，等于整源在 feed 里消失；或每次都拿 now 当时间、永远最新而霸榜）。

    两种都必须同时改写 `ts` 与 `date_raw`：只改前者会被产物字符串覆盖回去，记忆链断掉。

    优先级（A 类）：条目级记忆 > 源级记忆（源级只兜底「条目掉出 120 条窗口」那种，
    B 类按 guid 即可、不需要源级——无时间源每条都靠 link+src 区分，误用源级最小值
    会把新稿错配成旧时间，反而造出新 bug）。

    now 允许调用方传入固定值（测试/可复现）；不传则取本轮抓取时刻。
    """
    if now is None:
        now = int(time.time())
    clocked = clock_stamped_sources(items)
    # 没有失真源、且每条都带着可信时间戳 → 原样返回，省一次遍历
    if not clocked and all((it.get("ts") or 0) > 0 for it in items):
        return 0, 0
    prev = prev_seen_index(prev_items)        # guid → 上次认定的 ts
    prev_src = prev_source_index(prev_items)  # 源 → 上次该源最早 ts（A 类兜底）

    settled, no_time = 0, 0
    for it in items:
        src = it.get("src") or ""
        ts = it.get("ts") or 0
        if ts == 0:
            # 情况 B：源没给时间，以首次获取时间为准。prev 查得到就沿用首见时间，
            # 查不到（第一次见到）就以本轮抓取时刻 now 落定，并打标记。
            no_time += 1
            was = prev.get(_guid(it)) or now
            it["ts"] = was
            it["date_raw"] = email.utils.format_datetime(
                datetime.fromtimestamp(was, timezone.utc))
            it["no_source_time"] = True
            continue
        if src in clocked:                       # 情况 A
            # 条目级记忆优先，查不到才退到源级（为什么需要源级见 prev_source_index）
            was = prev.get(_guid(it)) or prev_src.get(src)
            if was and was < ts:
                it["ts"] = was
                it["date_raw"] = email.utils.format_datetime(
                    datetime.fromtimestamp(was, timezone.utc))
                settled += 1
    return settled, no_time


def _guid(it):
    """条目的稳定身份：`render.item_guid`（链接 + 源的短哈希）。"""
    from render import item_guid       # 延迟导入：render 不依赖本模块，避免循环
    return item_guid(it)
