#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轮播 —— 设备版（Dot Quote/0 墨水屏）「一次只有一条」的选条规则与游标状态。

三个模块的分工：
  render.py     显示规则：标题怎么切、正文怎么拼、RSS 怎么序列化（纯函数）
  rotate.py     本文件：池子怎么切片、这一窗该播哪一条、游标状态怎么落盘
                —— 选条部分是纯函数（给定 items + state + now 就有确定输出），可单测
  build_feed.py 抓取、失败回退、写文件、编排（main）

为什么设备版只有一条：Dot 云端只渲染文档里的第一条。按时间倒序发全量的话屏幕永远
停在最新那条，其余条目一辈子轮不到；所以设备版只发一条，选谁由「已播标记」决定。

规则（只有一条，别再拆台阶）：
    把池子按 ROTATE_SECONDS 切成时间片，片内取最新一条当代表；
    每窗锚定当前时间窗，取「全池没播过里最新的一片」上屏，并标注已播
      —— 没有新内容时就往旧里找，没有年龄上限（宁可播旧的也不空屏，见 _pick_bucket）；
    全池都播过 → 清掉所有标注，从池子最新的片重新开始一轮（cycle + 1）；
    同一时间窗内（w = now // ROTATE_SECONDS）重复构建不推进，后台多跑几轮也不跳条。

关键词是「锚定当前窗」：每窗都从「现在」往回看，而不是沿着一张开轮时拍好的清单往下走。
后者第 k 窗必播「第 k 新」，延迟随轮次线性增长（线上实测滞后 75 分钟、24 小时仿真中位
120 分钟），而且冻结的清单里没有运行期间新涌入的条目 —— 本轮一条新消息都播不到。

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
# 轮播池上限：设备端只消费「最新 120 条」。最差滞后 = 池条数 ÷ 每窗新增，
# 池子涨到 245 条会把最差滞后从 4.3 小时推到 8 小时以上 —— 所以完整版条数放开
# （ITEM_LIMIT=0）之后，设备池子仍显式钉在 120（0 = 不限，1.2.x 时代的默认值）。
ROTATE_POOL_MAX = max(0, int(os.environ.get("ROTATE_POOL_MAX", "120")))

ROTATE_STATE_VERSION = 3      # 状态结构版本，改了就自动丢弃旧状态（宁可重复一条，不能乱序）
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


