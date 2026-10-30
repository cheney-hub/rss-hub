#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轮播 —— 设备版（Dot Quote/0 墨水屏）「一次只有一条」的选条规则与游标状态。

三个模块的分工：
  render.py     显示规则：标题怎么切、正文怎么拼、RSS 怎么序列化（纯函数）
  rotate.py     本文件：这一窗该播哪一条、已播标记怎么记、游标状态怎么落盘
                —— 选条部分是纯函数（给定 items + state + now 就有确定输出），可单测
  build_feed.py 抓取、失败回退、写文件、编排（main）

为什么设备版只有一条：Dot 云端只渲染文档里的第一条。按时间倒序发全量的话屏幕永远
停在最新那条，其余条目一辈子轮不到；所以设备版只发一条，选谁由「已播标记」决定。

规则（只有一条，别再拆台阶）：
    每窗锚定当前时间窗，取「全池还没标注过的最新一条」上屏，并用 guid 标注它；
    全池都标注过 → 清掉全部标注，从池子最新的一条重新开始一轮（cycle + 1）；
    同一时间窗内（w = now // ROTATE_SECONDS）重复构建不推进，后台多跑几轮也不跳条。

为什么按「条」而不是按「时间片」（这里是本模块最大的一次改规则，理由写清楚）：
  1.4.0 ~ 1.6.x 把池子按 ROTATE_SECONDS 切成时间片、片内取最新一条当代表、shown 记片号。
  缺陷是同一片里后到的那些条目**永远无法成为代表** —— 既拿不到代表位、也永远不会被标注，
  等于进池即弃。实测池子 120 条只切出 26~44 片，一天里大半条目从未获得上屏机会；
  而这套机制立起来的初衷恰恰是「别让老条目轮不到」—— 规则与初衷自相矛盾。
  按条轮 + 用 guid 标注两件事一起解决了它：同片内每条各有一票；标记跟着条目身份走，
  池子滚动（新条目入池、旧条目被 ROTATE_POOL_MAX 挤掉）也不会像按序号那样错位。
  代价要说清楚：产能是消费的十几倍（每 5 分钟各源十几条、屏幕只放 1 条），按条「取最新
  未播」仍会偏向新内容，最老的那批照样可能被挤出池子——但它们至少有一条完整生命：
  入池即进入候选队列，只要没被挤出就一定会被选中一次。

「锚定当前窗」这部分没有变：每窗都从「现在」往回看最新未播条，而不是沿着一张开轮时
拍好的清单往下走。后者第 k 窗必播「第 k 新」，延迟随轮次线性增长（线上实测滞后 75 分钟、
24 小时仿真中位 120 分钟），而且冻结的清单里没有运行期间新涌入的条目 —— 本轮一条新消息
都播不到。

