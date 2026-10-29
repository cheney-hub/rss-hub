#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轮播机制仿真：按真实节律跑 24 小时，量「一条消息多久能上屏」。

为什么需要它：轮播的正确性靠单测只能验证「逐片推进、不重复」这类局部性质，
真正的指标是端到端的 —— 新鲜度（新消息多久上屏）、覆盖率（多少条播得到）、
顺序性（有没有倒序）。这三项必须在「产能 > 消费」的真实压力下才暴露。

被仿真的机制是「锚定当前窗」（`build_feed.rotate_pick`）：每窗播池子里最新的、
还没播过的那一片。关键看「新鲜度」是否**不随运行时长增长**——旧机制（开轮时冻结清单、
从新到旧推进）的表现是延迟随轮次线性上升（24 小时中位 120 分、最差 4.3 小时），
换成锚定当前窗后中位降到 0~1 分，但**尾巴仍很长**（低产能时只能播池里的存货，见下）。
也可以用 `builder=` 注入别的实现来做对照。

仿真假设（取自线上实测）：
  * 白天 06:00–23:59 每 5 分钟构建一次，每窗各源合计新增数按**泊松抽样**（均值 2.35 条）
    —— 真实产出是随机的，有的窗 0 条、有的窗 6 条。写成「每窗恒 2~3 条」会人为保证
    「每窗都有新片可播」，于是永远在播「刚发布的」，量出来的尾巴（最差延迟）就全是假的。
  * 夜间 00:00–05:59 每小时构建一次，但源一直在产出，故每次积压约 28 条（同样按泊松抽）
  * 条目发布时间围绕抓取时刻抖动：白天摊满**一个时间片**（0~`DWELL`-1 秒），
    夜间摊满**一小时**（0~3600 秒）。这个抖动幅度是模型的关键 —— 若延迟小于一个片，
    每窗必然恰好 mint 出一个新片，低产能的真相（一窗一片都凑不齐）就被抹掉了。
  * 设备池上限 `ROTATE_POOL_MAX` 条，超出按时间砍最旧（仿真只关心进轮播池的部分；
    完整版条数由 `ITEM_LIMIT` 管，与上屏节奏无关 —— 两者现默认为 120）

怎么读结果（踩过的坑，别再踩）：
  * 单跑一次得不出结论 —— 延迟分布在 6~60 分之间几乎是空的（约 3%），p90 正好落在这条缝上，
    尾部多几个窗就从 5 分跳到 60 分。所以默认报 6 个种子均值，`--seed=N` 才能固定对表。
  * 尾部不是夜间效应：`--verbose` 打出的延迟最大 10 个窗全是「本窗新增 0 条」的时刻
    （5 分钟里各源都没发东西 → 只能吃池子存货），夜里反而最健康（一小时一抓，总有新的）。
  * 最差那一端由池底决定：`ROTATE_POOL_MAX ÷ 每窗新增 ≈ 120 ÷ 2.35 ≈ 51 片 ≈ 4.3 小时`。

输出四张表：
  1. 新鲜度：上屏时距发布多久（中位数 / p90 / 最大值）
  2. 覆盖率：进过池子的条目里，有多少最终播到过屏上
  3. 排位：上屏条目在池子里的名次（0 = 最新那条）
  4. 落后片数：`window - bucket`，即屏上是几分钟前的内容；「落后 ≥2 片」的窗占比
     就是「在吃池子存货」的比例，尾巴有多长全看它

默认跑 6 个种子报均值：单种子方差很大（p90 在 **4~60 分**之间跳、最差 3.7~6.0 小时），
拿单次结果跟别人对表会得出相反结论。`--seed=N` 单跑指定种子（可配 `--verbose` 看尾部）。
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build_feed as B  # noqa: E402

DWELL = B.ROTATE_SECONDS
# 决定上屏节奏的是「设备池」的深度，不是完整版条数：池上限读 ROTATE_POOL_MAX，
# 绝不能改读 ITEM_LIMIT —— 它一旦被设成 0，`del pool[0:]` 会每窗把池子删空、
# rotate_pick 永远返回 None，不报错、退出码 0、打印一张全 0 的表，等于交假结论。
# ROTATE_POOL_MAX 自身语义是 0 = 不限，删除处的 if 守卫同样不能省。
LIMIT = B.ROTATE_POOL_MAX
WINDOWS_PER_HOUR = 3600 // DWELL
SEEDS = 6               # 默认跑几个种子取均值：单种子 p90 方差能差一个数量级


def night(hour):
    return 0 <= hour < 6


