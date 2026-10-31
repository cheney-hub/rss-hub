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

规则（选条只有一条，别再拆台阶）：
    每窗锚定当前时间窗，取「全池还没标注过的最新一条」上屏，并用 guid 标注它；
    全池都标注过 → 清掉全部标注，从池子最新的一条重新开始一轮（cycle + 1）；
    同一时间窗内（w = now // ROTATE_SECONDS）重复构建不推进，后台多跑几轮也不跳条。

上面那条之外还有一层**观感约束**（不是选条规则，所以没写进同一句）：
最近 `RECENT_MAX` 个已上屏窗（默认 3 窗）内不许出现同一个「屏上显示串」（= `split_title()`
返回的 head，宽度由 `SCREEN_LINE_WIDTH` 决定，可能短于该值），

免得两条不同新闻因为前缀撞车、在屏上长得一模一样，连着几窗看下来像屏幕卡住了。
「3 窗」不等于「间隔 15 分钟」：本窗选的那条不在记忆里，所以同一个显示串之间真正的最小
间隔是 **4 窗 = 20 分钟**（详见 `RECENT_MAX` 的注释）。
被这条跳过的条目仍是「未标注」，几窗后照常上屏 —— 内容不丢，只是往后挪（见 RECENT_MAX）。

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
import tempfile
import urllib.request
from datetime import datetime, timezone

from render import item_guid, split_title, strip_dateline

# 每条在屏上驻留多少秒（= 设备刷新周期）。调大等于放慢轮播。
# Pages CDN 缓存 600 秒，若真机上出现「同一条连显两次」，把它改成 600。
# 下限 60：比 1 分钟更短的节拍既追不上设备刷新，也会让 window 号涨得飞快（状态全是噪点）。
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
# 最近几个窗（1 窗 = ROTATE_SECONDS，3 窗 = 15 分钟）不许出现同一个「屏上显示串」。
#
# 记住的是**已经上屏的那几窗**（w-1 / w-2 / w-3），本窗正在选的那条不在里面，
# 所以同一个显示串之间真正的最小间隔是 **4 窗 = 20 分钟**，不是 15 分钟
# （「最近 3 窗不出现」= 中间至少夹着 3 条别的）。排障时按 20 分钟算，别按 15 分钟。
#
# 这是**观感约束，不是内容去重**：内容去重归 render.dedupe() 管（同一事件的整个条目
# 会被丢掉），而这里处理的是「标题前 13 字撞车」—— 云端只渲染第一行，两条毫无关系的
# 新闻截到 13 字可能一模一样（线上实测：《TAIPEI TIMES》栏目稿四连发、「CFTC：截至…」
# 三连发都是同一前缀），连着三窗看下来屏幕像卡住了。
#   * 被这条约束跳过的条目**不写进 shown**，仍是「未标注」，几窗后照常上屏 —— 内容不丢，
#     只是往后挪；
#   * 真的无处可选时（候选全在 recent 里）会退回最新那条，宁可连播也不空屏，
#     理由与「不设年龄上限」一致：屏幕上「停住」和「设备坏了」在用户眼里是同一件事。
RECENT_MAX = 3