状态字段（rotate.json）的含义见 _build_state；排障入口是 OPERATIONS.md 第 15 节。
"""
import os
import json
import time
import urllib.request
from datetime import datetime, timezone

from render import item_guid

# 每条在屏上驻留多少秒（= 设备刷新周期）。调大等于放慢轮播。
# Pages CDN 缓存 600 秒，若真机上出现「同一条连显两次」，把它改成 600。
ROTATE_SECONDS = max(60, int(os.environ.get("ROTATE_SECONDS", "300")))
# 轮播池上限：设备端只消费「最新 120 条」。池子越深，被挤出池子的旧条目越多
# （按条轮之后每条入池就有候选资格，能不能轮到只取决于它离场前来不来得及排到）。
# 0 = 不限，1.2.x 时代的默认值。两个参数要一起调：完整版条数看 ITEM_LIMIT（默认 120），
# 只放大 ITEM_LIMIT 不会改变上屏节奏。
ROTATE_POOL_MAX = max(0, int(os.environ.get("ROTATE_POOL_MAX", "120")))

ROTATE_STATE_VERSION = 4      # 状态结构版本，改了就自动丢弃旧状态（宁可重复一条，不能乱序）
# 容忍的墙钟回拨窗数（1 窗 = ROTATE_SECONDS，2 窗 = 10 分钟）：NTP 校正会往回跳几十秒，
# 那不算坏状态；超过这个幅度才认定 rotate.json 的 window 是坏的（详见 rotate_pick）。
CLOCK_SKEW_WINDOWS = 2


# ---------------- 状态读写 ----------------
def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_rotate_state(state_path, remote_url, user_agent, timeout=8):
    """本地那份和线上那份取 window 更新的一个。

    作业是整点 checkout 的，仓库里的 rotate.json 可能是一小时前的；
    线上那份才是上一轮真正发布出去的。谁 window 大用谁，一样大就用本地
    （本轮可能已经推进过，本地更新）。

    路径与地址都由调用方给：状态文件跟着 OUT_FILE 走、线上地址跟着 FEED_URL 走，
    那两个在 build_feed 里算，本模块不复制它们的环境变量默认值（复制就会两处漂移）。
    """
    local = _read_json(state_path)
    remote = {}
    try:
        req = urllib.request.Request(
            "%s?cb=%d" % (remote_url, time.time()),
            headers={"User-Agent": user_agent, "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        remote = data if isinstance(data, dict) else {}
    except Exception:
        remote = {}
    if int(remote.get("window") or 0) > int(local.get("window") or 0):
        return remote
    return local


def save_rotate_state(state, state_path):
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, state_path)


# ---------------- 选条（纯函数） ----------------
def _bucket_of(item, dwell):
    """条目的时间片号（epoch 秒 // 片长）。

    片号现在只剩一个用途：判断「这条的发布时间是不是在未来」（见 _pickable）。
    选条早已不按片走 —— 同一个片里来几条就排几条，片号不再参与排序与标记。
    """
    return int((item.get("ts") or 0) // dwell)


def _pickable(items, w, dwell):
    """能参与本轮选条的条目（保持 items 原序，即按发布时间倒序）。

    剔掉「发布时间晚于当前窗」的条目：源时钟错乱（时区写错、服务器漂移、模板把年份
    拼成 2030）会造出未来时间戳，这种条目永远排在最前，每窗都是「最新未播条」→
    屏幕被它独吞，`back` 还会失真。
      为什么不能改在解析层「钳到抓取时刻」：抓取每 5 分钟重跑一次，钳值跟着往前跑，
      它永远落在当前窗、永远「没播过」—— 同一条每窗重播一次（实测连跑 6 窗全是它），
      比不钳更糟。按「时间还没到就先不播」处理后：源的时间真走到那天自然上屏，
      一直错就一直不上设备版（条目仍完整留在 feed.xml 里）。
      解析失败 ts=0 的条目落在片 0（1970），永远 ≤ 当前窗，所以它照常参与选条，
      只是排序垫底 —— 正常情况下要等池子里排空才轮得到它，但池子退化到只剩它时
      不会漏掉（宁可播一条没时间的，也不空屏）。
    """
    return [it for it in items if _bucket_of(it, dwell) <= w]


def _pick_item(state, items):
    """选出这一窗该上屏的条目，返回 (条目 或 None, 已播 guid 集合, 轮次)。

    规则只有一条：**播池子里最新的、还没播过的那一条**。items 已按发布时间倒序、
    已由 _pickable() 剔掉未来条目，所以第一个没被标注的就是答案。
    全池都播过才清标记、从池子最新的一条重开一轮（cycle + 1）。

    标记用 guid 而不是「池子里第几条」：池子每窗都在滚动（新条目从头部进来、
    旧条目从尾部被 ROTATE_POOL_MAX 挤出去），序号会跟着错位 —— 屏上第 3 条这一窗
    是 A、下一窗就变成 B 了，按序号标注等于把别人的票记自己账上。guid 跟着条目身份走，
    滚到哪都不错位；`ROTATE_STATE_VERSION` 因此从 3 升到 4（旧状态里存的是片号，
    宁可丢状态重开一轮，也不能让两种标记混在一起来一次乱序）。

    为什么不给「往旧里找」设年龄上限（刻意的，不是漏了参数）：
      墨水屏不显示时间戳，「内容停住」和「设备坏了」在用户眼里是同一件事，
      所以宁可播一条旧闻也不空屏。代价必须说清楚：极端低产能时屏上可能是几小时前的
      内容（实测最差到过 22.9 小时，见 README「代价」）—— 那个交换这次没有改。

    这里曾经有个 ROTATE_MAX_LAG 参数，把选择拆成「先看最近 N 片，取不到才放宽到全池」。
    它从未生效：第一级的候选集是第二级的子集、两处又都取最新的那一个，所以两级给出的
    永远是同一条。穷举 5 万组状态 + 24 小时端到端，参数取 0/1/6/24 结果逐项相同，
    于是连同环境变量一起删掉了 —— 留着只会让排障的人对着一个假旋钮调半天。
    """
    shown = {str(g) for g in (state.get("shown") or [])}
    cycle = int(state.get("cycle") or 1)        # 1 = 第 1 轮；全池播完重开时 +1
    if not items:
        return None, shown, cycle

    fresh = next((it for it in items if item_guid(it) not in shown), None)
    if fresh is not None:
        return fresh, shown, cycle

    # 全池都播过：清标记重开一轮（cycle + 1），从池子最新的一条重新开始。
    # 唯一的例外是「刚刚上屏那一条」—— 池子小、换血快时 items[0] 往往就是它，
    # 清完标记立刻又选中它，屏幕上看起来像「没换」。这时改从「最新的另一条」起轮。
    # 只跳过这一条、不保留它的标记：留着的话它要到下一轮才回来，一轮就会少播一条。
    prev_g = str(state.get("current") or "")
    restart = next((it for it in items if item_guid(it) != prev_g), None)
    if restart is None:                  # 池子只有这一条：只能重播它（无解，但不能崩）
        restart = items[0]
    return restart, set(), cycle + 1


def _trim_shown(shown, live_guids, pick_g):
    """把 shown 裁成「池子里还在的条目 + 刚刚上屏那一条」，避免它随运行时间无限增长。

    为什么不按「发布时间晚于 N 窗之前」裁（先这么写过，被仿真否掉了）：「取全池最新未播条」
    这条规则让游标能走到池子里任何一条，按窗口裁就会抹掉「几小时前播过、但仍可能被再次
    选中」的标记，于是同一条隔几窗重播、`cycle` 永远数不到头、`back` 一路上涨。
    按「还在池子里」裁同样有界：上限就是条目上限（`ROTATE_POOL_MAX`，默认 120），所以
    shown 最多一百来个 12 位 md5、不随时间增长；离开池子的条目再也轮不到，标记没用。
    `pick_g` 必须保留：它是刚上屏那一条，抹掉它下一窗就会把同一条再播一遍。
    """
    return {pick_g} | {g for g in shown if g in live_guids}


def _hold_current(state, items, w, prev_w):
    """同一个时间窗内重复构建：屏幕锁死在这一条，不推进。返回 (动作, 条目)。

    保证的是「游标不推进」：一个 5 分钟窗内跑多少遍构建，都停在同一个条目、
    不会跳到下一条，也不会因为池子滚动而回退。找不到原条目时宁可返回 None
    （调用方保留上一版 feed-wrap 不动），也不要凭空换一条。

    按 guid 找必须坚持两点：
      * 找的时候看**全池**，不看 _pickable() 过滤后的集合 —— 时钟回拨后屏上那一条的
        发布时间会显得「在未来」，只看可播集合会误判它离场，进而 skip：设备版不刷新、
        rotate.json 不落盘，白白空转一版。
      * 这一窗内**新来的、更晚发布的条目不顶屏**（按条轮之前是有这条路的：片内代表被
        顶掉时 hold 会换成新代表）。同一窗不去换内容，比追到更新的一条更值钱；
        真追新的话，同一个 5 分钟窗里抓几次就可能换几次，"这条还没看完"会被抱怨。
    """
    if w > prev_w:
        return "go", None
    cur_g = str(state.get("current") or "")
    cur = next((it for it in items if item_guid(it) == cur_g), None)
    if cur is not None:
        return "hold", cur
    return "skip", None


def _fmt_at(item):
    """上屏条目的发布时间串（排障时看「屏上这条是哪会儿发的」用的）。

    显式带 UTC：本地 +08、runner UTC，差 8 小时足以让人误判「这条怎么是 8 小时前发的」。
    解析失败（ts = 0 / None）时给一个一眼能认的占位，而不是打出 1970-01-01 —— 那是
    「pubDate 缺失」而非「1970 年发布的老条目」，两者处置完全不同。
    """
    ts = int(item.get("ts") or 0)
    if not ts:
        return "发布时间未知"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M") + " UTC"


def _build_state(w, pick, shown, cycle, dwell, back, n_pool, n_items):
    """组装本轮要落盘的状态。

    字段含义（OPERATIONS.md 第 15 节按这份解释排障，改字段名请同步改文档）：
      window  时间窗号 = now // dwell，幂等键，同一窗内不推进
      back    落后最新几条（0 = 屏上就是池子里最新的一条）
      current 当前上屏条目的 guid —— 「这条是否已播」的唯一身份键，hold / skip 都靠它找回
      shown   已播过条目的 guid 列表（只留池子里还在的条目，见 _trim_shown）
      cycle   第几轮（1 起），全池播完重开时 +1
      pool    可播条数（不含发布时间在未来的条目）；items 是进池的原始条数
      at      上屏条目的发布时间（UTC）。1.7.0 之前这里记的是「时间片对应的时间」，
              按条轮之后改成这一条自己的发布时间 —— 旧状态版本号不同会被直接丢弃。
    """
    return {
        "version": ROTATE_STATE_VERSION,
        "window": w,
        "back": back,
        "current": item_guid(pick),
        "shown": sorted(shown),
        "cycle": cycle,
        "pool": n_pool,
        "items": n_items,
        "dwell": dwell,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": (pick.get("title") or "")[:40],
        "at": _fmt_at(pick),
    }


def rotate_pick(items, state, now=None):
    """取「这一窗该上屏的那一条」，返回 (条目 或 None, 新状态, 说明)。

    items 必须已按发布时间倒序（run_once 保证）。

    规则（为什么这么定、代价是什么见 _pick_item）：
      1. 每窗取「全池还没标注过的最新一条」上屏，guid 记进 shown；
      2. 全池都播过 → 清空 shown，从池子最新的一条重新开始（cycle + 1）；
      3. 同一时间窗内重复调用不推进，也不因本窗来了更新的条目而换台。

    第 1~2 步只在 `_pickable()` 给出的条目里选：发布时间在未来的条目不参与（原因见该函数）。
    """
    dwell = ROTATE_SECONDS
    w = int((now if now is not None else time.time()) // dwell)
    # 节拍改过（60 秒 ↔ 300 秒）或状态结构升级：标记的口径变了，重新起轮
    if (state.get("version") != ROTATE_STATE_VERSION
            or int(state.get("dwell") or dwell) != dwell):
        state = {}
    prev_w = int(state.get("window") or 0)
    # 状态里的 window 比当前窗还大，只可能是「写它的那次运行时钟错了」或文件被写坏：
    # 不丢的话 w > prev_w 永远为假 → 一直 hold → window 永不前进 → 屏幕永久卡死，
    # 且每次运行都原样读回这份坏状态，自己治不了自己。容忍 CLOCK_SKEW_WINDOWS 窗以内的
    # 回拨（NTP 校正会让墙钟往回跳几十秒，冻结一版就好），跳得更多才认定状态坏了。
    if prev_w > w + CLOCK_SKEW_WINDOWS:
        state, prev_w = {}, 0
    if not items:
        return None, state, "池子里没有可播的条目"

    action, held = _hold_current(state, items, w, prev_w)
    if action == "hold":
        return held, state, "同窗保持"
    if action == "skip":
        return None, state, "同窗内该条离开池子，保持上一版"

    # 只在选条时排除未来条目（hold 要看全池，见 _hold_current）；顺便数一下被排除了几条
    live = _pickable(items, w, dwell)
    late = len(items) - len(live)
    pick, shown, cycle = _pick_item(state, live)
    if pick is None:
        return None, state, "池子里没有可播的条目"
    pick_g = item_guid(pick)
    shown = _trim_shown(shown | {pick_g}, {item_guid(it) for it in live}, pick_g)
    back = next((i for i, it in enumerate(live) if item_guid(it) == pick_g), 0)
    new_state = _build_state(w, pick, shown, cycle, dwell, back,
                             len(live), len(items))
    return pick, new_state, _advance_note(new_state, shown, live, items, late)


def _advance_note(st, shown, live, items, late):
    """「新窗推进（…）」这行日志。

    被排除的未来条目数只在 > 0 时追加：剔除的后果是「某个源在设备版上悄悄消失」，
    不打出来就没人发现源的时间戳坏了。这一句是那件事唯一的出口（`_pickable` 是纯函数，
    不该自己打印）。`main()` 已经把它和标题打在同一行，追加长一点不影响排版。
    """
    where = "上屏最新那条" if st["back"] == 0 else "上屏第 %d 新的那条" % st["back"]
    note = ("新窗推进（%s · %s · 已播 %d 条 · 池 %d 条/%d 条 · 第 %d 轮）"
            % (where, st["at"], len(shown), len(live), len(items), st["cycle"]))
    if late:
        note += " · 另有 %d 条时间戳晚于当前，已排除出设备版轮播" % late
    return note