def _poisson(rnd, lam):
    """按泊松分布抽一个到达数（Knuth 算法）。

    为什么不直接用 `int(pending)` 那种确定性累加：累加器在小均值下退化成
    「每窗恒 2 条、永不为 0」，等于假设源不会停更 —— 于是每窗都有新内容可播，
    24 小时里一次都不会走到「池子里只剩旧货」的情形，仿真永远交不出「最差延迟」。
    真实产出是成簇的：一小时里可能连着 3 个空窗，然后一起来 7 条。

    均值小于 10 时 Knuth 的期望循环次数就是 `lam`，够快；不引第三方依赖。
    """
    limit = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        k += 1
        p *= rnd.random()
        if p <= limit:
            return k - 1


def _mint(pool, now, n_new, rnd, uid, spread):
    """把这一窗抓回来的 n_new 条塞进池子，按时间倒序截到上限，返回新的自增号。

    发布时间比抓取时刻早 `0~spread` 秒（源的发布延迟）。`spread` 的取值很要紧：
      * 白天传 `DWELL - 1`：摊满一个时间片，条目的片号落在 `w-1`，稳态 lag 才会是 1 片；
      * 夜间传 `3600`：06:00 一次抓回的一小时积压要摊满整小时。
        写成 `0~120` 等于假设 28 条全发布在同一分钟 —— 它们的片号会挤在最新的一片里，
        池子里于是凭空多出一堆「刚发布的片」，低产能的真相照样被抹掉。
    """
    for _ in range(n_new):
        uid += 1
        pool.append({"title": "T%d" % uid, "link": "http://x/%d" % uid,
                     "src": "s%d" % (uid % 43), "uid": uid,
                     "ts": now - rnd.randint(0, spread)})
    pool.sort(key=lambda x: x["ts"], reverse=True)
    if LIMIT:                        # 0 = 池子不限（`del pool[0:]` 会删空，守卫必须留）
        del pool[LIMIT:]
    return uid


def _pct(v, q):
    """已排序列表的分位数（不走插值：这里只用来看量级）。"""
    return v[min(len(v) - 1, int(len(v) * q))] if v else 0


