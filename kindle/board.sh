#!/bin/sh
# Kindle 端看板客户端（越狱后放到 /mnt/us/board.sh，chmod +x）
#
# 定时从树莓派拉最新一张看板图 → 用 eips 画到电子墨水屏上。
# 前置：已越狱 + 装过 Hotfix；Kindle 与树莓派在同一个 wifi。
#
# 试跑：  ssh root@<kindle-ip> 'sh /mnt/us/board.sh'
# 常驻：  ssh root@<kindle-ip> 'nohup sh /mnt/us/board.sh >/dev/null 2>&1 &'
#         （Kindle 没有 systemd，重启后要重新拉一次，或挂到 KUAL 启动项）
# 一次性：sh board.sh --once   —— 拉一张图画上去就退出，KUAL 扩展的「立即刷新一次」
#         走这条；它不写锁文件，所以和正在跑的常驻循环互不干扰。
#
# 刻意不写 set -e：这是一个永不退出的循环，一次 wget 失败或 eips 报错都只该记一笔、
# 继续下一轮；加上 -e 会让整个看板因为一次网络抖动就停摆。

# 图的来源：默认直接走公网（Cloudflare Tunnel 出去的那条）。
# 曾经默认是局域网直连 http://192.168.6.6:8082/…，但 Pi 把 wlan0 入站全 DROP 了
# （22/53/139/445/8082 一律超时），且那个 8082 服务已删除 —— 局域网这条口径不复存在。
# 要临时换源仍可用 BOARD_URL 覆盖，例如指向另一台机器：
#   BOARD_URL="http://192.168.1.20/board.png" sh board.sh
# 两处细节：路径里的 /kindle 是 serve.py 的 --alias（不是磁盘目录）；文件名
# rss-pw2.png 由 kindle_screen.py 的 BOARD_PNG 决定 —— 改任一侧都要同步改这里。
# 若 Kindle 的 wget 报证书错（KPW2 那套根证书是 2019 年的，未必认 Let's Encrypt
# 现在这条链），自动退到 http 重试一次 —— 见 fetch()。
URL="${BOARD_URL:-https://moreaix.com/kindle/rss-pw2.png}"
# 临时文件按 pid 命名：常驻循环与「立即刷新一次」是**并发**的（refresh.sh 的设计就是
# 循环正在跑也能点），而两者都用 wget -O 写同一个文件的话，交错写出来的必然是半张图；
# 更要命的是循环那边的 rm -f 会在 --once 的 wget 与 mv 之间把文件删掉。
TMP="/mnt/us/board.png.new.$$"
IMG="/mnt/us/board.png"
LOCK="/mnt/us/board.lock"
INTERVAL=300          # 秒。墨水屏别刷太勤，3~5 分钟一次够了
FULL_EVERY=10         # 每 N 次刷新清一次残影

# 画图与清屏的工具。两者必须是**同一个**（eips ↔ fbink），混用没有报错、只是不生效：
# fbink 有自己的显存快照，它记着的还是 eips 画的内容，于是清屏清成别的帧、残影越积越多。
# 默认用系统自带的 eips；装了 FBInk 的话两个一起改成 "fbink -g" / "fbink -c"（有抖动和缩放）。
DRAW="eips -g"
CLEAR="eips -c"

# 日志写两处：系统 syslog（logger），和标准输出。后者是给「拔线看文件」用的——
# start.sh 起常驻循环时把这里的 stdout/stderr 一并追加到 /mnt/us/rssboard-diag.txt
# （见 extension/rssboard/bin/common.sh 里的 $DIAG），所以 wget 的报错原文会留在那个
# 文件里，排查时看的也是它。前台试跑则直接打到终端上。
log() {
    logger "$1"
    echo "$(date '+%F %T') $1"
}

# 同一个地址的 http 版本（HTTPS 退路）。这句话用 sed 而不是再写一个变量：
# 只改 URL 一处就能同时换两个协议，漏改一侧的那种漂移不存在。
http_url() {
    printf '%s' "$URL" | sed 's#^https://#http://#'
}