# ---------------- 状态读写 ----------------
def _int(v, default):
    """把状态里读出来的数值字段转成 int；转不了就用 default（与 `_str_list` 同一口径）。

    rotate.json 是**发布出去的产物**，形状不由本模块保证。裸 `int()` 在这里会抛两类异常：
    字符串抛 ValueError、list/dict 抛 TypeError，而 `build_feed.main()` 与 Pi 的
    `rotate_screen.py` 都没有 try —— 异常发生在选条之前，feed 与状态都不落盘，
    屏幕就永久停在上一版，且下次读回的仍是这份坏状态（自己治不了自己）。

    能 `int()` 的数字字符串（"300"）按合法值接受：`json.dump` 写出来永远是 int，
    字符串只可能来自手工编辑，接受它的行为与直接写 int 完全一致，额外拒绝反而会把
    「静默接受」变成「每次读都丢状态重开」（shown 被清空、同批条目重播）。
    bool 是 int 的子类，True 会转成 1，这里不做特殊处理。
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read_json(path):
    """读一份 JSON 状态；不存在 / 坏 JSON / 顶层不是 dict 一律当空状态。

    这是「已发布产物」容错链的第一环（第二环是 `_str_list` 与 `_int` 这类字段级守卫），
    坏文件的代价只能是「重复播一条」，不能是「这一轮死掉」。
    """
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
    # 两份都是外部输入，window 可能被写成字符串或列表；坏值当 0（= 没有状态）
    if _int(remote.get("window"), 0) > _int(local.get("window"), 0):
        return remote
    return local


def save_rotate_state(state, state_path):
    """原子写盘：同目录建唯一临时文件 → 写 → `os.replace` 覆盖。

    临时文件名必须唯一（`tempfile.mkstemp`）：固定成 `path + ".tmp"` 时，
    两个进程同时写同一份状态会踩踏——B 打开旧 tmp、A 先 replace、B 的写入就落进了
    已经被替换成正式文件的那个 inode，读者能看到半份 JSON（原子性丢失）。
    `ensure_ascii=False` + `indent=1` 是刻意选的：线上那份要能直接读懂（排障只看这个文件）。
    不 fsync：丢一次状态最多重复播一条，不值得为它付每次落盘的同步开销。
    """
    d = os.path.dirname(state_path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(state_path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)          # mkstemp 建的是 0600，发布出去的文件要可读
        os.replace(tmp, state_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------- 选条（纯函数） ----------------
def _bucket_of(item, dwell):
    """条目的时间片号（epoch 秒 // 片长）。

    片号现在只剩一个用途：判断「这条的发布时间是不是在未来」（见 _pickable）。
    选条早已不按片走 —— 同一个片里来几条就排几条，片号不再参与排序与标记。
    """
    return int((item.get("ts") or 0) // dwell)


def _pickable(items, w, dwell):
    """能参与本轮选条的条目（保持 items 原序，即按发布时间倒序）。

    口径：**发布时间落在当前或更早的时间片才参与选条**（`ts // dwell <= w`）。
    注意它比「发布时间已到」宽一档：同一个片内的条目都算可播，所以最多容许
    「本窗内、未来不到一个片长（默认 ≤ 5 分钟）」的条目 —— 这是刻意的，
    抓取与显示是两条时间线，刚发布的那条不该因为比抓取时刻晚了几十秒就被判为未来、
    白等一整个窗。

    这么做的原因是另一头：源时钟错乱（时区写错、服务器漂移、模板把年份拼成 2030）
    会造出真正的未来时间戳，这种条目永远排在最前，每窗都是「最新未播条」→
    屏幕被它独吞，`back` 还会失真。
      为什么不能改在解析层「钳到抓取时刻」：抓取每 5 分钟重跑一次，钳值跟着往前跑，
      它永远落在当前窗、永远「没播过」—— 同一条每窗重播一次（实测连跑 6 窗全是它），
      比不钳更糟。按「时间片还没到就先不播」处理后：源的时间真走到那天自然上屏，
      一直错就一直不上设备版（条目仍完整留在 feed.xml 里）。
      解析失败 ts=0 的条目落在片 0（1970），永远 ≤ 当前窗，所以它照常参与选条，
      只是排序垫底 —— 正常情况下要等池子里排空才轮得到它，但池子退化到只剩它时
      不会漏掉（宁可播一条没时间的，也不空屏）。
    """
    return [it for it in items if _bucket_of(it, dwell) <= w]


def _display_key(item):
    """这条在屏上真正显示的那一行字：`split_title()` 返回的 head。

    head 的宽度由 `SCREEN_LINE_WIDTH`（默认 13 全角）决定，而且**可能短于它**：
    词内退让（不足行宽 60% 就不退）、数字/单位退让、尾部标点 rstrip 都会让它更短。
    所以别把它当成「固定 13 字前缀」。

    用 render.split_title() 而不是自己截：屏上那一行到底取到哪几个字，是 render 的
    显示规则（含「不把数字和单位切开」「不在半个英文词中间切」），在这里重算一遍必然
    漂移 —— 一旦漂了，「防连播」拦的就是一条长得跟屏幕不一样的键，等于没拦。
    空标题返回 ""，由调用方当成「不参与这条约束」处理（它连显示串都没有）。
    """
    title = str(item.get("title") or "")
    if not title:
        return ""
    head, _tail = split_title(strip_dateline(title))
    return str(head or "")


def _str_list(v):
    """把状态里读出来的列表字段当成字符串列表用；不是列表/元组就当这个字段不存在。

    rotate.json 是**发布出去的产物**，形状不由本模块保证：CDN 缓存住的旧版本、
    别的分支写过、手工编辑坏了，都可能让它变成 `{"recent": 12345}` 这种样子。
    列表推导遇到整数是 TypeError，而 `build_feed.main()` 没有 try —— 整个这一轮
    会死在选条之前，feed 和 rotate.json 都不落盘，屏幕就此冻在上一版（和「设备坏了」
    分不出来）。按「没有这个字段」继续的代价只是重复播一条，划算。
    字符串本身也会掉进这个坑（`"abc"` 是可迭代的，会被逐字符拆成 a/b/c），所以只认
    list / tuple 两种形状。
    """
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x) for x in v if str(x)]


def _pick_item(state, items):
    """选出这一窗该上屏的条目，返回 (条目 或 None, 已播 guid 集合, 轮次)。

    规则只有一条：**播池子里最新的、还没播过的那一条**。items 已按发布时间倒序、
    已由 _pickable() 剔掉未来条目，所以第一个没被标注的就是答案。
    在此之上还有一层观感约束：避开 `state["recent"]` 里记着的、最近几窗的屏显串
    （见 RECENT_MAX；被跳过的不写 shown，内容不丢）。
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
    shown = set(_str_list(state.get("shown")))  # 形状不对就当没播过（见 _str_list）
    recent = _str_list(state.get("recent"))
    # 1 = 第 1 轮；全池播完重开时 +1。0 / 非数字 / 坏形状一律回到 1（轮次从 1 起算）
    cycle = _int(state.get("cycle"), 1) or 1
    if not items:
        return None, shown, cycle

    unplayed = [it for it in items if item_guid(it) not in shown]
    if unplayed:
        return _without_recent(unplayed, recent), shown, cycle

    # 全池都播过：清标记重开一轮（cycle + 1），从池子最新的一条重新开始。
    # 唯一的例外是「刚刚上屏那一条」—— 池子小、换血快时 items[0] 往往就是它，
    # 清完标记立刻又选中它，屏幕上看起来像「没换」。这时改从「最新的另一条」起轮。
    # 只跳过这一条、不保留它的标记：留着的话它要到下一轮才回来，一轮就会少播一条。
    # recent 在这里**不清空**：它是「最近几窗别连播」的滑动记忆，跟着轮次走，
    # 刚清完标记就允许连播等于这条约束没加。
    prev_g = str(state.get("current") or "")
    restart = [it for it in items if item_guid(it) != prev_g] or [items[0]]
    return _without_recent(restart, recent), set(), cycle + 1