def simulate(hours=24, per_window=2.35, seed=7, builder=None, verbose=False):
    """跑一遍仿真，返回统计字典。builder(items, state, now) → (pick, state, note)。"""
    if builder is None:
        builder = B.rotate_pick
    rnd = random.Random(seed)
    pool, state, uid = [], {}, 0
    onscreen, rank_hist, lags, seen, minted = [], [], [], set(), 0
    cycles, worst = 0, []

    for w in range(1, hours * 3600 // DWELL):
        now = w * DWELL
        hour = (now // 3600) % 24
        # 夜间设备不刷新，但源一直在产出，所以只在整点构建时把一小时积压一次灌进来
        if night(hour) and w % WINDOWS_PER_HOUR != 0:
            continue
        if night(hour):
            n_new = _poisson(rnd, per_window * WINDOWS_PER_HOUR)   # 一小时积压
            spread = 3600
        else:
            n_new = _poisson(rnd, per_window)
            spread = DWELL - 1

        minted += n_new
        uid = _mint(pool, now, n_new, rnd, uid, spread)

        pick, state, note = builder(pool, state, now)
        if pick is None:
            continue
        cycles = state.get("cycle", 0)
        onscreen.append(now - pick["ts"])
        lags.append(int(state.get("lag") or 0))
        rank = next((i for i, it in enumerate(pool) if it is pick), -1)
        rank_hist.append(rank)
        seen.add(pick["uid"])
        if verbose:
            worst.append((now - pick["ts"], now, state, rank, pick["title"], n_new))

    if verbose:
        # 只打最差的那一端。以前这里是「前 40 窗」，而 40 窗只覆盖 0:00–3:20 —— 全是夜间，
        # 于是 `--verbose` 永远展示不出真正的尾部，看着像「最差 7 分」。尾部在哪必须能看见。
        worst.sort(key=lambda x: -x[0])
        print("\n延迟最大的 10 个窗（尾部来自哪里）：")
        for d, now, st, rank, title, n_new in worst[:10]:
            print("  %02d:%02d 延迟 %3.0f 分 · 落后 %s 片 · 名次 %d · 池 %s 片 · 本窗新增 %d 条 · %s"
                  % ((now // 3600) % 24, (now // 60) % 60, d / 60,
                     st.get("lag"), rank, st.get("pool"), n_new, title))

    return _stats(onscreen, rank_hist, lags, seen, minted, cycles)


def _stats(onscreen, rank_hist, lags, seen, minted, cycles):
    """把逐窗记录折成指标字典（排序与分位数都在这里，方便单独对拍）。"""
    for seq in (onscreen, rank_hist, lags):
        seq.sort()
    # 「播的是旧片」的占比：落后 ≥2 片意味着最近两窗都没有可播的新内容，
    # 也就是低产能时段在吃池子里的存货。这个比例直接决定尾巴有多长。
    stale = sum(1 for x in lags if x > 1)

    # 两个「稳」的指标：p90 落在 6~60 分的空档上，多几个尾部窗就从 5 分跳到 60 分，
    # 跨种子不可比；「≤6 分的占比」「>30 分的占比及其分位」在 20 个种子上是平的。
    fast = sum(1 for x in onscreen if x <= 360)
    slow = [x for x in onscreen if x > 1800]

    # 覆盖率只统计还活着或曾上屏的；被池子挤掉且没播过的 = 白抓
    return {
        "minted": minted,
        "aired": len(onscreen),
        "unique": len(seen),
        "cycles": cycles,
        "delay_med": _pct(onscreen, 0.5) / 60,
        "delay_p90": _pct(onscreen, 0.9) / 60,
        "delay_max": (onscreen[-1] if onscreen else 0) / 60,
        "fast": fast / max(1, len(onscreen)),
        "slow": len(slow) / max(1, len(onscreen)),
        "slow_med": _pct(slow, 0.5) / 60,
        "lag_med": _pct(lags, 0.5),
        "lag_max": lags[-1] if lags else 0,
        "stale": stale / max(1, len(lags)),
        "rank_med": _pct(rank_hist, 0.5),
        "rank_p90": _pct(rank_hist, 0.9),
        "rank_zero": sum(1 for r in rank_hist if r == 0) / max(1, len(rank_hist)),
        "cover": len(seen) / max(1, minted),
    }


COUNT_KEYS = ("minted", "aired", "unique", "cycles", "lag_med", "lag_max",
              "rank_med", "rank_p90")


def _mean_of(rows):
    """多种子的逐项均值。计数类取整，分钟/比率类保留小数（show 按 %d / %.0f 打印）。"""
    out = {}
    for k in rows[0]:
        avg = sum(r[k] for r in rows) / float(len(rows))
        out[k] = int(round(avg)) if k in COUNT_KEYS else avg
    return out


def show(tag, r):
    print("\n== %s ==" % tag)
    print("  抓取 %d 条 / 上屏 %d 次 / 不同条目 %d 条 / 走完 %d 轮 / 覆盖率 %.0f%%"
          % (r["minted"], r["aired"], r["unique"], r["cycles"], r["cover"] * 100))
    print("  新鲜度（距发布）：中位 %.0f 分 · p90 %.0f 分 · 最差 %.0f 分（%.1f 小时）"
          % (r["delay_med"], r["delay_p90"], r["delay_max"], r["delay_max"] / 60))
    print("  上屏名次（0=当时最新）：中位 %d · p90 %d · 命中最新 %.0f%%"
          % (r["rank_med"], r["rank_p90"], r["rank_zero"] * 100))
    print("  落后片数（window-bucket）：中位 %d 片 · 最大 %d 片 · 播的是旧片（落后 ≥2 片）占 %.0f%%"
          % (r["lag_med"], r["lag_max"], r["stale"] * 100))
    print("  分布：%.0f%% 在 6 分内 · %.0f%% 超过 30 分（这批中位 %.0f 分）"
          % (r["fast"] * 100, r["slow"] * 100, r["slow_med"]))


def _arg_seed(default=None):
    """`--seed=N`：换一条随机序列复现别人的数（对表时固定种子用）。没给就返回 default。"""
    for arg in sys.argv:
        if arg.startswith("--seed="):
            return int(arg.split("=", 1)[1])
    return default


def main():
    print("参数：节拍 %d 秒 · 池上限 %d 条 · 白天每窗新增 %.2f 条（泊松）"
          % (DWELL, LIMIT, 2.35))
    seed = _arg_seed()
    if "--verbose" in sys.argv:
        seed = 7 if seed is None else seed
        show("单跑 seed=%d（单次结果方差很大，结论看默认的多种子均值）" % seed,
             simulate(seed=seed))
        simulate(seed=seed, verbose=True)   # 跑满 24 小时，只打延迟最大的 10 个窗
        return
    if seed is not None:
        # `--seed=N` 单独用也必须生效。以前这里只在 --verbose 分支取种子，于是
        # `--seed=7` 被静默忽略、打出来的还是 6 种子均值 —— 对表时最危险的一种错。
        show("单跑 seed=%d（--seed 指定的种子）" % seed, simulate(seed=seed))
        return
    # 单种子方差很大（p90 能在个位数和几十分钟之间跳），只报一次容易得出错误结论，
    # 所以默认跑 6 个种子并报均值；质疑某个数时用 --verbose 逐窗看。
    rows = [simulate(seed=s) for s in range(1, SEEDS + 1)]
    for s, r in enumerate(rows, 1):
        print("  seed=%d  中位 %2.0f 分 · p90 %2.0f 分 · 最差 %3.0f 分 · 命中最新 %4.1f%%"
              " · 落后最大 %2d 片 · 覆盖 %2.0f%%"
              % (s, r["delay_med"], r["delay_p90"], r["delay_max"],
                 r["rank_zero"] * 100, r["lag_max"], r["cover"] * 100))
    show("%d 个种子的均值（当前实现）" % SEEDS, _mean_of(rows))


if __name__ == "__main__":
    main()