# 取一张图：成功就把候选图落在 $TMP 并返回 0；失败返回 1（调用方负责 rm -f）。
#
# **https 不成功一定再试一次 http**（2026-11-01 实测得来的教训）：moreaix.com 的
# 证书刚换到 Let's Encrypt 的新链，末端是 ISRG Root X2，而 KPW2 那套 2019 年的根
# 证书里没有它 —— wget 因此**秒失败且只报一句证书错误**，表现为「点了刷新、屏上一动
# 不动」，光看屏完全看不出是 TLS 还是网络。第一跳故意只给 8 秒：真走到退路说明有问题，
# 别让一次失败拖住整轮。内容是公开新闻，这一跳的取舍是「能不能刷出来」而不是加密。
fetch() {
    if wget -q -T 8 -O "$TMP" "$URL" 2>/dev/null && [ -s "$TMP" ] && is_png "$TMP"; then
        return 0
    fi
    alt=$(http_url)
    [ "$alt" = "$URL" ] && return 1                     # 本来就是 http，没有退路可走
    wget -q -T 25 -O "$TMP" "$alt" 2>/dev/null && [ -s "$TMP" ] && is_png "$TMP"
}

# 必须是**完整**的 PNG 才往下走：服务端的 404/500 会带一段 HTML body，wget -O 有可能
# 把它落盘，直接 mv 上去就会把 board.png 写坏（eips 打不开，屏幕停在上一版）。
# 只验开头 4 字节还不够：wget 超时或被服务端掐断时留下的是**半张图**，而它的文件头
# 完好 —— 放行之后 eips 打不开，屏幕停在上一版，而下一轮 cmp 又会拿这张坏图当基准，
# 症状变成「一直在刷新、画面却再也不动」。所以末尾也验：PNG 一定以 IEND 块收尾
# （00 00 00 00 | I E N D | AE 42 60 82）。
is_png() {
    [ "$(head -c 4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "89504e47" ] || return 1
    [ "$(tail -c 12 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" \
        = "0000000049454e44ae426082" ]
}

# 单帧模式：不看「图有没有变」，用户点这一下就是想立刻看到画面。
if [ "$1" = "--once" ]; then
    if fetch; then
        mv "$TMP" "$IMG"
        $CLEAR
        $DRAW "$IMG"
        log "board: once ok"
    else
        rm -f "$TMP"
        log "board: once fetch failed"
        # 必须退出非 0：refresh.sh 靠返回码在屏上提示失败。成败都 exit 0 的话，
        # 失败时它仍会去画上一张旧图（画面「动了一下」像是刷新成功了），
        # 而诊断文件里记的 rc 永远是 0 —— 排障时唯一的线索就成了假证据。
        exit 1
    fi
    exit 0
fi

# 锁里的 pid 是不是**本脚本**的实例。
# 只判 kill -0 是不够的：锁文件留在 /mnt/us 上跨重启保留，而 pid 会被复用，
# kill -0 只能证明「现在有个进程占着这个号」，证明不了它是 board.sh。哪天这个号
# 落到任何常驻进程上，脚本就永远打印 already running 然后安静退出 —— 屏上一动不动，
# 而日志里分明写着「已在运行」，这是最难归位的一类故障。
lock_is_ours() {
    pid=$(cat "$LOCK" 2>/dev/null)
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # 读不到 /proc 就退回「信 kill -0」（没有 procfs 的极端环境）
    [ -r "/proc/$pid/cmdline" ] || return 0
    grep -q "board.sh" "/proc/$pid/cmdline" 2>/dev/null
}

# 单实例：文档教「先前台试跑、确认后再 nohup」，忘了 kill 前台那个，两个进程就会
# 抢着刷屏（画面来回跳）。发现已有实例就安静退出。
if lock_is_ours; then
    log "board: already running (pid $(cat "$LOCK")), skip"
    exit 0
fi
echo $$ > "$LOCK"

# 不让屏保把画面盖掉（这一步失败不影响出图，只是屏保可能来抢屏）
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null

n=0
while true; do
    if fetch; then
        # 图没变就不刷屏：e-ink 每刷一次都要闪一下、也要耗电。服务端已支持
        # If-Modified-Since/304，但 Kindle 自带的 busybox wget 不一定有 -N，
        # 而 GNU wget 的 -N 与 -O 互斥（同时给会被忽略），所以这里按内容比对，
        # 效果等价而且不依赖 wget 的版本。
        if cmp -s "$TMP" "$IMG"; then
            rm -f "$TMP"
            log "board: unchanged, skip refresh"
        else
            mv "$TMP" "$IMG"
            # 整屏闪一次并清屏，紧接着重画 —— 这是清残影最稳的做法。
            # n 从 0 开始，所以第一轮必然清一次屏（首次上屏本来就该整屏刷）。
            if [ $((n % FULL_EVERY)) -eq 0 ]; then
                $CLEAR
            fi
            $DRAW "$IMG"
            n=$((n + 1))
            log "board: drew frame $n"
        fi
    else
        rm -f "$TMP"
        log "board: fetch failed, keep last image"
    fi
    sleep "$INTERVAL"
done