# ---------------- 切片与选条（纯函数） ----------------
def _bucket_of(item, dwell):
    """条目的时间片号（epoch 秒 // 片长）。片号是轮播里唯一的坐标。"""
    return int((item.get("ts") or 0) // dwell)


def _bucketize(items, dwell):
    """把池子按 dwell 秒切成时间片，每片取最新一条当代表。

    返回 (片号 → 条目, 片号倒序表)。items 必须已按时间倒序，
    所以 setdefault 拿到的第一条就是片内最新的那条。

    这里不剔除「片号大于当前窗」的片：hold 要能在时钟回拨时仍认出屏上那一条
    （回拨后它的片号会显得「在未来」）。未来片只在「选下一条」时排除，见 rotate_pick。
    """
    buckets = {}
    for it in items:
        buckets.setdefault(_bucket_of(it, dwell), it)
    return buckets, sorted(buckets, reverse=True)


def _pickable(keys, w):
    """能参与「选下一条」的片号：发布时间已到（片号 ≤ 当前窗）。

    剔掉「片号 > w」的片，也就是发布时间在未来的条目：源时钟错乱（时区写错、
    服务器漂移、模板把年份拼成 2030）会造出未来时间戳，这种片号永远最大，
    每窗都是「最近几片里最新的未播片」→ 屏幕被它独吞，lag 还变成巨大负数。
      为什么不能改在解析层「钳到抓取时刻」：抓取每 5 分钟重跑一次，钳值跟着往前跑，
      片号永远是本窗、永远「没播过」—— 同一条每窗重播一次（实测连跑 6 窗全是它），
      比不钳更糟。按「时间还没到就先不播」处理后：源的时间真走到那天自然上屏，
      一直错就一直不上设备版（条目仍完整留在 feed.xml 里），lag 回到非负。
      解析失败 ts=0 的条目落在片 0，排在池子最末，全池往回找也基本轮不到
      （只有池子退化成只剩那一条时才会播它）。

    只管「选下一条」：hold 必须看全量片 —— 时钟回拨后屏上那条的片号会显得在未来，
    这里一并剔掉的话，回拨期间就再也认不出屏上内容，会误判成「该条离场」。
    """
    return [b for b in keys if b <= w]


def _pick_bucket(state, keys):
    """选出这一窗该上屏的片号，返回 (片号 或 None, 已播集合, 轮次)。

    规则只有一条：**播池子里最新的、还没播过的那一片**。keys 已按片号倒序、
    已由 _pickable() 剔掉未来片，所以 `keys[0]` 就是「最新的可播片」。
    全池都播过才清标记、从最新的片重开一轮（cycle + 1）。这一条与用户原规则一致。

    签名里没有窗号：候选集已由调用方按窗过滤（`_pickable(keys, w)`），这里再收一个
    `w` 就是一个不参与决策的形参 —— 上一版正是「有参数、看起来像旋钮、其实不影响结果」
    的东西把人骗了一轮，别再来一个。

    为什么不是「开轮时拍一张清单快照、从新到旧推进」：
      快照 + 顺序推进等价于「第 k 窗播第 k 新的那条」，延迟随轮次线性增长 ——
      线上实测走到清单尾部已滞后 75 分钟，24 小时仿真中位 120 分、最差 4.3 小时；
      更糟的是冻结的清单里没有运行期间新涌入的条目，本轮一条新消息都播不到。
      「取全池最新的未播片」天然锚定「现在」：有新内容就播新的，延迟不随轮次累积
      （稳态 lag 中位 1 片），新条目下一窗就能上屏。

    为什么不给「往旧里找」设年龄上限（刻意的，不是漏了参数）：
      墨水屏不显示时间戳，「内容停住」和「设备坏了」在用户眼里是同一件事，
      所以宁可播一条旧闻也不空屏。代价必须说清楚：极端低产能时会真的播很旧的片 ——
      全源停 2 小时 lag 36 片，0.4 条/窗的最坏场景实测到过 22.9 小时（见 README「代价」）。
      另一头是覆盖率：白天新内容不断，几小时前的旧条目基本轮不到，会被池子上限挤掉
      （仿真约 35%）。墨水屏上「看到 3 小时前的快讯」比「少看几条旧闻」更伤，这个交换是刻意的。

    这里曾经有个 ROTATE_MAX_LAG 参数，把选择拆成「先看最近 N 片，取不到才放宽到全池」。
    它从未生效：第一级的候选集是第二级的子集、两处又都取最大片号，所以两级给出的永远是
    同一条。穷举 5 万组状态 + 24 小时端到端，参数取 0/1/6/24 结果逐项相同，
    于是连同环境变量一起删掉了 —— 留着只会让排障的人对着一个假旋钮调半天。
    """
    shown = {int(b) for b in (state.get("shown") or [])}
    cycle = int(state.get("cycle") or 1)        # 1 = 第 1 轮；全池播完重开时 +1
    if not keys:
        return None, shown, cycle

    fresh = next((b for b in keys if b not in shown), None)
    if fresh is not None:
        return fresh, shown, cycle

    # 全池都播过：按原规则清标记重开一轮（cycle + 1），从池子最新的片重新开始。
    # 唯一的例外是「刚刚上屏那一片」——池子小、换血快时 keys[0] 往往就是它，
    # 清完标记立刻又选它，屏幕上看起来像「没换」。这时改从「最新的另一片」起轮。
    # 只跳过这一片、不保留它的标记：留着的话它要到下一轮才回来，一轮就会少播一条。
    prev_b = int(state.get("bucket") or 0)
    restart = next((b for b in keys if b != prev_b), None)
    if restart is None:                  # 池子只有这一片：只能重播同一条（无解，但不能崩）
        restart = keys[0]
    return restart, set(), cycle + 1


def _trim_shown(shown, live, pick_b):
    """把 shown 裁成「池子里还在的片 + 刚上屏那一片」，避免它随运行时间无限增长。

    为什么不按「片号 > 当前窗 - N 片」裁（先这么写过，被仿真否掉了）：「取全池最新的未播片」这条
    规则让游标能走到池子里任何一片，按窗口裁就会抹掉「几小时前播过、但仍可能被再次选中」的标记，
    于是同一条隔几窗重播、`cycle` 永远数不到头、lag 一路上涨（实测：池子冻住 30 片连跑 200 窗，
    只出了 13 个不同条目、cycle 卡在 1；按「还在池子里」裁是 30 片整轮循环、cycle 正常递增）。

    按「还在池子里」裁同样有界：片数上限就是条目上限（`ITEM_LIMIT`，默认 120），所以 shown
    最多一百多个整数、不随时间增长；离开池子的片再也取不到代表条目，标记没用。
    `pick_b` 必须保留：它是刚上屏的那一片，抹掉它下一窗就会把同一条再播一遍。
    """
    return {pick_b} | {b for b in shown if b in live}


def _hold_current(state, buckets, w, prev_w):
    """同一个时间窗内重复构建：屏幕锁死在这一片，不推进。返回 (动作, 条目, 待落盘 guid)。

    保证的是「游标不推进」：一个 5 分钟窗内跑多少遍构建，都还停在同一个时间片、
    不会跳到下一片，也不会因为池子滚动而回退。找不到原条目时宁可返回 None
    （调用方保留上一版 feed-wrap 不动），也不要凭空换一条。

    查找顺序是「片号优先、guid 兜底」：片号是这一窗的稳定坐标；guid 只在
    片号已经不在池子里时用得上。反过来的话有个漏洞——同一片内后到的条目会
    把该片的代表换掉（setdefault 取第一条，新的更早入表就顶掉旧的），
    guid 找不到就认为该条离场，进而让本窗换了内容。

    「不推进」不等于「内容绝对不变」：片内代表被顶掉时（片号还在池子里，但
    `buckets[b]` 已经不是 `state["current"]` 那一条）选择「接受更新」——
    照播新代表，并把 guid 一并交回调用方改写状态。为什么不选「保持旧内容（返回 skip）」：
      skip 会让这一窗停在上一版，下一窗片号一变又正常推进，而旧 guid 滞留在状态里，
      `rotate.json` 的 title/current 与实际屏上内容长期不一致 —— 排障时按它推断会得出
      错误结论；片号失效后走 guid 兜底也会因为找不到那条而误判「该条离场」，白空转一版。
    代价：同一 5 分钟窗内若又抓到本片更新的条目，屏上那条会跟着换一次
      （仍是同一个时间片的内容，不是另一条新闻）。接受它，是因为「片内更新」正是
      新机制想要的「最新优先」，而状态与真实内容一致比省这一次切换更值钱。
    片号命中且代表的 guid 没变时返回 None，表示状态无需改写。
    """
    if w > prev_w:
        return "go", None, None
    b = int(state.get("bucket") or 0)
    if b in buckets:
        cur = buckets[b]
        guid = item_guid(cur)
        return "hold", cur, (guid if guid != state.get("current") else None)
    by_guid = {item_guid(it): it for it in buckets.values()}
    cur = by_guid.get(state.get("current"))
    if cur is not None:
        return "hold", cur, None      # 片号失效但条目还在池子里：内容不变，状态不用改
    return "skip", None, None


def _build_state(w, pick_b, pick, shown, cycle, dwell, n_buckets, n_items):
    """组装本轮要落盘的状态。

    字段含义（OPERATIONS.md 第 15 节按这份解释排障，改字段名请同步改文档）：
      window  时间窗号 = now // dwell，幂等键，同一窗内不推进
      bucket  当前上屏的片号
      lag     落后当前窗几片（= window - bucket），排障第一眼就看这一个数字
      current 当前上屏条目的 guid，片号失效时用它兜底找回
      shown   已播过的片号（只留池子里还在的片，见 _trim_shown）
      cycle   第几轮（1 起），全池播完重开时 +1
      pool    可播的时间片数（不含发布时间在未来的片）；items 池子有多少条原始条目
    """
    return {
        "version": ROTATE_STATE_VERSION,
        "window": w,
        "bucket": pick_b,
        "lag": w - pick_b,
        "current": item_guid(pick),
        "shown": sorted(shown),
        "cycle": cycle,
        "pool": n_buckets,
        "items": n_items,
        "dwell": dwell,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": (pick.get("title") or "")[:40],
        "at": datetime.fromtimestamp(pick_b * dwell, timezone.utc).strftime("%m-%d %H:%M"),
    }


def rotate_pick(items, state, now=None):
    """取「这一窗该上屏的那一条」，返回 (条目 或 None, 新状态, 说明)。

    items 必须已按发布时间倒序（run_once 保证）。

    为什么按「时间片」而不是按「条目」轮：
      每 5 分钟各源新产出十几条，而屏幕 5 分钟只放得下 1 条（产能是消费的十几倍）。
      若按条目「取未显示里最新的一条」，新条目永远比旧的新 —— 每一窗都在追新，
      池子里剩的上百条还没轮到就被 120 条上限挤出去，等于从来没上过屏。
      按时间片轮，一个片只出一条代表，产出和消费才对得上。

    规则（为什么这么定、代价是什么见 _pick_bucket）：
      1. 把池子按 dwell 切成时间片，片内取最新一条作为该片代表；
      2. 每窗取「全池没播过里最新的一片」上屏，片号记进 shown
         （存片号不存 guid，池子滚动也不丢标记）；
      3. 全池都播过 → 清空 shown，从池子最新的片重新开始（cycle + 1）；
      4. 同一时间窗内重复调用不推进。

    第 2~3 步只在 `_pickable()` 给出的片里选：发布时间在未来的片不参与（原因见该函数）。
    """
    dwell = ROTATE_SECONDS
    w = int((now if now is not None else time.time()) // dwell)
    # 节拍改过（60 秒 ↔ 300 秒）或状态结构升级：片号基数变了，重新起轮
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

    buckets, keys = _bucketize(items, dwell)
    if not buckets:
        return None, state, "池子里没有可播的片"

    action, held, guid = _hold_current(state, buckets, w, prev_w)
    if action == "hold":
        if guid is not None:                     # 片内代表被顶掉：内容换了，状态跟着改
            state = dict(state, current=guid, title=(held.get("title") or "")[:40])
            return held, state, "同窗保持（片内代表更新）"
        return held, state, "同窗保持"
    if action == "skip":
        return None, state, "同窗内该条离开池子，保持上一版"

    # 只在选条时排除未来片（hold 要看全量片，见 _pickable）；顺便数一下被排除了几条
    live = _pickable(keys, w)
    late = sum(1 for it in items if _bucket_of(it, dwell) > w)
    pick_b, shown, cycle = _pick_bucket(state, live)
    if pick_b is None:
        return None, state, "池子里没有可播的片"
    pick = buckets[pick_b]

    shown = _trim_shown(shown | {pick_b}, set(live), pick_b)
    new_state = _build_state(w, pick_b, pick, shown, cycle, dwell,
                             len(live), len(items))
    return pick, new_state, _advance_note(new_state, shown, live, items, late)


def _advance_note(st, shown, live, items, late):
    """「新窗推进（…）」这行日志。

    被排除的未来条目数只在 > 0 时追加：剔除的后果是「某个源在设备版上悄悄消失」，
    不打出来就没人发现源的时间戳坏了。这一句是那件事唯一的出口（`_pickable` 是纯函数，
    不该自己打印）。`main()` 已经把它和标题打在同一行，追加长一点不影响排版。
    """
    note = ("新窗推进（%s 片 · 落后 %d 片 · 已播 %d 片 · 池 %d 片/%d 条 · 第 %d 轮）"
            % (st["at"], st["lag"], len(shown), len(live), len(items), st["cycle"]))
    if late:
        note += " · 另有 %d 条时间戳晚于当前，已排除出设备版轮播" % late
    return note