def _without_recent(cands, recent):
    """在候选里跳过最近几窗的屏显串；全被挡住就退回第一条候选。

    cands 已按发布时间倒序，所以「退回第一条」=「宁可跟上一窗撞串，也要接着播」。

    空串不参与比较：`_display_key` 对空标题返回 ""，若把它当成被约束值，
    一堆没有标题的条目会互相挡住（它们本来就没有可看的显示串）。
    """
    if not recent:
        return cands[0]
    # 显示串只算一次：split_title 里有两轮正则 + 宽度扫描，逐个候选算两遍纯属浪费
    for it in cands:
        key = _display_key(it)
        if not key or key not in recent:
            return it
    return cands[0]


def _trim_recent(recent, key):
    """把本窗上屏的显示串压进 recent：最近的在前、去重、长度封顶 RECENT_MAX。

    已经命中时就把它挪到队首（而不是留两个副本）：它代表的是「距离现在几窗」，
    两个副本会让后面几窗被同一条挡两次，白白浪费一格记忆。
    """
    keys = _str_list(recent)
    if key and key in keys:
        keys.remove(key)
    if key:
        keys.insert(0, key)
    return keys[:RECENT_MAX]


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


def _build_state(w, pick, shown, recent, cycle, dwell, back, n_pool, n_items):
    """组装本轮要落盘的状态。

    字段含义（OPERATIONS.md 第 15 节按这份解释排障，改字段名请同步改文档）：
      version 状态结构版本（ROTATE_STATE_VERSION）；与代码不一致时整份状态作废重开
      window  时间窗号 = now // dwell，幂等键，同一窗内不推进
      dwell   落盘时的节拍（ROTATE_SECONDS 秒）。与当前节拍不一致 → 状态作废重开
      back    落后最新几条（0 = 屏上就是池子里最新的一条）
      current 当前上屏条目的 guid —— 「这条是否已播」的唯一身份键，hold / skip 都靠它找回
      shown   已播过条目的 guid 列表（只留池子里还在的条目，见 _trim_shown）
      recent  最近几窗上屏过的**屏显串**（≤ RECENT_MAX，最近的在前）：
              选条时会避开这些串，防「屏显串撞车」造成的连播观感。
              它是观感约束、不是内容去重 —— 被它跳过的条目不进 shown，内容不丢。
      cycle   第几轮（1 起），全池播完重开时 +1
      pool    可播条数（不含发布时间在未来的条目）；items 是进池的原始条数
      updated 写这份状态的时间（UTC，排障看「这份是谁什么时候写的」）
      title   上屏条目的标题前 40 字（只是为了让人不用翻 feed 就能看懂这份状态）
      at      上屏条目的发布时间（UTC）。1.7.0 之前这里记的是「时间片对应的时间」，
              按条轮之后改成这一条自己的发布时间 —— 旧状态版本号不同会被直接丢弃。
    """
    return {
        "version": ROTATE_STATE_VERSION,
        "window": w,
        "back": back,
        "current": item_guid(pick),
        "shown": sorted(shown),
        "recent": recent,
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
      2. 上屏那一条再记一层「最近几窗的屏显串」recent（≤ RECENT_MAX，见 RECENT_MAX），
         选条时会避开它，防标题前 13 字撞车的连播观感 —— 被它跳过的条目不写 shown；
      3. 全池都播过 → 清空 shown，从池子最新的一条重新开始（cycle + 1）；
      4. 同一时间窗内重复调用不推进，也不因本窗来了更新的条目而换台。

    第 1~2 步只在 `_pickable()` 给出的条目里选：发布时间在未来的条目不参与（原因见该函数）。
    """
    dwell = ROTATE_SECONDS
    w = int((now if now is not None else time.time()) // dwell)
    # 节拍改过（60 秒 ↔ 300 秒）或状态结构升级：标记的口径变了，重新起轮。
    # dwell 缺失时按「当前节拍」处理（老状态没有这个键不该被无故清空）；但写坏了
    # （非数字 / 列表）一律当「节拍不符」—— 若让它回落到默认 dwell，下面这个比较
    # 就恒为假，旧 cycle / shown 被静默沿用，比崩溃更隐蔽（见 _int 的 docstring）。
    st_dwell = state.get("dwell")
    if (state.get("version") != ROTATE_STATE_VERSION
            or (st_dwell is not None and _int(st_dwell, None) != dwell)):
        state = {}
    prev_w = _int(state.get("window"), 0)
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
    recent = _trim_recent(state.get("recent"), _display_key(pick))
    # back = 这一条在可播集合里的下标，即「落后池子里最新那条几条」；0 表示播的就是最新那条
    back = next((i for i, it in enumerate(live) if item_guid(it) == pick_g), 0)
    new_state = _build_state(w, pick, shown, recent, cycle, dwell, back,
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
        note += " · 另有 %d 条时间片晚于当前窗，已排除出设备版轮播" % late
    return note
