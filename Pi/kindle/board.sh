#!/bin/sh
# Kindle 端看板客户端（越狱后放到 /mnt/us/board.sh，chmod +x）
#
# 定时从树莓派拉最新一张看板图 → 用 eips 画到电子墨水屏上。
# 前置：已越狱 + 装过 Hotfix；Kindle 与树莓派在同一个 wifi。
#
# 试跑：  ssh root@<kindle-ip> 'sh /mnt/us/board.sh'
# 常驻：  ssh root@<kindle-ip> 'nohup sh /mnt/us/board.sh >/dev/null 2>&1 &'
#         （Kindle 没有 systemd，重启后要重新拉一次，或挂到 KUAL 启动项）
#
# 刻意不写 set -e：这是一个永不退出的循环，一次 wget 失败或 eips 报错都只该记一笔、
# 继续下一轮；加上 -e 会让整个看板因为一次网络抖动就停摆。

URL="http://192.168.6.6:8082/kindle.png"   # 必须是 Pi 的 wlan0 地址，换 IP 要同步改 JAILBREAK.md
TMP="/mnt/us/board.png.new"
IMG="/mnt/us/board.png"
LOCK="/mnt/us/board.lock"
INTERVAL=300          # 秒。墨水屏别刷太勤，3~5 分钟一次够了
FULL_EVERY=10         # 每 N 次刷新清一次残影

# 画图的工具。默认用系统自带的 eips；装了 FBInk 的话改成 "fbink -g" 效果更好（有抖动和缩放）
DRAW="eips -g"

# 单实例：文档教「先前台试跑、确认后再 nohup」，忘了 kill 前台那个，两个进程就会
# 抢着刷屏（画面来回跳）。发现已有实例就安静退出。
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
    logger "board: already running (pid $(cat "$LOCK")), skip"
    exit 0
fi
echo $$ > "$LOCK"

# 不让屏保把画面盖掉（这一步失败不影响出图，只是屏保可能来抢屏）
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null

# 下载结果必须是 PNG 才往下走：服务端的 404/500 会带一段 HTML body，
# wget -O 有可能把它落盘，直接 mv 上去就会把 board.png 写坏（eips 打不开，
# 屏幕停在上一版，而且下一轮会拿这张坏图做比对）。
is_png() {
    [ "$(head -c 4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "89504e47" ]
}

n=0
while true; do
    if wget -q -T 20 -O "$TMP" "$URL" && [ -s "$TMP" ] && is_png "$TMP"; then
        # 图没变就不刷屏：e-ink 每刷一次都要闪一下、也要耗电。服务端已支持
        # If-Modified-Since/304，但 Kindle 自带的 busybox wget 不一定有 -N，
        # 而 GNU wget 的 -N 与 -O 互斥（同时给会被忽略），所以这里按内容比对，
        # 效果等价而且不依赖 wget 的版本。
        if cmp -s "$TMP" "$IMG"; then
            rm -f "$TMP"
            logger "board: unchanged, skip refresh"
        else
            mv "$TMP" "$IMG"
            # eips -c 会整屏闪一次并清屏，紧接着重画 —— 这是清残影最稳的做法。
            # n 从 0 开始，所以第一轮必然清一次屏（首次上屏本来就该整屏刷）。
            if [ $((n % FULL_EVERY)) -eq 0 ]; then
                eips -c
            fi
            $DRAW "$IMG"
            n=$((n + 1))
        fi
    else
        rm -f "$TMP"
        logger "board: fetch failed, keep last image"
    fi
    sleep "$INTERVAL"
done
